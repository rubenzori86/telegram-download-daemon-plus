import asyncio
import glob
import os
import re
import shutil
import subprocess

from humanize import naturalsize
from telethon.tl.types import DocumentAttributeFilename

from .utils import TELEGRAM_DAEMON_TEMP_SUFFIX

import logging
# Robustez 8: logger de módulo para errores de extracción en producción
logger = logging.getLogger(__name__)

# Factor de seguridad para estimar el espacio en disco necesario antes de extraer.
# El contenido descomprimido suele pesar bastante más que los archivos comprimidos;
# un factor de 3 es conservador y deja margen para archivos de texto muy comprimidos.
EXTRACTION_DISK_SAFETY_FACTOR = 3


class ExtractionManagerMixin:
    """Lógica de detección y autodescompresión."""

    def detect_multipart_set(self, file_path):
        """Detecta conjuntos multipart y devuelve metadatos para extraer con 7z."""
        folder = os.path.dirname(file_path)
        filename = os.path.basename(file_path)

        m = re.match(r"^(?P<base>.+?)\.part(?P<num>\d{1,3})\.rar$", filename, re.IGNORECASE)
        if m:
            base = m.group('base')
            candidates = [
                os.path.join(folder, f"{base}.part1.rar"),
                os.path.join(folder, f"{base}.part01.rar"),
                os.path.join(folder, f"{base}.part001.rar"),
            ]
            start = next((c for c in candidates if os.path.exists(c)), candidates[0])
            return {
                'type': 'rar_part',
                'base': base,
                'start': start,
                'parts_patterns': [os.path.join(folder, f"{base}.part*.rar")],
            }

        if filename.lower().endswith('.rar'):
            base = filename[:-4]
            return {
                'type': 'rar_rNN',
                'base': base,
                'start': os.path.join(folder, f"{base}.rar"),
                'parts_patterns': [
                    os.path.join(folder, f"{base}.rar"),
                    os.path.join(folder, f"{base}.r??"),
                ],
            }
        m = re.match(r"^(?P<base>.+?)\.r(?P<num>\d{2})$", filename, re.IGNORECASE)
        if m:
            base = m.group('base')
            return {
                'type': 'rar_rNN',
                'base': base,
                'start': os.path.join(folder, f"{base}.rar"),
                'parts_patterns': [
                    os.path.join(folder, f"{base}.rar"),
                    os.path.join(folder, f"{base}.r??"),
                ],
            }

        m = re.match(r"^(?P<base>.+?)\.7z\.(?P<num>\d{3})$", filename, re.IGNORECASE)
        if m:
            base = m.group('base')
            return {
                'type': '7z_001',
                'base': base,
                'start': os.path.join(folder, f"{base}.7z.001"),
                'parts_patterns': [os.path.join(folder, f"{base}.7z.*")],
            }

        if filename.lower().endswith('.zip'):
            base = filename[:-4]
            return {
                'type': 'zip_z01',
                'base': base,
                'start': os.path.join(folder, f"{base}.zip"),
                'parts_patterns': [
                    os.path.join(folder, f"{base}.zip"),
                    os.path.join(folder, f"{base}.z??"),
                ],
            }
        m = re.match(r"^(?P<base>.+?)\.z(?P<num>\d{2})$", filename, re.IGNORECASE)
        if m:
            base = m.group('base')
            return {
                'type': 'zip_z01',
                'base': base,
                'start': os.path.join(folder, f"{base}.zip"),
                'parts_patterns': [
                    os.path.join(folder, f"{base}.zip"),
                    os.path.join(folder, f"{base}.z??"),
                ],
            }

        # Detectar archivos .zip.001, .zip.002, etc.
        m = re.match(r"^(?P<base>.+?)\.zip\.(?P<num>\d{3})$", filename, re.IGNORECASE)
        if m:
            base = m.group('base')
            return {
                'type': 'zip_001',
                'base': base,
                'start': os.path.join(folder, f"{base}.zip.001"),
                'parts_patterns': [os.path.join(folder, f"{base}.zip.[0-9][0-9][0-9]")],
            }

        m = re.match(r"^(?P<base>.+?)\.(?P<num>\d{3})$", filename, re.IGNORECASE)
        if m and not filename.lower().endswith('.7z.001') and not filename.lower().endswith('.zip.001') and not re.search(r"\.z\d{2}$", filename, re.IGNORECASE):
            base = m.group('base')
            # Robustez 7: verificar que existe al menos una segunda parte antes de clasificar como
            # multipart genérico. Un único .001 sin .002 podría ser un nombre de archivo o versión,
            # no necesariamente un set comprimido particionado.
            folder = os.path.dirname(file_path)
            second_part = os.path.join(folder, f"{base}.002")
            if not os.path.exists(second_part):
                return None
            return {
                'type': 'generic_001',
                'base': base,
                'start': os.path.join(folder, f"{base}.001"),
                'parts_patterns': [os.path.join(folder, f"{base}.*")],
            }

        # Catch-all para extensiones compuestas: .iso.001, .mkv.001, .exe.gz.001,
        # .tar.gz.001, .tar.bz2.001, .tar.xz.001, etc.
        # El regex es greedy (.+) para que base capture todo antes del .NNN final.
        m = re.match(r"^(?P<base>.+\.[a-z0-9]+)\.\d{3}$", filename, re.IGNORECASE)
        if m:
            base = m.group('base')
            second_part = os.path.join(folder, f"{base}.002")
            if not os.path.exists(second_part):
                return None
            return {
                'type': 'generic_001',
                'base': base,
                'start': os.path.join(folder, f"{base}.001"),
                'parts_patterns': [os.path.join(folder, f"{base}.*")],
            }

        return None

    def _all_needed_parts_present(self, info):
        return os.path.exists(info['start'])

    async def _find_message_ids_by_content(self, client, peer_channel, matched_names, limit=200):
        """Busca IDs de mensajes que contengan nombres de archivo de un set multipart.

        Robustez 6: este método es un FALLBACK de último recurso; únicamente debe llamarse cuando
        no existen IDs registrados explícitamente en self.part_message_ids. El escaneo por texto
        es más frágil porque depende de literales de mensaje que pueden cambiar entre versiones.
        """
        ids = []
        try:
            async for msg in client.iter_messages(peer_channel, limit=limit):
                # Mensajes de texto: buscar el patrón de completado que usa el worker
                if getattr(msg, 'message', None):
                    text = msg.message or ''
                    if '\u2705' in text and 'listo' in text:
                        for part_name in matched_names:
                            if part_name in text:
                                ids.append(msg.id)
                                break
                # Mensajes con documento adjunto: verificar nombre del archivo
                if hasattr(msg, 'media') and msg.media and hasattr(msg.media, 'document'):
                    from telethon.tl.types import DocumentAttributeFilename as _DAF
                    for attr in msg.media.document.attributes:
                        if isinstance(attr, _DAF) and attr.file_name in matched_names:
                            ids.append(msg.id)
                            break
        except Exception as e:
            logger.warning("Error en _find_message_ids_by_content: %s", e)
        return ids

    async def try_auto_extract(self, file_path, queue, client, peer_channel):
        """Ejecuta la extracción automática si corresponde."""
        if not self.auto_extract:
            return
        try:
            await asyncio.sleep(2)
        except Exception:
            pass

        info = self.detect_multipart_set(file_path)
        if not info:
            return

        folder = os.path.dirname(file_path)

        # Helper de log: consola + archivo en la carpeta del set
        log_file_path = os.path.join(folder, f"{os.path.basename(folder)}_extraccion.log")

        def xlog(msg, level='debug'):
            """Log a consola (via logger) y a archivo en la carpeta del set."""
            if level == 'info':
                logger.info(msg)
            elif level == 'warning':
                logger.warning(msg)
            elif level == 'error':
                logger.error(msg)
            else:
                logger.debug(msg)
            try:
                with open(log_file_path, 'a', encoding='utf-8') as lf:
                    import datetime
                    lf.write(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
            except Exception:
                pass

        patterns = info['parts_patterns']
        base_name = info['base']
        matched_files = []
        total_bytes = 0
        indices = []

        for pattern in patterns:
            if glob.glob(pattern + "." + TELEGRAM_DAEMON_TEMP_SUFFIX):
                xlog(f"⏳ multipart: Hay descargas temporales activas para '{base_name}'. Esperando...", 'debug')
                return

        temp_list = []
        found_in_queue = False
        while not queue.empty():
            item = await queue.get()
            temp_list.append(item)
            if item.get('filename') and any(re.search(p.replace('*', '.*'), item['filename'], re.I) for p in patterns):
                found_in_queue = True

        for item in temp_list:
            await queue.put(item)

        if found_in_queue:
            xlog(f"⏳ multipart: Hay partes de '{base_name}' en la cola. Esperando...", 'debug')
            return

        for active_item in self.active_downloads.values():
            active_filename = active_item.get('filename')
            if active_filename and any(re.search(p.replace('*', '.*'), active_filename, re.I) for p in patterns):
                xlog(f"⏳ multipart: Otro worker está descargando una parte de '{base_name}'. Esperando...", 'debug')
                return

        if not self._all_needed_parts_present(info):
            xlog(f"⚠️ Aún no están todas las partes físicas para: {file_path}", 'debug')
            return

        start_file = info['start']
        extracted_label = info.get('base') or os.path.basename(start_file)

        set_key = extracted_label
        if set_key in self.extracted_sets:
            # ... (clean residual parts logic) ...
            if self.clean_parts_after_extract and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    xlog(f"🧹 Parte residual eliminada: {os.path.basename(file_path)}", 'debug')
                except Exception as e:
                    xlog(f"Error eliminando parte residual: {e}", 'warning')

            try:
                ids_to_delete = self.part_message_ids.get(set_key, [])
                if not ids_to_delete:
                    filename_only = os.path.basename(file_path)
                    async for msg in client.iter_messages(peer_channel, limit=100):
                        if getattr(msg, 'message', None) and filename_only in msg.message and 'listo' in msg.message:
                            ids_to_delete.append(msg.id)

                if ids_to_delete:
                    await client.delete_messages(peer_channel, ids_to_delete, revoke=True)
                    if set_key in self.part_message_ids:
                        del self.part_message_ids[set_key]
            except Exception as e:
                xlog(f"Error borrando mensajes residuales: {e}", 'warning')
            return

        # Smart Detection: Check if all parts are present
        matched_files = []
        for pattern in info['parts_patterns']:
            for f in glob.glob(pattern):
                # Para tipos generic_001 (incluidas extensiones compuestas), filtrar
                # solo archivos que terminen en exactamente 3 dígitos
                if info.get('type') == 'generic_001':
                    if re.search(r'\.\d{3}$', f):
                        matched_files.append(f)
                else:
                    matched_files.append(f)
        matched_files = sorted(set(matched_files))
        
        should_extract = False
        
        # Check if we have the last part to trigger extraction
        # Heuristic: If we have matched files, check if they form a complete sequence
        if matched_files:
            # Simple heuristic: Check if the file being processed is roughly the last one or if we have enough parts
            # For now, let's trust the patterns. If the set is complete-ish.
            # Robust check:
            # 1. Parse indices from filenames (e.g. .part01.rar -> 1)
            # 2. Check if we have 1..N continuous sequence
            # 3. If the current file is the last one (or we re-scan), we go.
            
            # Simplified for this specific user request:
            # "If 5 is smaller (last part), and we have 1, 2, 3, 4 -> extract"
            
            # Let's assume the current event triggered this check.
            pass
            
        # Initialization of extracting_sets as dict if needed
        if not hasattr(self, 'extracting_sets'):
            self.extracting_sets = {}
            
        if set_key in self.extracting_sets:
            return

        # Verify completeness strictly
        # We need to know the TOTAL number of parts to know if we have them all.
        # But we often don't know N.
        # Heuristic: If we have a file that looks like the last part (smaller size?)
        # OR if we just want to try extracting if 'unar' thinks it's okay? 'unar' might fail if parts missing.
        
        # Let's try to verify sequence continuity at least
        try:
            indices = []
            for f in matched_files:
                # Find the number in the part suffix
                match = re.search(r'\.part(\d+)\.rar$', f, re.IGNORECASE)
                if match:
                    indices.append(int(match.group(1)))
                else:
                    # Maybe .001, .002 format?
                    match = re.search(r'\.(\d{3})$', f)
                    if match:
                         indices.append(int(match.group(1)))
            
            if indices:
                indices.sort()
                # Check continuity: 1, 2, 3...
                # If first is 1 and last is len(indices), we have a continuous sequence 1..N
                if indices[0] == 1 and indices[-1] == len(indices):
                    # We have 1..N. Is N the last one?
                    # Check file sizes
                    last_index = indices[-1]
                    # Find file corresponding to last index
                    last_part_file = None
                    first_part_file = None
                    
                    # Re-scan to match indices to files (inefficient but safe)
                    for f in matched_files:
                        if f.endswith(f"{last_index:03d}") or f.endswith(f"part{last_index}.rar") or f.endswith(f"part{last_index:02d}.rar"):
                            last_part_file = f
                        if f.endswith("001") or f.endswith("part1.rar") or f.endswith("part01.rar"):
                            first_part_file = f
                    
                    if last_part_file and first_part_file:
                        try:
                            s_last = os.path.getsize(last_part_file)
                            s_first = os.path.getsize(first_part_file)
                            # If last part is smaller or equal (and we have multiple parts), good.
                            # If we have only 1 part and it matches start/end logic, ok.
                            if len(indices) > 1:
                                if s_last <= s_first:
                                    # Very likely the end
                                    should_extract = True
                                else:
                                    # Corrección 2: última parte mayor que la primera — dejar decidir al probe
                                    xlog(f"⚠️ Última parte ({s_last} bytes) >= primera ({s_first} bytes), dejando decidir al probe de 7z", 'debug')
                                    should_extract = False
                        except:
                            pass
                else:
                    # Missing intermediate parts
                    xlog(f"🧩 Faltan partes intermedias: Tenemos {indices}", 'debug')
                    return
        except Exception as e:
            xlog(f"Error checking continuity: {e}", 'warning')

        xlog(f"🚀 Iniciando extracción: should_extract={should_extract}, índices={indices}, archivos={len(matched_files)}, total={naturalsize(total_bytes)}", 'info')
        self.extracting_sets[set_key] = f"🔄 Descomprimiendo {extracted_label} ({len(matched_files)} partes)..."

        matched_names = {os.path.basename(p) for p in matched_files}
        total_bytes = 0
        for p in matched_files:
            try:
                total_bytes += os.path.getsize(p)
            except Exception:
                pass

        # Bug 5: ejecutar subprocesos (7z, unrar, unar) en un hilo aparte con
        # asyncio.to_thread() para NO bloquear el event loop principal. Antes se
        # usaba subprocess.run() síncrono dentro de una corrutina async, lo que
        # congelaba descargas, keepalives de Telethon y la respuesta a botones
        # mientras duraba la extracción (minutos en archivos grandes).
        def _run_sync(cmd, cwd=None):
            return subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors='ignore',
                cwd=cwd,
            )

        async def _run_subprocess(cmd, cwd=None):
            return await asyncio.to_thread(_run_sync, cmd, cwd)

        async def _probe_archive_info(start_path):
            try:
                res = await _run_subprocess(["7z", "l", "-slt", start_path])
                vols = None
                tps = None
                for line in (res.stdout or '').splitlines():
                    line = line.strip()
                    if line.startswith("Volumes ="):
                        try:
                            vols = int(line.split('=', 1)[1].strip())
                        except Exception:
                            pass
                    if line.startswith("Total Physical Size ="):
                        try:
                            tps = int(line.split('=', 1)[1].strip())
                        except Exception:
                            pass
                return {"volumes": vols, "total_physical_size": tps}
            except Exception:
                return {"volumes": None, "total_physical_size": None}

        def _ensure_unique_output_dir(base_folder, base_label):
            safe = os.path.join(base_folder, f"{base_label}_EXTRAIDO")
            if not os.path.exists(safe):
                try:
                    os.makedirs(safe, exist_ok=True)
                except Exception:
                    pass
                return safe
            suffix = 2
            while True:
                cand = os.path.join(base_folder, f"{base_label}_EXTRAIDO_{suffix}")
                if not os.path.exists(cand):
                    try:
                        os.makedirs(cand, exist_ok=True)
                    except Exception:
                        pass
                    return cand
                suffix += 1

        try:
            if not should_extract:
                probe = await _probe_archive_info(start_file)
                xlog(f"🔍 Probe 7z resultado: volumes={probe.get('volumes')}, total_physical_size={probe.get('total_physical_size')}", 'debug')
                if probe.get('volumes'):
                    if len(matched_files) < probe['volumes']:
                        xlog(f"⏳ Esperando partes: {len(matched_files)}/{probe['volumes']} presentes para {os.path.basename(start_file)}", 'debug')
                        if set_key in self.extracting_sets:
                            del self.extracting_sets[set_key]
                        return
                if probe.get('total_physical_size'):
                    if total_bytes < probe['total_physical_size']:
                        xlog(f"⏳ Esperando bytes: {naturalsize(total_bytes)}/{naturalsize(probe['total_physical_size'])} para {os.path.basename(start_file)}", 'debug')
                        if set_key in self.extracting_sets:
                            del self.extracting_sets[set_key]
                        return
                # Si el probe no pudo determinar nada Y tenemos secuencia continua 1..N, extraer igualmente
                if indices and indices[0] == 1 and indices[-1] == len(indices):
                    xlog(f"✅ Probe sin datos pero secuencia continua 1..{len(indices)} confirmada, extrayendo...", 'info')
                    should_extract = True
                else:
                    xlog(f"⚠️ Probe sin datos y secuencia incompleta {indices}, no se extrae", 'warning')
                    if set_key in self.extracting_sets:
                        del self.extracting_sets[set_key]
                    return
            else:
                xlog(f"🚀 Escaneado inteligente completado para {extracted_label}. Iniciando extracción forzada.", 'info')

            base_target = os.path.join(folder, f"{extracted_label}_EXTRAIDO")
            tmp_target = base_target + ".tmp"
            if os.path.isdir(base_target) and any(os.scandir(base_target)):
                xlog(f"⚠️ Ya existe extracción previa para '{extracted_label}' en {base_target}. Se omite nueva extracción.", 'info')
                self.extracted_sets.add(set_key)
                if self.clean_parts_after_extract:
                    removed = 0
                    for p in matched_files:
                        try:
                            os.remove(p)
                            removed += 1
                        except Exception:
                            pass
                    xlog(f"🧹 Partes locales eliminadas (preexistente): {removed}", 'debug')
                try:
                    ids = []
                    async for msg in client.iter_messages(peer_channel, limit=1000):
                        if hasattr(msg, 'media') and msg.media and hasattr(msg.media, 'document'):
                            for attr in msg.media.document.attributes:
                                if isinstance(attr, DocumentAttributeFilename) and attr.file_name in matched_names:
                                    ids.append(msg.id)
                                    break
                        if getattr(msg, 'message', None):
                            text = msg.message or ''
                            if '✅' in text and 'listo' in text:
                                for part_name in matched_names:
                                    if part_name in text:
                                        ids.append(msg.id)
                                        break
                    if ids:
                        try:
                            await client.delete_messages(peer_channel, ids, revoke=True)
                            xlog(f"🗑️ Mensajes de partes eliminados: {len(ids)}", 'debug')
                            if set_key in self.part_message_ids:
                                del self.part_message_ids[set_key]
                        except Exception as e:
                            xlog(f"No se pudieron borrar mensajes: {e}", 'warning')
                except Exception as e:
                    xlog(f"Error buscando mensajes para borrar: {e}", 'warning')
                return

            try:
                if os.path.isdir(tmp_target):
                    for e in os.scandir(tmp_target):
                        try:
                            if e.is_file():
                                os.remove(e.path)
                        except Exception:
                            pass
                else:
                    os.makedirs(tmp_target, exist_ok=True)
            except Exception:
                pass

            xlog(f"🗜️ Intentando extraer set multipart desde: {start_file}", 'info')
            start_extract_msg = None
            try:
                start_extract_msg = await client.send_message(
                    peer_channel,
                    f"🗜️ **Descomprimiendo set multipart:**\n📦 `{extracted_label}` ({len(matched_files)} partes, {naturalsize(total_bytes)})\n⏳ _Procesando en segundo plano en el NAS..._"
                )
            except Exception as e:
                logger.debug("No se pudo enviar aviso de inicio de extracción: %s", e)

            async with self.extraction_lock:
                xlog(f"🔒 Lock de extracción adquirido para '{extracted_label}'", 'debug')

                # Verificar espacio libre en disco DENTRO del lock para evitar la carrera:
                # si dos sets terminan casi a la vez, ambos podrían pasar el chequeo antes
                # de que el primero libere espacio al borrar sus partes. Con el lock adquirido
                # el chequeo siempre refleja el espacio real disponible en ese instante.
                try:
                    safety_factor = getattr(self, 'extraction_disk_safety_factor', EXTRACTION_DISK_SAFETY_FACTOR)
                    free_bytes = shutil.disk_usage(folder).free
                    needed_bytes = total_bytes * safety_factor
                    if free_bytes < needed_bytes:
                        xlog(
                            f"⚠️ Espacio insuficiente para extraer '{extracted_label}': "
                            f"libre={naturalsize(free_bytes)}, "
                            f"estimado necesario={naturalsize(needed_bytes)} "
                            f"({safety_factor}× {naturalsize(total_bytes)} comprimido). "
                            f"Se reintentará cuando haya más espacio.",
                            'warning'
                        )
                        if set_key in self.extracting_sets:
                            del self.extracting_sets[set_key]
                        return
                except Exception as e:
                    xlog(f"⚠️ No se pudo verificar el espacio en disco: {e}. Continuando de todas formas.", 'warning')
                if info['type'].startswith('rar'):
                    try:
                        xlog("Intentando con unrar (prioritario para RAR)...", 'debug')
                        result = await _run_subprocess(["unrar", "x", "-o-", "-y", start_file, tmp_target])
                        xlog(result.stdout, 'debug')
                    except FileNotFoundError:
                        result = subprocess.CompletedProcess(args=[], returncode=1, stdout="unrar no disponible")

                    if result.returncode != 0:
                        xlog("unrar falló o no disponible, intentando con 7z...", 'debug')
                        result = await _run_subprocess(["7z", "x", "-aos", "-y", f"-o{tmp_target}", start_file])
                        xlog(result.stdout, 'debug')
                        if result.returncode != 0:
                            try:
                                xlog("7z falló, intentando con 'unar'...", 'debug')
                                result = await _run_subprocess(["unar", "-quiet", "-o", tmp_target, start_file])
                                xlog(result.stdout, 'debug')
                            except FileNotFoundError:
                                xlog("❌ 'unar' no está disponible", 'error')
                else:
                    result = await _run_subprocess(["7z", "x", "-aos", "-y", f"-o{tmp_target}", start_file])
                    xlog(result.stdout, 'debug')
                    if result.returncode != 0:
                        try:
                            xlog("7z falló, intentando con 'unar'...", 'debug')
                            result = await _run_subprocess(["unar", "-quiet", "-o", tmp_target, start_file])
                            xlog(result.stdout, 'debug')
                        except FileNotFoundError:
                            xlog("❌ 'unar' no está disponible", 'error')

                if result.returncode == 0:
                    xlog("✅ Extracción completada exitosamente", 'info')
                    try:
                        parts_count = len(matched_files)
                        size_str = naturalsize(total_bytes)
                        origin_link = self.origin_links.get(set_key, "N/A")
                        success_msg = f"✅ El archivo **'{extracted_label}'** se ha extraído correctamente ({parts_count} partes, {size_str})."
                        if origin_link != "N/A":
                            success_msg += f"\n🔗 **Origen:** {origin_link}"

                        from telethon import Button
                        buttons = None
                        if origin_link != "N/A":
                            try:
                                link_parts = origin_link.replace('https://t.me/c/', '').split('/')
                                if len(link_parts) >= 2:
                                    channel = link_parts[0]
                                    msg_id = link_parts[1]
                                    button_data = f"delete_extraction_{channel}_{msg_id}"
                                    buttons = [
                                        [Button.inline("🗑️ Borrar este mensaje y el original", data=button_data)]
                                    ]
                            except Exception as e:
                                xlog(f"Error creando botón de borrado: {e}", 'debug')

                        extraction_msg = await client.send_message(peer_channel, success_msg, buttons=buttons)
                        xlog("✅ Mensaje informativo enviado", 'info')

                        if origin_link != "N/A" and hasattr(self, 'register_extraction_message'):
                            self.register_extraction_message(origin_link, extraction_msg.id)
                    except Exception as e:
                        xlog(f"No se pudo enviar mensaje informativo: {e}", 'warning')

                    try:
                        if not (os.path.isdir(base_target) and any(os.scandir(base_target))):
                            os.replace(tmp_target, base_target)
                        else:
                            for e in os.scandir(tmp_target):
                                try:
                                    if e.is_file():
                                        os.remove(e.path)
                                except Exception:
                                    pass
                            try:
                                os.rmdir(tmp_target)
                            except Exception:
                                pass
                    except Exception:
                        pass
                    self.extracted_sets.add(set_key)

                    if hasattr(self, 'rename_media_in_folder'):
                        asyncio.create_task(self.rename_media_in_folder(base_target, client, peer_channel))

                    if self.clean_parts_after_extract:
                        removed = 0
                        for p in matched_files:
                            try:
                                os.remove(p)
                                removed += 1
                            except Exception as e:
                                xlog(f"No se pudo eliminar parte {p}: {e}", 'warning')
                        xlog(f"🧹 Partes locales eliminadas: {removed}", 'debug')

                    try:
                        ids = []
                        if set_key in self.part_message_ids:
                            ids.extend(self.part_message_ids[set_key])
                            xlog(f"Agregados {len(self.part_message_ids[set_key])} IDs desde cache para borrado", 'debug')

                        if not ids or len(ids) < len(matched_names):
                            xlog("Buscando mensajes de partes en el chat (fallback por texto)...", 'debug')
                            content_ids = await self._find_message_ids_by_content(
                                client, peer_channel, matched_names, limit=200
                            )
                            for mid in content_ids:
                                if mid not in ids:
                                    ids.append(mid)

                        if ids:
                            ids = list(set(ids))
                            try:
                                await client.delete_messages(peer_channel, ids, revoke=True)
                                xlog(f"🗑️ Mensajes de partes eliminados: {len(ids)}", 'debug')
                            except Exception as e:
                                xlog(f"No se pudieron borrar mensajes de partes: {e}", 'warning')
                        else:
                            xlog("No se encontraron mensajes de partes para borrar.", 'debug')
                    except Exception as e:
                        xlog(f"Error general buscando/borrando mensajes de partes: {e}", 'warning')

                    if set_key in self.part_message_ids:
                        del self.part_message_ids[set_key]
                    if set_key in self.origin_links:
                        del self.origin_links[set_key]
                    if set_key in self.extracting_sets:
                        del self.extracting_sets[set_key]
                else:
                    xlog(f"❌ La extracción falló con código {result.returncode}. Se reintentará más tarde.", 'error')
                    if set_key in self.extracting_sets:
                        del self.extracting_sets[set_key]
        except FileNotFoundError:
            xlog("❌ Herramientas de extracción no disponibles en el contenedor", 'error')
            if set_key in self.extracting_sets:
                del self.extracting_sets[set_key]
        except Exception as e:
            xlog(f"❌ Error durante la extracción: {e}", 'error')
            if set_key in self.extracting_sets:
                del self.extracting_sets[set_key]
        finally:
            if start_extract_msg:
                try:
                    await client.delete_messages(peer_channel, [start_extract_msg.id], revoke=True)
                except Exception:
                    pass
            if set_key in self.extracting_sets:
                del self.extracting_sets[set_key]
