# -*- coding: utf-8 -*-
import asyncio
import logging
import os
import signal
import sys
import warnings
from telethon import TelegramClient, events, __version__ as telethon_version
from telethon.tl.types import PeerChannel

# Suprimir warning de Telethon sobre sesiones asíncronas experimentales
warnings.filterwarnings('ignore', message='Using async sessions support is an experimental feature', category=UserWarning)

from .account_manager import AccountManagerMixin
from .command_handler import CommandHandlerMixin
from .completion_manager import CompletionManagerMixin
from .config_manager import ConfigManagerMixin
from .download_engine import DownloadEngineMixin
from .extraction_manager import ExtractionManagerMixin
from .menu import BotMenu, MenuManagerMixin
from .queue_manager import QueueManagerMixin
from .rename_manager import RenameManagerMixin
from .history_manager import HistoryManagerMixin
from .panel import PanelMixin
from .questions_manager import QuestionsManagerMixin
from .session_manager import get_session, save_session, TELEGRAM_DAEMON_SESSION_PATH
from .status_manager import StatusManagerMixin
from .utils import UtilsMixin, sanitize_path_component

LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    format='[%(levelname) 5s/%(asctime)s]%(name)s:%(message)s',
    level=getattr(logging, LOG_LEVEL, logging.INFO)
)

logger = logging.getLogger(__name__)


