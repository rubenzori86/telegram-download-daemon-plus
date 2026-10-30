# -*- coding: utf-8 -*-
"""
Renombrado automático de películas y series con FileBot Pro, usando los mismos
patrones que tinyMediaManager, y colocación en la biblioteca (library_manager):

  Películas: ${title} (${year}) [${videoFormat}] [${videoCodec}][${3Dformat}]
  Series:    ${showTitle} (${showYear}) / ${showTitle} - ${seasonNr2}x${episodeNr2} - ${title}
"""
import asyncio
import logging
import os
import re
import shutil
import subprocess

from . import library_manager as library

logger = logging.getLogger(__name__)

FILEBOT_BIN = os.environ.get("FILEBOT_BIN", "/opt/filebot-pro/bin/filebot-pro")
FILEBOT_LANG = os.environ.get("FILEBOT_LANG", "es")
# Carpeta (dentro de la de descargas) donde se queda lo que no se puede colocar
RENAME_SUBFOLDER = os.environ.get("FILEBOT_OUTPUT_SUBFOLDER", "renombrados")
# Ruta real de la carpeta de descargas dentro de un montaje que también incluya la
# biblioteca (p.ej. /volume2): así mover al disco de la biblioteca es un rename instantáneo.
DOWNLOADS_HOST_PATH = os.environ.get("DOWNLOADS_HOST_PATH", "")
# Si es "1" solo se decide el destino, sin mover nada a la biblioteca
LIBRARY_DRY_RUN = os.environ.get("LIBRARY_DRY_RUN", "0") == "1"

VIDEO_EXTENSIONS = {'.mkv', '.mp4', '.avi', '.m4v', '.mov', '.wmv', '.ts', '.m2ts', '.mpg', '.mpeg', '.webm'}
# Por debajo de este tamaño se considera muestra/trailer y no se toca
MIN_VIDEO_BYTES = 50 * 1024 * 1024

# Primer nivel de la ruta: 'anim' si el género incluye Animación (-> *_Infantiles)
GENRE_PREFIX = '{any{genres.any{it =~ /(?i)anima/} ? "anim" : "live"}{"live"}}/'
# Colon replacement de tmm: películas "-", series "" (se elimina).
# Códec como en la biblioteca: h264/h265, y HEVC/AV1 en las 4K
MOVIE_FORMAT = GENRE_PREFIX + (
    '{n.replace(": ", " - ").replace(":", "-")} ({y}) [{vf}] '
    '[{any{def f = video[0].Format; f =~ /HEVC/ ? (vf == "2160p" ? "HEVC" : "h265") : '
    'f =~ /AVC/ ? "h264" : f =~ /AV1/ ? "AV1" : f.lower()}{""}}]'
    '{fn =~ /(?i)\\b3D\\b/ ? "[3D]" : ""}'
)
SERIES_FORMAT = GENRE_PREFIX + (
    '{n.replace(":", "")} ({y})/'
    '{n.replace(":", "")} - {s.pad(2)}x{e.pad(2)} - {t.replace(":", "")}'
)

# S01E02, 1x02, Temporada 1, Cap.102 / Capitulo 102, Episodio 3
EPISODE_PATTERNS = [
    re.compile(r'(?i)\bS\d{1,2}[ ._-]?E\d{1,3}\b'),
    re.compile(r'(?i)(?<![0-9a-z])\d{1,2}x\d{2,3}(?![0-9])'),
    re.compile(r'(?i)\btemporada[ ._-]?\d+'),
    re.compile(r'(?i)\bcap(?:itulo|ítulo)?[ ._-]*\d{3,4}\b'),
    re.compile(r'(?i)\bepisodio[ ._-]?\d+'),
]

MOVE_RE = re.compile(r'^\[(?:MOVE|RENAME)\] from \[(?P<src>.+)\] to \[(?P<dst>.+)\]$', re.M)
TEST_RE = re.compile(r'^\[TEST\] from \[(?P<src>.+)\] to \[(?P<dst>.+)\]$', re.M)
YEAR_RE = re.compile(r'(?<![0-9])((?:19|20)\d{2})(?![0-9pPiI])')


