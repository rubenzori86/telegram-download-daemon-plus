# -*- coding: utf-8 -*-
import asyncio
import logging
import os
import re
import sys
from telethon import Button
from telethon.tl.types import PeerChannel
from .menu import BotMenu
from .session_manager import TELEGRAM_DAEMON_SESSION_PATH
from .utils import sanitize_path_component

logger = logging.getLogger(__name__)


class CommandHandlerMixin:
    """Mixin para el análisis y ejecución de comandos de texto y eventos de CallbackQuery (botones del menú)."""

    async def parse_command(self, event, queue=None, client=None):
        """Interpreta un comando de texto o URL de Telegram y ejecuta la acción correspondiente."""
        client_ref = client or self.client
        queue_ref = queue or getattr(self, 'current_queue', None)
        raw_msg = (event.message.message or "").strip()
        first_token = raw_msg.split()[0].lower() if raw_msg else ""
        command = first_token.lstrip('/').split('@')[0]

        match command:
            case 'menu' | 'start' | 'panel':
                return await self.show_main_menu(event)
            case 'help' | 'ayuda':
                return (
                    "📖 **Guía de Uso - Telegram Download Daemon**\n\n"
                    "⚡ **Cómo descargar:**\n"
                    "• **Reenviar archivos:** Reenvía cualquier archivo o vídeo al canal.\n"
                    "• **Pegar enlaces:** Pega enlaces de Telegram (`https://t.me/c/.../123`).\n"
                    "• **Subida directa:** Envía archivos directamente al chat.\n\n"
                    "🎛️ **Comandos rápidos:**\n"
                    "• `/menu` o `/start` — Abre el panel de control interactivo.\n"
                    "• `/descargas` — Activas, cola y pausadas con progreso.\n"
                    "• `/biblioteca` — Espacio de los discos, últimas añadidas y pendientes.\n"
                    "• `/buscar nombre` — Dónde está una serie o peli y cuántos capítulos tiene.\n"
                    "• `/pendientes` — Lo que no se pudo identificar o colocar.\n"
                    "• `/historial` — Últimos movimientos.\n"
                    "• `/limpiarchat` — Borra todo el chat y deja solo el panel.\n"
                    "• `/status` o `/estado` — Muestra el progreso de descargas en vivo.\n"
                    "• `/cola` — Lista y gestiona los archivos en espera.\n"
                    "• `/archivos` o `/list` — Muestra los archivos descargados.\n"
                    "• `/config` — Consulta los ajustes del sistema.\n"
                    "• `/limpiar` o `/clean` — Limpia archivos temporales.\n"
                    "• `/reanudar` — Fuerza la reanudación de pendientes.\n"
                    "• `/ayuda` — Muestra este mensaje de ayuda.\n\n"
                    "💡 _Consejo: Puedes usar el botón `[/]` de Telegram para acceder al menú de comandos._"
                )
            case 'descargas' | 'downloads' | 'status' | 'estado':
                text, buttons = self.downloads_view()
                await self.show_in_panel(event, text, buttons, view='downloads')
                return None
            case 'biblioteca' | 'library':
                text, buttons = self.library_view()
                await self.show_in_panel(event, text, buttons)
                return None
            case 'pendientes':
                text, buttons = self.pending_view()
                await self.show_in_panel(event, text, buttons)
                return None
            case 'historial' | 'history':
                text, buttons = self.history_view(25)
                await self.show_in_panel(event, text, [[Button.inline("🎛️ Menú", data="menu_main")]])
                return None
            case 'limpiarchat' | 'limpiar_chat' | 'clearchat':
                res = await self.clear_chat()
                text, buttons = self.main_menu_content()
                note = ("⚠️ Hace falta el modo híbrido para limpiar el chat.\n\n" if res is None
                        else f"🧽 Chat limpio: {res[0]} mensajes borrados\n\n")
                await self.show_in_panel(event, note + text, buttons, view='main')
                return None
            case 'buscar' | 'search':
                query = raw_msg.split(None, 1)[1] if len(raw_msg.split(None, 1)) > 1 else ''
                if not query.strip():
                    return "🔎 Uso: `/buscar nombre` (ej. `/buscar bear`)"
                return await self.search_text(query.strip())
            case 'list' | 'archivos' | 'files':
                return await self.list_download_folder()
            case 'clean' | 'limpiar':
                return self.clean_downloads_folder()
            case 'cola' | 'queue':
                return self.show_queue_status(queue=queue_ref)
            case 'reanudar' | 'resume':
                return await self.force_resume_downloads(queue_ref)
            case 'crear_pendiente':
                return await self.create_test_pending()
            case 'limpiar_cache':
                return await self.clean_partial_cache()
            case 'duplicados':
                return self.show_duplicates_policy()
            case 'duplicados_ignorar':
                return self.set_duplicates_policy("ignore")
            case 'duplicados_renombrar':
                return self.set_duplicates_policy("rename")
            case 'duplicados_sobrescribir':
                return self.set_duplicates_policy("overwrite")
            case 'config' | 'configuracion':
                return self.show_full_config()
            case 'workers':
                return self.show_worker_config()
            case 'retries':
                return self.show_retries_config()
            case 'reset_config':
                return self.reset_config_to_defaults()
            case 'limpiar_pendientes':
                return await self.clean_invalid_pending()
            case 'auto_extract_on':
                return self.set_auto_extract(True)
            case 'auto_extract_off':
                return self.set_auto_extract(False)
            case 'limpiar_partes_on':
                return self.set_clean_parts_after_extract(True)
            case 'limpiar_partes_off':
                return self.set_clean_parts_after_extract(False)
            case 'renombrar_on':
                return self.set_auto_rename(True)
            case 'renombrar_off':
                return self.set_auto_rename(False)
            case 'mantenimiento' | 'reiniciar' | 'restart':
                text, buttons = BotMenu.get_maintenance_menu()
                await client_ref.send_message(event.chat_id, text, buttons=buttons)
                return None
            case _:
                download_uris = list(
                    re.finditer(
                        r"https://t.me/(c/)?(?P<message_channel>\w+)(/(?P<message_id>[0-9]+))?",
                        event.message.message,
                    )
                )
                if download_uris:
                    for download_uri in download_uris:
                        message_channel = download_uri['message_channel']
                        message_channel = (
                            message_channel
                            if message_channel.isdigit()
                            else (await client_ref.get_entity(message_channel)).id
                        )
                        message_id = download_uri['message_id']
                        if message_id is None:
                            channel_msgs = await client_ref.get_messages(
                                PeerChannel(int(message_channel)), None, reverse=True
                            )
                            for channel_msg in channel_msgs:
                                await self.queue_download(
                                    event,
                                    channel=channel_msg.chat.id,
                                    message_id=channel_msg.id,
                                    destination=os.path.join(
                                        '.', sanitize_path_component(channel_msg.chat.title)
                                    ),
                                    client=client_ref,
                                    queue=queue_ref
                                )
                        else:
                            await self.queue_download(
                                event, channel=message_channel, message_id=message_id,
                                client=client_ref, queue=queue_ref
                            )
                else:
                    return None

    async def handle_callback_query(self, event, queue, client):
        """Procesa todos los clics de botones inline del bot."""
        data = event.data.decode('utf-8')

        is_account_menu = (data.startswith('menu_config_account') or data.startswith('cmd_account_'))
        if is_account_menu:
            if self.owner_id is None:
                text, buttons = BotMenu.get_account_menu(self.account_mode, owner_configured=False)
                await event.edit(text, buttons=buttons)
                await event.answer("⚠️ Configura OWNER_ID primero")
                return
            elif event.sender_id != self.owner_id:
                await event.answer("❌ No tienes autorización para modificar la cuenta.", alert=True)
                return

        # Panel nuevo (vistas con datos en vivo); si no lo reconoce, sigue el clásico
        if await self.handle_panel_callback(event, data, queue, client):
            return

        # Menús de navegación
        if data.startswith('menu_'):
            if data == 'menu_main':
                text, buttons = BotMenu.get_main_menu()
                await event.edit(text, buttons=buttons)
            elif data == 'menu_files':
                text, buttons = BotMenu.get_files_menu()
                await event.edit(text, buttons=buttons)
            elif data == 'menu_status':
                text, buttons = BotMenu.get_status_menu()
                await event.edit(text, buttons=buttons)
            elif data == 'menu_downloads':
                text, buttons = BotMenu.get_downloads_menu()
                await event.edit(text, buttons=buttons)
            elif data == 'menu_config':
                config = {
                    'duplicates': self.duplicates,
                    'workers': self.worker_count,
                    'auto_extract': self.auto_extract,
                    'individual_progress_messages': self.individual_progress_messages,
                    'account_mode': getattr(self, 'account_mode', 'bot'),
                    'auto_rename': getattr(self, 'auto_rename', False),
                    'daily_summary': getattr(self, 'daily_summary_enabled', False),
                }
                text, buttons = BotMenu.get_config_menu(config)
                await event.edit(text, buttons=buttons)
            elif data == 'menu_config_account':
                session_path = os.path.join(
                    TELEGRAM_DAEMON_SESSION_PATH if TELEGRAM_DAEMON_SESSION_PATH else "session",
                    "DownloadDaemon_user.session"
                )
                user_session_exists = os.path.isfile(session_path)

                text, buttons = BotMenu.get_account_menu(
                    current_mode=self.account_mode,
                    user_phone=self.user_phone,
                    has_user_session=user_session_exists,
                    owner_configured=True
                )
                await event.edit(text, buttons=buttons)
            elif data == 'menu_config_account_info':
                text, buttons = BotMenu.get_account_info_menu()
                await event.edit(text, buttons=buttons)
            elif data == 'menu_config_dupes':
                text, buttons = BotMenu.get_duplicates_menu(self.duplicates)
                await event.edit(text, buttons=buttons)
            elif data == 'menu_config_extract':
                text, buttons = BotMenu.get_extract_menu(self.auto_extract)
                await event.edit(text, buttons=buttons)
            elif data == 'menu_config_progress':
                text, buttons = BotMenu.get_progress_menu(self.individual_progress_messages)
                await event.edit(text, buttons=buttons)
            elif data == 'menu_config_workers':
                text, buttons = BotMenu.get_workers_menu()
                await event.edit(text, buttons=buttons)
            elif data == 'menu_config_frequency':
                text, buttons = BotMenu.get_frequency_menu(self.update_frequency)
                await event.edit(text, buttons=buttons)
            elif data == 'menu_manage_queue':
                active_list = []
                for worker_item in self.active_downloads.values():
                    link = worker_item['download_media'].get_link()
                    link_id = f"{worker_item['download_media'].channel}_{worker_item['download_media'].message_id}"
                    label = worker_item.get('filename') or link or "(sin nombre)"
                    active_list.append((label, link_id))

                queued_list = []
                temp_list = []
                while not queue.empty():
                    item = await queue.get()
                    temp_list.append(item)
                    link_id = f"{item['download_media'].channel}_{item['download_media'].message_id}"
                    link = item['download_media'].get_link()
                    label = item.get('filename') or link or "(sin nombre)"
                    queued_list.append((label, link_id))
                for item in temp_list:
                    await queue.put(item)

                paused_list = [(it.get('filename') or it['download_media'].get_link(),
                                f"{it['download_media'].channel}_{it['download_media'].message_id}")
                               for it in self.paused_items.values()]
                if not active_list and not queued_list and not paused_list:
                    await event.edit("📦 No hay descargas activas, en cola ni pausadas.", buttons=[Button.inline("🔙 Volver", data="menu_downloads")])
                else:
                    text, buttons = BotMenu.get_manage_queue_menu(active_list, queued_list, self.paused_links, paused_list)
                    await event.edit(text, buttons=buttons)
            elif data == 'menu_maintenance':
                text, buttons = BotMenu.get_maintenance_menu()
                await event.edit(text, buttons=buttons)
            elif data == 'menu_close':
                await event.delete()
                if event.message_id == self.menu_message_id:
                    self.menu_message_id = None
                    self._save_ui_state()
            return

        # Borrado de mensajes de extracción
        if data.startswith('delete_extraction_'):
            link_parts = data.replace('delete_extraction_', '').split('_')
            if len(link_parts) >= 2:
                channel = link_parts[0]
                message_id = link_parts[1]
                origin_link = f"https://t.me/c/{channel}/{message_id}"

                extraction_msg_id = event.message_id
                chat_id = self.raw_channel_id

                original_message_id = None
                try:
                    parts = origin_link.split('/')
                    if len(parts) >= 5 and parts[-1].isdigit():
                        original_message_id = int(parts[-1])
                except Exception as e:
                    logger.debug("Error parseando link %s: %s", origin_link, e)

                messages_to_delete = []
                if extraction_msg_id:
                    messages_to_delete.append(extraction_msg_id)
                if original_message_id:
                    messages_to_delete.append(original_message_id)

                deleted_count = 0
                if messages_to_delete:
                    try:
                        await client.delete_messages(chat_id, messages_to_delete, revoke=True)
                        deleted_count = len(messages_to_delete)
                    except Exception as e:
                        logger.warning("Error eliminando mensajes en lote: %s", e)
                        for msg_id in messages_to_delete:
                            try:
                                await client.delete_messages(chat_id, [msg_id], revoke=True)
                                deleted_count += 1
                            except Exception:
                                pass

                if deleted_count > 0:
                    await event.answer(f"✅ {deleted_count} mensaje(s) borrado(s)")
                    try:
                        await asyncio.sleep(0.3)
                        await event.delete()
                    except Exception:
                        pass
                else:
                    await event.answer("❌ No se pudieron borrar los mensajes")
                return

        # Confirmaciones de comandos destructivos
        if BotMenu.is_confirmation_command(data):
            result = None
            if data == 'clean_confirm':
                result = self.clean_downloads_folder()
            elif data == 'limpiar_cache_confirm':
                result = await self.clean_partial_cache()
            elif data == 'limpiar_pendientes_confirm':
                result = await self.clean_invalid_pending()
            elif data == 'cancel_all_confirm':
                result = await self.cancel_all_downloads(queue, client)
            elif data == 'delete_completed_confirm':
                result = await self.delete_completed_messages(self.client)
            elif data == 'reset_config_confirm':
                result = self.reset_config_to_defaults()
            elif data == 'restart_bot_confirm':
                await event.edit("🔄 **Reiniciando bot...**\n\n⏳ Guardando estado y cerrando conexiones...\nEl bot volverá en unos segundos.")
                try:
                    queue_ref = queue or getattr(self, 'current_queue', None)
                    if queue_ref:
                        await self.save_all_pending_downloads_async(queue_ref)
                    elif hasattr(self, 'save_progress_async'):
                        await self.save_progress_async()
                except Exception as e:
                    logger.warning("Error guardando estado antes de reiniciar: %s", e)
                logger.info("🔄 Reinicio solicitado desde menú de Telegram. Saliendo...")
                await asyncio.sleep(1)
                os._exit(0)

            if result:
                if isinstance(result, str):
                    if len(result) > 1000:
                        await self.log_reply(event, result)
                        await event.answer("✅ Acción completada")
                    else:
                        await event.edit(f"✅ **Acción completada**\n\n{result}",
                                         buttons=[Button.inline("🔙 Volver al Menú", data="menu_main")])
                else:
                    await event.answer("✅ Acción completada")
            else:
                await event.answer("✅ Acción completada")
            return

        # Ejecución de comandos
        if data.startswith('cmd_'):
            command = data.replace('cmd_', '')

            # Delegar comandos de cuenta al AccountManagerMixin
            if command.startswith('account_'):
                handled = await self.handle_account_command(command, event, client)
                if handled:
                    return

            destructive_commands = BotMenu.get_destructive_commands()
            if command in destructive_commands:
                confirm_text, confirm_buttons = BotMenu.get_confirmation_menu(command)
                if confirm_text and confirm_buttons:
                    await event.edit(confirm_text, buttons=confirm_buttons)
                    await event.answer("Confirma la acción")
                return

            result = None
            if command == 'list':
                result = await self.list_download_folder()
            elif command == 'status':
                text, buttons = self.downloads_view()
                self.panel_view = 'downloads'
                await event.edit(text, buttons=buttons)
                return
            elif command == 'cola':
                result = self.show_queue_status(queue=queue)
            elif command == 'reanudar':
                result = await self.force_resume_downloads(queue)
            elif command == 'show_completed':
                result = self.show_completed_downloads()
            elif command == 'duplicados_ignorar':
                result = self.set_duplicates_policy("ignore")
            elif command == 'duplicados_renombrar':
                result = self.set_duplicates_policy("rename")
            elif command == 'duplicados_sobrescribir':
                result = self.set_duplicates_policy("overwrite")
            elif command == 'auto_extract_on':
                result = self.set_auto_extract(True)
            elif command == 'auto_extract_off':
                result = self.set_auto_extract(False)
            elif command in ('renombrar_on', 'renombrar_off', 'summary_on', 'summary_off'):
                if command.startswith('renombrar'):
                    msg = self.set_auto_rename(command.endswith('_on'))
                else:
                    msg = self.set_daily_summary(command.endswith('_on'))
                config = {
                    'duplicates': self.duplicates, 'workers': self.worker_count,
                    'auto_extract': self.auto_extract,
                    'individual_progress_messages': self.individual_progress_messages,
                    'account_mode': getattr(self, 'account_mode', 'bot'),
                    'auto_rename': self.auto_rename, 'daily_summary': self.daily_summary_enabled,
                }
                text, buttons = BotMenu.get_config_menu(config)
                await event.edit(text, buttons=buttons)
                await event.answer(msg[:190])
                return
            elif command == 'progress_on':
                result = self.set_individual_progress(True)
            elif command == 'progress_off':
                result = self.set_individual_progress(False)
            elif command.startswith('workers_'):
                try:
                    new_count = int(command.split('_')[1])
                    result = self.set_worker_count(new_count)
                except Exception:
                    result = "❌ Error al cambiar el número de workers"
            elif command.startswith('frequency_'):
                try:
                    new_freq = int(command.split('_')[1])
                    old_freq = self.update_frequency
                    self.update_frequency = new_freq
                    self.save_config()
                    result = f"✅ Frecuencia de actualización cambiada:\n• Anterior: {old_freq}s\n• Nueva: {new_freq}s\n• Configuración guardada"
                except Exception:
                    result = "❌ Error al cambiar la frecuencia"
            elif command.startswith(('pause_', 'resume_', 'cancel_', 'top_')):
                action, link_id = command.split('_', 1)
                result = None
                target_link = ""
                found_in_active = False

                for worker_item in self.active_downloads.values():
                    dm = worker_item['download_media']
                    if f"{dm.channel}_{dm.message_id}" == link_id:
                        target_link = dm.get_link()
                        found_in_active = True
                        break

                if not target_link:
                    for plink, pitem in self.paused_items.items():
                        pdm = pitem['download_media']
                        if f"{pdm.channel}_{pdm.message_id}" == link_id:
                            target_link = plink
                            break

                if not target_link:
                    temp_list = []
                    found_item = None
                    while not queue.empty():
                        item = await queue.get()
                        dm = item['download_media']
                        if f"{dm.channel}_{dm.message_id}" == link_id:
                            found_item = item
                            target_link = dm.get_link()
                        else:
                            temp_list.append(item)

                    if action == 'top' and found_item:
                        await queue.put(found_item)
                        for item in temp_list:
                            await queue.put(item)
                        result = f"✅ Priorizado: {found_item.get('filename') or target_link}"
                    elif action == 'cancel' and found_item:
                        for item in temp_list:
                            await queue.put(item)
                        if target_link in self.active_and_queued_links:
                            self.active_and_queued_links.remove(target_link)
                        result = f"✅ Cancelado de cola: {found_item.get('filename') or target_link}"
                    else:
                        for item in temp_list:
                            await queue.put(item)

                if action == 'top' and found_in_active and not result:
                    result = "⚠️ Solo se puede priorizar un item que esté en cola, no uno activo."

                if action == 'pause':
                    self.paused_links.add(target_link)
                    result = f"⏸️ Pausando: {target_link}. Se detendrá en el próximo bloque."
                elif action == 'resume':
                    await self.resume_link(target_link, queue)
                    result = f"▶️ Reanudado: {target_link}"
                elif action == 'cancel' and not result and target_link in self.paused_items:
                    self.paused_items.pop(target_link, None)
                    self.paused_links.discard(target_link)
                    self.active_and_queued_links.discard(target_link)
                    result = f"❌ Quitada (estaba pausada): {target_link}"
                elif action == 'cancel' and not result:
                    self.cancelled_links.add(target_link)
                    result = f"❌ Cancelando: {target_link}. Se eliminará en breve."

                await asyncio.sleep(0.5)
                active_list = []
                for worker_item in self.active_downloads.values():
                    dm = worker_item['download_media']
                    active_list.append((worker_item.get('filename') or dm.get_link(), f"{dm.channel}_{dm.message_id}"))
                queued_list = []
                temp_list = []
                while not queue.empty():
                    item = await queue.get()
                    temp_list.append(item)
                    dm = item['download_media']
                    queued_list.append((item.get('filename') or dm.get_link(), f"{dm.channel}_{dm.message_id}"))
                for item in temp_list:
                    await queue.put(item)

                paused_list = [(it.get('filename') or it['download_media'].get_link(),
                                f"{it['download_media'].channel}_{it['download_media'].message_id}")
                               for it in self.paused_items.values()]
                text, buttons = BotMenu.get_manage_queue_menu(active_list, queued_list, self.paused_links, paused_list)
                await event.edit(f"✅ **Acción ejecutada:** {result}\n\n{text}", buttons=buttons)
                return

            if result:
                if isinstance(result, str):
                    if len(result) > 1000:
                        await self.log_reply(event, result)
                        await event.answer("Resultado enviado")
                    else:
                        try:
                            await event.edit(f"✅ **Resultado:**\n\n{result}", buttons=[Button.inline("🔙 Volver al Menú", data="menu_main")])
                        except Exception as edit_error:
                            if "not modified" not in str(edit_error).lower():
                                logger.debug("Error editando mensaje: %s", edit_error)
                else:
                    try:
                        await event.answer("Comando ejecutado")
                    except Exception:
                        pass
            else:
                try:
                    await event.answer("Comando completado")
                except Exception:
                    pass
