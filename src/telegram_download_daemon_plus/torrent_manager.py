# -*- coding: utf-8 -*-
"""
Archivos .torrent. Van, por orden de preferencia:
  1. a Download Station, si está conectado (Ajustes → Download Station);
  2. a la carpeta TORRENTS_DIR, si está definida, para que los recoja el cliente que la vigile.
Si no hay ninguna de las dos, un .torrent se trata como cualquier otro archivo.
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
    def _ds_ready(self):
        return hasattr(self, 'ds_send_torrents') and self.ds_send_torrents()

    def wants_torrent_folder(self, filename):
        return is_torrent(filename) and (TORRENTS_ENABLED or self._ds_ready())

    async def deliver_torrent(self, source, filename, size, link, origin_msg_id, reply_msg):
        ds_error = None
        if self._ds_ready():
            try:
                with open(source, 'rb') as f:
                    await self.ds_add_torrent(filename, f.read())
                os.remove(source)
                await self.log_reply(reply_msg, f"🧲 **Torrent enviado a Download Station**\n📄 `{filename}`")
                self.register_completed_download(link, filename, getattr(reply_msg, 'id', None), origin_msg_id)
                self.add_history('downloaded', filename, size=size, path="Download Station")
                return
            except Exception as e:
                ds_error = e
                logger.error("Download Station no aceptó %s: %s", filename, e)
                if not TORRENTS_ENABLED:
                    raise Exception(f"PERMANENT:Download Station no ha aceptado el .torrent ({e})")

        try:
            path = place_torrent(source, filename, TORRENTS_MOUNT)
        except OSError as e:
            logger.error("No se pudo dejar %s en %s: %s", filename, TORRENTS_MOUNT, e)
            raise Exception(f"PERMANENT:no he podido dejar el .torrent en la carpeta de torrents ({e})")

        note = f"\n_Download Station falló ({ds_error}), así que lo he dejado en la carpeta._" if ds_error else ""
        await self.log_reply(reply_msg, f"🧲 **Torrent enviado a la carpeta vigilada**\n📄 `{filename}`{note}")
        self.register_completed_download(link, filename, getattr(reply_msg, 'id', None), origin_msg_id)
        self.add_history('downloaded', filename, size=size, path=path)