def is_video_file(path):
    return os.path.splitext(path)[1].lower() in VIDEO_EXTENSIONS


def looks_like_episode(filename):
    return any(p.search(filename) for p in EPISODE_PATTERNS)


AI_MIN_CONFIDENCE = 0.9


def ai_can_autoplace(suggest, filename):
    """La IA solo coloca sola capítulos de serie, con confianza alta y cuyo título cubre
    casi todo el nombre del archivo ('bluey_t3_c12' → Bluey sí; 'DragonBall Daima' →
    'Dragon Ball' no, le falta 'Daima'). Si no, su respuesta solo es un botón sugerido."""
    if suggest.get('type') != 'series' or (suggest.get('confidence') or 0) < AI_MIN_CONFIDENCE:
        return False
    info = series_query(filename)
    if info:
        name = library.normalize(info['name'])
    else:
        stem = os.path.splitext(filename)[0]
        words = re.split(r'[\s._\-()\[\]]+', stem)
        name = library.normalize(" ".join(w for w in words
                                          if w and not re.fullmatch(r'(?i)[tsce]?\d+|\d{3,4}p|x26[45]|h26[45]', w)))
    title = library.normalize(suggest.get('name', ''))
    return bool(name and title) and title in name and len(title) >= 0.85 * len(name)


SERIES_SPLIT_RE = re.compile(
    r'(?i)^(?P<name>.*?)[\s._\-\[(]*'
    r'(?:s(?P<s>\d{1,2})[\s._-]?e(?P<e>\d{1,3})|(?<![0-9])(?P<s2>\d{1,2})x(?P<e2>\d{2,3})(?![0-9]))')


def series_query(filename):
    """'Dragón Ball Daima 1x03 1080p .mkv' -> nombre 'Dragón Ball Daima', limpio
    'Dragon Ball Daima', temporada 1, capítulo 3. None si no hay SxE en el nombre."""
    import unicodedata
    stem = os.path.splitext(filename)[0]
    m = SERIES_SPLIT_RE.search(stem)
    if not m:
        return None
    name = re.sub(r'[._]+', ' ', m.group('name')).strip(' -[(')
    name = re.sub(r'\s+', ' ', name)
    if not name:
        return None
    clean = ''.join(c for c in unicodedata.normalize('NFKD', name) if not unicodedata.combining(c))
    season = int(m.group('s') or m.group('s2'))
    episode = int(m.group('e') or m.group('e2'))
    return {'name': name, 'clean': clean, 'season': season, 'episode': episode}


