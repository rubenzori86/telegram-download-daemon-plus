# -*- coding: utf-8 -*-
"""
Historial persistente (descargas, colocaciones en la biblioteca, fallos), lista de
pendientes por resolver, avisos de espacio bajo y resumen diario.
"""
import asyncio
import datetime
import json
import logging
import os
import time

from humanize import naturalsize

from . import library_manager as library

logger = logging.getLogger(__name__)

HISTORY_MAX = 500


def hsize(b, fmt='%.1f'):
    """Tamaño en unidades binarias como DSM (916.7 GB = 916.7 GiB)."""
    return naturalsize(b, binary=True, format=fmt).replace('iB', 'B')
# Avisar cuando el espacio libre de un disco baje de reserva + este margen
SPACE_WARNING_MARGIN = 50 * 1024 ** 3

# Tipos de evento
PLACED = 'placed'            # colocado en la biblioteca
DOWNLOADED = 'downloaded'    # descargado (no es vídeo o renombrado desactivado)
FAILED = 'failed'            # descarga fallida tras reintentos
DUPLICATE = 'duplicate'      # identificado pero ya existía
UNPLACED = 'unplaced'        # identificado pero no se pudo colocar (espacio, error)
UNIDENTIFIED = 'unidentified'  # FileBot no lo identificó
PENDING_KINDS = (DUPLICATE, UNPLACED, UNIDENTIFIED)

KIND_ICON = {
    PLACED: "📚", DOWNLOADED: "📥", FAILED: "❌",
    DUPLICATE: "♻️", UNPLACED: "⚠️", UNIDENTIFIED: "❓",
}


def fmt_when(ts):
    d = datetime.datetime.fromtimestamp(ts)
    today = datetime.date.today()
    if d.date() == today:
        return d.strftime("%H:%M")
    if d.date() == today - datetime.timedelta(days=1):
        return "ayer " + d.strftime("%H:%M")
    return d.strftime("%d/%m %H:%M")


def fmt_duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


class _RecentErrorsHandler(logging.Handler):
    """Guarda en memoria los últimos avisos/errores para mostrarlos en Sistema."""

    def __init__(self, store):
        super().__init__(level=logging.WARNING)
        self.store = store

    def emit(self, record):
        try:
            self.store.append((record.created, record.levelno, record.getMessage()))
            del self.store[:-50]
        except Exception:
            pass