class TelegramDownloadDaemon(
    UtilsMixin,
    ConfigManagerMixin,
    AccountManagerMixin,
    StatusManagerMixin,
    QueueManagerMixin,
    ExtractionManagerMixin,
    RenameManagerMixin,
    HistoryManagerMixin,
    PanelMixin,
    QuestionsManagerMixin,
    CompletionManagerMixin,
    MenuManagerMixin,
    DownloadEngineMixin,
    CommandHandlerMixin,
):
    """Demonio de descargas automatizadas de Telegram con soporte híbrido Bot/Userbot."""

    def __init__(self, args):
        self.api_id = args.api_id
        self.api_hash = args.api_hash
        self.channel_id = int(args.channel)
        self.raw_channel_id = self.channel_id
        if str(self.channel_id).startswith("-100"):
            self.normalized_channel_id = int(str(self.channel_id)[4:])
        else:
            self.normalized_channel_id = abs(self.channel_id)

        self.bot = args.bot
        self.download_folder = args.dest
        self.temp_folder = args.temp
        self.duplicates = args.duplicates
        self.worker_count = 4
        self.last_update = 0
        self.update_frequency = 10
        self.in_progress = {}
        self.pending_downloads_file = os.path.join(self.temp_folder, "pending_downloads.json")
        self.resume_offsets = {}
        self.active_and_queued_links = set()
        self.max_retries = 3
        self.active_downloads = {}
        self.auto_extract = True
        self.clean_parts_after_extract = True
        self.extracting_sets = {}
        self.extracted_sets = set()
        self.extraction_lock = asyncio.Lock()
        self.paused_links = set()
        self.paused_items = {}
        self.progress_info = {}
        self.cancelled_links = set()
        self.origin_links = {}
        self.part_message_ids = {}

        self.owner_id = int(args.owner_id) if getattr(args, 'owner_id', None) is not None else None
        self.account_mode = "bot"
        self.user_phone = None
        self.fallback_to_bot_warning = False
        self.client = None
        self.user_client = None
        self.download_client = None
        self.login_wizard_state = {}
        self.current_queue = None
        self.proxy = None
        self._last_save_time = 0
        self._save_debounce_seconds = 2

        self.menu_message_id = None
        self.menu_chat_id = None
        self.active_status_message_id = None
        self.active_status_chat_id = None
        self.last_status_content = None

        self.__init_completion_manager__()
        self.__init_menu_manager__()
        self.__init_rename_manager__()

        if not self.temp_folder:
            self.temp_folder = self.download_folder

        if os.path.exists("/session"):
            self.config_file = "/session/config.json"
        else:
            self.config_file = os.path.join("session", "config.json")

        self.__init_history_manager__()
        self.__init_questions_manager__()
        self.load_config()

    async def send_hello_message(self, version, client_ref, peer_channel):
        """Envía el mensaje de bienvenida y el menú interactivo al canal configurado."""
        logger.debug("send_hello_message() llamado con peer_channel=%s", peer_channel)
        entity = peer_channel
        try:
            entity = await client_ref.get_entity(peer_channel)
        except Exception as e:
            logger.warning("No se pudo obtener la entidad para %s: %s", peer_channel, e)

        if getattr(self, 'user_client', None) and self.account_mode == 'user':
            mode_text = "⚡ **Modo: HÍBRIDO** (UI → Bot | Descargas → Userbot)\n"
        else:
            mode_text = "🤖 **Modo: Bot completo**\n"

        hello_text = f"🚀 **Telegram Download Daemon Plus {version}**\nUsando Telethon {telethon_version}\n{mode_text}\n"
        if getattr(self, 'fallback_to_bot_warning', False):
            hello_text = "⚠️ **No se encontró la sesión de usuario guardada, se ha vuelto a modo Bot automáticamente. Usa el menú para reconfigurar la cuenta de usuario.**\n\n" + hello_text
        logger.info(hello_text.strip())

        text, buttons = self.main_menu_content()
        try:
            # Panel único: se borra el del arranque anterior y los menús residuales
            self._load_ui_state()
            await self.post_panel(hello_text + text, buttons, view='main', chat_id=self.raw_channel_id)
            asyncio.create_task(self.sweep_old_menus())
        except Exception as e:
            logger.error("Error enviando menú de bienvenida: %s", e, exc_info=True)

    def start(self, version):
        """Punto de entrada principal para iniciar los clientes de Telegram y los workers."""
        try:
            sys.stdout.reconfigure(line_buffering=True)
            sys.stderr.reconfigure(line_buffering=True)
        except Exception:
            pass
        os.environ['PYTHONUNBUFFERED'] = '1'

        logger.info("Iniciando start() con account_mode=%s", self.account_mode)
        bot_session_name = "DownloadDaemon_bot"
        bot_client = TelegramClient(get_session(bot_session_name), self.api_id, self.api_hash, proxy=self.proxy)
        self.client = bot_client

        user_client = None
        if getattr(self, 'account_mode', 'bot') == 'user':
            session_path = os.path.join(TELEGRAM_DAEMON_SESSION_PATH if TELEGRAM_DAEMON_SESSION_PATH else "session", "DownloadDaemon_user.session")
            if os.path.isfile(session_path):
                logger.info("👤 Inicializando cliente de usuario para descargas...")
                user_client = TelegramClient(
                    get_session("DownloadDaemon_user"),
                    self.api_id,
                    self.api_hash,
                    proxy=self.proxy,
                    connection_retries=5,
                    retry_delay=1,
                    request_retries=5,
                )
            else:
                logger.warning("No se encontró la sesión de usuario guardada. Volviendo a modo Bot para descargas.")
                self.account_mode = "bot"
                self.save_config()
                self.fallback_to_bot_warning = True

        self.user_client = user_client
        self.download_client = user_client if user_client else bot_client

        async def run_clients():
            nonlocal bot_client
            logger.debug("Conectando bot_client con bot_token...")
            await bot_client.start(bot_token=self.bot)

            bot_me = await bot_client.get_me()
            if bot_me:
                is_bot = getattr(bot_me, 'bot', False)
                logger.info("bot_client conectado como: %s (id=%s, is_bot=%s)", bot_me.first_name, bot_me.id, is_bot)
                if not is_bot:
                    logger.warning("¡bot_client NO es un bot! Sesión corrupta. Reconectando...")
                    await bot_client.disconnect()
                    from telethon.sessions import StringSession
                    bot_client_clean = TelegramClient(StringSession(), self.api_id, self.api_hash, proxy=self.proxy)
                    await bot_client_clean.start(bot_token=self.bot)
                    clean_me = await bot_client_clean.get_me()
                    logger.info("bot_client reconectado como: %s (is_bot=%s)", clean_me.first_name, getattr(clean_me, 'bot', False))
                    self.client = bot_client_clean
                    if not user_client:
                        self.download_client = bot_client_clean
                    save_session(bot_client_clean.session, bot_session_name)
                    bot_client = bot_client_clean
                    return

            save_session(bot_client.session, bot_session_name)

            try:
                from telethon.tl.functions.bots import SetBotCommandsRequest
                from telethon.tl.types import BotCommand, BotCommandScopeDefault
                await bot_client(SetBotCommandsRequest(
                    scope=BotCommandScopeDefault(),
                    lang_code='',
                    commands=[
                        BotCommand(command='menu', description='🎛️ Panel de control'),
                        BotCommand(command='descargas', description='📥 Descargas activas, cola y pausadas'),
                        BotCommand(command='biblioteca', description='📚 Discos, últimas añadidas y pendientes'),
                        BotCommand(command='buscar', description='🔎 Buscar serie o peli: /buscar nombre'),
                        BotCommand(command='pendientes', description='⚠️ Lo que falta por revisar'),
                        BotCommand(command='historial', description='🧾 Últimos movimientos'),
                        BotCommand(command='limpiarchat', description='🧽 Borrar todo el chat y dejar solo el panel'),
                        BotCommand(command='ayuda', description='📖 Guía de uso y comandos'),
                    ]
                ))
                logger.info("✅ Comandos nativos de bot registrados en Telegram.")
            except Exception as e:
                logger.debug("No se pudieron registrar los comandos de Telegram: %s", e)

            if user_client:
                try:
                    logger.debug("Conectando user_client...")
                    await user_client.connect()
                    if not await user_client.is_user_authorized():
                        raise Exception("La sesión de usuario existe pero no está autorizada.")
                    user_me = await user_client.get_me()
                    logger.info("👤 Cliente de usuario conectado: %s %s (id=%s)", user_me.first_name, user_me.last_name or '', user_me.id)
                    save_session(user_client.session, "DownloadDaemon_user")
                    logger.info("👤 Cliente de usuario conectado y autorizado con éxito.")
                except Exception as e:
                    logger.warning("Error al conectar cliente de usuario: %s. Volviendo a modo Bot.", e)
                    self.user_client = None
                    self.download_client = bot_client
                    self.account_mode = "bot"
                    self.save_config()
                    self.fallback_to_bot_warning = True

            logger.info("🚀 Clientes conectados con éxito")

        bot_client.loop.run_until_complete(run_clients())

        client = bot_client
        with client:
            queue = asyncio.Queue()
            self.current_queue = queue
            peer_channel = PeerChannel(self.normalized_channel_id)

            async def save_progress_async():
                await self.save_current_state(queue)
            self.save_progress_async = save_progress_async

            logger.info("Event handlers registrados. Escuchando canal raw=%s, normalized=%s", self.raw_channel_id, self.normalized_channel_id)

            @client.on(events.NewMessage())
            async def handler(event):
                chat_id = event.chat_id
                is_target_channel = (chat_id == self.normalized_channel_id or
                                     chat_id == self.raw_channel_id or
                                     str(chat_id).endswith(str(self.normalized_channel_id)))

                if not is_target_channel and not event.is_private:
                    return

                if event.out:
                    return

                sender = await event.get_sender()
                if sender and getattr(sender, 'bot', False) and sender.id == (await client.get_me()).id:
                    return

                # Interceptar mensajes de login en privado
                if event.is_private and event.sender_id in self.login_wizard_state:
                    if self.owner_id is None or event.sender_id == self.owner_id:
                        handled = await self.handle_login_wizard_message(event)
                        if handled:
                            return

                try:
                    # Reenvíos
                    if event.message and event.message.forward:
                        if hasattr(self, 'refresh_sticky_menu'):
                            await self.refresh_sticky_menu(event.chat_id)
                        try:
                            fwd_from = event.message.fwd_from
                            if fwd_from.from_id is None or not hasattr(fwd_from.from_id, 'channel_id'):
                                await self.queue_download(event,
                                                          channel=event.message.peer_id.channel_id,
                                                          message_id=event.message.id,
                                                          destination=".",
                                                          client=client,
                                                          queue=queue)
                                return

                            channel_id = fwd_from.from_id.channel_id
                            if fwd_from.channel_post is None:
                                try:
                                    src_entity = await self.download_client.get_entity(fwd_from.from_id)
                                    dest_folder = f"./{sanitize_path_component(getattr(src_entity, 'title', 'Desconocido'))}"
                                except Exception:
                                    dest_folder = "."
                                await self.queue_download(
                                    event,
                                    channel=event.message.peer_id.channel_id,
                                    message_id=event.message.id,
                                    destination=dest_folder,
                                    client=client,
                                    queue=queue
                                )
                                return
                            else:
                                message_id = fwd_from.channel_post
                                try:
                                    channel_entity = await self.download_client.get_entity(channel_id)
                                    channel_title = sanitize_path_component(channel_entity.title)
                                except Exception:
                                    channel_title = f"Canal_{channel_id}"

                                has_media = bool(event.message and event.message.media)
                                direct_msg = event.message if has_media else None

                                if self.download_client is not self.client:
                                    try:
                                        fetched = await self.download_client.get_messages(channel_id, ids=message_id)
                                        if fetched and fetched.media:
                                            direct_msg = fetched
                                    except Exception as e:
                                        logger.debug("No se pudo obtener mensaje original via userbot: %s", e)

                                await self.queue_download(event,
                                                          channel=channel_id,
                                                          message_id=message_id,
                                                          destination=os.path.join('.', channel_title),
                                                          direct_message=direct_msg,
                                                          client=client,
                                                          queue=queue)
                        except Exception as e:
                            logger.error("Error procesando mensaje reenviado: %s", e, exc_info=True)
                            await self.log_reply(event, f"Error procesando mensaje reenviado: {str(e)}")
                        return

                    # Respuesta escrita a una pregunta («¿qué es este archivo?»)
                    if not event.media and await self.handle_question_text(event):
                        return

                    # Comandos de texto
                    if (not event.media or hasattr(event.media, 'webpage')) and event.message and event.message.message:
                        output = await self.parse_command(event, queue=queue, client=client)
                        if output:
                            await self.show_in_panel(event, output)
                        return

                    # Media directa
                    if event.media:
                        await self.queue_download(event,
                                                  channel=event.message.peer_id.channel_id,
                                                  message_id=event.message.id,
                                                  client=client,
                                                  queue=queue)
                except Exception as e:
                    logger.error("Error en el manejador de eventos: %s", e, exc_info=True)

            @client.on(events.CallbackQuery())
            async def callback_handler(event):
                await self.handle_callback_query(event, queue=queue, client=client)

            async def start_app():
                tasks = []
                loop = asyncio.get_event_loop()

                logger.info("Buscando archivos parcialmente descargados...")
                partial_files = self.find_partial_downloads()
                self.partial_files_cache = {partial['filename']: partial for partial in partial_files}
                self.clean_invalid_partial_cache()

                await self.restore_pending_downloads(queue)

                for _ in range(self.worker_count):
                    task = loop.create_task(self.run_worker_loop(queue, client, peer_channel))
                    tasks.append(task)

                async def watchdog():
                    while True:
                        await asyncio.sleep(60)
                        for i, task in enumerate(tasks):
                            if task.done():
                                logger.warning("Worker %s caído, relanzando...", i)
                                tasks[i] = loop.create_task(self.run_worker_loop(queue, client, peer_channel))

                watchdog_task = loop.create_task(watchdog())
                summary_task = loop.create_task(self.daily_summary_loop(client, self.raw_channel_id))
                panel_task = loop.create_task(self.panel_autorefresh_loop())
                loop.create_task(self.ask_for_queued(queue))
                loop.create_task(self.recover_staging(client, self.raw_channel_id))

                def _on_stop_signal():
                    logger.info("Señal de parada recibida, guardando descargas pendientes...")
                    asyncio.create_task(self.save_all_pending_downloads_async(queue))

                try:
                    loop.add_signal_handler(signal.SIGTERM, _on_stop_signal)
                    loop.add_signal_handler(signal.SIGINT, _on_stop_signal)
                except (NotImplementedError, AttributeError):
                    def _sync_signal_handler(signum, frame):
                        logger.info("Señal de parada recibida, guardando descargas pendientes...")
                        self.save_all_pending_downloads(queue)
                    signal.signal(signal.SIGTERM, _sync_signal_handler)
                    signal.signal(signal.SIGINT, _sync_signal_handler)

                await self.send_hello_message(version, client, self.raw_channel_id)

                if self.user_client:
                    await asyncio.gather(
                        client.run_until_disconnected(),
                        self.user_client.run_until_disconnected(),
                        return_exceptions=True
                    )
                else:
                    await client.run_until_disconnected()

                for task in tasks:
                    task.cancel()
                watchdog_task.cancel()
                summary_task.cancel()
                panel_task.cancel()
                await asyncio.gather(*tasks, watchdog_task, summary_task, panel_task, return_exceptions=True)

            client.loop.run_until_complete(start_app())