# -*- coding: utf-8 -*-
"""
Ajustes de nombres y formato, configurables desde Ajustes y guardados en config.json:
  - calidad ([1080p]) y códec ([h264]) en el nombre de las películas;
  - pasar los MP4 a MKV con mkvmerge (sin recomprimir: solo cambia el contenedor).
FileBot siempre renombra con todas las etiquetas, porque la biblioteca usa la calidad para
separar las 4K; las que estén desactivadas se quitan después, al colocar el archivo.
"""
import asyncio
import logging
import os
import re
import shutil
import subprocess

from telethon import Button

logger = logging.getLogger(__name__)

MKVMERGE = shutil.which("mkvmerge")
REMUX_TIMEOUT = 3 * 3600
MOVIE_TAGS_RE = re.compile(r'^(?P<base>.+? \(\d{4}\)) \[(?P<res>[^\]]*)\] \[(?P<codec>[^\]]*)\](?P<rest>.*)$')
EXAMPLE = "Dune - Parte dos (2024) [1080p] [h264].mkv"


def movie_filename(name, quality=True, codec=True):
    """'Peli (2024) [1080p] [h264].mkv' sin las etiquetas desactivadas (y sin las vacías)."""
    root, ext = os.path.splitext(name)
    m = MOVIE_TAGS_RE.match(root)
    if not m:
        return name
    parts = [m.group('base')]
    if quality and m.group('res'):
        parts.append(f"[{m.group('res')}]")
    if codec and m.group('codec'):
        parts.append(f"[{m.group('codec')}]")
    return " ".join(parts) + m.group('rest') + ext


def remux_to_mkv(path, mkvmerge=None):
    """MP4 -> MKV sin recomprimir. Devuelve la ruta del MKV, o None si no se pudo
    (entonces el MP4 se queda tal cual)."""
    mkvmerge = mkvmerge or MKVMERGE
    root = os.path.splitext(path)[0]
    target = root + ".mkv"
    n = 0
    while os.path.exists(target):
        n += 1
        target = f"{root} ({n}).mkv"
    tmp = target + ".remux"
    try:
        r = subprocess.run([mkvmerge, "-q", "-o", tmp, path], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, errors='ignore', timeout=REMUX_TIMEOUT)
        # 0 = bien, 1 = bien con avisos, 2 = error
        if r.returncode not in (0, 1) or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
            logger.warning("mkvmerge no pudo convertir %s (código %s): %s",
                           os.path.basename(path), r.returncode, (r.stdout or '').strip()[-300:])
            return None
        os.replace(tmp, target)
        os.remove(path)
        return target
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("Error convirtiendo %s a MKV: %s", os.path.basename(path), e)
        return None
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _check(on):
    return "✅" if on else "❌"


class MediaToolsMixin:
    def __init_media_tools__(self):
        self.name_quality = True
        self.name_codec = True
        self.remux_mp4 = False

    def final_movie_name(self, name):
        return movie_filename(name, self.name_quality, self.name_codec)

    async def maybe_remux(self, path):
        """Si está activado, convierte un MP4 a MKV. Devuelve la ruta con la que seguir."""
        if not (self.remux_mp4 and path.lower().endswith(".mp4")):
            return path
        if not MKVMERGE:
            logger.warning("MP4 a MKV activado pero falta mkvmerge: reconstruye la imagen")
            return path
        new = await asyncio.to_thread(remux_to_mkv, path)
        if new:
            logger.info("🎞️ %s convertido a MKV", os.path.basename(path))
        return new or path

    def naming_view(self):
        example = self.final_movie_name(EXAMPLE)
        text = ("🏷️ **Nombres y formato**\n\n"
                f"Así quedará una película:\n`{example}`\n\n"
                f"📐 Calidad en el nombre: {_check(self.name_quality)}\n"
                f"🎞️ Códec en el nombre: {_check(self.name_codec)}\n"
                f"📦 Pasar MP4 a MKV: {_check(self.remux_mp4)}\n\n"
                "_La conversión a MKV no recomprime: no pierde calidad y tarda poco._")
        if self.remux_mp4 and not MKVMERGE:
            text += ("\n\n⚠️ **Falta mkvmerge.** Reconstruye la imagen para que funcione:\n"
                     "`docker compose up -d --build`")
        if not self.name_quality:
            text += "\n\n_La biblioteca sigue separando las 4K: el bot mira la calidad antes de quitarla._"
        buttons = [
            [Button.inline(f"📐 Calidad: {_check(self.name_quality)}", data="cmd_name_quality"),
             Button.inline(f"🎞️ Códec: {_check(self.name_codec)}", data="cmd_name_codec")],
            [Button.inline(f"📦 MP4 a MKV: {_check(self.remux_mp4)}", data="cmd_remux_mp4")],
            [Button.inline("🔙 Volver", data="menu_config")],
        ]
        return text, buttons

    async def handle_media_settings_callback(self, event, data):
        toggles = {'cmd_name_quality': 'name_quality', 'cmd_name_codec': 'name_codec',
                   'cmd_remux_mp4': 'remux_mp4'}
        if data in toggles:
            attr = toggles[data]
            setattr(self, attr, not getattr(self, attr))
            self.save_config()
        elif data != 'menu_naming':
            return False
        text, buttons = self.naming_view()
        await self._safe_edit(event, text, buttons)
        return True
