# -*- coding: utf-8 -*-
"""
Preguntas al usuario cuando el nombre de un vídeo no basta para identificarlo
(p.ej. '3.mp4' o 'KRAKEN.mp4' sin año). Se pregunta al encolar (o al arrancar para lo
que ya estaba en cola), mientras se descarga. La respuesta se aplica al terminar:
el archivo se renombra con la pista ('VisionQuest S01E03.mp4', 'Kraken (2025).mp4') y
sigue el flujo normal de FileBot + biblioteca.
"""
import asyncio
import json
import logging
import os
import re

from telethon import Button

from . import media_links
from .rename_manager import VIDEO_EXTENSIONS, YEAR_RE, looks_like_episode, series_query

logger = logging.getLogger(__name__)

# Palabras que no cuentan como título (calidad, códec, fuente…)
NOISE = re.compile(r'(?i)^(\d{3,4}p|[hx]26[45]|hevc|avc|av1|web(-?dl|rip)?|bluray|bdrip|hdtv|hdr|dv|'
                   r'10bit|aac|ac3|dts|esp|spa|cast(ellano)?|lat(ino)?|dual|multi|sub(s|titulado)?|vose|mkv|mp4|avi)$')
SERIES_ANSWER = re.compile(r'(?i)^(?P<name>.+?)\s+(?:s|t|temp(?:orada)?\s*)?(?P<s>\d{1,2})'
                           r'(?:\s*(?:x|e|cap(?:itulo)?)\s*(?P<e>\d{1,3}))?\s*$')
MOVIE_ANSWER = re.compile(r'^(?P<t>.+?)(?:\s*\(?(?P<y>(?:19|20)\d{2})\)?)?\s*$')
# Temporada y capítulo que acompañan a un enlace: '1x03', 's01e03', 't1 c3'
EPISODE_ANSWER = re.compile(r'(?i)(?<![0-9a-z])[st]?\s*(?P<s>\d{1,2})\s*(?:x|e|c|cap(?:itulo)?)\s*(?P<e>\d{1,3})(?![0-9])')
# Solo la temporada: '2', 't2', 'temporada 2' (el capítulo sale del nombre del archivo)
SEASON_ANSWER = re.compile(r'(?i)^\s*(?:s|t|temp(?:orada)?\s*)?(?P<s>\d{1,2})\s*$')
LINK_PROMPT = ("\n\n🔗 **Envía el enlace en un mensaje a este chat**, por ejemplo:\n"
               "`https://www.themoviedb.org/movie/1559984-my-weird-school`\n"
               "`https://www.thetvdb.com/series/the-walking-dead`")


def title_words(filename):
    stem = os.path.splitext(filename)[0]
    words = [w for w in re.split(r'[\s._\-()\[\]]+', stem) if w]
    return [w for w in words if not w.isdigit() and not NOISE.match(w) and len(w) >= 2]


def needs_question(filename):
    """True si es un vídeo cuyo nombre no permite identificarlo con seguridad."""
    if os.path.splitext(filename)[1].lower() not in VIDEO_EXTENSIONS:
        return False
    if looks_like_episode(filename):
        return False
    return not (YEAR_RE.search(filename) and title_words(filename))


def number_in_name(filename):
    nums = re.findall(r'\d{1,3}', os.path.splitext(filename)[0])
    nums = [int(n) for n in nums if not YEAR_RE.fullmatch(n)]
    return nums[-1] if nums else None


def episode_for_link(text, filename):
    """(temporada, capítulo) para un enlace de serie: lo escrito manda y, si no, el nombre
    del archivo. None si no se puede saber, para preguntarlo en vez de adivinar."""
    m = EPISODE_ANSWER.search(text or "")
    if m:
        return int(m.group('s')), int(m.group('e'))
    m = SEASON_ANSWER.match(text or "")
    if m:
        ep = number_in_name(filename)
        return (int(m.group('s')), ep) if ep else None
    info = series_query(filename)
    return (info['season'], info['episode']) if info else None


