# -*- coding: utf-8 -*-
"""
Aviso de nuevas versiones: cada 12 horas mira la última release de GitHub y, si es más
nueva que la instalada, avisa en el canal una sola vez por versión. Solo avisa: actualizar
requiere reconstruir la imagen en la máquina del usuario. Se desactiva con UPDATE_CHECK=0.
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

from .panel import DAEMON_VERSION

logger = logging.getLogger(__name__)

UPDATE_REPO = os.environ.get("UPDATE_REPO", "rubenzori86/telegram-download-daemon-plus")
UPDATE_CHECK = os.environ.get("UPDATE_CHECK", "1") != "0"
CHECK_INTERVAL = 12 * 3600
FIRST_CHECK_DELAY = 60
NOTES_LIMIT = 900
UPDATE_STEPS = ("```\ncd telegram-download-daemon-plus\ngit pull\n"
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


class UpdateManagerMixin:

    def __init_update_manager__(self):
        self.update_info = None         # última release, si es más nueva que la instalada
        self.update_checked_at = None
        self._update_notified = None    # versión de la que ya se ha avisado
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

    async def notify_update(self):
        """Avisa en el canal, una sola vez por versión."""
        info = self.update_info
        if not info or info['version'] == self._update_notified:
            return
        text = f"🆕 **Hay una versión nueva: {info['version']}** (tienes {DAEMON_VERSION})\n"
        notes = short_notes(info['notes'])
        if notes:
            text += f"\n{notes}\n"
        text += f"\n⬆️ **Para actualizar:**\n{UPDATE_STEPS}"
        try:
            await self.client.send_message(self.raw_channel_id, text,
                                           buttons=[[Button.url("📄 Ver novedades", info['url'])]])
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

    def update_view(self):
        back = [Button.inline("🔙 Volver", data="menu_system")]
        info = self.update_info
        if not info:
            return f"✅ **Tienes la última versión** ({DAEMON_VERSION})", [back]
        text = (f"🆕 **Versión nueva: {info['version']}** (tienes {DAEMON_VERSION})\n\n"
                f"⬆️ **Para actualizar:**\n{UPDATE_STEPS}")
        return text, [[Button.url("📄 Ver novedades", info['url'])], back]
