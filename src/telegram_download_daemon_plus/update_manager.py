# -*- coding: utf-8 -*-
"""
Versiones nuevas: cada 12 horas mira la última release de GitHub y, si es más nueva que la
instalada, avisa en el canal una sola vez por versión, con un botón para actualizar desde
el propio bot (ver launcher.py). Se desactiva el aviso con UPDATE_CHECK=0.
"""
import asyncio
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request

from telethon import Button

from . import launcher
from .panel import DAEMON_VERSION

logger = logging.getLogger(__name__)

UPDATE_REPO = os.environ.get("UPDATE_REPO", "rubenzori86/telegram-download-daemon-plus")
UPDATE_CHECK = os.environ.get("UPDATE_CHECK", "1") != "0"
CHECK_INTERVAL = 12 * 3600
FIRST_CHECK_DELAY = 60
NOTES_LIMIT = 900
REBUILD_STEPS = ("```\ncd <carpeta del bot>\ngit pull\n"
                 "docker compose up -d --build\n```")


def parse_version(text):
    """'v1.2.0' -> (1, 2, 0). None si no es una versión normal (p.ej. una beta)."""
    m = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', (text or "").strip())
    return tuple(int(n) for n in m.groups()) if m else None


def is_newer(latest, current):
    a, b = parse_version(latest), parse_version(current)
    return bool(a and b) and a > b


def fetch_latest_release(repo=UPDATE_REPO):
    """{'version', 'url', 'notes'} de la última release, o None si el repositorio no tiene."""
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/latest",
                                 headers={'User-Agent': 'telegram-download-daemon',
                                          'Accept': 'application/vnd.github+json'})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    return {'version': (data.get('tag_name') or "").lstrip('v'), 'url': data.get('html_url'),
            'notes': data.get('body') or ""}


def short_notes(notes):
    notes = re.sub(r'(?m)^#+\s*(.+)$', r'**\1**', notes.replace('\r\n', '\n')).strip()
    if len(notes) > NOTES_LIMIT:
        notes = notes[:NOTES_LIMIT].rsplit('\n', 1)[0] + "\n…"
    return notes


def baked_version():
    """Versión del código de la imagen (o del montado en ella), sin contar la descargada."""
    try:
        if not launcher.running_version():
            return DAEMON_VERSION
        overlay = os.path.abspath(launcher.code_root())
        for root in ('/usr/src/app', os.getcwd()):
            p = os.path.join(root, launcher.PACKAGE)
            if os.path.isfile(os.path.join(p, 'panel.py')) and not os.path.abspath(p).startswith(overlay):
                return launcher.version_of(p) or "0.0.0"
    except Exception:
        pass
    return "0.0.0"