class RenameManagerMixin:
    """Mixin que renombra pelis y series con FileBot Pro y las coloca en la biblioteca."""

    is_video_file = staticmethod(is_video_file)

    def __init_rename_manager__(self):
        self.auto_rename = True
        self.rename_lock = asyncio.Lock()

    def set_auto_rename(self, enabled: bool):
        old = self.auto_rename
        self.auto_rename = enabled
        self.save_config()
        return f"✅ Renombrado automático (FileBot): {old} → {self.auto_rename}"

    def _to_host(self, path):
        """/downloads/x -> DOWNLOADS_HOST_PATH/x si ese montaje existe."""
        if DOWNLOADS_HOST_PATH and os.path.isdir(DOWNLOADS_HOST_PATH):
            base = os.path.abspath(self.download_folder)
            p = os.path.abspath(path)
            if p == base or p.startswith(base + os.sep):
                return DOWNLOADS_HOST_PATH + p[len(base):]
        return path

    @property
    def rename_output_folder(self):
        return os.path.join(self._to_host(self.download_folder), RENAME_SUBFOLDER)

    @property
    def rename_staging_folder(self):
        return os.path.join(self.rename_output_folder, ".staging")

    def _filebot_rename_sync(self, file_path, is_episode, strict=True, action="move", query=None):
        os.makedirs(self.rename_staging_folder, exist_ok=True)
        cmd = [
            FILEBOT_BIN, "-rename", file_path,
            "--db", "TheTVDB" if is_episode else "TheMovieDB",
            "--lang", FILEBOT_LANG,
            "--action", action,
            "--conflict", "index",
            "--output", self.rename_staging_folder,
            "--format", SERIES_FORMAT if is_episode else MOVIE_FORMAT,
        ]
        if not strict:
            cmd.append("-non-strict")
        if query:
            # una sola búsqueda: evita «Multiple queries … requires -non-strict»
            cmd += ["--q", query]
        # Sin atributos extendidos: identificar siempre por el nombre, no por lo que
        # FileBot guardó en el archivo (o en otro hardlink del mismo archivo)
        env = dict(os.environ)
        env["JAVA_TOOL_OPTIONS"] = (env.get("JAVA_TOOL_OPTIONS", "") + " -DuseExtendedFileAttributes=false").strip()
        return subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors='ignore', timeout=600, env=env,
        )

    def _identify_and_move_sync(self, file_path, is_episode, trusted=False):
        """Modo estricto primero. Para pelis, si falla y el nombre trae año, se
        reintenta en modo no estricto solo si la película encontrada es de ese año
        (evita falsos positivos tipo 'archivo_raro.mkv' -> 'Sin (1915)')."""
        if is_episode:
            return self._identify_episode_sync(file_path, trusted)
        result = self._filebot_rename_sync(file_path, is_episode)
        if result.returncode == 0 and MOVE_RE.search(result.stdout or ''):
            return result
        if trusted:
            # el usuario ha dicho qué es: se acepta la búsqueda no estricta
            return self._filebot_rename_sync(file_path, is_episode, strict=False)
        year = YEAR_RE.search(os.path.basename(file_path))
        if is_episode or not year:
            return result
        test = self._filebot_rename_sync(file_path, is_episode, strict=False, action="test")
        match = TEST_RE.search(test.stdout or '')
        if not match or f"({year.group(1)})" not in os.path.basename(match.group('dst')):
            logger.info("Coincidencia no estricta descartada para %s: %s",
                        os.path.basename(file_path), match.group('dst') if match else None)
            return result
        return self._filebot_rename_sync(file_path, is_episode, strict=False)

    def _identify_episode_sync(self, file_path, trusted=False):
        """Capítulos. FileBot, con nombres como 'Dragón Ball Daima 1x03' o 'DragonBall Daima',
        genera varias búsquedas ([Dragon Ball Daima, dragón ball daima]) y en modo estricto
        se niega a elegir. Por eso:
          1. si la serie ya está en la biblioteca, se busca exactamente esa (--q título carpeta);
          2. si no, el nombre limpio (sin tildes) en modo estricto;
          3. si no, la búsqueda libre de FileBot, aceptada solo si la serie encontrada
             coincide con el nombre del archivo (sin tildes, espacios ni signos)."""
        name = os.path.basename(file_path)

        def ok(r):
            return r is not None and r.returncode == 0 and MOVE_RE.search(r.stdout or '')

        def checked(query, strict, expect_title=None, expect_year=None):
            """Prueba primero (sin mover) y solo mueve si la serie encontrada es la esperada."""
            test = self._filebot_rename_sync(file_path, True, strict=strict, action="test", query=query)
            m = TEST_RE.search(test.stdout or '')
            if not m:
                return None
            show = os.path.basename(os.path.dirname(m.group('dst')))
            title, year = library.split_title_year(show)
            if expect_title and library.normalize(title) != library.normalize(expect_title):
                logger.info("Descartado para %s: «%s» no es «%s»", name, show, expect_title)
                return None
            if expect_year and year != expect_year:
                logger.info("Descartado para %s: «%s» no es del año %s", name, show, expect_year)
                return None
            return self._filebot_rename_sync(file_path, True, strict=strict, query=query)

        info = series_query(name)
        if info:
            found = library.find_series(info['name'], None)
            if found:
                title = found[2]
                _, year = library.split_title_year(os.path.basename(found[1]))
                logger.info("📺 %s → serie de la biblioteca «%s»", name, os.path.basename(found[1]))
                r = checked(title, True, title, year) or checked(title, False, title, year)
                if ok(r):
                    return r
                # la serie está en la biblioteca pero FileBot no la confirma: mejor preguntar
                return self._filebot_rename_sync(file_path, True, action="test", query=title)

        result = self._filebot_rename_sync(file_path, True)
        if ok(result) or not info:
            return result
        clean = info['clean']
        expect = None if trusted else info['name']
        r = checked(clean, True, expect) or checked(clean, False, expect) or checked(None, False, expect)
        return r if ok(r) else result

    def _place_sync(self, staged_path, is_episode):
        """Decide el destino en la biblioteca y mueve. Si no se puede, deja el archivo
        en renombrados/. Devuelve (ruta_final, plan)."""
        rel = os.path.relpath(staged_path, self.rename_staging_folder)
        is_anim = rel.split(os.sep, 1)[0] == 'anim'
        plan = library.plan_series(staged_path, is_anim) if is_episode else library.plan_movie(staged_path, is_anim)
        if plan['ok'] and not LIBRARY_DRY_RUN:
            try:
                return library.move_into_library(staged_path, plan['dest']), plan
            except Exception as e:
                plan = {'ok': False, 'reason': f"error moviendo a la biblioteca: {e}"}
        # Se queda en renombrados/ (sin el prefijo anim/live)
        fallback = os.path.join(self.rename_output_folder, rel.split(os.sep, 1)[1])
        os.makedirs(os.path.dirname(fallback), exist_ok=True)
        if os.path.exists(fallback):
            root, ext = os.path.splitext(fallback)
            fallback = f"{root}.1{ext}"
        shutil.move(staged_path, fallback)
        try:
            os.removedirs(os.path.dirname(staged_path))
        except OSError:
            pass
        return fallback, plan

    async def _report(self, client, peer_channel, line, notify_msg=None, base_text=""):
        """Añade el resultado al aviso de la descarga (si lo hay) o envía uno nuevo."""
        if notify_msg is not None and base_text:
            await self._notify(client, peer_channel, f"{base_text}\n{line}", edit_msg=notify_msg)
        else:
            await self._notify(client, peer_channel, line)

    async def rename_media_file(self, file_path, client=None, peer_channel=None, notify_msg=None,
                                base_text="", size=None, force_type=None, allow_staged=False, trusted=False):
        """Renombra un vídeo si es peli o serie y lo coloca en la biblioteca.
        force_type: 'movie' / 'series' para saltarse la detección automática.
        allow_staged: permite reprocesar archivos que se quedaron en renombrados/.
        Devuelve la nueva ruta o None."""
        if not getattr(self, 'auto_rename', False) and not allow_staged:
            return None
        if not is_video_file(file_path) or not os.path.isfile(file_path):
            return None
        file_path = self._to_host(file_path)
        in_output = os.path.abspath(file_path).startswith(os.path.abspath(self.rename_output_folder) + os.sep)
        if in_output and not allow_staged:
            return None
        if not os.path.exists(FILEBOT_BIN):
            logger.warning("FileBot no encontrado en %s, no se renombra", FILEBOT_BIN)
            return None
        try:
            size = size or os.path.getsize(file_path)
            if size < MIN_VIDEO_BYTES:
                logger.debug("Vídeo demasiado pequeño para renombrar (muestra?): %s", file_path)
                return None
        except OSError:
            return None

        filename = os.path.basename(file_path)
        if force_type:
            is_episode = force_type == 'series'
        else:
            is_episode = looks_like_episode(filename)
        tipo = "serie" if is_episode else "película"
        emoji = "📺" if is_episode else "🎬"

        async with self.rename_lock:
            try:
                result = await asyncio.to_thread(self._identify_and_move_sync, file_path, is_episode, trusted)
            except Exception as e:
                logger.error("Error ejecutando FileBot para %s: %s", filename, e)
                return None

            match = MOVE_RE.search(result.stdout or '')
            if result.returncode != 0 or not match:
                logger.warning("FileBot no pudo identificar %s (%s). Se deja sin renombrar.\n%s",
                               filename, tipo, (result.stdout or '')[-1500:])
                entry = self.add_history('unidentified', filename, path=file_path, size=size,
                                         guess=('series' if is_episode else 'movie'),
                                         reason=f"no se identificó como {tipo}")
                if hasattr(self, 'ask_for_pending') and not trusted:
                    # 1) la IA interpreta el nombre y FileBot tiene que confirmarlo
                    suggest = await self.ai_suggestion(filename) if hasattr(self, 'ai_suggestion') else None
                    if suggest and ai_can_autoplace(suggest, filename):
                        hinted = self.hinted_path(file_path, suggest)
                        try:
                            os.rename(file_path, hinted)
                            ai_type = 'series' if suggest['type'] == 'series' else 'movie'
                            r = await asyncio.to_thread(self._identify_and_move_sync, hinted, ai_type == 'series')
                            m2 = MOVE_RE.search(r.stdout or '')
                            if r.returncode == 0 and m2:
                                logger.info("🤖 IA + FileBot: %s → %s", filename, os.path.basename(m2.group('dst')))
                                self.resolve_history_entry(entry['id'], 'ia')
                                await self._report(client, peer_channel, f"🤖 _Identificado con ayuda de IA_",
                                                   notify_msg, base_text)
                                base_text = f"{base_text}\n🤖 _Identificado con ayuda de IA_" if base_text else base_text
                                final_path, plan = await asyncio.to_thread(self._place_sync, m2.group('dst'), ai_type == 'series')
                                return await self._finish_placement(final_path, plan, filename, ai_type == 'series',
                                                                    size, client, peer_channel, notify_msg, base_text)
                            os.rename(hinted, file_path)  # FileBot no lo confirma: se deshace
                        except Exception as e:
                            logger.warning("Fallo aplicando la sugerencia de IA a %s: %s", filename, e)
                            if os.path.exists(hinted) and not os.path.exists(file_path):
                                os.rename(hinted, file_path)
                    # 2) se pregunta ya, con un botón relleno (IA o nombre del archivo)
                    if not suggest:
                        info = series_query(filename) if is_episode else None
                        suggest = ({'type': 'series', 'name': info['clean'], 'season': info['season'],
                                    'episode': info['episode']} if info else None)
                    await self._report(client, peer_channel,
                                       f"❓ **No la identifico** (probé como {tipo}) · te pregunto justo debajo",
                                       notify_msg, base_text)
                    try:
                        await self.ask_for_pending(entry, suggest=suggest)
                    except Exception as e:
                        logger.warning("No se pudo preguntar por %s: %s", filename, e)
                    return None
                await self._report(client, peer_channel,
                                   f"❓ **No identificada** (probé como {tipo}) · se queda como está\n"
                                   f"_Biblioteca → Pendientes · #{entry['id']}_",
                                   notify_msg, base_text)
                return None

            try:
                final_path, plan = await asyncio.to_thread(self._place_sync, match.group('dst'), is_episode)
            except Exception as e:
                logger.error("Error colocando %s en la biblioteca: %s", filename, e, exc_info=True)
                return None

        return await self._finish_placement(final_path, plan, filename, is_episode, size,
                                            client, peer_channel, notify_msg, base_text)

    async def _finish_placement(self, final_path, plan, filename, is_episode, size,
                                client, peer_channel, notify_msg=None, base_text=""):
        """Avisa y registra el resultado de colocar en la biblioteca (o pregunta si es duplicado)."""
        emoji = "📺" if is_episode else "🎬"
        if plan['ok'] and not LIBRARY_DRY_RUN:
            disk = plan['disk']
            lib_rel = final_path.split(f"/{disk}/", 1)[-1]
            nuevo = " · serie nueva" if is_episode and plan.get('new') else ""
            logger.info("📚 %s → %s/%s%s", filename, disk, lib_rel, nuevo)
            self.add_history('placed', os.path.basename(final_path), disk=disk, path=final_path,
                             size=size, series=is_episode, new=bool(plan.get('new')))
            await self._report(client, peer_channel,
                               f"📚 **{disk}**{nuevo}\n{emoji} `{lib_rel}`", notify_msg, base_text)
            await self.check_space_warnings(client, peer_channel)
        else:
            rel = os.path.relpath(final_path, os.path.dirname(self.rename_output_folder))
            if LIBRARY_DRY_RUN and plan['ok']:
                nuevo = " (serie nueva)" if is_episode and plan.get('new') else ""
                logger.info("🧪 [prueba] %s iría a %s%s", filename, plan['dest'], nuevo)
                await self._report(client, peer_channel,
                                   f"🧪 **Prueba:** iría a {plan['disk']}{nuevo}\n{emoji} `{plan['dest']}`",
                                   notify_msg, base_text)
            else:
                kind = 'duplicate' if plan.get('duplicate') else 'unplaced'
                icono = "♻️" if kind == 'duplicate' else "⚠️"
                logger.warning("%s %s no se coloca: %s (queda en %s)", icono, filename, plan['reason'], rel)
                entry = self.add_history(kind, os.path.basename(final_path), path=final_path, size=size,
                                         guess=('series' if is_episode else 'movie'), reason=plan['reason'])
                if kind == 'duplicate' and plan.get('existing') and hasattr(self, 'ask_replace'):
                    await self._report(client, peer_channel,
                                       f"♻️ **Ya lo tienes** en {plan.get('disk', '?')} · te pregunto justo debajo si lo reemplazo",
                                       notify_msg, base_text)
                    await self.ask_replace(entry, final_path, plan, is_episode)
                    return final_path
                await self._report(client, peer_channel,
                                   f"{icono} **No colocado:** _{plan['reason']}_\n{emoji} `{rel}`\n"
                                   f"_Biblioteca → Pendientes · #{entry['id']}_",
                                   notify_msg, base_text)
        return final_path

    async def recover_staging(self, client=None, peer_channel=None):
        """Al arrancar: termina de colocar lo que un reinicio dejó a medias en .staging
        (ya identificado y renombrado por FileBot, pendiente solo de moverse)."""
        staging = self.rename_staging_folder
        if not os.path.isdir(staging):
            return
        for root, _dirs, files in os.walk(staging):
            for f in sorted(files):
                path = os.path.join(root, f)
                if not is_video_file(f) or f.endswith('.part'):
                    continue
                is_episode = bool(library.EPISODE_RE.search(f))
                emoji = "📺" if is_episode else "🎬"
                try:
                    async with self.rename_lock:
                        final_path, plan = await asyncio.to_thread(self._place_sync, path, is_episode)
                except Exception as e:
                    logger.error("No se pudo recuperar %s: %s", f, e)
                    continue
                size = os.path.getsize(final_path) if os.path.exists(final_path) else None
                if plan['ok'] and not LIBRARY_DRY_RUN:
                    disk = plan['disk']
                    lib_rel = final_path.split(f"/{disk}/", 1)[-1]
                    logger.info("📚 (recuperado tras reinicio) %s → %s/%s", f, disk, lib_rel)
                    self.add_history('placed', os.path.basename(final_path), disk=disk, path=final_path,
                                     size=size, series=is_episode, new=bool(plan.get('new')))
                    await self._notify(client, peer_channel, f"📚 **{disk}** · _recuperado tras reinicio_\n{emoji} `{lib_rel}`")
                else:
                    kind = 'duplicate' if plan.get('duplicate') else 'unplaced'
                    self.add_history(kind, os.path.basename(final_path), path=final_path, size=size,
                                     guess=('series' if is_episode else 'movie'), reason=plan.get('reason'))

    async def rename_media_in_folder(self, folder, client=None, peer_channel=None):
        """Renombra todos los vídeos de una carpeta (p.ej. tras descomprimir)."""
        if not getattr(self, 'auto_rename', False) or not os.path.isdir(folder):
            return
        videos = []
        for root, _dirs, files in os.walk(folder):
            for f in files:
                if is_video_file(f):
                    videos.append(os.path.join(root, f))
        for video in sorted(videos):
            await self.rename_media_file(video, client, peer_channel)
        # Si la carpeta extraída queda vacía, se elimina
        try:
            for root, dirs, files in os.walk(folder, topdown=False):
                if not os.listdir(root):
                    os.rmdir(root)
        except Exception:
            pass
