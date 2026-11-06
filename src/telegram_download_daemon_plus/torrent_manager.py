# -*- coding: utf-8 -*-
"""
Archivos .torrent: si TORRENTS_DIR está definida, se dejan en esa carpeta para que los
recoja el cliente de torrents que la vigile (Download Station, qBittorrent...). Sin la
variable, un .torrent se trata como cualquier otro archivo.
"""
import logging
import os
import shutil

logger = logging.getLogger(__name__)

TORRENTS_MOUNT = "/torrents"
TORRENTS_ENABLED = bool(os.environ.get("TORRENTS_DIR", "").strip())


def is_torrent(filename):
    return (filename or "").lower().endswith(".torrent")


def unique_path(folder, filename):
    """Ruta libre en la carpeta: 'a.torrent', 'a (1).torrent'..."""
    name, ext = os.path.splitext(os.path.basename(filename))
    path = os.path.join(folder, name + ext)
    n = 0
    while os.path.exists(path):
        n += 1
        path = os.path.join(folder, f"{name} ({n}){ext}")
    return path


def place_torrent(source, filename, folder):
    """Copia y luego renombra: el cliente que vigila nunca ve un archivo a medias."""
    final_path = unique_path(folder, filename)
    part_path = final_path + ".part"
    try:
        shutil.copyfile(source, part_path)
        # el contenedor escribe como root: el cliente tiene que poder leerlo y borrarlo
        os.chmod(part_path, 0o666)
        os.replace(part_path, final_path)
    except OSError:
        if os.path.exists(part_path):
            os.remove(part_path)
        raise
    os.remove(source)
    return final_path


class TorrentManagerMixin:
    def wants_torrent_folder(self, filename):
        return TORRENTS_ENABLED and is_torrent(filename)

    async def deliver_torrent(self, source, filename, size, link, origin_msg_id, reply_msg):
        try:
            path = place_torrent(source, filename, TORRENTS_MOUNT)
        except OSError as e:
            logger.error("No se pudo dejar %s en %s: %s", filename, TORRENTS_MOUNT, e)
            raise Exception(f"PERMANENT:no he podido dejar el .torrent en la carpeta de torrents ({e})")

        await self.log_reply(reply_msg, f"🧲 **Torrent enviado a la carpeta vigilada**\n📄 `{filename}`")
        self.register_completed_download(link, filename, getattr(reply_msg, 'id', None), origin_msg_id)
        self.add_history('downloaded', filename, size=size, path=path)
