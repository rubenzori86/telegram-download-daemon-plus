from telethon import Button
import asyncio
import json
import logging
import os

logger = logging.getLogger(__name__)

# Marcas de botones que identifican un mensaje como menú del bot (para la limpieza)
MENU_CALLBACK_PREFIXES = (b"menu_", b"cmd_", b"pend_")
STATUS_PREFIXES = ("⚡ **Descargas activas**", "⚡ Descargas activas")


class MenuManagerMixin:
    """Panel único: un solo mensaje de menú vivo en el canal. Se recuerda entre reinicios,
    los botones lo editan, los comandos de texto se muestran dentro de él y se actualiza
    solo mientras hay descargas. Nunca deja menús viejos acumulados."""

    def __init_menu_manager__(self):
        self.menu_message_id = None
        self.menu_chat_id = None
        self.active_status_message_id = None
        self.active_status_chat_id = None
        self.last_status_content = None
        self.panel_view = 'main'
        self._panel_last_text = None
        self._panel_lock = asyncio.Lock()

    # ---------- persistencia ----------
    @property
    def _ui_state_file(self):
        return os.path.join(os.path.dirname(self.config_file), "ui_state.json")

    def _load_ui_state(self):
        try:
            with open(self._ui_state_file, 'r', encoding='utf-8') as f:
                st = json.load(f)
            self.menu_message_id = st.get('menu_message_id')
            self.menu_chat_id = st.get('menu_chat_id')
        except (OSError, ValueError):
            pass

    def _save_ui_state(self):
        try:
            with open(self._ui_state_file, 'w', encoding='utf-8') as f:
                json.dump({'menu_message_id': self.menu_message_id, 'menu_chat_id': self.menu_chat_id}, f)
        except OSError as e:
            logger.debug("No se pudo guardar el estado del panel: %s", e)

    # ---------- contenido según la vista actual ----------
    def _current_view_content(self):
        if self.panel_view == 'downloads':
            return self.downloads_view()
        return self.main_menu_content()

    # ---------- publicar / mover el panel ----------
    async def _delete_panel(self):
        if self.menu_message_id and self.menu_chat_id:
            try:
                await self.client.delete_messages(self.menu_chat_id, [self.menu_message_id], revoke=True)
            except Exception as e:
                logger.debug("No se pudo borrar el panel anterior: %s", e)
        self.menu_message_id = None

    async def post_panel(self, text, buttons, view='main', chat_id=None):
        """Borra el panel anterior y publica el nuevo al final del chat."""
        chat_id = chat_id or self.raw_channel_id
        async with self._panel_lock:
            await self._delete_panel()
            msg = await self.client.send_message(chat_id, text, buttons=buttons)
            self.menu_message_id = msg.id
            self.menu_chat_id = chat_id
            self.panel_view = view
            self._panel_last_text = text
            self._save_ui_state()
            return msg

    async def show_in_panel(self, event, text, buttons=None, view='other'):
        """Muestra el resultado de un comando de texto dentro del panel y borra el comando."""
        try:
            await event.message.delete()
        except Exception:
            pass
        if buttons is None:
            buttons = [[Button.inline("🎛️ Menú", data="menu_main")]]
        if len(text) > 4000:
            text = text[:3990] + "\n…"
        await self.post_panel(text, buttons, view=view, chat_id=event.chat_id)

    async def refresh_sticky_menu(self, chat_id=None):
        """Lleva el panel al final del chat (p.ej. tras reenviar un archivo)."""
        try:
            if self.active_status_message_id:
                try:
                    await self.client.delete_messages(self.active_status_chat_id, [self.active_status_message_id], revoke=True)
                except Exception:
                    pass
                self.active_status_message_id = None
            if self.panel_view not in ('main', 'downloads'):
                self.panel_view = 'main'
            text, buttons = self._current_view_content()
            await self.post_panel(text, buttons, view=self.panel_view, chat_id=chat_id or self.raw_channel_id)
        except Exception as e:
            logger.warning("Error moviendo el panel: %s", e)

    async def show_main_menu(self, event):
        text, buttons = self.main_menu_content()
        if hasattr(event, 'message') and getattr(event, 'message', None) is not None and not hasattr(event, 'data'):
            await self.show_in_panel(event, text, buttons, view='main')
        else:
            await self.post_panel(text, buttons, view='main', chat_id=event.chat_id)
        return None

    async def adopt_panel_message(self, event):
        """Si se pulsa un menú viejo, ese pasa a ser el panel y el otro se borra."""
        if event.message_id != self.menu_message_id:
            old_id, old_chat = self.menu_message_id, self.menu_chat_id
            self.menu_message_id = event.message_id
            self.menu_chat_id = event.chat_id
            self._save_ui_state()
            if old_id and old_id != event.message_id:
                try:
                    await self.client.delete_messages(old_chat, [old_id], revoke=True)
                except Exception:
                    pass

    # ---------- autorefresco ----------
    async def panel_autorefresh_loop(self):
        while True:
            try:
                await asyncio.sleep(max(5, self.update_frequency))
                if not self.menu_message_id or self.panel_view not in ('main', 'downloads'):
                    continue
                if not self.active_downloads and not self._panel_last_text_has_activity():
                    continue
                text, buttons = self._current_view_content()
                if text == self._panel_last_text:
                    continue
                async with self._panel_lock:
                    await self.client.edit_message(self.menu_chat_id, self.menu_message_id, text, buttons=buttons)
                    self._panel_last_text = text
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if "not modified" not in str(e).lower():
                    logger.debug("Autorefresco del panel: %s", e)
                    if "not found" in str(e).lower() or "invalid" in str(e).lower():
                        self.menu_message_id = None

    def _panel_last_text_has_activity(self):
        # Un refresco más tras terminar, para que el panel no se quede con la barra a medias
        t = self._panel_last_text or ""
        return "%" in t or "empezando" in t

    # ---------- limpiar todo el chat ----------
    def _messages_to_keep(self):
        """Panel, preguntas sin contestar y mensajes de progreso de descargas en marcha."""
        keep = {self.menu_message_id}
        for q in getattr(self, 'media_questions', {}).values():
            if q.get('msg_id'):
                keep.add(q['msg_id'])
        items = list(self.active_downloads.values()) + list(getattr(self, 'paused_items', {}).values())
        q = getattr(self, 'current_queue', None)
        if q is not None:
            items += list(q._queue)
        for it in items:
            msg = it.get('reply_msg')
            if getattr(msg, 'id', None):
                keep.add(msg.id)
        keep.discard(None)
        return keep

    async def clear_chat(self):
        """Borra todos los mensajes del canal menos el panel (y lo que sigue en uso).
        Necesita la cuenta de usuario: los bots no pueden leer el historial."""
        reader = getattr(self, 'user_client', None)
        if reader is None:
            return None
        keep = self._messages_to_keep()
        ids = [m.id async for m in reader.iter_messages(self.raw_channel_id, limit=None) if m.id not in keep]
        deleted = 0
        for i in range(0, len(ids), 100):
            try:
                await reader.delete_messages(self.raw_channel_id, ids[i:i + 100], revoke=True)
                deleted += len(ids[i:i + 100])
            except Exception as e:
                logger.warning("Limpiar chat: no se pudo borrar un bloque: %s", e)
        logger.info("🧽 Chat limpiado: %s mensajes borrados, %s conservados", deleted, len(keep))
        return deleted, len(keep) - 1

    # ---------- limpieza de menús residuales ----------
    async def sweep_old_menus(self, limit=1500):
        """Borra menús y mensajes de estado antiguos del bot (no los avisos de descarga).
        Necesita la cuenta de usuario: los bots no pueden leer el historial."""
        reader = getattr(self, 'user_client', None)
        if reader is None:
            return 0
        to_delete = []
        try:
            async for msg in reader.iter_messages(self.raw_channel_id, limit=limit):
                if msg.id == self.menu_message_id:
                    continue
                text = msg.message or ""
                is_menu = False
                rm = getattr(msg, 'reply_markup', None)
                if rm is not None and getattr(rm, 'rows', None):
                    # capa antigua: boton.data · capa nueva (Telethon 1.45): boton.type.data
                    datas = [getattr(b, 'data', None) or getattr(getattr(b, 'type', None), 'data', None) or b""
                             for row in rm.rows for b in row.buttons]
                    is_menu = bool(datas) and all(d.startswith(MENU_CALLBACK_PREFIXES) for d in datas)
                if text.startswith(STATUS_PREFIXES):
                    is_menu = True
                if is_menu:
                    to_delete.append(msg.id)
            for i in range(0, len(to_delete), 100):
                await self.client.delete_messages(self.raw_channel_id, to_delete[i:i + 100], revoke=True)
            if to_delete:
                logger.info("🧹 Menús residuales borrados: %s", len(to_delete))
        except Exception as e:
            logger.warning("Limpieza de menús residuales: %s", e)
        return len(to_delete)


