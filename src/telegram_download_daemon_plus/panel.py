# -*- coding: utf-8 -*-
"""
Panel del bot: vistas con datos en vivo (principal, descargas, biblioteca, pendientes,
historial, sistema) y sus botones. handle_panel_callback() atiende los botones nuevos;
lo que no reconoce lo deja pasar al manejador clásico de command_handler.
"""
import asyncio
import logging
import os
import platform
import shutil
import time

from humanize import naturalsize
from telethon import Button, __version__ as telethon_version

from . import library_manager as library
from .history_manager import KIND_ICON, PENDING_KINDS, fmt_duration, fmt_when, hsize

logger = logging.getLogger(__name__)

DAEMON_VERSION = "1.1.0"
# Velocidad máxima observada para cuentas sin Premium (Telegram limita por cuenta)
ACCOUNT_SPEED_NOTE = "≈5 MB/s por cuenta sin Premium"


def short(name, n=38):
    return name if len(name) <= n else name[:n - 1] + "…"


def bar(pct, length=12):
    filled = max(0, min(length, int(round(length * pct / 100))))
    return "▰" * filled + "▱" * (length - filled)


class PanelMixin:

    # ---------- datos ----------
    def _queued_items(self):
        q = getattr(self, 'current_queue', None)
        return list(q._queue) if q is not None else []  # solo lectura, sin sacar de la cola

    def _active_info(self):
        out = []
        for item in list(self.active_downloads.values()):
            name = item.get('filename') or item['download_media'].get_link()
            p = self.progress_info.get(name, {})
            out.append({'name': name, 'link': item['download_media'].get_link(), **p})
        return out

    def _total_speed(self):
        return sum(p.get('speed', 0) for p in self.progress_info.values()
                   if time.time() - p.get('ts', 0) < 30)

    # ---------- vista principal ----------
    def main_menu_content(self):
        active = self._active_info()
        queued = self._queued_items()
        paused = len(getattr(self, 'paused_items', {}))
        today = self.entries_since(self.today_start())
        placed = sum(1 for e in today if e['kind'] == 'placed')
        pending = len(self.pending_entries())

        text = "🎛️ **Panel de control**\n\n"
        parts = [f"{len(active)} activa{'s' if len(active) != 1 else ''}", f"{len(queued)} en cola"]
        if paused:
            parts.append(f"{paused} pausada{'s' if paused != 1 else ''}")
        text += "📥 **Descargas:** " + " · ".join(parts) + "\n"
        for a in active[:3]:
            if a.get('total'):
                pct = a['received'] * 100 / a['total']
                text += f"   └ {short(a['name'], 30)} {pct:.0f}% · {hsize(a.get('speed', 0))}/s\n"
            else:
                text += f"   └ {short(a['name'], 30)} · empezando…\n"
        text += f"📚 **Hoy:** {placed} añadida{'s' if placed != 1 else ''} a la biblioteca"
        text += f" · ⚠️ {pending} pendiente{'s' if pending != 1 else ''}\n" if pending else "\n"
        text += "💾 " + " · ".join(f"{d['disk']} {hsize(d['free'], '%.0f')}"
                                   for d in self.disks_status()) + "\n"
        text += (f"🏷️ Renombrado {'✅' if self.auto_rename else '❌'} · "
                 f"📦 Extracción {'✅' if self.auto_extract else '❌'} · "
                 f"👷 {self.worker_count} simultánea{'s' if self.worker_count != 1 else ''}")
        buttons = [
            [Button.inline("📥 Descargas", data="menu_downloads"), Button.inline("📚 Biblioteca", data="menu_library")],
            [Button.inline("⚙️ Ajustes", data="menu_config"), Button.inline("🛠️ Sistema", data="menu_system")],
            [Button.inline("🔄 Actualizar", data="menu_main"), Button.inline("🧽 Limpiar chat", data="cmd_clear_chat"),
             Button.inline("✖️ Cerrar", data="menu_close")],
        ]
        return text, buttons

    # ---------- descargas ----------
    def downloads_view(self):
        active = self._active_info()
        queued = self._queued_items()
        paused = list(getattr(self, 'paused_items', {}).values())
        today = self.entries_since(self.today_start())
        done = sum(1 for e in today if e['kind'] not in ('failed',))
        failed = sum(1 for e in today if e['kind'] == 'failed')

        text = "📥 **Descargas**\n\n"
        if active:
            text += f"⚡ **Activas ({len(active)})**\n"
            for a in active:
                text += f"• {short(a['name'])}\n"
                if a.get('total'):
                    pct = a['received'] * 100 / a['total']
                    eta = (a['total'] - a['received']) / a['speed'] if a.get('speed') else 0
                    text += (f"   {bar(pct)} {pct:.0f}%\n"
                             f"   {hsize(a['received'])} / {hsize(a['total'])} · "
                             f"{hsize(a.get('speed', 0))}/s" + (f" · ⏱️ {fmt_duration(eta)}" if eta else "") + "\n")
            text += f"   ⚡ Total: {hsize(self._total_speed())}/s ({ACCOUNT_SPEED_NOTE})\n\n"
        else:
            text += "⚡ Sin descargas activas\n\n"
        if queued:
            text += f"📋 **En cola ({len(queued)})**\n"
            for it in queued[:6]:
                text += f"• {short(it.get('filename') or it['download_media'].get_link())}\n"
            if len(queued) > 6:
                text += f"• … y {len(queued) - 6} más\n"
            text += "\n"
        if paused:
            text += f"⏸️ **Pausadas ({len(paused)})**\n"
            for it in paused[:4]:
                text += f"• {short(it.get('filename') or it['download_media'].get_link())}\n"
            text += "\n"
        text += f"✅ Hoy: {done} completada{'s' if done != 1 else ''}"
        text += f" · ❌ {failed} fallida{'s' if failed != 1 else ''}" if failed else ""
        buttons = [
            [Button.inline("⏯️ Gestionar cola", data="menu_manage_queue"), Button.inline("🔄 Actualizar", data="menu_downloads")],
            [Button.inline("⏸️ Pausar todo", data="cmd_pause_all"), Button.inline("▶️ Reanudar todo", data="cmd_resume_all")],
            [Button.inline("🧾 Historial", data="menu_history"), Button.inline("🔁 Reanudar pendientes", data="cmd_reanudar")],
            [Button.inline("🧹 Limpieza", data="menu_cleanup"), Button.inline("🛑 Cancelar todas", data="cmd_cancel_all")],
            [Button.inline("🔙 Volver", data="menu_main")],
        ]
        return text, buttons

    def cleanup_view(self):
        text = ("🧹 **Limpieza**\n\n"
                "• **Temporales:** borra descargas a medias (.tdd).\n"
                "• **Caché de parciales:** olvida parciales que ya no existen.\n"
                "• **Pendientes inválidos:** limpia la lista de reanudación.\n"
                "• **Mensajes completados:** borra del chat los avisos ya terminados.")
        buttons = [
            [Button.inline("🗑️ Temporales", data="cmd_clean"), Button.inline("🧠 Caché parciales", data="cmd_limpiar_cache")],
            [Button.inline("📄 Pendientes inválidos", data="cmd_limpiar_pendientes")],
            [Button.inline("💬 Mensajes completados", data="cmd_delete_completed")],
            [Button.inline("🔙 Volver", data="menu_downloads")],
        ]
        return text, buttons

    def history_view(self, limit=15):
        entries = self.recent_entries(limit)
        text = "🧾 **Historial**\n\n"
        if not entries:
            text += "_Todavía no hay nada registrado._"
        for e in entries:
            extra = f" → {e['disk']}" if e.get('disk') else ""
            text += f"{KIND_ICON.get(e['kind'], '•')} `{fmt_when(e['ts'])}` {short(e['title'], 44)}{extra}\n"
        text += "\n📚 añadido · 📥 descargado · ♻️ duplicado · ⚠️ sin colocar · ❓ no identificado · ❌ fallido"
        return text, [[Button.inline("🔙 Volver", data="menu_downloads")]]

    # ---------- biblioteca ----------
    def library_view(self):
        text = "📚 **Biblioteca**\n\n"
        for d in self.disks_status():
            if d.get('error'):
                text += f"**{d['disk']}** · no accesible\n\n"
                continue
            text += (f"**{d['disk']}** · {hsize(d['free'])} libres de {hsize(d['total'])}\n"
                     f"{bar(d['used_pct'], 16)} {d['used_pct']}%\n")
        placed = self.recent_entries(5, kinds=('placed',))
        if placed:
            text += "\n🕘 **Últimas añadidas**\n"
            for e in placed:
                text += f"`{fmt_when(e['ts'])}` {short(e['title'], 40)} → {e.get('disk')}\n"
        pending = self.pending_entries()
        text += f"\n⚠️ Pendientes de revisar: **{len(pending)}**" if pending else "\n✅ Nada pendiente de revisar"
        text += f"\n_Reserva por disco: {hsize(library.RESERVE_BYTES)}_"
        buttons = [
            [Button.inline("🕘 Últimas 20", data="menu_lib_recent"),
             Button.inline(f"⚠️ Pendientes ({len(pending)})", data="menu_lib_pending")],
            [Button.inline("🔎 Buscar", data="menu_lib_search"), Button.inline("📊 Estadísticas", data="menu_lib_stats")],
            [Button.inline("🔙 Volver", data="menu_main")],
        ]
        return text, buttons

    def library_recent_view(self):
        entries = self.recent_entries(20, kinds=('placed',))
        text = "🕘 **Últimas añadidas a la biblioteca**\n\n"
        if not entries:
            text += "_Todavía no se ha añadido nada._"
        for e in entries:
            icon = "📺" if e.get('series') else "🎬"
            nueva = " 🆕" if e.get('new') and e.get('series') else ""
            text += f"{icon} `{fmt_when(e['ts'])}` {short(e['title'], 42)} → {e.get('disk')}{nueva}\n"
        return text, [[Button.inline("🔙 Volver", data="menu_library")]]

    def pending_view(self):
        entries = self.pending_entries()
        text = "⚠️ **Pendientes de revisar**\n\n"
        if not entries:
            text += "✅ No hay nada pendiente."
            return text, [[Button.inline("🔙 Volver", data="menu_library")]]
        text += "Elige uno para ver el motivo y qué hacer con él:\n\n"
        buttons = []
        for e in entries[:12]:
            text += f"{KIND_ICON[e['kind']]} **#{e['id']}** {short(e['title'], 40)}\n   _{e.get('reason', '')}_\n"
            buttons.append([Button.inline(f"{KIND_ICON[e['kind']]} #{e['id']} {short(e['title'], 28)}",
                                          data=f"pend_view_{e['id']}")])
        if len(entries) > 12:
            text += f"\n… y {len(entries) - 12} más"
        buttons.append([Button.inline("🔙 Volver", data="menu_library")])
        return text, buttons

    def pending_detail_view(self, e):
        where = os.path.relpath(e['path'], os.path.dirname(os.path.dirname(e['path'])))
        text = (f"{KIND_ICON[e['kind']]} **Pendiente #{e['id']}**\n\n"
                f"📄 `{e['title']}`\n"
                f"📁 `{where}`\n"
                f"📦 {hsize(e.get('size', 0))} · `{fmt_when(e['ts'])}`\n"
                f"❔ _{e.get('reason', '')}_\n\n")
        if e['kind'] == 'duplicate':
            text += "Ya tienes este título en la misma calidad. Puedes borrarlo o dejarlo donde está."
        else:
            text += "Puedes reintentar (por ejemplo tras liberar espacio) o decir si es peli o serie."
        i = e['id']
        buttons = [
            [Button.inline("✍️ Indicar qué es", data=f"pend_ask_{i}"), Button.inline("🔁 Reintentar", data=f"pend_retry_{i}")],
            [Button.inline("🎬 Es una peli", data=f"pend_movie_{i}"), Button.inline("📺 Es una serie", data=f"pend_series_{i}")],
            [Button.inline("🙈 Ignorar", data=f"pend_ignore_{i}"), Button.inline("🗑️ Borrar archivo", data=f"pend_delask_{i}")],
            [Button.inline("🔙 Volver", data="menu_lib_pending")],
        ]
        return text, buttons

    async def library_stats_view(self):
        st = await asyncio.to_thread(library.stats)
        text = "📊 **Estadísticas de la biblioteca**\n\n"
        tot_s = tot_e = tot_m = 0
        for disk, d in st.items():
            text += f"**{disk}** · {d['series']} series ({d['episodes']} caps) · {d['movies']} pelis\n"
            for cat, desc in sorted(d['categories'].items()):
                text += f"   └ {cat}: {desc}\n"
            tot_s += d['series']; tot_e += d['episodes']; tot_m += d['movies']
        text += f"\n**Total:** {tot_s} series · {tot_e} capítulos · {tot_m} películas"
        return text, [[Button.inline("🔙 Volver", data="menu_library")]]

    async def search_text(self, query):
        results = await asyncio.to_thread(library.search, query)
        if not results:
            return f"🔎 No encuentro nada con «{query}» en {', '.join(d for d, _ in library.LIBRARY_DISKS)}."
        text = f"🔎 **Resultados para «{query}»**\n\n"
        for r in results:
            if r['kind'] == 'serie':
                temps = ", ".join(str(int(s)) for s in r['seasons']) if r['seasons'] else "-"
                text += f"📺 **{r['name']}**\n   {r['disk']}/{r['category']} · {r['episodes']} caps · temporadas {temps}\n"
            else:
                text += f"🎬 {r['name']}\n   {r['disk']}/{r['category']}\n"
        return text

    # ---------- sistema ----------
    @staticmethod
    def _filebot_version():
        try:
            cfg = os.path.join(os.path.dirname(os.path.dirname(os.environ.get(
                "FILEBOT_BIN", "/opt/filebot-pro/bin/filebot-pro"))), "lib", "app", "filebot-pro.cfg")
            for line in open(cfg, encoding='utf-8'):
                if 'jpackage.app-version=' in line:
                    return line.split('=', 2)[-1].strip()
        except OSError:
            pass
        return "?"

    @staticmethod
    def _memory():
        try:
            for line in open('/proc/self/status'):
                if line.startswith('VmRSS:'):
                    return hsize(int(line.split()[1]) * 1024)
        except OSError:
            pass
        return "?"

    def system_view(self):
        up = fmt_duration(time.time() - self.start_time)
        mode = "Híbrido (descargas con tu cuenta)" if getattr(self, 'user_client', None) else "Solo bot"
        errors = list(getattr(self, 'recent_errors', []))
        text = ("🛠️ **Sistema**\n\n"
                f"🤖 Daemon {DAEMON_VERSION} · ⏱️ en marcha {up}\n"
                f"📡 Telethon {telethon_version} · 🐍 Python {platform.python_version()}\n"
                f"🏷️ FileBot Pro {self._filebot_version()}\n"
                f"🧠 Memoria: {self._memory()}\n"
                f"👤 Modo: {mode}\n"
                f"⚡ Velocidad máxima: {ACCOUNT_SPEED_NOTE}\n"
                f"🧯 Avisos/errores recientes: {len(errors)}")
        buttons = [
            [Button.inline("🧯 Últimos errores", data="menu_errors"), Button.inline("👤 Modo de cuenta", data="menu_config_account")],
            [Button.inline("🔄 Reiniciar bot", data="cmd_restart_bot")],
            [Button.inline("🔙 Volver", data="menu_main")],
        ]
        return text, buttons

    def errors_view(self):
        errors = list(getattr(self, 'recent_errors', []))[-12:]
        text = "🧯 **Últimos avisos y errores**\n\n"
        if not errors:
            text += "✅ Sin errores desde el último arranque."
        for ts, level, msg in reversed(errors):
            text += f"`{fmt_when(ts)}` {'❌' if level >= logging.ERROR else '⚠️'} {short(msg.splitlines()[0], 120)}\n"
        return text, [[Button.inline("🔙 Volver", data="menu_system")]]

    # ---------- acciones ----------
    async def pause_all(self):
        n = 0
        for item in list(self.active_downloads.values()) + self._queued_items():
            link = item['download_media'].get_link()
            if link not in self.paused_links:
                self.paused_links.add(link)
                n += 1
        return f"⏸️ {n} descarga(s) pausadas. Las activas se detienen en el próximo bloque."

    async def resume_link(self, link, queue):
        self.paused_links.discard(link)
        item = self.paused_items.pop(link, None)
        if item is not None:
            await queue.put(item)
            return True
        return False

    async def resume_all(self, queue):
        links = set(self.paused_links) | set(self.paused_items.keys())
        n = 0
        for link in links:
            if await self.resume_link(link, queue):
                n += 1
        self.paused_links.clear()
        return f"▶️ Reanudadas: {n} en cola de nuevo." if n else "▶️ No había descargas pausadas."

    async def _pending_action(self, event, client, action, entry):
        peer = self.raw_channel_id
        if action == 'ignore':
            self.resolve_history_entry(entry['id'], 'ignorado')
            return "🙈 Marcado como revisado; el archivo se queda donde está."
        if action == 'del':
            try:
                os.remove(entry['path'])
                try:
                    os.removedirs(os.path.dirname(entry['path']))
                except OSError:
                    pass
            except OSError as e:
                return f"❌ No se pudo borrar: {e}"
            self.resolve_history_entry(entry['id'], 'borrado')
            return f"🗑️ Borrado: `{entry['title']}`"
        force = {'retry': entry.get('guess'), 'movie': 'movie', 'series': 'series'}[action]
        self.resolve_history_entry(entry['id'], 'reintentado')
        await event.edit(f"🔁 Procesando `{entry['title']}`…\n_El resultado llegará como mensaje nuevo._",
                         buttons=[Button.inline("🔙 Pendientes", data="menu_lib_pending")])
        asyncio.create_task(self.rename_media_file(entry['path'], client, peer,
                                                   force_type=force, allow_staged=True))
        return None

    async def handle_panel_callback(self, event, data, queue, client):
        """Devuelve True si el botón era del panel nuevo y ya se atendió."""
        if data.startswith('q_'):
            return await self.handle_question_callback(event, data)
        if data.startswith(('menu_', 'cmd_', 'pend_', 'clear_chat')):
            await self.adopt_panel_message(event)
            self.panel_view = 'other'
        views = {
            'menu_main': self.main_menu_content,
            'menu_downloads': self.downloads_view,
            'menu_cleanup': self.cleanup_view,
            'menu_history': self.history_view,
            'menu_library': self.library_view,
            'menu_lib_recent': self.library_recent_view,
            'menu_lib_pending': self.pending_view,
            'menu_system': self.system_view,
            'menu_maintenance': self.system_view,
            'menu_errors': self.errors_view,
            'menu_files': self.downloads_view,
            'menu_status': self.downloads_view,
        }
        try:
            if data in views:
                text, buttons = views[data]()
                self.panel_view = {'menu_main': 'main', 'menu_downloads': 'downloads',
                                   'menu_files': 'downloads', 'menu_status': 'downloads'}.get(data, 'other')
                await self._safe_edit(event, text, buttons)
                self._panel_last_text = text
                return True
            if data == 'menu_lib_stats':
                await event.answer("Contando… puede tardar unos segundos")
                text, buttons = await self.library_stats_view()
                await self._safe_edit(event, text, buttons)
                return True
            if data == 'menu_lib_search':
                await self._safe_edit(event, "🔎 **Buscar en la biblioteca**\n\nEscribe en el chat:\n`/buscar nombre`\n\n"
                                      "Ejemplo: `/buscar bear` · `/buscar dragon ball`",
                                      [[Button.inline("🔙 Volver", data="menu_library")]])
                return True
            if data == 'cmd_clear_chat':
                await self._safe_edit(event, "🧽 **¿Limpiar todo el chat?**\n\n"
                                      "Se borran todos los mensajes del canal y queda solo este panel.\n"
                                      "Se conservan las preguntas sin contestar y el progreso de las descargas en marcha.\n\n"
                                      "_No afecta a ningún archivo, solo a los mensajes._",
                                      [[Button.inline("✅ Sí, limpiar", data="clear_chat_go"),
                                        Button.inline("❌ No", data="menu_main")]])
                return True
            if data == 'clear_chat_go':
                await event.answer("Limpiando…")
                res = await self.clear_chat()
                text, buttons = self.main_menu_content()
                self.panel_view = 'main'
                if res is None:
                    note = "⚠️ Para limpiar el chat hace falta el modo híbrido (cuenta de usuario).\n\n"
                else:
                    note = f"🧽 Chat limpio: {res[0]} mensajes borrados" + (f", {res[1]} en uso conservados" if res[1] else "") + "\n\n"
                await self._safe_edit(event, note + text, buttons)
                self._panel_last_text = note + text
                return True
            if data in ('cmd_pause_all', 'cmd_resume_all'):
                msg = await self.pause_all() if data == 'cmd_pause_all' else await self.resume_all(queue)
                await event.answer(msg[:190])
                await asyncio.sleep(0.5)
                text, buttons = self.downloads_view()
                self.panel_view = 'downloads'
                await self._safe_edit(event, text, buttons)
                self._panel_last_text = text
                return True
            if data.startswith('pend_'):
                _, action, num = data.split('_', 2)
                entry = self.get_history_entry(int(num))
                if not entry or not os.path.exists(entry.get('path', '')):
                    await event.answer("Ese pendiente ya no existe", alert=True)
                    text, buttons = self.pending_view()
                    await self._safe_edit(event, text, buttons)
                    return True
                if action == 'view':
                    text, buttons = self.pending_detail_view(entry)
                    await self._safe_edit(event, text, buttons)
                    return True
                if action == 'ask':
                    await self.ask_for_pending(entry)
                    await event.answer("Te he preguntado justo debajo")
                    await self.refresh_sticky_menu()
                    return True
                if action == 'delask':
                    await self._safe_edit(event, f"⚠️ **¿Borrar este archivo?**\n\n`{entry['title']}`\n\nNo se puede deshacer.",
                                          [[Button.inline("✅ Sí, borrar", data=f"pend_del_{entry['id']}"),
                                            Button.inline("❌ No", data=f"pend_view_{entry['id']}")]])
                    return True
                result = await self._pending_action(event, client, action, entry)
                if result:
                    text, buttons = self.pending_view()
                    await self._safe_edit(event, f"{result}\n\n{text}", buttons)
                return True
        except Exception as e:
            logger.error("Error en el panel (%s): %s", data, e, exc_info=True)
            try:
                await event.answer("❌ Error, mira Sistema → Últimos errores", alert=True)
            except Exception:
                pass
            return True
        return False

    @staticmethod
    async def _safe_edit(event, text, buttons):
        try:
            await event.edit(text, buttons=buttons)
        except Exception as e:
            if "not modified" in str(e).lower():
                try:
                    await event.answer("Sin cambios")
                except Exception:
                    pass
            else:
                raise
