# -*- coding: utf-8 -*-
import asyncio
import logging
import math
import os
import time
from humanize import naturalsize
from .utils import get_file_emoji

logger = logging.getLogger(__name__)


class StatusManagerMixin:
    """Mixin para el seguimiento del progreso de descarga, panel de estado y notificaciones."""

    async def log_reply(self, message, reply):
        """Edita un mensaje de respuesta de forma segura con log."""
        logger.info(reply)
        if message is None:
            return
        try:
            if hasattr(message, 'id') and hasattr(self, 'menu_message_id'):
                if message.id == self.menu_message_id:
                    await message.reply(reply)
                    return
            await message.edit(reply)
        except Exception as e:
            if "message not found" not in str(e).lower() and "message to edit not found" not in str(e).lower():
                logger.debug("No se pudo editar mensaje de progreso: %s", e)

    async def set_progress(self, filename, message, received, total):
        """Actualiza el progreso de descarga y programa la actualización de la UI."""
        link = ""
        for worker_item in self.active_downloads.values():
            if worker_item.get('filename') == filename:
                link = worker_item['download_media'].get_link()
                break

        if link in self.cancelled_links:
            raise Exception("CANCELLED")
        if link in self.paused_links:
            raise Exception("PAUSED")

        if received >= total:
            try:
                self.in_progress.pop(filename, None)
                self.progress_info.pop(filename, None)
                if hasattr(self, 'download_start_time') and filename in self.download_start_time:
                    del self.download_start_time[filename]
                if hasattr(self, 'resume_offsets') and filename in self.resume_offsets:
                    del self.resume_offsets[filename]

                if hasattr(self, 'print_status') and self.active_status_message_id:
                    asyncio.create_task(self.print_status())
            except Exception:
                pass
            return

        percentage = math.trunc(received / total * 10000) / 100

        initial_offset = self.resume_offsets.get(filename, 0) if hasattr(self, 'resume_offsets') else 0
        received_this_session = received - initial_offset

        current_time = time.time()
        if hasattr(self, 'download_start_time') and filename in self.download_start_time:
            elapsed_time = current_time - self.download_start_time[filename]
            if elapsed_time > 0:
                speed = received_this_session / elapsed_time
                speed_str = naturalsize(speed) + "/s"
                eta_seconds = (total - received) / speed if speed > 0 else 0
                eta_str = f"ETA: {int(eta_seconds//60)}m {int(eta_seconds%60)}s" if eta_seconds > 0 else ""
            else:
                speed_str = "calculando..."
                eta_str = ""
        else:
            speed_str = "iniciando..."
            eta_str = ""
            if not hasattr(self, 'download_start_time'):
                self.download_start_time = {}
            self.download_start_time[filename] = current_time

        # Datos estructurados para el panel
        self.progress_info[filename] = {
            'received': received, 'total': total, 'ts': current_time,
            'speed': (received_this_session / (current_time - self.download_start_time[filename]))
            if current_time > self.download_start_time.get(filename, current_time) else 0,
        }

        bar_length = 20
        filled_length = int(bar_length * received / total)
        bar = "▰" * filled_length + "▱" * (bar_length - filled_length)

        speed_display = f"⚡{speed_str}" if speed_str not in ("calculando...", "iniciando...") else speed_str
        eta_display = f"⏱️{eta_str}" if eta_str else ""

        progress_message = "{0}\n{1} {2}% ({3} / {4}) {5} {6}".format(
            get_file_emoji(filename) + " " + filename, bar, percentage, naturalsize(received), naturalsize(total), speed_display, eta_display
        )
        self.in_progress[filename] = progress_message

        # Guardado inteligente de progreso en disco (cada 5%)
        if not hasattr(self, 'last_save_percentage'):
            self.last_save_percentage = {}

        last_percentage = self.last_save_percentage.get(filename, -1)
        current_percentage_rounded = int(percentage / 5) * 5

        if current_percentage_rounded > last_percentage:
            self.last_save_percentage[filename] = current_percentage_rounded
            if hasattr(self, 'save_progress_async'):
                asyncio.create_task(self.save_progress_async())

        # Throttling por archivo individual para evitar EditMessageRequest FloodWait
        if not hasattr(self, '_last_file_ui_update'):
            self._last_file_ui_update = {}

        last_file_update = self._last_file_ui_update.get(filename, 0)
        if (current_time - last_file_update) >= self.update_frequency:
            self._last_file_ui_update[filename] = current_time
            if message is not None:
                async def _safe_edit(msg_target, text_content):
                    try:
                        await self.log_reply(msg_target, text_content)
                    except Exception:
                        pass
                asyncio.create_task(_safe_edit(message, progress_message))

        # Actualizar panel de estado global
        if (current_time - self.last_update) > self.update_frequency:
            if self.active_status_message_id and hasattr(self, 'print_status'):
                asyncio.create_task(self.print_status())
            self.last_update = current_time

    async def print_status(self, event=None, queue=None, client=None):
        """Genera y actualiza el panel interactivo de estado de descargas activas y cola."""
        try:
            client_ref = client or self.client
            queue_size = queue.qsize() if queue else (self.current_queue.qsize() if hasattr(self, 'current_queue') else 0)

            extracting_items = []
            if hasattr(self, 'extracting_sets') and isinstance(self.extracting_sets, dict):
                extracting_items = list(self.extracting_sets.values())

            if not self.in_progress and not extracting_items:
                if event is None and self.active_status_message_id:
                    try:
                        await client_ref.delete_messages(self.active_status_chat_id, self.active_status_message_id)
                        logger.debug("Mensaje de estado eliminado por inactividad.")
                    except Exception as e:
                        logger.debug("Error borrando mensaje estado: %s", e)
                    self.active_status_message_id = None
                    return None

                return f"✅ **No hay descargas activas**\n📋 Cola pendiente: {queue_size}"

            grouped = {}
            singles = []

            for filename, progress_text in self.in_progress.items():
                temp_path = os.path.join(self.download_folder, filename)
                info = self.detect_multipart_set(temp_path)

                if info:
                    base = info['base']
                    if base not in grouped:
                        grouped[base] = []
                    grouped[base].append(progress_text)
                else:
                    singles.append(progress_text)

            status_msg = "⚡ **Descargas activas** (Actualizado auto)\n\n"

            if extracting_items:
                status_msg += "🏗️ **Extracciones en curso:**\n"
                for ext_status in extracting_items:
                    status_msg += f"  {ext_status}\n"
                status_msg += "\n"

            for base, parts in grouped.items():
                status_msg += f"📦 **{base}**\n"
                for part in parts:
                    status_msg += f"  ┕ {part}\n"
                status_msg += "\n"

            for single in singles:
                status_msg += f"📄 {single}\n\n"

            status_msg += f"📋 **Cola pendiente:** {queue_size}"

            chat_id = event.chat_id if event else self.raw_channel_id

            if self.active_status_message_id and self.active_status_chat_id == chat_id:
                if self.last_status_content != status_msg:
                    try:
                        await client_ref.edit_message(self.active_status_chat_id, self.active_status_message_id, status_msg)
                        self.last_status_content = status_msg
                        return
                    except Exception as e:
                        if "MessageNotModified" in str(type(e).__name__) or "not modified" in str(e).lower():
                            return
                        logger.debug("No se pudo editar mensaje de estado: %s", e)
                        self.active_status_message_id = None
                else:
                    return

            msg = await client_ref.send_message(chat_id, status_msg)
            self.active_status_message_id = msg.id
            self.active_status_chat_id = chat_id
            self.last_status_content = status_msg

            if hasattr(self, 'refresh_sticky_menu'):
                await self.refresh_sticky_menu(chat_id)

        except Exception as e:
            logger.error("Error en print_status: %s", e, exc_info=True)
            return "Ocurrió un error al verificar el estado. Inténtalo de nuevo."

    def show_queue_status(self, queue=None):
        """Muestra el resumen completo de cola, parciales y descargas activas."""
        try:
            queue_size = queue.qsize() if queue else (self.current_queue.qsize() if hasattr(self, 'current_queue') else 0)
            active_downloads = len(self.in_progress)

            partial_files = self.find_partial_downloads()
            partial_count = len(partial_files)

            pending_downloads = self.load_pending_downloads()
            pending_count = len(pending_downloads)

            status = " Estado del sistema:\n"
            status += f"• Cola de descargas: {queue_size} items\n"
            status += f"• Descargas activas: {active_downloads}\n"
            status += f"• Archivos parciales: {partial_count}\n"
            status += f"• Descargas pendientes (JSON): {pending_count}\n\n"

            if partial_files:
                status += " Archivos parciales encontrados:\n"
                for partial in partial_files:
                    status += f"• {partial['filename']} ({naturalsize(partial['current_size'])})\n"
                status += "\n"

            if pending_downloads:
                status += " Descargas pendientes en JSON:\n"
                for pending in pending_downloads:
                    status += f"• Canal {pending['channel']}, Mensaje {pending['message_id']}\n"
                status += "\n"

            if self.in_progress:
                status += "⚡ Descargas activas:\n"
                for filename, progress in self.in_progress.items():
                    status += f"• {progress}\n"

            return status
        except Exception as e:
            return f"Error obteniendo estado: {str(e)}"