class BotMenu:
    @staticmethod
    def get_main_menu():
        """Genera el menú principal con botones"""
        text = "📂 **Panel de Control - Telegram Download Daemon**\n\nSelecciona una opción para gestionar el bot:"
        buttons = [
            [
                Button.inline("📁 Archivos", data="menu_files"),
                Button.inline("📊 Estado", data="menu_status")
            ],
            [
                Button.inline("🚀 Descargas", data="menu_downloads"),
                Button.inline("⚙️ Configuración", data="menu_config")
            ],
            [
                Button.inline("🔧 Mantenimiento", data="menu_maintenance")
            ],
            [
                Button.inline("🔄 Cerrar Menú", data="menu_close")
            ]
        ]
        return text, buttons

    @staticmethod
    def get_files_menu():
        """Menú de gestión de archivos"""
        text = "📁 **Gestión de Archivos**\n\nAdministra los archivos descargados y temporales:"
        buttons = [
            [
                Button.inline("📜 Listar Archivos", data="cmd_list"),
                Button.inline("🧹 Limpiar Temporales", data="cmd_clean")
            ],
            [
                Button.inline("🔙 Volver", data="menu_main")
            ]
        ]
        return text, buttons

    @staticmethod
    def get_status_menu():
        """Menú de estado del sistema"""
        text = "📊 **Estado del Sistema**\n\nConsulta el progreso y la cola de descargas:"
        buttons = [
            [
                Button.inline("📈 Estado Actual", data="cmd_status"),
                Button.inline("⏯️ Gestionar Cola", data="menu_manage_queue")
            ],
            [
                Button.inline("🔙 Volver", data="menu_main")
            ]
        ]
        return text, buttons

    @staticmethod
    def get_downloads_menu():
        """Menú de gestión de descargas"""
        text = "🚀 **Gestión de Descargas**\n\nMantén tus descargas al día:"
        buttons = [
            [
                Button.inline("⏯️ Reanudar Pendientes", data="cmd_reanudar")
            ],
            [
                Button.inline("🧹 Limpiar Cache", data="cmd_limpiar_cache"),
                Button.inline("🗑️ Limpiar Inválidos", data="cmd_limpiar_pendientes")
            ],
            [
                Button.inline("✅ Ver Completadas", data="cmd_show_completed"),
                Button.inline("🗑️ Borrar Completadas", data="cmd_delete_completed")
            ],
            [
                Button.inline("🛑 Cancelar Todas", data="cmd_cancel_all")
            ],
            [
                Button.inline("🔙 Volver", data="menu_main")
            ]
        ]
        return text, buttons

    @staticmethod
    def get_config_menu(current_config):
        """Menú de configuración dinámica"""
        duplicates_map = {'ignore': 'IGNORAR', 'rename': 'RENOMBRAR', 'overwrite': 'SOBRESCRIBIR'}
        dup_display = duplicates_map.get(current_config['duplicates'], current_config['duplicates'].upper())
        acc_mode = "Usuario" if current_config.get('account_mode', 'bot') == 'user' else "Bot"
        
        text = f"⚙️ **Configuración del Sistema**\n\n"
        text += f"• Duplicados: `{dup_display}`\n"
        text += f"• Workers: `{current_config['workers']}`\n"
        text += f"• Frecuencia actualización: `{current_config.get('update_frequency', 10)}s`\n"
        text += f"• Auto-Extract: `{'✅' if current_config['auto_extract'] else '❌'}`\n"
        text += f"• Progreso Individual: `{'✅' if current_config.get('individual_progress_messages', False) else '❌'}`\n"
        text += f"• Modo Cuenta: `{acc_mode}`\n"
        text += f"• Renombrar y colocar: `{'✅' if current_config.get('auto_rename') else '❌'}`\n"
        text += f"• Resumen diario (23:00): `{'✅' if current_config.get('daily_summary') else '❌'}`\n\n"
        text += "Selecciona un parámetro para modificar:"
        
        buttons = [
            [
                Button.inline("🔄 Política Duplicados", data="menu_config_dupes"),
                Button.inline("👷 Workers", data="menu_config_workers")
            ],
            [
                Button.inline("📦 Auto-Extracción", data="menu_config_extract"),
                Button.inline("⏱️ Frecuencia", data="menu_config_frequency")
            ],
            [
                Button.inline("📊 Progreso Individual", data="menu_config_progress")
            ],
            [
                Button.inline("🏷️ Renombrar: " + ("✅" if current_config.get('auto_rename') else "❌"),
                              data="cmd_renombrar_" + ("off" if current_config.get('auto_rename') else "on")),
                Button.inline("🌙 Resumen: " + ("✅" if current_config.get('daily_summary') else "❌"),
                              data="cmd_summary_" + ("off" if current_config.get('daily_summary') else "on"))
            ],
            [
                Button.inline("⚙️ Modo de Cuenta", data="menu_config_account")
            ],
            [
                Button.inline("♻️ Resetear Config", data="cmd_reset_config")
            ],
            [
                Button.inline("🔙 Volver", data="menu_main")
            ]
        ]
        return text, buttons

    @staticmethod
    def get_account_menu(current_mode, user_phone=None, has_user_session=False, owner_configured=True):
        """Menú de configuración del modo de cuenta (Bot / Híbrido con Usuario)"""
        text = "⚙️ **Modo de Cuenta**\n\n"
        if not owner_configured:
            text += "⚠️ **Configuración Requerida**\n"
            text += "Configura la variable de entorno `TELEGRAM_DAEMON_OWNER_ID` para usar esta función."
            buttons = [[Button.inline("🔙 Volver", data="menu_config")]]
            return text, buttons
            
        if current_mode == 'user':
            phone_str = user_phone if user_phone else "Desconocido"
            text += f"👤 **Modo actual: HÍBRIDO (UI Bot + Descargas Usuario: {phone_str})**\n\n"
            text += "La interfaz, los menús y los comandos son gestionados por el Bot. Las descargas pesadas se delegan al Userbot para descargar a máxima velocidad.\n"
        else:
            text += "🤖 **Modo actual: Bot completo**\n\n"
            text += "Tanto la interfaz como las descargas se realizan a través del Bot. Las descargas están limitadas a ~1-1.2 MB/s.\n"
            if has_user_session:
                text += "\n💡 *Ya existe una sesión de usuario guardada y lista para usarse.*"
            else:
                text += (
                    "\n👤 **No hay ninguna cuenta de usuario configurada todavía.**\n\n"
                    "Pulsa el botón **'👤 Configurar Cuenta de Usuario'** para iniciar el asistente de login por chat privado "
                    "con el bot de forma segura y directa, sin tocar la terminal.\n\n"
                    "Te pedirá el teléfono, el código de verificación y, si tu cuenta tiene verificación en dos pasos, la contraseña."
                )
                
        buttons = []
        if current_mode == 'user':
            buttons.append([Button.inline("🤖 Volver a modo Bot", data="cmd_account_set_bot")])
        else:
            if has_user_session:
                buttons.append([Button.inline("👤 Activar Modo Híbrido (Userbot)", data="cmd_account_set_user")])
            else:
                buttons.append([Button.inline("👤 Configurar Cuenta de Usuario", data="cmd_account_set_user")])
                
        if has_user_session or current_mode == 'user':
            buttons.append([Button.inline("🗑️ Borrar sesión usuario", data="cmd_account_delete_user_session")])
        buttons.append([Button.inline("ℹ️ ¿Qué diferencia hay?", data="menu_config_account_info")])
        buttons.append([Button.inline("🔙 Volver", data="menu_config")])
        return text, buttons

    @staticmethod
    def get_account_info_menu():
        """Texto explicativo de la diferencia entre modo Bot y Modo Híbrido"""
        text = (
            "ℹ️ **Modo Bot vs. Modo Híbrido**\n\n"
            "• **Modo Bot Completo:**\n"
            "  - Fácil de iniciar (solo requiere token).\n"
            "  - Telegram impone límites de velocidad a las cuentas de bot (~1-1.2 MB/s por descarga).\n\n"
            "• **Modo Híbrido (Bot + Userbot):**\n"
            "  - Combina lo mejor de ambos mundos: la UI interactiva (menús, botones) la maneja el Bot de forma estable.\n"
            "  - La descarga de archivos pesados y media la realiza tu cuenta de usuario (Userbot) en segundo plano.\n"
            "  - Se beneficia de la velocidad de descarga completa e ilimitada de tu cuenta personal.\n"
            "  - Se configura directamente chateando con el bot por privado o mediante `--setup-user-login`.\n"
        )
        buttons = [[Button.inline("🔙 Volver", data="menu_config_account")]]
        return text, buttons

    @staticmethod
    def get_duplicates_menu(current_state):
        """Submenú para política de duplicados"""
        duplicates_map = {'ignore': 'IGNORAR', 'rename': 'RENOMBRAR', 'overwrite': 'SOBRESCRIBIR'}
        state_display = duplicates_map.get(current_state, current_state.upper())
        
        text = f"🔄 **Política de Duplicados**\n\nEstado actual: `{state_display}`\n\nElige cómo manejar archivos que ya existen:"
        
        def label(val, text):
            return f"✅ {text}" if current_state == val else text

        buttons = [
            [Button.inline(label("ignore", "❌ Ignorar"), data="cmd_duplicados_ignorar")],
            [Button.inline(label("rename", "📝 Renombrar"), data="cmd_duplicados_renombrar")],
            [Button.inline(label("overwrite", "⚠️ Sobrescribir"), data="cmd_duplicados_sobrescribir")],
            [Button.inline("🔙 Volver", data="menu_config")]
        ]
        return text, buttons

    @staticmethod
    def get_extract_menu(current_state):
        """Submenú para auto-extracción"""
        text = f"📦 **Auto-Extracción**\n\nEstado actual: `{'Activado' if current_state else 'Desactivado'}`"
        buttons = [
            [
                Button.inline("✅ Activar", data="cmd_auto_extract_on") if not current_state else 
                Button.inline("❌ Desactivar", data="cmd_auto_extract_off")
            ],
            [Button.inline("🔙 Volver", data="menu_config")]
        ]
        return text, buttons

    @staticmethod
    def get_progress_menu(current_state):
        """Submenú para progreso individual"""
        text = f"📊 **Progreso Individual**\n\nEstado actual: `{'Activado' if current_state else 'Desactivado'}`\n\n"
        text += "Si está activado, se mostrarán mensajes individuales de progreso para cada descarga.\n"
        text += "Si está desactivado, solo verás el progreso en el menú de estado."
        buttons = [
            [
                Button.inline("✅ Activar", data="cmd_progress_on") if not current_state else 
                Button.inline("❌ Desactivar", data="cmd_progress_off")
            ],
            [Button.inline("🔙 Volver", data="menu_config")]
        ]
        return text, buttons
    @staticmethod
    def get_workers_menu():
        """Submenú para configurar el número de workers"""
        text = "👷 **Configurar Workers**\n\nElige el número de descargas simultáneas:"
        buttons = [
            [
                Button.inline("1", data="cmd_workers_1"),
                Button.inline("2", data="cmd_workers_2"),
                Button.inline("3", data="cmd_workers_3")
            ],
            [
                Button.inline("4", data="cmd_workers_4"),
                Button.inline("5", data="cmd_workers_5"),
                Button.inline("6", data="cmd_workers_6")
            ],
            [
                Button.inline("8", data="cmd_workers_8"),
                Button.inline("10", data="cmd_workers_10")
            ],
            [Button.inline("🔙 Volver", data="menu_config")]
        ]
        return text, buttons

    @staticmethod
    def get_frequency_menu(current_frequency):
        """Submenú para configurar la frecuencia de actualización del progreso"""
        text = (
            f"⏱️ **Frecuencia de Actualización**\n\n"
            f"Valor actual: `{current_frequency}s`\n\n"
            f"Controla cada cuántos segundos se actualiza el mensaje de progreso.\n"
            f"⚠️ Valores muy bajos pueden causar FloodWait de Telegram (mínimo recomendado: 5s)."
        )
        buttons = [
            [
                Button.inline("5s ⚡", data="cmd_frequency_5"),
                Button.inline("10s", data="cmd_frequency_10"),
                Button.inline("15s", data="cmd_frequency_15"),
            ],
            [
                Button.inline("20s", data="cmd_frequency_20"),
                Button.inline("30s", data="cmd_frequency_30"),
                Button.inline("60s 🐢", data="cmd_frequency_60"),
            ],
            [Button.inline("🔙 Volver", data="menu_config")]
        ]
        return text, buttons

    @staticmethod
    def get_manage_queue_menu(active_list, queued_list, paused_links, paused_list=()):
        """Menú para gestionar descargas activas y en cola"""
        text = "⏯️ **Gestión de Cola y Activas**\n\n"
        buttons = []

        if active_list:
            text += "⚡ **Descargas Activas:**\n"
            for label, link_id in active_list[:10]:
                link_full = f"msg_{link_id}" # Reconstruir identificador si es necesario
                is_paused = link_id in paused_links
                text += f"• {label} {'(⏸️ PAUSADO)' if is_paused else ''}\n"
                
                row = []
                if is_paused:
                    row.append(Button.inline(f"▶️ Reanudar", data=f"cmd_resume_{link_id}"))
                else:
                    row.append(Button.inline(f"⏸️ Pausar", data=f"cmd_pause_{link_id}"))
                
                row.append(Button.inline(f"❌ Cancelar", data=f"cmd_cancel_{link_id}"))
                buttons.append(row)
                text += "\n"
        else:
            text += "❌ No hay descargas activas.\n"

        if queued_list:
            text += "\n📋 **En Cola (Siguientes):**\n"
            for label, link_id in queued_list[:10]:
                is_paused = link_id in paused_links
                text += f"• {label} {'(⏸️ PAUSADO)' if is_paused else ''}\n"
                
                row = []
                if is_paused:
                    row.append(Button.inline(f"▶️ Reanudar", data=f"cmd_resume_{link_id}"))
                else:
                    row.append(Button.inline(f"⏸️ Pausar", data=f"cmd_pause_{link_id}"))
                
                row.append(Button.inline(f"🔝 Subir", data=f"cmd_top_{link_id}"))
                row.append(Button.inline(f"❌ Quitar", data=f"cmd_cancel_{link_id}"))
                buttons.append(row)
                text += "\n"
        
        if paused_list:
            text += "\n⏸️ **Pausadas:**\n"
            for label, link_id in paused_list[:10]:
                text += f"• {label}\n"
                buttons.append([Button.inline("▶️ Reanudar", data=f"cmd_resume_{link_id}"),
                                Button.inline("❌ Quitar", data=f"cmd_cancel_{link_id}")])

        buttons.append([Button.inline("🔙 Volver", data="menu_downloads")])
        return text, buttons

    @staticmethod
    def get_maintenance_menu():
        """Menú de mantenimiento del sistema (reinicio, rebuild)"""
        text = "🔧 **Mantenimiento del Sistema**\n\n"
        text += "Gestiona el contenedor Docker desde aquí:\n\n"
        text += "• **🔄 Reiniciar Bot**: Guarda el estado y reinicia el contenedor.\n"
        text += "  _Los cambios de código se aplican automáticamente._\n\n"
        text += "> ⚠️ Las descargas activas se pausarán y reanudarán tras el reinicio."
        buttons = [
            [
                Button.inline("🔄 Reiniciar Bot", data="cmd_restart_bot")
            ],
            [
                Button.inline("🔙 Volver", data="menu_main")
            ]
        ]
        return text, buttons

    @staticmethod
    def get_destructive_commands():
        """Retorna el diccionario de comandos destructivos que requieren confirmación"""
        return {
            'clean': ('🧹 Limpiar archivos temporales', 'clean_confirm'),
            'limpiar_cache': ('🧹 Limpiar cache parcial', 'limpiar_cache_confirm'),
            'limpiar_pendientes': ('🗑️ Limpiar pendientes inválidos', 'limpiar_pendientes_confirm'),
            'cancel_all': ('🛑 Cancelar todas las descargas', 'cancel_all_confirm'),
            'delete_completed': ('🗑️ Borrar mensajes completados', 'delete_completed_confirm'),
            'reset_config': ('♻️ Resetear configuración', 'reset_config_confirm'),
            'restart_bot': ('🔄 Reiniciar el bot (Docker)', 'restart_bot_confirm')
        }

    @staticmethod
    def get_confirmation_menu(command):
        """Genera el mensaje y botones de confirmación para un comando destructivo"""
        destructive_commands = BotMenu.get_destructive_commands()
        
        if command not in destructive_commands:
            return None, None
        
        action_name, confirm_cmd = destructive_commands[command]
        confirm_text = f"⚠️ **Confirmación requerida**\n\n"
        confirm_text += f"¿Estás seguro de que quieres **{action_name.lower()}**?\n\n"
        confirm_text += "Esta acción no se puede deshacer."
        
        # Determinar el menú de retorno según el comando
        return_menu = BotMenu.get_return_menu_for_command(command)
        
        confirm_buttons = [
            [
                Button.inline("✅ Sí, confirmar", data=confirm_cmd),
                Button.inline("❌ No, cancelar", data=return_menu)
            ]
        ]
        
        return confirm_text, confirm_buttons

    @staticmethod
    def is_confirmation_command(data):
        """Verifica si un comando es de confirmación"""
        return data.endswith('_confirm')

    @staticmethod
    def get_return_menu_for_command(command):
        """Obtiene el menú de retorno apropiado para un comando"""
        if command == 'reset_config':
            return "menu_config"
        elif command in ('clean', 'limpiar_cache', 'limpiar_pendientes', 'delete_completed'):
            return "menu_cleanup"
        elif command == 'restart_bot':
            return "menu_system"
        else:
            return "menu_downloads"