class HistoryManagerMixin:

    fmt_duration = staticmethod(fmt_duration)

    def __init_history_manager__(self):
        self.recent_errors = []
        logging.getLogger().addHandler(_RecentErrorsHandler(self.recent_errors))
        self.history = []
        self.history_seq = 0
        self.start_time = time.time()
        self.daily_summary_enabled = True
        self.daily_summary_hour = 23
        self._last_summary_date = None
        self._space_warned = {}
        base = os.path.dirname(self.config_file) if getattr(self, 'config_file', None) else "session"
        self.history_file = os.path.join(base, "history.json")
        self._load_history()

    # ---------- persistencia ----------
    def _load_history(self):
        try:
            if os.path.exists(self.history_file):
                with open(self.history_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.history = data.get('events', [])
                self.history_seq = data.get('seq', len(self.history))
                self._last_summary_date = data.get('last_summary_date')
        except Exception as e:
            logger.warning("No se pudo cargar el historial: %s", e)

    def _save_history(self):
        try:
            tmp = self.history_file + ".tmp"
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({'seq': self.history_seq, 'events': self.history[-HISTORY_MAX:],
                           'last_summary_date': self._last_summary_date}, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.history_file)
        except Exception as e:
            logger.warning("No se pudo guardar el historial: %s", e)

    def add_history(self, kind, title, **extra):
        self.history_seq += 1
        entry = {'id': self.history_seq, 'ts': time.time(), 'kind': kind, 'title': title}
        entry.update({k: v for k, v in extra.items() if v is not None})
        self.history.append(entry)
        self.history = self.history[-HISTORY_MAX:]
        self._save_history()
        return entry

    def get_history_entry(self, entry_id):
        for e in self.history:
            if e['id'] == entry_id:
                return e
        return None

    def resolve_history_entry(self, entry_id, status='resuelto'):
        e = self.get_history_entry(entry_id)
        if e:
            e['resolved'] = status
            self._save_history()

    # ---------- consultas ----------
    def pending_entries(self):
        """Pendientes por resolver cuyo archivo sigue existiendo."""
        out = []
        for e in reversed(self.history):
            if e['kind'] in PENDING_KINDS and not e.get('resolved') and e.get('path') and os.path.exists(e['path']):
                out.append(e)
        return out

    def recent_entries(self, limit=20, kinds=None):
        out = [e for e in reversed(self.history) if not kinds or e['kind'] in kinds]
        return out[:limit]

    def entries_since(self, ts):
        return [e for e in self.history if e['ts'] >= ts]

    @staticmethod
    def today_start():
        return datetime.datetime.combine(datetime.date.today(), datetime.time()).timestamp()

    # ---------- discos ----------
    @staticmethod
    def disks_status():
        out = []
        for disk, root in library.LIBRARY_DISKS:
            try:
                u = __import__('shutil').disk_usage(root)
                out.append({'disk': disk, 'free': u.free, 'total': u.total, 'used_pct': u.used * 100 // u.total})
            except OSError:
                out.append({'disk': disk, 'free': 0, 'total': 0, 'used_pct': 0, 'error': True})
        return out

    async def check_space_warnings(self, client=None, peer_channel=None):
        threshold = library.RESERVE_BYTES + SPACE_WARNING_MARGIN
        today = datetime.date.today().isoformat()
        for d in self.disks_status():
            if d.get('error') or d['free'] >= threshold:
                continue
            if self._space_warned.get(d['disk']) == today:
                continue
            self._space_warned[d['disk']] = today
            text = (f"💾 **Poco espacio en {d['disk']}**\n"
                    f"Quedan {hsize(d['free'])} libres ({d['used_pct']}% usado).\n"
                    f"_Por debajo de {hsize(library.RESERVE_BYTES)} ya no se colocará nada nuevo ahí._")
            await self._notify(client or self.client, peer_channel or self.raw_channel_id, text)

    # ---------- resumen diario ----------
    def build_daily_summary(self, since_ts):
        events = self.entries_since(since_ts)
        placed = [e for e in events if e['kind'] == PLACED]
        pending = [e for e in events if e['kind'] in PENDING_KINDS]
        failed = [e for e in events if e['kind'] == FAILED]
        downloaded_bytes = sum(e.get('size', 0) for e in events if e['kind'] in (PLACED, DOWNLOADED) or e['kind'] in PENDING_KINDS)
        if not events:
            return None
        text = f"🌙 **Resumen del día** · {datetime.date.today().strftime('%d/%m')}\n\n"
        text += f"📥 Descargado: {hsize(downloaded_bytes)}\n"
        if placed:
            text += f"\n📚 **Añadido a la biblioteca ({len(placed)}):**\n"
            for e in placed[:15]:
                text += f"• {e['title']} → {e.get('disk', '?')}\n"
            if len(placed) > 15:
                text += f"• … y {len(placed) - 15} más\n"
        if pending:
            text += f"\n⚠️ **Pendientes de revisar: {len(pending)}** (Biblioteca → Pendientes)\n"
        if failed:
            text += f"\n❌ **Descargas fallidas: {len(failed)}**\n"
        text += "\n💾 " + " · ".join(f"{d['disk']} {hsize(d['free'])}" for d in self.disks_status())
        return text

    async def daily_summary_loop(self, client, peer_channel):
        while True:
            try:
                await asyncio.sleep(60)
                now = datetime.datetime.now()
                today = now.date().isoformat()
                # una vez al día: vaciar lo reemplazado hace más de N días
                if getattr(self, '_last_purge_date', None) != today:
                    self._last_purge_date = today
                    n, freed = await asyncio.to_thread(library.purge_trash)
                    if n:
                        logger.info("🗑️ Vaciados %s archivos reemplazados (%s)", n, hsize(freed))
                if not self.daily_summary_enabled:
                    continue
                if now.hour >= self.daily_summary_hour and self._last_summary_date != today:
                    self._last_summary_date = today
                    self._save_history()
                    text = self.build_daily_summary(self.today_start())
                    if text:
                        await self._notify(client, peer_channel, text)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Error en el resumen diario: %s", e)

    def set_daily_summary(self, enabled):
        self.daily_summary_enabled = enabled
        self.save_config()
        return f"✅ Resumen diario: {'activado' if enabled else 'desactivado'} ({self.daily_summary_hour}:00)"

    async def _notify(self, client, peer_channel, text, edit_msg=None):
        """Envía un mensaje, o edita edit_msg si se indica."""
        if not client or not peer_channel:
            return None
        try:
            if edit_msg is not None:
                return await edit_msg.edit(text)
            return await client.send_message(peer_channel, text)
        except Exception as e:
            logger.debug("No se pudo enviar notificación: %s", e)
            return None
