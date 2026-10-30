# -*- coding: utf-8 -*-
import asyncio
import glob
import json
import logging
import os
import random
import re
import subprocess
import time
from shutil import move
from humanize import naturalsize
from telethon.errors import FloodWaitError
from .download_media import DownloadMedia
from .utils import TELEGRAM_DAEMON_TEMP_SUFFIX, get_file_emoji

logger = logging.getLogger(__name__)


class DownloadEngineMixin:
    """Mixin para el motor de descarga, workers asíncronos, reanudación y gestión de archivos."""

    async def list_download_folder(self):
        """Lista el contenido de la carpeta de descargas de forma asíncrona."""
        result = await asyncio.to_thread(
            subprocess.run, ["ls -l " + self.download_folder],
            shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        return result.stdout.decode('utf-8')

    def clean_downloads_folder(self):
        """Limpia archivos temporales .tdd de la carpeta de descargas."""
        pattern = os.path.join(self.temp_folder, f"*.{TELEGRAM_DAEMON_TEMP_SUFFIX}")
        files = glob.glob(pattern)
        if not files:
            return f"🧹 No hay archivos temporales (.{TELEGRAM_DAEMON_TEMP_SUFFIX}) en {self.temp_folder}"
        removed = 0
        for f in files:
            try:
                os.remove(f)
                removed += 1
            except Exception as e:
                logger.warning("Error eliminando %s: %s", f, e)
        return f"🧹 Archivos temporales limpiados: {removed} en {self.temp_folder}"

    async def get_media_message(self, message_media):
        """Obtiene el mensaje con media verificando duplicados."""
        if isinstance(message_media, DownloadMedia):
            message = await message_media.get_message(self.download_client)
        else:
            message = message_media

        if message and hasattr(message, 'media') and message.media:
            if hasattr(message.media, 'document') or hasattr(message.media, 'photo'):
                filename = self.get_filename(message)

                temp_exists = os.path.exists(
                    os.path.join(self.temp_folder, "{0}.{1}".format(filename, TELEGRAM_DAEMON_TEMP_SUFFIX))
                )
                final_exists = os.path.exists(os.path.join(self.download_folder, filename))

                if (temp_exists or final_exists) and self.duplicates == "ignore":
                    raise Exception("DUPLICATE_IGNORED:{0}".format(filename))

                return message
            else:
                raise Exception("NOT_DOWNLOADABLE")

    async def queue_download(self, event, channel, message_id,
                             destination='.', direct_message=None,
                             *, client=None, queue=None, save_fn=None):
        """Encola una descarga nueva, pre-resuelve el nombre y persiste la cola."""
        client_ref = client or self.client
        queue_ref = queue or getattr(self, 'current_queue', None)

        new_download_media = DownloadMedia(
            channel=channel, message_id=message_id, destination=destination
        )
        link = new_download_media.get_link()

        if link in self.active_and_queued_links:
            return

        resolved_filename = "unknown"
        caption = None
        try:
            if direct_message and direct_message.media:
                resolved_filename = self.get_filename(direct_message)
                caption = direct_message.message
            else:
                message = await client_ref.get_messages(channel, ids=message_id)
                if message and message.media:
                    resolved_filename = self.get_filename(message)
                    caption = message.message
        except Exception as e:
            logger.debug("No se pudo pre-resolver el nombre del archivo: %s", e)

        self.active_and_queued_links.add(link)

        if not self.individual_progress_messages:
            reply_msg = await event.reply(
                "✅ {item} agregado correctamente a la cola".format(item=new_download_media.get_link())
            )
            async def _delete_temp_message():
                await asyncio.sleep(5)
                try:
                    await reply_msg.delete()
                except Exception:
                    pass
            asyncio.create_task(_delete_temp_message())
        else:
            reply_msg = await event.reply(
                "{item} agregado a la cola".format(item=new_download_media.get_link())
            )

        if queue_ref:
            await queue_ref.put(dict(
                event=event,
                download_media=new_download_media,
                reply_msg=reply_msg,
                retries=0,
                filename=resolved_filename,
                direct_message=direct_message,
            ))
            logger.debug("Descarga agregada a la cola. Tamaño de cola: %s", queue_ref.qsize())

        async def _safe_save():
            try:
                if save_fn:
                    await save_fn()
                elif hasattr(self, 'save_queue_to_disk') and queue_ref:
                    await self.save_queue_to_disk(queue_ref)
            except Exception as e:
                logger.warning("No se pudo guardar la cola en disco: %s", e)

        asyncio.create_task(_safe_save())

        if hasattr(self, 'ensure_question'):
            try:
                await self.ensure_question(link, resolved_filename, caption)
            except Exception as e:
                logger.debug("No se pudo preguntar por %s: %s", resolved_filename, e)

        if hasattr(self, 'refresh_sticky_menu'):
            await self.refresh_sticky_menu(event.chat_id)

    async def cancel_all_downloads(self, queue, client):
        """Cancela todas las descargas, vacía la cola, limpia temp y mensajes."""
        try:
            cancelled_count = 0
            message_ids_to_delete = []

            active_links = []
            for worker_item in list(self.active_downloads.values()):
                link = worker_item['download_media'].get_link()
                active_links.append(link)
                self.cancelled_links.add(link)
                cancelled_count += 1
                if worker_item.get('reply_msg') and hasattr(worker_item['reply_msg'], 'id'):
                    message_ids_to_delete.append(worker_item['reply_msg'].id)

            queued_count = 0
            queued_links = []
            while not queue.empty():
                try:
                    item = await queue.get_nowait()
                    link = item['download_media'].get_link()
                    queued_links.append(link)
                    self.cancelled_links.add(link)
                    if link in self.active_and_queued_links:
                        self.active_and_queued_links.remove(link)
                    queued_count += 1
                    if item.get('reply_msg') and hasattr(item['reply_msg'], 'id'):
                        message_ids_to_delete.append(item['reply_msg'].id)
                except asyncio.QueueEmpty:
                    break

            await asyncio.sleep(0.5)

            pattern = os.path.join(self.temp_folder, f"*.{TELEGRAM_DAEMON_TEMP_SUFFIX}")
            temp_files = glob.glob(pattern)
            temp_removed = 0
            for temp_file in temp_files:
                try:
                    os.remove(temp_file)
                    temp_removed += 1
                except Exception as e:
                    logger.warning("Error eliminando %s: %s", temp_file, e)

            try:
                with open(self.pending_downloads_file, 'w', encoding='utf-8') as f:
                    json.dump([], f, indent=2)
            except Exception as e:
                logger.warning("Error vaciando pending_downloads.json: %s", e)

            deleted_messages = 0
            chat_id = self.raw_channel_id

            if message_ids_to_delete:
                try:
                    for i in range(0, len(message_ids_to_delete), 100):
                        batch = message_ids_to_delete[i:i+100]
                        try:
                            await client.delete_messages(chat_id, batch, revoke=True)
                            deleted_messages += len(batch)
                        except Exception as e:
                            logger.warning("Error eliminando mensajes batch %s: %s", i, e)
                except Exception as e:
                    logger.warning("Error eliminando mensajes de descarga: %s", e)

            try:
                progress_message_ids = []
                filenames_in_progress = list(self.in_progress.keys())

                async for msg in client.iter_messages(chat_id, limit=200):
                    if hasattr(msg, 'message') and msg.message:
                        msg_text = msg.message
                        if '▰' in msg_text or '▱' in msg_text or '%' in msg_text:
                            for filename in filenames_in_progress:
                                if filename in msg_text:
                                    progress_message_ids.append(msg.id)
                                    break
                        elif 'Descargando archivo' in msg_text or ('✅' in msg_text and 'listo' in msg_text):
                            progress_message_ids.append(msg.id)

                if progress_message_ids:
                    progress_message_ids = list(set(progress_message_ids))
                    for i in range(0, len(progress_message_ids), 100):
                        batch = progress_message_ids[i:i+100]
                        try:
                            await client.delete_messages(chat_id, batch, revoke=True)
                            deleted_messages += len(batch)
                        except Exception as e:
                            logger.warning("Error eliminando mensajes de progreso batch %s: %s", i, e)
            except Exception as e:
                logger.warning("Error buscando mensajes de progreso: %s", e)

            self.in_progress.clear()
            self.partial_files_cache = {}
            self.resume_offsets.clear()
            self.paused_links.clear()
            self.active_status_message_id = None
            self.active_status_chat_id = None
            self.last_status_content = None

            async def delayed_cleanup():
                await asyncio.sleep(2)
                links_to_remove = set(active_links + queued_links)
                workers_to_remove = []
                for worker_id, worker_item in list(self.active_downloads.items()):
                    link = worker_item['download_media'].get_link()
                    if link in links_to_remove:
                        workers_to_remove.append(worker_id)
                for worker_id in workers_to_remove:
                    if worker_id in self.active_downloads:
                        del self.active_downloads[worker_id]

            asyncio.create_task(delayed_cleanup())

            return f"🛑 **Todas las descargas canceladas:**\n\n" \
                   f"• Descargas activas canceladas: {cancelled_count}\n" \
                   f"• Items en cola eliminados: {queued_count}\n" \
                   f"• Archivos temporales eliminados: {temp_removed}\n" \
                   f"• Mensajes eliminados del chat: {deleted_messages}\n" \
                   f"• JSON de pendientes vaciado\n\n" \
                   f"✅ Sistema limpiado completamente"
        except Exception as e:
            import traceback
            traceback.print_exc()
            return f"❌ Error cancelando todas las descargas: {str(e)}"

    async def run_worker_loop(self, queue, client, peer_channel):
        """Ejecuta el ciclo de vida de un worker de descarga."""
        worker_id = id(asyncio.current_task())
        logger.debug("Worker %s iniciado", worker_id)

        while True:
            logger.debug("Worker %s esperando trabajo...", worker_id)
            item = await queue.get()
            download_item = None
            reply_msg = None
            success = False
            filename = "unknown"

            # Pausado mientras esperaba en cola: se aparta hasta que se reanude
            paused_link = item['download_media'].get_link()
            if paused_link in self.paused_links:
                self.paused_items[paused_link] = item
                queue.task_done()
                continue

            try:
                self.active_downloads[worker_id] = item
                logger.debug("Worker %s recibió trabajo: %s", worker_id, item['download_media'].get_link())
                download_item = item['download_media']
                reply_msg = item['reply_msg']
                item.setdefault('started_at', time.time())

                message = await self.get_media_message(download_item)

                if not message and self.download_client == self.client and 'direct_message' in item and item.get('direct_message'):
                    if hasattr(item['direct_message'], 'media') and item['direct_message'].media:
                        message = item['direct_message']
                        logger.debug("Usando mensaje directo del reenvío como fallback")

                if not message or not hasattr(message, 'media') or not message.media:
                    raise Exception(f"No se pudo obtener el mensaje con media para {download_item.get_link()}")

                filename = self.get_filename(message)
                item['filename'] = filename
                if hasattr(self, 'ensure_question'):
                    asyncio.create_task(self.ensure_question(download_item.get_link(), filename,
                                                             getattr(message, 'message', None)))

                file_name, file_extension = os.path.splitext(filename)
                random_id = self.get_random_id(8)
                temp_filename = f"{file_name}-{random_id}{file_extension}"

                temp_filepath = os.path.join(self.temp_folder, "{0}.{1}".format(temp_filename, TELEGRAM_DAEMON_TEMP_SUFFIX))

                partial_file = self.find_best_partial_file(filename)
                if partial_file:
                    if os.path.exists(partial_file['filepath']):
                        temp_filepath = partial_file['filepath']
                        temp_filename = partial_file['temp_filename']
                        logger.info("✅ Reanudando descarga parcial: %s desde %s", filename, naturalsize(partial_file['current_size']))
                    else:
                        logger.warning("Archivo temporal no existe: %s — Iniciando descarga completa desde cero", partial_file['filepath'])
                        partial_file = None
                        self.clean_duplicate_files(filename)
                else:
                    self.clean_duplicate_files(filename)

                final_filepath_folder = os.path.join(self.download_folder, download_item.destination)
                final_filepath = os.path.join(final_filepath_folder, filename)

                if self.duplicates == "rename" and (os.path.exists(temp_filepath) or os.path.exists(final_filepath)):
                    filename = temp_filename
                    logger.debug("Archivo duplicado detectado, renombrando a: %s", filename)
                elif self.duplicates == "overwrite" and os.path.exists(final_filepath):
                    logger.debug("Archivo existente detectado, sobrescribiendo: %s", filename)
                    try:
                        os.remove(final_filepath)
                    except Exception as e:
                        logger.warning("No se pudo eliminar archivo existente: %s", e)

                if hasattr(message.media, 'photo'):
                    size = 0
                else:
                    size = message.media.document.size

                if reply_msg and self.individual_progress_messages:
                    await self.log_reply(
                        reply_msg,
                        "Descargando archivo {0} ({1} bytes)".format(item['filename'], size)
                    )
                else:
                    if not self.individual_progress_messages:
                        reply_msg = None

                real_filename = item['filename']
                if partial_file:
                    initial_progress = partial_file['current_size']
                    if not hasattr(self, 'resume_offsets'):
                        self.resume_offsets = {}
                    self.resume_offsets[real_filename] = initial_progress

                    download_callback = lambda received, total: self.set_progress(
                        real_filename, reply_msg, initial_progress + received, total
                    )
                else:
                    initial_progress = 0
                    download_callback = lambda received, total: self.set_progress(real_filename, reply_msg, received, total)

                link = download_item.get_link()
                if link in self.cancelled_links:
                    raise Exception("CANCELLED")

                if reply_msg is None:
                    try:
                        reply_msg = await client.send_message(
                            peer_channel,
                            f"⏳ Reanudando descarga pendiente...\n📄 {filename}"
                        )
                    except Exception as e:
                        logger.debug("No se pudo crear mensaje de progreso para reanudación: %s", e)

                CHUNK_SIZE = 512 * 1024
                downloaded_file = None
                if partial_file:
                    logger.info("Reanudando descarga (manual) desde byte %s", initial_progress)
                    downloaded_bytes_session = 0
                    try:
                        with open(temp_filepath, 'ab') as f:
                            async for chunk in self.download_client.iter_download(
                                message.media,
                                offset=initial_progress,
                                request_size=CHUNK_SIZE,
                            ):
                                if link in self.cancelled_links:
                                    f.close()
                                    raise Exception("CANCELLED")
                                f.write(chunk)
                                downloaded_bytes_session += len(chunk)
                                await download_callback(downloaded_bytes_session, size)
                        downloaded_file = temp_filepath
                    except Exception as e:
                        if str(e) == "CANCELLED":
                            raise
                        logger.warning("Error en reanudación manual, usando descarga normal: %s", e)
                        downloaded_file = await self.download_client.download_media(message, temp_filepath, progress_callback=download_callback)
                else:
                    try:
                        with open(temp_filepath, 'wb') as f:
                            async for chunk in self.download_client.iter_download(
                                message.media,
                                request_size=CHUNK_SIZE,
                            ):
                                if link in self.cancelled_links:
                                    raise Exception("CANCELLED")
                                f.write(chunk)
                                await download_callback(f.tell(), size)
                        downloaded_file = temp_filepath
                    except Exception as e:
                        if str(e) == "CANCELLED":
                            raise
                        logger.warning("Error en descarga optimizada, usando descarga normal: %s", e)
                        downloaded_file = await self.download_client.download_media(message, temp_filepath, progress_callback=download_callback)

                if downloaded_file is None:
                    if os.path.exists(temp_filepath) and os.path.getsize(temp_filepath) >= size:
                        downloaded_file = temp_filepath
                    else:
                        raise Exception("La descarga manual no se completó.")

                await self.set_progress(item['filename'], reply_msg, size, size)

                if not os.path.exists(final_filepath_folder):
                    os.makedirs(final_filepath_folder)
                move(temp_filepath, final_filepath)
                final_filename = item['filename'] if 'filename' in item else filename
                link = item['download_media'].get_link()

                download_msg_id = None
                COMPRESSED_EXTENSIONS = {'.rar', '.zip', '.7z', '.r00', '.r01'}
                file_ext = os.path.splitext(final_filename)[1].lower()
                is_compressed = file_ext in COMPRESSED_EXTENSIONS
                is_part_rar = bool(re.search(r'\.part\d+\.rar$', final_filename, re.IGNORECASE))

                # Resumen de la descarga: tamaño, duración y velocidad media de esta sesión
                elapsed = max(1.0, time.time() - item['started_at'])
                session_bytes = max(0, size - initial_progress)
                stats_line = (f"📦 {naturalsize(size)} · ⏱️ {self.fmt_duration(elapsed)} · "
                              f"⚡ {naturalsize(session_bytes / elapsed)}/s")
                is_video = self.is_video_file(final_filename) if hasattr(self, 'is_video_file') else False
                will_rename = is_video and getattr(self, 'auto_rename', False)
                notif_msg = None
                notif_text = None

                if reply_msg and self.individual_progress_messages:
                    notif_text = (f"✅ **{get_file_emoji(final_filename)} {final_filename}**\n{stats_line}\n🔗 {link}")
                    await self.log_reply(reply_msg, notif_text)
                    notif_msg = reply_msg
                    download_msg_id = reply_msg.id

                    temp_path = os.path.join(self.download_folder, final_filename)
                    info = self.detect_multipart_set(temp_path)
                    if info:
                        base = info['base']
                        if base not in self.part_message_ids:
                            self.part_message_ids[base] = []
                        self.part_message_ids[base].append(reply_msg.id)
                        if base not in self.origin_links:
                            self.origin_links[base] = link
                elif not is_compressed and not is_part_rar:
                    logger.info("✅ %s listo - notificando", final_filename)
                    try:
                        emoji = get_file_emoji(final_filename)
                        notif_text = (
                            f"{emoji} **Descarga completada**\n"
                            f"📄 `{final_filename}`\n"
                            f"{stats_line}"
                        )
                        if will_rename:
                            notif_text += "\n🏷️ _Identificando…_"
                        notif_msg = await client.send_message(peer_channel, notif_text)
                        download_msg_id = notif_msg.id

                        temp_path = os.path.join(self.download_folder, final_filename)
                        info = self.detect_multipart_set(temp_path)
                        if info:
                            base = info['base']
                            if base not in self.part_message_ids:
                                self.part_message_ids[base] = []
                            self.part_message_ids[base].append(notif_msg.id)
                            if base not in self.origin_links:
                                self.origin_links[base] = link
                    except Exception as e:
                        logger.error("Error enviando notificación de completado: %s", e)
                else:
                    logger.debug("✅ %s listo - notificación delegada al extractor", final_filename)

                origin_msg_id = getattr(download_item, 'message_id', None)
                self.register_completed_download(link, final_filename, download_msg_id, origin_msg_id)

                try:
                    asyncio.create_task(self.try_auto_extract(final_filepath, queue, client, peer_channel))
                except Exception as e:
                    logger.error("Error lanzando auto-extracción en background: %s", e, exc_info=True)

                if will_rename and not is_compressed and not is_part_rar:
                    base_text = (notif_text or "").replace("\n🏷️ _Identificando…_", "")
                    async def _after_download(path=final_filepath, nmsg=notif_msg, btext=base_text, fsize=size, lk=link):
                        # si hay una pregunta pendiente para este archivo, se encarga ella
                        if hasattr(self, 'on_download_finished') and await self.on_download_finished(lk, path, nmsg, btext, fsize):
                            return
                        await self.rename_media_file(path, client, peer_channel,
                                                     notify_msg=nmsg, base_text=btext, size=fsize)
                    try:
                        asyncio.create_task(_after_download())
                    except Exception as e:
                        logger.error("Error lanzando renombrado en background: %s", e, exc_info=True)
                elif not is_compressed and not is_part_rar and hasattr(self, 'add_history'):
                    self.add_history('downloaded', final_filename, size=size, path=final_filepath)

                real_filename = item['filename'] if 'filename' in item else filename
                if hasattr(self, 'resume_offsets') and real_filename in self.resume_offsets:
                    del self.resume_offsets[real_filename]

                if hasattr(self, 'partial_files_cache'):
                    files_to_remove = []
                    for partial in self.partial_files_cache.values():
                        if partial['filename'] == real_filename:
                            files_to_remove.append(partial['temp_filename'])

                    for temp_filename in files_to_remove:
                        temp_filepath = os.path.join(self.temp_folder, temp_filename)
                        try:
                            if os.path.exists(temp_filepath):
                                os.remove(temp_filepath)
                                logger.debug("Archivo temporal eliminado: %s", temp_filename)
                        except Exception as e:
                            logger.warning("Error eliminando archivo temporal %s: %s", temp_filename, e)

                    self.partial_files_cache = {k: v for k, v in self.partial_files_cache.items()
                                                if v['filename'] != real_filename}

                success = True
                queue.task_done()
                logger.debug("Worker %s completó trabajo", worker_id)

            except Exception as e:
                if isinstance(e, FloodWaitError):
                    wait_seconds = e.seconds + 5
                    logger.warning("⏳ FloodWait detectado: esperando %ss antes de reintentar...", wait_seconds)
                    try:
                        await self.log_reply(reply_msg, f"⏳ Límite de Telegram alcanzado. Esperando {wait_seconds}s...")
                    except Exception:
                        pass
                    await asyncio.sleep(wait_seconds)
                    await queue.put(item)
                    queue.task_done()
                    continue

                if str(e) == "PAUSED":
                    # Se aparta (no vuelve a la cola) hasta que se reanude; el parcial se conserva
                    logger.debug("Worker %s - Descarga pausada: %s", worker_id, filename)
                    item.pop('started_at', None)
                    self.paused_items[item['download_media'].get_link()] = item
                    queue.task_done()
                    continue

                if str(e) == "CANCELLED":
                    logger.debug("Worker %s - Descarga cancelada: %s", worker_id, filename)
                    link = item['download_media'].get_link()
                    if link in self.active_and_queued_links:
                        self.active_and_queued_links.remove(link)
                    if link in self.cancelled_links:
                        self.cancelled_links.remove(link)
                    queue.task_done()
                    continue

                if str(e).startswith("DUPLICATE_IGNORED:"):
                    ignored_filename = str(e).split(":", 1)[1]
                    msg = f"⏭️ {ignored_filename} ya existe. Ignorándolo (política: ignore)."
                    await self.log_reply(reply_msg, msg)
                    logger.debug("Worker %s - %s", worker_id, msg)
                    link = item['download_media'].get_link()
                    if link in self.active_and_queued_links:
                        self.active_and_queued_links.remove(link)
                    queue.task_done()
                    continue

                if str(e) == "NOT_DOWNLOADABLE":
                    msg = "❌ Ese archivo no es descargable. Intenta enviarlo como archivo adjunto."
                    await self.log_reply(reply_msg, msg)
                    logger.debug("Worker %s - %s", worker_id, msg)
                    link = item['download_media'].get_link()
                    if link in self.active_and_queued_links:
                        self.active_and_queued_links.remove(link)
                    queue.task_done()
                    continue

                if str(e).startswith("PERMANENT:"):
                    perm_reason = str(e).split(":", 1)[1].strip()
                    msg = f"❌ Error permanente: {perm_reason}"
                    await self.log_reply(reply_msg, msg)
                    logger.error("Worker %s - %s", worker_id, msg)
                    link = item['download_media'].get_link()
                    if link in self.active_and_queued_links:
                        self.active_and_queued_links.remove(link)
                    queue.task_done()
                    continue

                try:
                    item['retries'] += 1
                    error_msg = f"Error: {e}. Reintento {item['retries']}/{self.max_retries}."
                    await self.log_reply(reply_msg, error_msg)
                    logger.error("Worker %s - %s", worker_id, error_msg, exc_info=True)

                    if item['retries'] < self.max_retries:
                        base_delay = 15
                        max_delay = 180
                        delay = min(base_delay * (2 ** (item['retries'] - 1)), max_delay)
                        jitter = delay * 0.2 * (2 * random.random() - 1)
                        wait = max(1.0, delay + jitter)
                        logger.debug("Worker %s - backoff reintento %s/%s: esperando %.1fs",
                                     worker_id, item['retries'], self.max_retries, wait)
                        await asyncio.sleep(wait)
                        await queue.put(item)
                    else:
                        final_error_msg = f"La descarga de {download_item.get_link()} ha fallado tras {self.max_retries} intentos."
                        await self.log_reply(reply_msg, final_error_msg)
                        logger.error("Worker %s - %s", worker_id, final_error_msg)
                        if hasattr(self, 'add_history'):
                            self.add_history('failed', item.get('filename') or download_item.get_link(),
                                             link=download_item.get_link(), reason=str(e)[:200])
                except Exception:
                    pass

                queue.task_done()

            finally:
                if worker_id in self.active_downloads:
                    del self.active_downloads[worker_id]

                if item and isinstance(item, dict) and 'filename' in item:
                    filename_key = item['filename']
                    if filename_key in self.in_progress:
                        self.in_progress.pop(filename_key, None)
                        if hasattr(self, 'print_status') and self.active_status_message_id:
                            asyncio.create_task(self.print_status())

                if download_item:
                    link_to_remove = download_item.get_link()
                    should_remove = success or (item.get('retries', 0) >= self.max_retries)
                    if should_remove and link_to_remove in self.active_and_queued_links:
                        self.active_and_queued_links.remove(link_to_remove)
                        logger.debug("Link %s eliminado del set de seguimiento.", link_to_remove)