class QuestionsManagerMixin:

    def __init_questions_manager__(self):
        self.media_questions = {}       # clave (link o 'pend_<id>') -> pregunta
        self.question_seq = 0
        self.awaiting_input = None      # {'key':…, 'type': 'series'|'movie'}
        self.last_series_hint = None    # {'name':…, 'season':…}
        self.pending_link = None        # enlace pegado a la espera de elegir archivo
        self.questions_file = os.path.join(os.path.dirname(self.config_file), "questions.json")
        self._question_runtime = {}     # clave -> {'notify_msg', 'base_text'} (solo en memoria)
        try:
            with open(self.questions_file, 'r', encoding='utf-8') as f:
                st = json.load(f)
            self.media_questions = st.get('questions', {})
            self.question_seq = st.get('seq', 0)
            self.last_series_hint = st.get('last_series_hint')
        except (OSError, ValueError):
            pass

    def _save_questions(self):
        try:
            with open(self.questions_file, 'w', encoding='utf-8') as f:
                json.dump({'questions': self.media_questions, 'seq': self.question_seq,
                           'last_series_hint': self.last_series_hint}, f, ensure_ascii=False, indent=1)
        except OSError as e:
            logger.debug("No se pudieron guardar las preguntas: %s", e)

    def _q_by_id(self, qid):
        for key, q in self.media_questions.items():
            if q['id'] == qid:
                return key, q
        return None, None

    def question_for(self, key):
        return self.media_questions.get(key)

    # ---------- crear la pregunta ----------
    def _question_text(self, q, extra=""):
        if q.get('need_link'):
            return (f"🔗 **Necesito el enlace de este archivo**\n📄 `{q['filename']}`\n\n"
                    f"No lo encuentro con ese nombre. Envía el enlace de TMDB o TheTVDB "
                    f"en un mensaje a este chat y lo coloco." + extra)
        text = f"❓ **¿Qué es este archivo?**\n📄 `{q['filename']}`\n"
        if q.get('caption'):
            text += f"💬 _{q['caption'][:200]}_\n"
        text += ("\nDímelo y lo colocaré bien en la biblioteca al terminar la descarga."
                 "\n🔗 Lo más seguro es enviar su enlace de TMDB o TheTVDB en un mensaje a este chat.")
        return text + extra

    def _question_buttons(self, q):
        i = q['id']
        rows = []
        sug = q.get('suggest')
        if sug and sug.get('type') == 'movie':
            label = f"✅ 🎬 {sug['title']}" + (f" ({sug['year']})" if sug.get('year') else "")
            rows.append([Button.inline(label[:60], data=f"q_sug_{i}")])
        elif sug:
            rows.append([Button.inline(f"✅ 📺 {sug['name']} · {sug['season']}x{sug['episode']:02d}"[:60], data=f"q_sug_{i}")])
        rows.append([Button.inline("🔗 Dar enlace de TMDB o TVDB", data=f"q_link_{i}")])
        rows.append([Button.inline("📺 Capítulo de serie", data=f"q_series_{i}"),
                     Button.inline("🎬 Película", data=f"q_movie_{i}")])
        hint = self.last_series_hint
        ep = number_in_name(q['filename'])
        if hint and ep:
            rows.append([Button.inline(f"📺 {hint['name']} T{hint['season']} · cap {ep}", data=f"q_same_{i}")])
        rows.append([Button.inline("⏭️ No renombrar", data=f"q_skip_{i}")])
        return rows

    async def ensure_question(self, key, filename, caption=None, file_path=None):
        """Pregunta (una sola vez por clave) si el nombre no basta para identificarlo."""
        if not getattr(self, 'auto_rename', False) or not filename or filename == "unknown":
            return None
        if key in self.media_questions or not needs_question(filename):
            return None
        self.question_seq += 1
        q = {'id': self.question_seq, 'filename': filename, 'caption': (caption or "").strip(),
             'answer': None, 'file': file_path, 'msg_id': None}
        self.media_questions[key] = q
        q['suggest'] = await self.ai_suggestion(filename, caption)
        try:
            msg = await self.client.send_message(self.raw_channel_id, self._question_text(q),
                                                 buttons=self._question_buttons(q))
            q['msg_id'] = msg.id
        except Exception as e:
            logger.warning("No se pudo enviar la pregunta para %s: %s", filename, e)
        self._save_questions()
        logger.info("❓ Pregunta #%s: ¿qué es %s?", q['id'], filename)
        return q

    async def ask_for_queued(self, queue):
        """Al arrancar: pregunta por lo que ya estaba en cola."""
        await asyncio.sleep(5)
        for item in list(queue._queue):
            dm = item['download_media']
            link = dm.get_link()
            try:
                name = item.get('filename')
                caption = None
                if not name or name == "unknown":
                    msg = await dm.get_message(self.download_client)
                    if not msg or not msg.media:
                        continue
                    name = self.get_filename(msg)
                    caption = msg.message
                    item['filename'] = name
                await self.ensure_question(link, name, caption)
            except Exception as e:
                logger.debug("No se pudo revisar %s: %s", link, e)
        # lo no identificado de las últimas 24 h que aún no tiene pregunta
        import time
        for entry in self.pending_entries():
            key = f"pend_{entry['id']}"
            if entry['kind'] == 'unidentified' and time.time() - entry['ts'] < 86400 and key not in self.media_questions:
                try:
                    await self.ask_for_pending(entry)
                except Exception as e:
                    logger.debug("No se pudo preguntar por el pendiente %s: %s", entry['id'], e)
        if any(q['answer'] is None for q in self.media_questions.values()):
            await self.refresh_sticky_menu()

    # ---------- botones ----------
    async def handle_question_callback(self, event, data):
        _, action, num = data.split('_', 2)
        key, q = self._q_by_id(int(num))
        if q is None:
            await event.answer("Esta pregunta ya no existe", alert=True)
            try:
                await event.delete()
            except Exception:
                pass
            return True
        if q.get('kind') == 'replace':
            if action in ('rep', 'dis', 'keep'):
                await event.answer("Hecho" if action != 'rep' else "Reemplazando…")
                await self._answer_replace(event, key, q, action)
            return True
        if action == 'series':
            self.awaiting_input = {'key': key, 'type': 'series'}
            ep = number_in_name(q['filename'])
            ej = f"`VisionQuest 1` → 1x{ep:02d}" if ep else "`VisionQuest 1x03`"
            await event.edit(self._question_text(q, f"\n\n✍️ **Escribe la serie y la temporada**, por ejemplo {ej}\n"
                                                    f"_(o con capítulo: `VisionQuest 1x03`)_"),
                             buttons=[[Button.inline("↩️ Atrás", data=f"q_back_{q['id']}")]])
        elif action == 'movie':
            self.awaiting_input = {'key': key, 'type': 'movie'}
            await event.edit(self._question_text(q, "\n\n✍️ **Escribe el título y el año**, por ejemplo `Kraken 2025`"),
                             buttons=[[Button.inline("↩️ Atrás", data=f"q_back_{q['id']}")]])
        elif action == 'use':
            pending, self.pending_link = self.pending_link, None
            try:
                await event.delete()
            except Exception:
                pass
            if pending:
                self.awaiting_input = None
                await self._handle_media_link(pending['link'], pending['text'], key)
            else:
                await event.answer("Vuelve a pegar el enlace", alert=True)
        elif action == 'link':
            self.awaiting_input = {'key': key, 'type': 'link'}
            await event.edit(self._question_text(q, LINK_PROMPT),
                             buttons=[[Button.inline("↩️ Atrás", data=f"q_back_{q['id']}")]])
        elif action == 'back':
            self.awaiting_input = None
            q['answer'] = None
            await event.edit(self._question_text(q), buttons=self._question_buttons(q))
        elif action == 'sug' and q.get('suggest'):
            sug = dict(q['suggest'])
            sug.setdefault('type', 'series')
            await self._set_answer(key, q, sug)
        elif action == 'same':
            ep = number_in_name(q['filename'])
            hint = self.last_series_hint
            answer = {'type': 'series', 'name': hint['name'], 'season': hint['season'], 'episode': ep}
            if hint.get('id'):
                answer.update(db=hint['db'], id=hint['id'], linked=True)
            await self._set_answer(key, q, answer)
        elif action == 'skip':
            await self._set_answer(key, q, {'type': 'skip'})
        self._save_questions()
        return True

    # ---------- texto escrito ----------
    async def handle_question_text(self, event):
        """Si hay una pregunta esperando texto, consume el mensaje. Devuelve True si lo usó."""
        aw = self.awaiting_input
        text = (event.message.message or "").strip() if event.message else ""
        if not text or text.startswith('/'):
            return False
        link = media_links.find_link(text)
        if link:
            await self._delete_message(event)
            await self._handle_media_link(link, text)
            return True
        # Un mensaje con vista previa solo llega aquí por si traía un enlace de TMDB o TVDB
        if not aw or event.media:
            return False
        key = aw['key']
        q = self.media_questions.get(key)
        if q is None:
            self.awaiting_input = None
            return False
        await self._delete_message(event)
        if aw['type'] == 'link':
            await self._edit_question(q, "\n\n⚠️ Eso no es un enlace de TMDB ni de TheTVDB." + LINK_PROMPT,
                                      [[Button.inline("↩️ Atrás", data=f"q_back_{q['id']}")]])
            return True
        if aw['type'] == 'series_link':
            episode = episode_for_link(text, q['filename'])
            if not episode:
                await self._edit_question(q, "\n\n⚠️ No lo entiendo. Escribe por ejemplo `1x03`.",
                                          [[Button.inline("↩️ Atrás", data=f"q_back_{q['id']}")]])
                return True
            self.awaiting_input = None
            await self._set_answer(key, q, self._link_answer(aw['target'], episode))
            return True
        if aw['type'] == 'series':
            m = SERIES_ANSWER.match(text)
            ep = None
            if m:
                ep = int(m.group('e')) if m.group('e') else number_in_name(q['filename'])
            if not m or not ep:
                await self._edit_question(q, "\n\n⚠️ No lo entiendo. Escribe por ejemplo `VisionQuest 1x03`.",
                                          [[Button.inline("↩️ Atrás", data=f"q_back_{q['id']}")]])
                return True
            answer = {'type': 'series', 'name': m.group('name').strip(), 'season': int(m.group('s')), 'episode': ep}
        else:
            m = MOVIE_ANSWER.match(text)
            answer = {'type': 'movie', 'title': m.group('t').strip(), 'year': m.group('y')}
        self.awaiting_input = None
        await self._set_answer(key, q, answer)
        return True

    @staticmethod
    async def _delete_message(event):
        try:
            await event.message.delete()
        except Exception:
            pass

    # ---------- enlaces de TMDB y TheTVDB ----------
    async def _link_target(self):
        """A qué archivo se refiere un enlace pegado: la pregunta que espera texto o, si solo
        hay un archivo posible, ese. Devuelve (clave, pregunta, nº de archivos posibles): con
        varios no se adivina, porque colocaría un archivo como otra película."""
        aw = self.awaiting_input
        if aw and aw['key'] in self.media_questions:
            return aw['key'], self.media_questions[aw['key']], 1
        open_questions = [key for key, q in self.media_questions.items()
                          if q['answer'] is None and q.get('kind') != 'replace']
        if len(open_questions) == 1:
            return open_questions[0], self.media_questions[open_questions[0]], 1
        if open_questions:
            return None, None, len(open_questions)
        unidentified = [e for e in self.pending_entries() if e['kind'] == 'unidentified']
        if len(unidentified) == 1:
            await self.ask_for_pending(unidentified[0])
            key = f"pend_{unidentified[0]['id']}"
            return key, self.media_questions[key], 1
        return None, None, len(unidentified)

    async def _ask_which_file(self, link, text):
        """Hay varios archivos esperando: el usuario elige para cuál es el enlace."""
        questions = sorted((q for q in self.media_questions.values()
                            if q['answer'] is None and q.get('kind') != 'replace'),
                           key=lambda q: -q['id'])
        if not questions:
            await self._notify(self.client, self.raw_channel_id,
                               "🔗 Hay varios archivos pendientes. Abre Biblioteca → Pendientes, "
                               "elige uno y pulsa **Dar enlace**.")
            return
        self.pending_link = {'link': link, 'text': text}
        buttons = [[Button.inline(f"📄 {q['filename'][:50]}", data=f"q_use_{q['id']}")] for q in questions[:8]]
        await self.client.send_message(self.raw_channel_id, "🔗 **¿Para qué archivo es este enlace?**",
                                       buttons=buttons)

    @staticmethod
    def _link_answer(target, episode=None):
        name = target['title'] or f"{target['db']} {target['id']}"
        answer = {'db': target['db'], 'id': target['id'], 'linked': bool(target['title'])}
        if target['kind'] == 'movie':
            return {**answer, 'type': 'movie', 'title': name, 'year': None}
        return {**answer, 'type': 'series', 'name': name, 'season': episode[0], 'episode': episode[1]}

    async def _handle_media_link(self, link, text, key=None):
        q = self.media_questions.get(key) if key else None
        if q is None:
            key, q, candidates = await self._link_target()
        if q is None:
            if candidates:
                await self._ask_which_file(link, text)
                return
            await self._notify(self.client, self.raw_channel_id,
                               "🔗 He recibido el enlace, pero no hay ningún archivo esperando identificación.")
            return
        try:
            target = await asyncio.to_thread(media_links.resolve, link)
        except media_links.LinkError as e:
            await self._edit_question(q, f"\n\n⚠️ {e}", self._question_buttons(q))
            return
        if target['kind'] == 'movie':
            self.awaiting_input = None
            await self._set_answer(key, q, self._link_answer(target))
            return
        episode = episode_for_link(media_links.text_without_link(text, link), q['filename'])
        if not episode:
            self.awaiting_input = {'key': key, 'type': 'series_link', 'target': target}
            await self._edit_question(q, f"\n\n📺 **{target['title'] or target['id']}**\n"
                                         f"✍️ ¿Qué temporada y capítulo es? Escríbelo así: `1x03`",
                                      [[Button.inline("↩️ Atrás", data=f"q_back_{q['id']}")]])
            return
        self.awaiting_input = None
        await self._set_answer(key, q, self._link_answer(target, episode))

    async def _edit_question(self, q, extra, buttons):
        if q.get('msg_id'):
            try:
                await self.client.edit_message(self.raw_channel_id, q['msg_id'], self._question_text(q, extra), buttons=buttons)
            except Exception as e:
                logger.debug("No se pudo editar la pregunta: %s", e)

    @staticmethod
    def describe_answer(a):
        link = " 🔗" if a.get('id') else ""
        if a['type'] == 'series':
            return f"📺 {a['name']} · {a['season']}x{a['episode']:02d}{link}"
        if a['type'] == 'movie':
            return f"🎬 {a['title']}" + (f" ({a['year']})" if a.get('year') else "") + link
        return "⏭️ No renombrar"

    async def _set_answer(self, key, q, answer):
        q['answer'] = answer
        if answer['type'] == 'series':
            self.last_series_hint = {'name': answer['name'], 'season': answer['season']}
            if answer.get('id'):
                self.last_series_hint.update(db=answer['db'], id=answer['id'])
        self._save_questions()
        if q.get('file') and os.path.exists(q['file']):
            if answer.get('id'):
                await self._notify(self.client, self.raw_channel_id,
                                   f"🔗 `{q['filename']}`\n→ {self.describe_answer(answer)}")
            await self._close_question(key, q)
            await self.apply_answer(key, q)
        else:
            await self._edit_question(q, f"\n\n✅ **Entendido:** {self.describe_answer(answer)}\n"
                                         f"_Lo colocaré cuando termine la descarga._",
                                      [[Button.inline("✏️ Cambiar", data=f"q_back_{q['id']}")]])
        # el atajo «como el anterior» aparece en las preguntas que aún esperan
        for other in self.media_questions.values():
            if (other is not q and other['answer'] is None and other.get('msg_id') and answer['type'] == 'series'
                    and other.get('kind') != 'replace'):
                await self._edit_question(other, "", self._question_buttons(other))

    async def _close_question(self, key, q):
        if q.get('msg_id'):
            try:
                await self.client.delete_messages(self.raw_channel_id, [q['msg_id']], revoke=True)
            except Exception:
                pass
        self.media_questions.pop(key, None)
        self._save_questions()

    # ---------- aplicar la respuesta ----------
    def hinted_path(self, path, answer):
        folder, name = os.path.split(path)
        ext = os.path.splitext(name)[1]
        if answer['type'] == 'series':
            new = f"{answer['name']} S{answer['season']:02d}E{answer['episode']:02d}{ext}"
        else:
            new = f"{answer['title']} ({answer['year']}){ext}" if answer.get('year') else f"{answer['title']}{ext}"
        new = new.replace('/', '-').replace(':', '')
        dest = os.path.join(folder, new)
        if os.path.exists(dest) and dest != path:
            root, e = os.path.splitext(dest)
            dest = f"{root}.{os.getpid()}{e}"
        return dest

    async def on_download_finished(self, key, file_path, notify_msg=None, base_text="", size=None):
        """Llamado al terminar una descarga con pregunta. Devuelve True si se encarga él."""
        q = self.media_questions.get(key)
        if q is None:
            return False
        q['file'] = file_path
        self._question_runtime[key] = {'notify_msg': notify_msg, 'base_text': base_text, 'size': size}
        self._save_questions()
        if q['answer'] is None:
            if notify_msg is not None and base_text:
                await self._notify(self.client, self.raw_channel_id,
                                   f"{base_text}\n❓ _Esperando tu respuesta para colocarlo (pregunta #{q['id']})_",
                                   edit_msg=notify_msg)
            return True
        await self._close_question(key, q)
        await self.apply_answer(key, q)
        return True

    async def apply_answer(self, key, q):
        rt = self._question_runtime.pop(key, {})
        answer = q['answer']
        path = q['file']
        if answer['type'] == 'skip':
            self.add_history('downloaded', os.path.basename(path), path=path, size=rt.get('size'))
            if rt.get('notify_msg') is not None:
                await self._notify(self.client, self.raw_channel_id,
                                   f"{rt.get('base_text', '')}\n⏭️ Sin renombrar, se queda en Descargas",
                                   edit_msg=rt['notify_msg'])
            return
        new_path = self.hinted_path(path, answer)
        try:
            os.rename(path, new_path)
        except OSError as e:
            logger.error("No se pudo aplicar la respuesta a %s: %s", path, e)
            return
        if key.startswith('pend_'):
            self.resolve_history_entry(int(key.split('_', 1)[1]), 'indicado')
        lookup = None
        if answer.get('id'):
            title = answer['title'] if answer['type'] == 'movie' else answer['name']
            lookup = {'db': answer['db'], 'id': answer['id'], 'title': title if answer.get('linked') else None}
        await self.rename_media_file(new_path, self.client, self.raw_channel_id,
                                     notify_msg=rt.get('notify_msg'), base_text=rt.get('base_text', ''),
                                     size=rt.get('size'), force_type=answer['type'] if answer['type'] == 'movie' else 'series',
                                     allow_staged=True, trusted=True, lookup=lookup)

    # ---------- ya lo tienes: ¿reemplazar? ----------
    @staticmethod
    def _fmt_info(i):
        from .history_manager import hsize
        parts = [i.get('res'), i.get('codec'), i.get('hdr')]
        txt = " · ".join(p for p in parts if p)
        txt = (txt + " · " if txt else "") + hsize(i.get('size', 0))
        if i.get('duration'):
            txt += f" · {int(i['duration'] // 60)} min"
        return txt

    async def ask_replace(self, entry, new_path, plan, is_episode):
        """El título ya existe: compara ambos archivos y pregunta qué hacer."""
        from . import library_manager as library
        from .history_manager import hsize
        old_info, new_info = await asyncio.gather(asyncio.to_thread(library.media_info, plan['existing']),
                                                  asyncio.to_thread(library.media_info, new_path))
        key = f"dup_{entry['id']}"
        self.question_seq += 1
        q = {'id': self.question_seq, 'kind': 'replace', 'filename': os.path.basename(new_path),
             'new_path': new_path, 'existing': plan['existing'], 'dest': plan['dest_if_replace'],
             'disk': plan.get('disk', '?'), 'entry_id': entry['id'], 'series': is_episode,
             'answer': None, 'msg_id': None}
        diff = new_info.get('size', 0) - old_info.get('size', 0)
        diff_txt = f"−{hsize(-diff)}" if diff < 0 else f"+{hsize(diff)}"
        what = "este capítulo" if is_episode else "esta película"
        q['text'] = (f"♻️ **Ya tienes {what}**\n"
                     f"{'📺' if is_episode else '🎬'} `{os.path.basename(plan['existing'])}`\n\n"
                     f"**El que tienes** ({q['disk']}):\n   {self._fmt_info(old_info)}\n"
                     f"**El nuevo:**\n   {self._fmt_info(new_info)}  ({diff_txt})\n\n"
                     f"¿Qué quieres hacer?")
        self.media_questions[key] = q
        i = q['id']
        buttons = [[Button.inline("♻️ Reemplazar", data=f"q_rep_{i}"),
                    Button.inline("🗑️ Descartar el nuevo", data=f"q_dis_{i}")],
                   [Button.inline("📁 Dejarlo aparte", data=f"q_keep_{i}")]]
        try:
            msg = await self.client.send_message(self.raw_channel_id, q['text'], buttons=buttons)
            q['msg_id'] = msg.id
        except Exception as e:
            logger.warning("No se pudo preguntar por el duplicado %s: %s", new_path, e)
        self._save_questions()
        logger.info("♻️ Pregunta #%s: ¿reemplazar %s?", q['id'], plan['existing'])
        return q

    async def _answer_replace(self, event, key, q, action):
        from . import library_manager as library
        new, old = q['new_path'], q['existing']
        if not os.path.exists(new):
            result = "⚠️ El archivo nuevo ya no está; no hago nada."
        elif action == 'rep':
            try:
                if not os.path.exists(old):
                    raise FileNotFoundError(old)
                trashed = await asyncio.to_thread(library.move_to_trash, old)
                try:
                    placed = await asyncio.to_thread(library.move_into_library, new, q['dest'])
                except Exception:
                    os.rename(trashed, old)  # deshacer: el antiguo vuelve a su sitio
                    raise
                lib_rel = placed.split(f"/{q['disk']}/", 1)[-1]
                self.add_history('placed', os.path.basename(placed), disk=q['disk'], path=placed,
                                 size=os.path.getsize(placed), series=q['series'], replaced=os.path.basename(old))
                result = (f"♻️ **Reemplazado en {q['disk']}**\n`{lib_rel}`\n"
                          f"_El antiguo queda {library.TRASH_DAYS} días en {q['disk']}/{library.TRASH_DIRNAME}_")
            except Exception as e:
                logger.error("Error reemplazando %s: %s", old, e, exc_info=True)
                result = f"❌ No se pudo reemplazar: {e}\n_No se ha tocado el que ya tenías._"
        elif action == 'dis':
            try:
                os.remove(new)
                try:
                    os.removedirs(os.path.dirname(new))
                except OSError:
                    pass
                result = f"🗑️ Descartado el nuevo. Te quedas con el que tenías."
            except OSError as e:
                result = f"❌ No se pudo borrar: {e}"
        else:
            result = f"📁 Se queda aparte en `renombrados` (Biblioteca → Pendientes)."
        if action in ('rep', 'dis'):
            self.resolve_history_entry(q['entry_id'], {'rep': 'reemplazado', 'dis': 'descartado'}[action])
        self.media_questions.pop(key, None)
        self._save_questions()
        try:
            await event.edit(q.get('text', '').split("\n\n¿Qué quieres hacer?")[0] + "\n\n" + result, buttons=None)
        except Exception:
            pass

    async def ai_suggestion(self, filename, caption=None):
        """Lo que la IA cree que es, en formato de respuesta sugerida (o None)."""
        from .ai_helper import ai_enabled, ask_ai
        if not ai_enabled():
            return None
        ai = await asyncio.to_thread(ask_ai, filename, caption)
        if not ai:
            return None
        if ai['type'] == 'movie':
            return {'type': 'movie', 'title': ai['title'], 'year': ai.get('year')}
        ep = ai.get('episode') or number_in_name(filename)
        if not ep:
            return None
        return {'type': 'series', 'name': ai['title'], 'season': ai.get('season') or 1, 'episode': ep,
                'confidence': ai.get('confidence') or 0}

    async def ask_for_pending(self, entry, suggest=None, need_link=False):
        """Preguntar qué es un archivo ya descargado (desde Pendientes, o en el momento si
        FileBot no lo identificó). suggest: {'name','season','episode'} para el botón rápido."""
        key = f"pend_{entry['id']}"
        self.media_questions.pop(key, None)
        self.question_seq += 1
        q = {'id': self.question_seq, 'filename': entry['title'], 'caption': "",
             'answer': None, 'file': entry['path'], 'msg_id': None, 'suggest': suggest,
             'need_link': need_link}
        self.media_questions[key] = q
        msg = await self.client.send_message(self.raw_channel_id, self._question_text(q), buttons=self._question_buttons(q))
        q['msg_id'] = msg.id
        self._save_questions()
