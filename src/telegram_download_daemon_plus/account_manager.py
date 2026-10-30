# -*- coding: utf-8 -*-
import asyncio
import logging
import os
import re
from telethon import TelegramClient, Button
from telethon.errors import (
    PhoneNumberInvalidError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    SessionPasswordNeededError,
    PasswordHashInvalidError,
    FloodWaitError,
)
from .session_manager import get_session, save_session, TELEGRAM_DAEMON_SESSION_PATH

logger = logging.getLogger(__name__)


class AccountManagerMixin:
    """Mixin para la gestión de cuentas de Telegram (Bot vs Usuario), asistente de inicio de sesión y hot-swap."""

    async def start_user_login_wizard(self, user_id, chat_id=None):
        """Inicia el asistente de inicio de sesión de usuario en chat privado."""
        target_chat = chat_id or user_id

        # Si ya hay un wizard previo para este usuario, cancelarlo y limpiar recursos
        await self.cancel_login_wizard(user_id, notify=False)

        try:
            session = get_session("DownloadDaemon_user")
            client = TelegramClient(
                session,
                self.api_id,
                self.api_hash,
                proxy=self.proxy,
                connection_retries=5,
                retry_delay=1,
                request_retries=5,
            )
            await client.connect()
        except Exception as e:
            logger.error("Error al conectar cliente para wizard de login: %s", e, exc_info=True)
            await self.client.send_message(
                target_chat,
                f"❌ Error al iniciar el asistente de conexión: `{e}`\nInténtalo de nuevo más tarde."
            )
            return

        async def _wizard_timeout():
            try:
                await asyncio.sleep(300)  # 5 minutos
                if user_id in self.login_wizard_state:
                    logger.info("Timeout de 5 minutos alcanzado en wizard de login para user_id %s", user_id)
                    await self.cancel_login_wizard(
                        user_id,
                        notify=True,
                        message="⌛ El asistente de inicio de sesión ha expirado por inactividad (5 minutos). Si deseas continuar, inicia el proceso nuevamente desde el menú."
                    )
            except asyncio.CancelledError:
                pass

        timeout_task = asyncio.create_task(_wizard_timeout())

        self.login_wizard_state[user_id] = {
            'step': 'awaiting_phone',
            'client': client,
            'phone': None,
            'phone_code_hash': None,
            'chat_id': target_chat,
            'timeout_task': timeout_task,
        }

        msg = (
            "🔐 **Asistente de Inicio de Sesión de Cuenta de Usuario**\n\n"
            "Este asistente te permitirá vincular tu cuenta de Telegram para realizar descargas a máxima velocidad en Modo Híbrido.\n\n"
            "📱 **Paso 1/3:** Introduce tu número de teléfono con el prefijo internacional (por ejemplo: `+34600112233` o `+5215512345678`).\n\n"
            "_Puedes escribir `cancelar` en cualquier momento para abortar el proceso._"
        )
        await self.client.send_message(target_chat, msg)

    async def cancel_login_wizard(self, user_id, notify=True, message=None):
        """Cancela el wizard activo y desconecta el cliente temporal."""
        state = self.login_wizard_state.pop(user_id, None)
        if state:
            timeout_task = state.get('timeout_task')
            if timeout_task and not timeout_task.done():
                timeout_task.cancel()

            temp_client = state.get('client')
            if temp_client and temp_client.is_connected():
                try:
                    await temp_client.disconnect()
                except Exception as e:
                    logger.debug("Error desconectando cliente temporal del wizard: %s", e)

            if notify and self.client:
                chat_id = state.get('chat_id', user_id)
                cancel_msg = message or "🛑 Asistente de inicio de sesión cancelado."
                try:
                    await self.client.send_message(chat_id, cancel_msg)
                except Exception as e:
                    logger.debug("No se pudo enviar mensaje de cancelación del wizard: %s", e)

    async def handle_login_wizard_message(self, event):
        """Procesa los mensajes de texto del wizard de login en chat privado."""
        user_id = event.sender_id
        state = self.login_wizard_state.get(user_id)
        if not state:
            return False

        text = (event.message.message or "").strip()

        # Reiniciar timeout de 5 minutos al recibir respuesta
        if state.get('timeout_task') and not state['timeout_task'].done():
            state['timeout_task'].cancel()

        async def _wizard_timeout():
            try:
                await asyncio.sleep(300)
                if user_id in self.login_wizard_state:
                    await self.cancel_login_wizard(
                        user_id,
                        notify=True,
                        message="⌛ El asistente de inicio de sesión ha expirado por inactividad."
                    )
            except asyncio.CancelledError:
                pass

        state['timeout_task'] = asyncio.create_task(_wizard_timeout())

        # Cancelación explícita
        if text.lower() in ['cancelar', '/cancel', '/cancelar', 'abortar']:
            await self.cancel_login_wizard(user_id, notify=True, message="🛑 Has cancelado el inicio de sesión.")
            return True

        step = state.get('step')
        client = state.get('client')

        if step == 'awaiting_phone':
            phone = text.replace(" ", "").replace("-", "")
            if not phone.startswith("+") and phone.isdigit():
                phone = "+" + phone

            if not re.match(r"^\+\d{7,15}$", phone):
                await event.reply(
                    "⚠️ Formato de teléfono no válido. Debe incluir el prefijo internacional (ejemplo: `+34600112233`).\n"
                    "Por favor, inténtalo de nuevo o escribe `cancelar`."
                )
                return True

            status_msg = await event.reply("⏳ Enviando código de verificación a Telegram/SMS...")
            try:
                code_req = await client.send_code_request(phone)
                state['phone'] = phone
                state['phone_code_hash'] = code_req.phone_code_hash
                state['step'] = 'awaiting_code'
                await status_msg.edit(
                    f"📩 **Paso 2/3:** Se ha enviado un código de verificación para `{phone}`.\n\n"
                    "Introduce el código que has recibido por Telegram o SMS.\n\n"
                    "⚠️ **IMPORTANTE:** Telegram bloquea los códigos que se envían directamente por chat como medida de seguridad antitráfico. "
                    "**Escribe el código separando cada número con un espacio o un guion**, por ejemplo:\n"
                    "👉 `8 6 5 6 4` o `8-6-5-6-4`\n\n"
                    "_Escribe `cancelar` para abortar._"
                )
            except PhoneNumberInvalidError:
                await status_msg.edit(
                    "❌ El número de teléfono introducido no es válido para Telegram.\n"
                    "Por favor, compruébalo e introduce el número de nuevo con su prefijo internacional:"
                )
            except FloodWaitError as e:
                await self.cancel_login_wizard(
                    user_id,
                    notify=True,
                    message=f"⏳ Telegram ha bloqueado temporalmente las solicitudes de código por seguridad (espera necesaria: {e.seconds}s). Inténtalo más tarde."
                )
            except Exception as e:
                logger.error("Error al enviar código de verificación: %s", e, exc_info=True)
                await status_msg.edit(
                    f"❌ Error al solicitar el código: `{e}`\n"
                    "Por favor, inténtalo de nuevo introduciendo tu número de teléfono o escribe `cancelar`."
                )
            return True

        elif step == 'awaiting_code':
            code = text.replace(" ", "").replace("-", "").strip()
            phone = state.get('phone')
            phone_code_hash = state.get('phone_code_hash')

            status_msg = await event.reply("⏳ Verificando código...")
            try:
                await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
                await self._complete_user_login(user_id, status_msg)
            except SessionPasswordNeededError:
                state['step'] = 'awaiting_password'
                await status_msg.edit(
                    "🔐 **Paso 3/3 (Verificación en dos pasos - 2FA):**\n\n"
                    "Tu cuenta de Telegram tiene una contraseña 2FA configurada.\n"
                    "Introduce tu contraseña de 2FA para completar el inicio de sesión:\n\n"
                    "_Escribe `cancelar` para abortar._"
                )
            except PhoneCodeInvalidError:
                await status_msg.edit(
                    "❌ El código introducido es incorrecto.\n"
                    "Por favor, revísalo e introduce el código nuevamente (o escribe `cancelar`):"
                )
            except PhoneCodeExpiredError:
                await self.cancel_login_wizard(
                    user_id,
                    notify=True,
                    message="⌛ El código de verificación ha expirado. Por favor, inicia el proceso nuevamente desde el menú."
                )
            except FloodWaitError as e:
                await self.cancel_login_wizard(
                    user_id,
                    notify=True,
                    message=f"⏳ Telegram ha bloqueado temporalmente los intentos por seguridad (espera necesaria: {e.seconds}s). Inténtalo más tarde."
                )
            except Exception as e:
                logger.error("Error al verificar código: %s", e, exc_info=True)
                await status_msg.edit(
                    f"❌ Error al verificar el código: `{e}`\n"
                    "Introduce el código de nuevo o escribe `cancelar`:"
                )
            return True

        elif step == 'awaiting_password':
            password = text
            status_msg = await event.reply("⏳ Verificando contraseña 2FA...")
            try:
                await client.sign_in(password=password)
                await self._complete_user_login(user_id, status_msg)
            except PasswordHashInvalidError:
                await status_msg.edit(
                    "❌ La contraseña 2FA introducida no es correcta.\n"
                    "Por favor, inténtalo de nuevo (o escribe `cancelar`):"
                )
            except FloodWaitError as e:
                await self.cancel_login_wizard(
                    user_id,
                    notify=True,
                    message=f"⏳ Telegram ha bloqueado temporalmente los intentos por seguridad (espera necesaria: {e.seconds}s). Inténtalo más tarde."
                )
            except Exception as e:
                logger.error("Error al verificar contraseña 2FA: %s", e, exc_info=True)
                await status_msg.edit(
                    f"❌ Error al verificar la contraseña: `{e}`\n"
                    "Introduce la contraseña de nuevo o escribe `cancelar`:"
                )
            return True

        return False

    async def _complete_user_login(self, user_id, status_msg):
        """Finaliza el login de usuario guardando la sesión y aplicando el hot-swap."""
        state = self.login_wizard_state.pop(user_id, None)
        if not state:
            return

        if state.get('timeout_task') and not state['timeout_task'].done():
            state['timeout_task'].cancel()

        client = state.get('client')
        try:
            me = await client.get_me()
            phone = me.phone if hasattr(me, 'phone') and me.phone else state.get('phone', 'Desconocido')
            if phone and not str(phone).startswith('+'):
                phone = f"+{phone}"
            user_name = f"{me.first_name or ''} {me.last_name or ''}".strip() or "Usuario"

            save_session(client.session, "DownloadDaemon_user")

            self.user_phone = phone
            self.account_mode = "user"
            self.save_config()

            self.user_client = client
            self.download_client = client
            logger.info("👤 Hot-swap completado con éxito: modo usuario activo con %s (%s)", user_name, phone)

            success_text = (
                "🎉 **¡Inicio de sesión completado con éxito!**\n\n"
                f"👤 **Cuenta vinculada:** {user_name} (`{phone}`)\n"
                f"⚡ **Modo Híbrido:** Activado en caliente (sin necesidad de reiniciar).\n\n"
                "A partir de ahora, las descargas de archivos pesados se descargarán a máxima velocidad con tu cuenta de usuario."
            )
            await status_msg.edit(success_text)

            try:
                await self.client.send_message(
                    self.normalized_channel_id,
                    f"⚡ **Modo Híbrido Activado** por el propietario.\n"
                    f"Descargas delegadas a la cuenta de usuario vinculada ({phone})."
                )
            except Exception as e:
                logger.debug("No se pudo enviar notificación de activación al canal: %s", e)

        except Exception as e:
            logger.error("Error al finalizar el login de usuario: %s", e, exc_info=True)
            await status_msg.edit(
                f"⚠️ El login se realizó pero ocurrió un error al configurar la sesión: `{e}`\n"
                "Se recomienda reiniciar el contenedor (`docker restart telegram-download-daemon-plus`)."
            )

    async def handle_account_command(self, command, event, client):
        """Maneja las acciones de cambio de modo de cuenta (bot/user/delete) desde botones."""
        if command == 'account_set_bot':
            self.account_mode = 'bot'
            self.download_client = self.client
            self.save_config()
            await event.edit(
                "✅ **Modo cambiado a Bot completo (en caliente).**\n\nLas descargas ahora se realizarán directamente a través del Bot.",
                buttons=[Button.inline("🔙 Volver al Menú", data="menu_main")]
            )
            return True

        elif command == 'account_set_user':
            if TELEGRAM_DAEMON_SESSION_PATH:
                session_path = os.path.join(TELEGRAM_DAEMON_SESSION_PATH, "DownloadDaemon_user.session")
            else:
                session_path = os.path.join("session", "DownloadDaemon_user.session")

            has_session = os.path.isfile(session_path)

            if has_session:
                if not self.user_client or not self.user_client.is_connected():
                    try:
                        user_client = TelegramClient(
                            get_session("DownloadDaemon_user"),
                            self.api_id,
                            self.api_hash,
                            proxy=self.proxy,
                            connection_retries=5,
                            retry_delay=1,
                            request_retries=5,
                        )
                        await user_client.connect()
                        if not await user_client.is_user_authorized():
                            raise Exception("La sesión guardada no está autorizada.")
                        self.user_client = user_client
                    except Exception as e:
                        logger.warning("Error conectando sesión existente: %s", e)
                        has_session = False

                if has_session and self.user_client:
                    self.account_mode = 'user'
                    self.download_client = self.user_client
                    self.save_config()
                    await event.edit(
                        "✅ **Modo Híbrido Activado (en caliente).**\n\nLas descargas ahora utilizarán tu cuenta de usuario a máxima velocidad.",
                        buttons=[Button.inline("🔙 Volver al Menú", data="menu_main")]
                    )
                    return True

            if self.owner_id is None:
                await event.answer("⚠️ Configura OWNER_ID primero para iniciar el login.", alert=True)
                return True

            bot_me = await client.get_me()
            bot_username = getattr(bot_me, 'username', None)

            buttons_dm = []
            if bot_username:
                buttons_dm.append([Button.url("💬 Abrir Chat Privado con el Bot", f"https://t.me/{bot_username}")])
            buttons_dm.append([Button.inline("🔙 Volver al Menú", data="menu_main")])

            await self.start_user_login_wizard(self.owner_id)

            dm_text = (
                "🔐 **Asistente de Login de Usuario iniciado**\n\n"
                "Por seguridad y privacidad, los datos de acceso (teléfono, código de verificación y contraseña 2FA) "
                "se solicitan únicamente por **chat privado** con el bot.\n\n"
                "✅ **Te he escrito por privado.** Abre el chat privado con el bot para completar la configuración."
            )
            await event.edit(dm_text, buttons=buttons_dm)
            return True

        elif command == 'account_delete_user_session':
            session_path = os.path.join(
                TELEGRAM_DAEMON_SESSION_PATH if TELEGRAM_DAEMON_SESSION_PATH else "session",
                "DownloadDaemon_user.session"
            )
            deleted = False
            try:
                if os.path.exists(session_path):
                    os.remove(session_path)
                    deleted = True
                for ext in ['-journal', '-wal', '-shm']:
                    extra = session_path + ext
                    if os.path.exists(extra):
                        os.remove(extra)
            except Exception as e:
                logger.warning("Error borrando sesión de usuario: %s", e)

            if self.user_client and self.user_client.is_connected():
                try:
                    await self.user_client.disconnect()
                except Exception as e:
                    logger.debug("Error desconectando user_client tras borrar sesión: %s", e)

            self.account_mode = 'bot'
            self.user_client = None
            self.download_client = self.client
            self.user_phone = None
            self.save_config()

            msg = (
                "✅ **Sesión de usuario borrada.**\n\n"
                "El archivo de sesión ha sido eliminado y el sistema ha vuelto a **Modo Bot completo** en caliente."
            ) if deleted else (
                "⚠️ No se encontró archivo de sesión (quizás ya estaba borrado). "
                "Modo cambiado a Bot igualmente."
            )
            await event.edit(msg, buttons=[Button.inline("🔙 Volver al Menú", data="menu_main")])
            return True

        return False
