import asyncio
import glob
import json
import logging
import os

from humanize import naturalsize

from .download_media import DownloadMedia
from .utils import TELEGRAM_DAEMON_TEMP_SUFFIX

# Robustez 8: logger de módulo
logger = logging.getLogger(__name__)


class QueueManagerMixin:
    """Gestión de cola, persistencia y reanudación."""

    def save_pending_downloads(self, queue_items):
        """Guarda las descargas pendientes en disco."""
        try:
            pending_data = []
            for item in queue_items:
                download_media = item['download_media']
                retries = item.get('retries', 0)
                if hasattr(download_media, 'channel') and hasattr(download_media, 'message_id'):
                    pending_data.append({
                        'channel': download_media.channel,
                        'message_id': download_media.message_id,
                        'destination': download_media.destination,
                        'retries': retries
                    })
                elif isinstance(download_media, dict):
                    pending_data.append({
                        'channel': download_media.get('channel', self.channel_id),
                        'message_id': download_media.get('message_id', 0),
                        'destination': download_media.get('destination', '.'),
                        'retries': retries
                    })
                else:
                    pending_data.append({
                        'channel': getattr(download_media, 'channel', self.channel_id),
                        'message_id': getattr(download_media, 'message_id', 0),
                        'destination': getattr(download_media, 'destination', '.'),
                        'retries': retries
                    })

            print(f"Guardando {len(pending_data)} items en {self.pending_downloads_file}")
            print(f"Datos a guardar: {pending_data}")

            with open(self.pending_downloads_file, 'w', encoding='utf-8') as f:
                json.dump(pending_data, f, indent=2)
            print(f"Descargas pendientes guardadas: {len(pending_data)} items")
        except Exception as e:
            print(f"Error guardando descargas pendientes: {e}")
            import traceback
            traceback.print_exc()

    def save_all_pending_downloads(self, queue=None):
        """Guarda todas las descargas pendientes de la cola actual."""
        try:
            queue_items = []

            if queue is not None:
                while not queue.empty():
                    try:
                        item = queue.get_nowait()
                        queue_items.append(item)
                    except asyncio.QueueEmpty:
                        break

                for item in queue_items:
                    queue.put_nowait(item)

            # Incluir descargas activas en la persistencia (para reanudar tras reinicio)
            if hasattr(self, 'active_downloads') and self.active_downloads:
                print(f"Incluyendo {len(self.active_downloads)} descargas activas en el guardado...")
                for active_item in self.active_downloads.values():
                    # Evitar duplicados si por alguna razón extraña está en ambas listas
                    if active_item not in queue_items:
                        queue_items.append(active_item)

            self.save_pending_downloads(queue_items)

        except Exception as e:
            print(f"Error guardando todas las descargas pendientes: {e}")

    def load_pending_downloads(self):
        """Carga las descargas pendientes desde disco."""
        try:
            if os.path.exists(self.pending_downloads_file):
                with open(self.pending_downloads_file, 'r', encoding='utf-8') as f:
                    pending_data = json.load(f)

                print(f"Descargas pendientes cargadas: {len(pending_data)} items")
                return pending_data
            return []
        except Exception as e:
            print(f"Error cargando descargas pendientes: {e}")
            return []

    def find_partial_downloads(self):
        """Encuentra archivos parcialmente descargados."""
        partial_downloads = []
        try:
            if os.path.exists(self.temp_folder):
                print(f"Buscando en carpeta: {self.temp_folder}")
                for filename in os.listdir(self.temp_folder):
                    print(f"  - Archivo encontrado: {filename}")
                    if filename.endswith('.' + TELEGRAM_DAEMON_TEMP_SUFFIX):
                        filepath = os.path.join(self.temp_folder, filename)
                        file_size = os.path.getsize(filepath)
                        print(f"  - Archivo .tdd: {filename}, tamaño: {naturalsize(file_size)}")
                        if file_size > 0:
                            base_name = filename[:-len('.' + TELEGRAM_DAEMON_TEMP_SUFFIX)]
                            print(f"  - Base name: {base_name}")
                            parts = base_name.rsplit('-', 1)
                            print(f"  - Parts: {parts}")
                            if len(parts) == 2:
                                original_name = parts[0]
                                random_id = parts[1]
                                partial_downloads.append({
                                    'filepath': filepath,
                                    'filename': original_name,
                                    'current_size': file_size,
                                    'temp_filename': filename
                                })
                                print(f"Archivo parcial encontrado: {original_name} ({naturalsize(file_size)})")
                            else:
                                print(f"  - No se pudo parsear el nombre: {filename}")
        except Exception as e:
            print(f"Error buscando archivos parciales: {e}")
            import traceback
            traceback.print_exc()
        return partial_downloads

    def clean_invalid_partial_cache(self):
        """Limpia el cache de archivos parciales eliminando entradas de archivos que no existen."""
        if not hasattr(self, 'partial_files_cache'):
            return

        print(" Limpiando cache de archivos parciales...")
        files_to_remove = []

        for partial_name, partial in self.partial_files_cache.items():
            if not os.path.exists(partial['filepath']):
                files_to_remove.append(partial_name)
                print(f"  - Archivo no existe: {partial['filepath']}")

        for partial_name in files_to_remove:
            del self.partial_files_cache[partial_name]
            print(f"  - Eliminado del cache: {partial_name}")

        if files_to_remove:
            print(f"✅ Cache limpiado: {len(files_to_remove)} entradas eliminadas")
        else:
            print("✅ Cache limpio: todos los archivos existen")

    def find_best_partial_file(self, filename):
        """Encuentra el archivo parcial más completo para un archivo dado."""
        print(f"DEBUG: find_best_partial_file buscando: '{filename}'")

        if not hasattr(self, 'partial_files_cache'):
            print("DEBUG: No hay cache de archivos parciales")
            return None

        print(f"DEBUG: Cache tiene {len(self.partial_files_cache)} archivos")
        matching_files = []

        filename_base = os.path.splitext(filename)[0]
        print(f"DEBUG: Nombre base a buscar: '{filename_base}'")

        for partial_name, partial in self.partial_files_cache.items():
            print(f"DEBUG: Comparando '{partial_name}' con '{filename_base}'")
            if partial['filename'] == filename_base:
                if os.path.exists(partial['filepath']):
                    matching_files.append(partial)
                    print(f"DEBUG: ¡Coincidencia encontrada! {partial_name}")
                else:
                    print(f"DEBUG: Archivo temporal no existe: {partial['filepath']}")
                    del self.partial_files_cache[partial_name]
                    print(f"DEBUG: Eliminado del cache: {partial_name}")

        print(f"DEBUG: Archivos coincidentes encontrados: {len(matching_files)}")

        if matching_files:
            best_file = max(matching_files, key=lambda x: x['current_size'])
            print(f"✅ Archivo parcial seleccionado: {best_file['temp_filename']} ({naturalsize(best_file['current_size'])})")
            return best_file

        print("DEBUG: No se encontraron archivos coincidentes")
        return None

    def clean_duplicate_files(self, filename):
        """Limpia archivos duplicados del mismo archivo base."""
        try:
            if os.path.exists(self.temp_folder):
                files_to_remove = []
                for temp_filename in os.listdir(self.temp_folder):
                    if temp_filename.endswith('.' + TELEGRAM_DAEMON_TEMP_SUFFIX):
                        base_name = temp_filename[:-len('.' + TELEGRAM_DAEMON_TEMP_SUFFIX)]
                        parts = base_name.rsplit('-', 1)
                        if len(parts) == 2:
                            original_name = parts[0]
                            if original_name == filename:
                                filepath = os.path.join(self.temp_folder, temp_filename)
                                file_size = os.path.getsize(filepath)
                                files_to_remove.append((temp_filename, file_size))

                if len(files_to_remove) > 1:
                    print(f"Encontrados {len(files_to_remove)} archivos duplicados para {filename}")
                    files_to_remove.sort(key=lambda x: x[1], reverse=True)
                    for temp_filename, file_size in files_to_remove[1:]:
                        filepath = os.path.join(self.temp_folder, temp_filename)
                        try:
                            os.remove(filepath)
                            print(f"Archivo duplicado eliminado: {temp_filename} ({naturalsize(file_size)})")
                        except Exception as e:
                            print(f"Error eliminando archivo duplicado {temp_filename}: {e}")

                    print(f"Archivo principal mantenido: {files_to_remove[0][0]} ({naturalsize(files_to_remove[0][1])})")

        except Exception as e:
            print(f"Error limpiando archivos duplicados: {e}")

    async def clean_partial_cache(self):
        """Limpia manualmente el cache de archivos parciales."""
        try:
            if not hasattr(self, 'partial_files_cache'):
                return "❌ No hay cache de archivos parciales para limpiar"

            cache_size_before = len(self.partial_files_cache)
            self.clean_invalid_partial_cache()
            cache_size_after = len(self.partial_files_cache)

            removed_count = cache_size_before - cache_size_after

            if removed_count > 0:
                return f"✅ Cache limpiado exitosamente:\n• Entradas eliminadas: {removed_count}\n• Entradas restantes: {cache_size_after}"
            else:
                return f"✅ Cache ya está limpio:\n• Entradas totales: {cache_size_after}"

        except Exception as e:
            return f"❌ Error limpiando cache: {str(e)}"

    async def clean_invalid_pending(self):
        """Limpia descargas pendientes con message_id inválidos del archivo JSON."""
        try:
            pending_downloads = self.load_pending_downloads()
            if not pending_downloads:
                return "❌ No hay descargas pendientes para limpiar"

            original_count = len(pending_downloads)
            valid_downloads = []
            removed_count = 0

            for pending in pending_downloads:
                if pending['message_id'] <= 0:
                    print(f"⚠️ Eliminando descarga inválida: Canal {pending['channel']}, Mensaje {pending['message_id']}")
                    removed_count += 1
                else:
                    valid_downloads.append(pending)

            if removed_count > 0:
                with open(self.pending_downloads_file, 'w', encoding='utf-8') as f:
                    json.dump(valid_downloads, f, indent=2)

                return f"✅ Limpieza completada:\n• Descargas originales: {original_count}\n• Descargas válidas: {len(valid_downloads)}\n• Descargas eliminadas: {removed_count}\n• Archivo actualizado: {self.pending_downloads_file}"
            else:
                return f"✅ No se encontraron descargas inválidas para limpiar\n• Descargas totales: {original_count}"

        except Exception as e:
            return f"❌ Error limpiando descargas pendientes: {str(e)}"

    async def force_resume_downloads(self, queue):
        """Fuerza la reanudación de descargas pendientes."""
        try:
            print(" Forzando reanudación de descargas...")
            partial_files = self.find_partial_downloads()
            self.partial_files_cache = {partial['filename']: partial for partial in partial_files}
            print(f"Archivos parciales encontrados: {len(partial_files)}")

            pending_downloads = self.load_pending_downloads()
            print(f"Descargas pendientes encontradas: {len(pending_downloads)}")

            if not pending_downloads:
                return "❌ No hay descargas pendientes en el archivo JSON"

            added_count = 0
            invalid_count = 0
            for pending in pending_downloads:
                if pending['message_id'] <= 0:
                    print(f"⚠️ Message ID inválido: {pending['message_id']}, saltando...")
                    invalid_count += 1
                    continue

                try:
                    download_media = DownloadMedia(
                        channel=pending['channel'],
                        message_id=pending['message_id'],
                        destination=pending['destination']
                    )
                    await queue.put(dict(event=None, download_media=download_media, reply_msg=None))
                    added_count += 1
                    print(f"✅ Descarga agregada: {download_media.get_link()}")
                except Exception as e:
                    print(f"❌ Error agregando descarga: {e}")

            result_message = f"✅ Reanudación forzada completada:\n• {added_count} descargas agregadas a la cola\n• {len(partial_files)} archivos parciales detectados"
            if invalid_count > 0:
                result_message += f"\n• {invalid_count} descargas con message_id inválido ignoradas"

            return result_message

        except Exception as e:
            return f"❌ Error en reanudación forzada: {str(e)}"

    async def create_test_pending(self):
        """Crea una descarga pendiente de prueba para el archivo parcial existente."""
        try:
            partial_files = self.find_partial_downloads()
            if not partial_files:
                return "❌ No hay archivos parciales para crear descarga pendiente"

            partial = partial_files[0]
            filename = partial['filename']

            test_pending = [{
                'channel': self.channel_id,
                'message_id': 12345,
                'destination': '.'
            }]

            with open(self.pending_downloads_file, 'w', encoding='utf-8') as f:
                json.dump(test_pending, f, indent=2)

            return f"✅ Descarga pendiente de prueba creada:\n• Archivo: {filename}\n• Canal: {self.channel_id}\n• Mensaje: 12345\n\nAhora puedes usar el comando 'reanudar' para probar"

        except Exception as e:
            return f"❌ Error creando descarga pendiente de prueba: {str(e)}"

    async def save_current_state(self, queue):
        """Guarda el estado completo de la cola y descargas activas."""
        try:
            queue_items = []
            temp_queue = asyncio.Queue()
            while not queue.empty():
                item = await queue.get()
                queue_items.append(item)
                await temp_queue.put(item)
            while not temp_queue.empty():
                await queue.put(await temp_queue.get())

            # las pausadas también se guardan: tras un reinicio vuelven a la cola
            active_items = list(self.active_downloads.values()) + list(getattr(self, 'paused_items', {}).values())
            all_items_to_save = {item['download_media'].get_link(): item for item in queue_items}
            all_items_to_save.update({item['download_media'].get_link(): item for item in active_items})

            self.save_pending_downloads(list(all_items_to_save.values()))
            print(f"Estado guardado: {len(all_items_to_save)} items en total (activos + en cola)")

        except Exception as e:
            print(f"Error guardando el estado actual: {e}")

    async def save_queue_to_disk(self, queue):
        """Guarda la cola actual en disco."""
        await self.save_current_state(queue)

    async def save_all_pending_downloads_async(self, queue):
        """Versión async segura de save_all_pending_downloads para usar desde loop.add_signal_handler.
        
        Bug 3: el signal handler síncrono (signal.signal) puede interrumpir operaciones de
        asyncio.Queue en curso. Al usar loop.add_signal_handler + asyncio.create_task, el guardado
        ocurre dentro del loop de eventos de forma cooperativa, sin condiciones de carrera.
        Usa await queue.get() en lugar de get_nowait() para mayor seguridad.
        """
        try:
            await self.save_current_state(queue)
        except Exception as e:
            print(f"Error guardando descargas pendientes (async): {e}")

    async def restore_pending_downloads(self, queue):
        """Restaura las descargas pendientes desde disco a la cola."""
        print("Cargando descargas pendientes...")
        pending_downloads = self.load_pending_downloads()
        print(f"Descargas pendientes encontradas: {len(pending_downloads)}")
        
        added_count = 0
        invalid_count = 0
        
        for pending in pending_downloads:
            channel = pending.get('channel')
            msg_id = pending.get('message_id')
            
            print(f"  - Procesando descarga pendiente: canal {channel}, mensaje {msg_id}")
            
            # Validar que el message_id sea válido
            if not msg_id or int(msg_id) <= 0:
                print(f"  ⚠️ Message ID inválido: {msg_id}, saltando...")
                invalid_count += 1
                continue
            
            try:
                download_media = DownloadMedia(
                    channel=channel,
                    message_id=msg_id,
                    destination=pending.get('destination', '.')
                )
                
                # Usar self.active_and_queued_links si está disponible en la clase principal
                if hasattr(self, 'active_and_queued_links'):
                    self.active_and_queued_links.add(download_media.get_link())
                
                # Añadir a la cola
                await queue.put(dict(
                    event=None, 
                    download_media=download_media, 
                    reply_msg=None, 
                    retries=pending.get('retries', 0)
                ))
                
                added_count += 1
                print(f"  ✅ Descarga agregada a la cola")
            except Exception as e:
                print(f"  ❌ Error agregando descarga: {e}")
                invalid_count += 1
        
        if invalid_count > 0:
            print(f"⚠️ {invalid_count} descargas con message_id inválido ignoradas")
        
        result_msg = f"✅ {added_count} descargas pendientes restauradas a la cola"
        print(result_msg)
        return result_msg