class UpdateManagerMixin:

    def __init_update_manager__(self):
        self.update_info = None         # última release, si es más nueva que la instalada
        self.update_checked_at = None
        self._update_notified = None    # versión de la que ya se ha avisado
        self._updating = False
        self.update_file = os.path.join(os.path.dirname(self.config_file), "update_state.json")
        try:
            with open(self.update_file, 'r', encoding='utf-8') as f:
                self._update_notified = json.load(f).get('notified')
        except (OSError, ValueError):
            pass

    def _save_update_state(self):
        try:
            with open(self.update_file, 'w', encoding='utf-8') as f:
                json.dump({'notified': self._update_notified}, f)
        except OSError as e:
            logger.debug("No se pudo guardar el estado de actualización: %s", e)

    async def check_for_update(self):
        """Consulta GitHub. Devuelve False si no se ha podido comprobar."""
        try:
            latest = await asyncio.to_thread(fetch_latest_release)
        except Exception as e:
            logger.info("No se pudo comprobar si hay versión nueva: %s", e)
            return False
        self.update_checked_at = time.time()
        self.update_info = latest if latest and is_newer(latest['version'], DAEMON_VERSION) else None
        return True

    @staticmethod
    def _update_buttons(info):
        return [[Button.inline(f"⬇️ Actualizar a la {info['version']}", data="cmd_update_ask")],
                [Button.url("📄 Ver novedades", info['url'])]]

    async def notify_update(self):
        """Avisa en el canal, una sola vez por versión."""
        info = self.update_info
        if not info or info['version'] == self._update_notified:
            return
        text = f"🆕 **Hay una versión nueva: {info['version']}** (tienes {DAEMON_VERSION})\n"
        notes = short_notes(info['notes'])
        if notes:
            text += f"\n{notes}\n"
        text += "\n_Se actualiza desde aquí mismo: el bot la descarga y se reinicia._"
        try:
            await self.client.send_message(self.raw_channel_id, text, buttons=self._update_buttons(info))
        except Exception as e:
            logger.warning("No se pudo avisar de la versión nueva: %s", e)
            return
        self._update_notified = info['version']
        self._save_update_state()

    async def update_check_loop(self):
        if not UPDATE_CHECK:
            return
        await asyncio.sleep(FIRST_CHECK_DELAY)
        while True:
            if await self.check_for_update():
                await self.notify_update()
            await asyncio.sleep(CHECK_INTERVAL)

    def update_status_line(self):
        if self.update_info:
            return f"🆕 Versión nueva disponible: **{self.update_info['version']}**"
        if not UPDATE_CHECK:
            return "🔕 Aviso de versiones desactivado"
        return "✅ Tienes la última versión" if self.update_checked_at else "🔎 Versión nueva: sin comprobar"

    @staticmethod
    def _previous_label():
        return launcher.load_state().get('previous') or baked_version()

    def _rollback_button(self):
        if not launcher.running_version():
            return []
        return [[Button.inline(f"↩️ Volver a la {self._previous_label()}", data="cmd_update_rollback_ask")]]

    def update_view(self):
        back = [Button.inline("🔙 Volver", data="menu_system")]
        info = self.update_info
        if not info:
            return f"✅ **Tienes la última versión** ({DAEMON_VERSION})", self._rollback_button() + [back]
        text = (f"🆕 **Versión nueva: {info['version']}** (tienes {DAEMON_VERSION})\n\n"
                "Pulsa **Actualizar**: el bot la descarga, comprueba que funciona y se reinicia. "
                "Si no arrancara, vuelve solo a la que tienes ahora.")
        return text, self._update_buttons(info) + self._rollback_button() + [back]

    # ---------- actualizar ----------
    async def _owner_only(self, event):
        owner = getattr(self, 'owner_id', None)
        if owner and event.sender_id != owner:
            await event.answer("❌ Solo el propietario del bot puede actualizarlo.", alert=True)
            return False
        return True

    async def _restart_process(self):
        """Guarda las descargas pendientes y sale: Docker vuelve a arrancar el contenedor."""
        try:
            queue_ref = getattr(self, 'current_queue', None)
            if queue_ref:
                await self.save_all_pending_downloads_async(queue_ref)
        except Exception as e:
            logger.warning("Error guardando estado antes de reiniciar: %s", e)
        logger.info("🔄 Reinicio para cambiar de versión. Saliendo...")
        await asyncio.sleep(1)
        os._exit(0)

    async def _install(self, event, info):
        steps = []
        loop = asyncio.get_running_loop()

        async def show(line):
            steps.append(line)
            try:
                await event.edit(f"⬆️ **Actualizando a la {info['version']}**\n\n" + "\n".join(steps))
            except Exception:
                pass

        def log(line):
            asyncio.run_coroutine_threadsafe(show(line), loop)

        back = [[Button.inline("🔙 Volver", data="menu_system")]]
        try:
            await asyncio.to_thread(launcher.install, UPDATE_REPO, info['version'], DAEMON_VERSION, log)
        except launcher.UpdateError as e:
            if str(e) == "REBUILD":
                text = (f"🧱 **La {info['version']} necesita reconstruir la imagen**\n\n"
                        "Trae cambios en los programas del sistema, así que esta vez no se puede "
                        f"actualizar desde el bot. En la máquina del bot:\n{REBUILD_STEPS}")
            else:
                text = f"❌ **No se ha actualizado.** Sigues con la {DAEMON_VERSION}.\n\n{e}"
            await self._safe_edit(event, text, back)
            return False
        except Exception as e:
            logger.error("Error actualizando: %s", e, exc_info=True)
            await self._safe_edit(event, f"❌ **No se ha actualizado.** Sigues con la {DAEMON_VERSION}.\n\n"
                                         f"No he podido descargarla: {e}", back)
            return False
        await asyncio.sleep(0.3)
        await show("✅ Lista. **Reiniciando…** vuelvo en unos segundos.")
        return True

    async def handle_update_callback(self, event, data):
        if data == 'cmd_update_ask':
            if not self.update_info:
                await event.answer("Buscando en GitHub…")
                await self.check_for_update()
            info = self.update_info
            if not info:
                text, buttons = self.update_view()
                await self._safe_edit(event, text, buttons)
                return True
            busy = len(getattr(self, 'active_downloads', None) or {})
            text = f"⬇️ **¿Actualizar a la {info['version']}?**\n\nEl bot se reiniciará en unos segundos."
            if busy:
                text += (f" Hay {busy} descarga{'s' if busy != 1 else ''} en curso: "
                         f"se reanudará{'n' if busy != 1 else ''} donde se quedó.")
            await self._safe_edit(event, text, [[Button.inline("✅ Sí, actualizar", data="cmd_update_go"),
                                                 Button.inline("❌ No", data="menu_update_check")]])
            return True
        if data == 'cmd_update_go':
            if not await self._owner_only(event):
                return True
            info = self.update_info
            if not info or self._updating:
                await event.answer("Ya se está actualizando" if self._updating else "No hay versión nueva")
                return True
            self._updating = True
            try:
                ok = await self._install(event, info)
            finally:
                self._updating = False
            if ok:
                await self._restart_process()
            return True
        if data == 'cmd_update_rollback_ask':
            await self._safe_edit(event, f"↩️ **¿Volver a la {self._previous_label()}?**\n\nEl bot se reiniciará.",
                                  [[Button.inline("✅ Sí, volver", data="cmd_update_rollback"),
                                    Button.inline("❌ No", data="menu_update_check")]])
            return True
        if data == 'cmd_update_rollback':
            if not await self._owner_only(event):
                return True
            target = launcher.rollback(baked_version())
            if not target:
                await event.answer("No hay versión anterior", alert=True)
                return True
            await self._safe_edit(event, f"↩️ **Volviendo a la {target}…** vuelvo en unos segundos.", [])
            await self._restart_process()
            return True
        return False

    async def announce_start(self):
        """Tras arrancar: marca la versión como buena y cuenta qué ha pasado."""
        try:
            launcher.mark_healthy()
            notes = launcher.pop_start_notes()
        except Exception as e:
            logger.warning("Estado de actualizaciones ilegible: %s", e)
            return
        rb, up = notes.get('rolled_back'), notes.get('updated')
        if rb:
            text = (f"⚠️ **La {rb['from']} no arrancaba**, así que he vuelto a la {DAEMON_VERSION}.\n"
                    "_Mira Sistema → Últimos errores._")
        elif up and up.get('to') == DAEMON_VERSION:
            text = f"✅ **Actualizado:** de la {up.get('from')} a la {DAEMON_VERSION}."
        elif up:
            text = (f"⚠️ He arrancado con la {DAEMON_VERSION} y no con la {up.get('to')}: "
                    "la de la imagen es igual o más nueva.")
        else:
            return
        try:
            await self.client.send_message(self.raw_channel_id, text)
        except Exception as e:
            logger.warning("No se pudo avisar del cambio de versión: %s", e)
