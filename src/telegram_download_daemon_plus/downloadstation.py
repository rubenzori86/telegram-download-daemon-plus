# -*- coding: utf-8 -*-
"""
Download Station de Synology (opcional). Se configura desde Ajustes → Download Station:
el bot pide por el chat la dirección, el puerto, el usuario y la contraseña, y lo guarda
en session/downloadstation.json (la contraseña cifrada, y su mensaje se borra del chat).

Con la conexión hecha:
  - el panel de Descargas muestra el progreso de Download Station;
  - los .torrent y los enlaces magnet del canal se mandan a Download Station;
  - al terminar una descarga avisa y, si el renombrado está activado, coloca los vídeos
    (sobre una copia o un enlace duro, para no cortar la compartición del torrent).

Con verificación en dos pasos el bot pide el código una sola vez: el NAS lo registra como
dispositivo de confianza y los siguientes inicios de sesión usan ese token (también cifrado).
"""
import asyncio
import base64
import glob
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import pyaes
from telethon import Button

logger = logging.getLogger(__name__)

POLL_ACTIVE = 15
# Antes de tocar lo terminado, los archivos tienen que llevar este tiempo sin cambiar de
# tamaño ni de fecha: un archivo de decenas de GB puede seguir escribiéndose un rato.
STABLE_SECONDS = 120
STABLE_POLL = 15
STABLE_MAX = 12 * 3600
# Días que se sigue compartiendo lo terminado antes de quitarlo (None = hasta que lo borres tú)
SEED_OPTIONS = (1, 3, 5, 7, 10, 15, 20, 30)
DAY = 86400


def seed_label(days):
    return f"{days} día{'s' if days != 1 else ''}" if days else "indefinido"
POLL_IDLE = 60
TIMEOUT = 20
DEVICE_NAME = "Telegram Download Daemon"
OTP_RE = re.compile(r'^\d{6,8}$')
ACTIVE = ('waiting', 'downloading', 'finishing', 'hash_checking', 'extracting', 'filehosting_waiting')
DONE = ('finished', 'seeding')
STATUS_ES = {'waiting': 'en espera', 'downloading': 'descargando', 'paused': 'en pausa',
             'finishing': 'terminando', 'finished': 'terminada', 'hash_checking': 'comprobando',
             'seeding': 'compartiendo', 'filehosting_waiting': 'en espera', 'extracting': 'extrayendo',
             'error': 'error'}
DS_ERRORS = {
    400: "usuario o contraseña incorrectos",
    401: "la cuenta está desactivada",
    402: "permiso denegado",
    403: "la cuenta pide el código de verificación en dos pasos: vuelve a conectar en Ajustes → Download Station",
    404: "código de verificación incorrecto o caducado",
    407: "el NAS ha bloqueado la IP por demasiados intentos",
    105: "ese usuario no tiene permiso para usar Download Station",
    119: "la sesión ha caducado",
}
SESSION_ERRORS = (106, 107, 119)
MAGNET_RE = re.compile(r'magnet:\?\S+', re.I)
TORRENT_URL_RE = re.compile(r'https?://\S+?\.torrent(?:[?#]\S*)?(?=\s|$)', re.I)


def torrent_link(text):
    """Un mensaje que es un magnet o un enlace web a un .torrent -> (uri, tipo, nombre)."""
    text = (text or "").strip()
    if text.lower().startswith('magnet:'):
        m = MAGNET_RE.match(text)
        if m:
            name = urllib.parse.parse_qs(urllib.parse.urlparse(m.group(0)).query).get('dn', [''])[0]
            return m.group(0), "magnet", name
    m = TORRENT_URL_RE.match(text)
    if m:
        path = urllib.parse.urlparse(m.group(0)).path
        return m.group(0), "de torrent", urllib.parse.unquote(path.rsplit('/', 1)[-1])
    return None, None, None
HOST_RE = re.compile(r'^[A-Za-z0-9.\-]+$|^\[[0-9A-Fa-f:]+\]$')


# ---------- contraseña cifrada (AES-CTR + HMAC) ----------
def _keys():
    """La clave sale del API hash de Telegram, que está en .env y no en session/:
    quien solo tenga la carpeta session no puede leer la contraseña."""
    secret = os.environ.get("TELEGRAM_DAEMON_API_HASH", "") + "|telegram-download-daemon-plus"
    base = hashlib.sha256(secret.encode()).digest()
    return hashlib.sha256(b"enc" + base).digest(), hashlib.sha256(b"mac" + base).digest()


def encrypt_secret(text):
    enc, mac = _keys()
    iv = os.urandom(16)
    data = pyaes.AESModeOfOperationCTR(enc, counter=pyaes.Counter(int.from_bytes(iv, 'big'))).encrypt(text.encode())
    tag = hmac.new(mac, iv + data, hashlib.sha256).digest()
    return base64.b64encode(iv + data + tag).decode()


def decrypt_secret(token):
    enc, mac = _keys()
    raw = base64.b64decode(token)
    iv, data, tag = raw[:16], raw[16:-32], raw[-32:]
    if not hmac.compare_digest(tag, hmac.new(mac, iv + data, hashlib.sha256).digest()):
        raise ValueError("no se puede descifrar (¿ha cambiado TELEGRAM_DAEMON_API_HASH?)")
    return pyaes.AESModeOfOperationCTR(enc, counter=pyaes.Counter(int.from_bytes(iv, 'big'))).decrypt(data).decode()


# ---------- cliente de la API ----------
class DSError(Exception):
    def __init__(self, code, text=None):
        self.code = code
        super().__init__(text or DS_ERRORS.get(code, f"error {code} de Download Station"))


def parse_address(text):
    """'192.168.1.10', 'https://nas.local:5001'... -> (host, esquema o None, puerto o None)."""
    text = text.strip().rstrip('/')
    scheme = None
    if '://' in text:
        scheme, text = text.split('://', 1)
        scheme = scheme.lower()
        if scheme not in ('http', 'https'):
            return None
    text = text.split('/', 1)[0]
    port = None
    m = re.match(r'^(.*):(\d{1,5})$', text)
    if m and not text.endswith(']'):
        text, port = m.group(1), int(m.group(2))
    if not text or not HOST_RE.match(text) or (port is not None and not 0 < port < 65536):
        return None
    return text, scheme, port


def base_url(conf):
    port = conf.get('port') or (5001 if conf.get('scheme') == 'https' else 5000)
    return f"{conf.get('scheme', 'http')}://{conf['host']}:{port}"


class DSClient:
    def __init__(self, url, user, password, device_id=None):
        self.url = url.rstrip('/')
        self.user = user
        self.password = password
        self.device_id = device_id
        self.sid = None
        # NAS de casa con certificado propio: no se comprueba el certificado
        self.ctx = ssl._create_unverified_context() if self.url.startswith('https') else None

    def _request(self, path, fields, file=None, file_field="file", query=None):
        url = f"{self.url}/webapi/{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        if file:
            boundary = uuid.uuid4().hex
            body = b""
            for k, v in fields.items():
                body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
            name, content = file
            safe = name.replace('"', '')
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{safe}\"\r\n"
                     "Content-Type: application/x-bittorrent\r\n\r\n").encode() + content + f"\r\n--{boundary}--\r\n".encode()
            headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        else:
            body = urllib.parse.urlencode(fields).encode()
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=self.ctx) as r:
                data = json.loads(r.read().decode('utf-8', 'replace'))
        except urllib.error.HTTPError as e:
            raise DSError(-1, f"el NAS respondió {e.code}") from None
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, 'reason', e)
            raise DSError(-1, f"no conecta con {self.url} ({reason})") from None
        except ValueError:
            raise DSError(-1, f"{self.url} no parece un NAS Synology") from None
        if not data.get('success'):
            raise DSError(data.get('error', {}).get('code', -1))
        return data.get('data') or {}

    def login(self, otp=None):
        fields = {"api": "SYNO.API.Auth", "version": 6, "method": "login",
                  "account": self.user, "passwd": self.password,
                  "session": "DownloadStation", "format": "sid"}
        if otp:
            # con el código, el NAS devuelve un token para no volver a pedirlo
            fields.update(otp_code=otp, enable_device_token="yes", device_name=DEVICE_NAME)
        elif self.device_id:
            fields.update(device_id=self.device_id, device_name=DEVICE_NAME)
        data = self._request("entry.cgi", fields)
        if data.get('did'):
            self.device_id = data['did']
        self.sid = data.get('sid')
        if not self.sid:
            raise DSError(-1, "el NAS no ha devuelto sesión")

    def _authed(self, send):
        """Llama con sesión; si ha caducado, vuelve a entrar una vez."""
        for attempt in (1, 2):
            if not self.sid:
                self.login()
            try:
                return send()
            except DSError as e:
                # con una sesión caducada DSM 7 responde 105 («sin permiso»), no 106/119
                if (e.code in SESSION_ERRORS or e.code == 105) and attempt == 1:
                    self.sid = None
                    continue
                raise

    def _call(self, fields):
        return self._authed(lambda: self._request(
            "DownloadStation/task.cgi", {"api": "SYNO.DownloadStation.Task", "version": 1, **fields, "_sid": self.sid}))

    def tasks(self):
        return self._call({"method": "list", "additional": "detail,transfer"}).get('tasks', [])

    def add_uri(self, uri):
        self._call({"method": "create", "uri": uri})

    def default_destination(self):
        data = self._authed(lambda: self._request(
            "DownloadStation/info.cgi",
            {"api": "SYNO.DownloadStation.Info", "version": 1, "method": "getconfig", "_sid": self.sid}))
        return data.get('default_destination') or ""

    def add_torrent(self, name, content):
        """Subir un .torrent solo funciona con la API DownloadStation2 y la sesión en la
        dirección: la API antigua responde siempre error 101 (probado contra DSM 7)."""
        dest = self.default_destination()
        self._authed(lambda: self._request(
            "entry.cgi",
            {"api": "SYNO.DownloadStation2.Task", "version": 2, "method": "create", "type": '"file"',
             "file": '["torrent"]', "create_list": "false", "destination": json.dumps(dest)},
            file=(name, content), file_field="torrent", query={"_sid": self.sid}))

    def delete_task(self, task_id):
        """Quita la tarea de Download Station (los archivos descargados no se tocan)."""
        self._call({"method": "delete", "id": task_id, "force_complete": "false"})


def task_line(t, bar, hsize):
    tr = (t.get('additional') or {}).get('transfer') or {}
    size = t.get('size') or 0
    done = tr.get('size_downloaded') or 0
    speed = tr.get('speed_download') or 0
    pct = done * 100 / size if size else 0
    status = STATUS_ES.get(t.get('status'), t.get('status', '?'))
    line = f"   {bar(pct)} {pct:.0f}% · {status}"
    if t.get('status') == 'downloading':
        line += f" · {hsize(speed)}/s"
        if speed and size > done:
            mins = (size - done) / speed / 60
            line += f" · ⏱️ {mins:.0f} min" if mins < 120 else f" · ⏱️ {mins / 60:.1f} h"
    return line


class DownloadStationMixin:
    def __init_download_station__(self):
        self.ds_conf = {}
        self.ds_tasks = []
        self.ds_error = None
        self.ds_wizard = {}
        self._ds_client = None
        self._ds_lock = asyncio.Lock()
        self._ds_wake = asyncio.Event()
        self._ds_inflight = set()
        self.ds_stable_seconds = STABLE_SECONDS
        self.ds_stable_poll = STABLE_POLL
        self._ds_load()

    # ---------- configuración ----------
    @property
    def ds_file(self):
        return os.path.join(os.path.dirname(self.config_file) or '.', "downloadstation.json")

    def _ds_load(self):
        try:
            with open(self.ds_file, encoding='utf-8') as f:
                self.ds_conf = json.load(f)
        except FileNotFoundError:
            self.ds_conf = {}
        except Exception as e:
            logger.error("No se pudo leer %s: %s", self.ds_file, e)
            self.ds_conf = {}

    def _ds_save(self):
        tmp = self.ds_file + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.ds_conf, f, indent=2, ensure_ascii=False)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.ds_file)

    def ds_configured(self):
        return bool(self.ds_conf.get('host') and self.ds_conf.get('user') and self.ds_conf.get('password'))

    def ds_send_torrents(self):
        return self.ds_configured() and self.ds_conf.get('send_torrents', True)

    def _client(self):
        if self._ds_client is None:
            device = self.ds_conf.get('device')
            self._ds_client = DSClient(base_url(self.ds_conf), self.ds_conf['user'],
                                       decrypt_secret(self.ds_conf['password']),
                                       decrypt_secret(device) if device else None)
        return self._ds_client

    async def ds_call(self, method, *args):
        async with self._ds_lock:
            return await asyncio.to_thread(lambda: getattr(self._client(), method)(*args))

    # ---------- enviar ----------
    async def ds_add_torrent(self, name, content):
        await self.ds_call('add_torrent', name, content)
        self._ds_wake.set()

    async def ds_add_magnet(self, uri):
        await self.ds_call('add_uri', uri)
        self._ds_wake.set()

    # ---------- seguimiento ----------
    async def ds_monitor_loop(self, client, peer):
        await asyncio.sleep(15)
        while True:
            busy = False
            if self.ds_configured():
                try:
                    tasks = await self.ds_call('tasks')
                    self.ds_error = None
                    self.ds_tasks = tasks
                    busy = any(t.get('status') in ACTIVE for t in tasks)
                    await self._ds_check_finished(tasks, client, peer)
                except Exception as e:
                    if self.ds_error != str(e):
                        logger.warning("Download Station: %s", e)
                    self.ds_error = str(e)
            self._ds_wake.clear()
            try:
                await asyncio.wait_for(self._ds_wake.wait(), POLL_ACTIVE if busy else POLL_IDLE)
            except asyncio.TimeoutError:
                pass

    async def _ds_check_finished(self, tasks, client, peer):
        conf = self.ds_conf
        done_ids = set(conf.get('done_ids', []))
        present = {t['id'] for t in tasks}
        pruned = done_ids & present
        changed = pruned != done_ids
        conf['done_ids'] = sorted(pruned)
        # cuándo terminó cada tarea (para el plazo de compartir) y si ya está en la biblioteca
        fin = {k: v for k, v in conf.get('finished_at', {}).items() if k in present}
        placed = {k: v for k, v in conf.get('placed', {}).items() if k in present}
        for t in tasks:
            if t.get('status') in DONE and t['id'] not in fin:
                completed = ((t.get('additional') or {}).get('detail') or {}).get('completed_time') or 0
                fin[t['id']] = completed if completed > 0 else time.time()
        changed = changed or fin != conf.get('finished_at', {}) or placed != conf.get('placed', {})
        conf['finished_at'], conf['placed'] = fin, placed
        if changed:
            self._ds_save()
        for t in tasks:
            if t.get('status') in DONE and t['id'] not in pruned and t['id'] not in self._ds_inflight:
                # en segundo plano: esperar a que el archivo se asiente puede llevar minutos
                self._ds_inflight.add(t['id'])
                asyncio.create_task(self._ds_process(t, client, peer))
        await self._ds_apply_seed_limit(tasks, client, peer)

    async def _ds_apply_seed_limit(self, tasks, client, peer):
        """Quita lo que lleva compartiéndose más días de los elegidos. Solo lo ya procesado:
        nunca algo que el bot aún no ha colocado o está colocando."""
        days = self.ds_conf.get('seed_days')
        if not days:
            return
        done = set(self.ds_conf.get('done_ids', []))
        fin = self.ds_conf.get('finished_at', {})
        for t in tasks:
            tid = t['id']
            if (t.get('status') in DONE and tid in done and tid not in self._ds_inflight
                    and tid in fin and time.time() - fin[tid] >= days * DAY):
                placed = bool(self.ds_conf.get('placed', {}).get(tid))
                try:
                    note = await self._ds_remove(t, delete_files=placed)
                except Exception as e:
                    logger.warning("No se pudo quitar %s de Download Station: %s", t.get('title'), e)
                    continue
                text = (f"🧹 **Dejo de compartir** `{t.get('title', '?')}`\n"
                        f"Llevaba {seed_label(days)} compartiéndose. {note}")
                try:
                    await client.send_message(peer, text)
                except Exception as e:
                    logger.debug("No se pudo avisar: %s", e)

    async def _ds_remove(self, t, delete_files):
        """Quita la tarea y, si se pide, sus archivos. Devuelve una frase para el aviso."""
        await self.ds_call('delete_task', t['id'])
        self._ds_wake.set()
        note = "La he quitado de Download Station."
        title = t.get('title') or ''
        destination = ((t.get('additional') or {}).get('detail') or {}).get('destination', '')
        path = self._ds_local_path(destination, title)
        if not delete_files:
            if path:
                note += f" El archivo sigue en `{destination}` porque no está en tu biblioteca."
            return note
        # seguridad: solo la entrada de esa tarea dentro de su carpeta de destino
        if (not path or not title or title in ('.', '..') or os.path.basename(path) != title
                or not os.path.abspath(path).startswith('/volume')):
            return note + " No he encontrado su archivo para borrarlo."
        def rm():
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        await asyncio.to_thread(rm)
        return note + " También he borrado su archivo: la copia de tu biblioteca sigue intacta."

    async def _ds_process(self, t, client, peer):
        try:
            await self._ds_finished(t, client, peer)
        except Exception as e:
            logger.error("Error procesando lo terminado en Download Station (%s): %s",
                         t.get('title'), e, exc_info=True)
        finally:
            # se marca como hecho al acabar: si el bot se reinicia a medias, se repite
            done = set(self.ds_conf.get('done_ids', []))
            done.add(t['id'])
            self.ds_conf['done_ids'] = sorted(done)
            try:
                self._ds_save()
            except Exception as e:
                logger.warning("No se pudo guardar el estado de Download Station: %s", e)
            self._ds_inflight.discard(t['id'])

    @staticmethod
    def _ds_snapshot(path):
        """Tamaño y fecha de cada archivo (de la carpeta entera si es una carpeta)."""
        files = []
        if os.path.isdir(path):
            for root, _d, names in os.walk(path):
                files += [os.path.join(root, n) for n in names]
        elif os.path.exists(path):
            files = [path]
        snap = []
        for f in sorted(files):
            try:
                st = os.stat(f)
                snap.append((f, st.st_size, st.st_mtime_ns))
            except OSError:
                snap.append((f, None, None))
        return tuple(snap)

    async def _ds_wait_stable(self, path):
        """Espera a que los archivos lleven ds_stable_seconds sin cambiar. False si no se
        asientan en STABLE_MAX (o si desaparecen)."""
        start = time.monotonic()
        last = await asyncio.to_thread(self._ds_snapshot, path)
        quiet_since = time.monotonic()
        while True:
            if not last:
                return False
            if time.monotonic() - quiet_since >= self.ds_stable_seconds:
                return True
            if time.monotonic() - start > STABLE_MAX:
                return False
            await asyncio.sleep(self.ds_stable_poll)
            cur = await asyncio.to_thread(self._ds_snapshot, path)
            if cur != last:
                last = cur
                quiet_since = time.monotonic()

    def _ds_local_path(self, destination, title):
        """La carpeta de destino de Download Station ('video/descargas') vista desde el bot:
        el contenedor tiene que tener montado ese volumen con la misma ruta (/volumeN/...)."""
        if not destination:
            return None
        for base in sorted(glob.glob(os.path.join('/volume*', destination))):
            path = os.path.join(base, title)
            if os.path.exists(path):
                return path
        return None

    def _ds_copy_for_library(self, src):
        """Enlace duro (instantáneo) o copia en la carpeta de descargas: el original sigue
        compartiéndose en Download Station."""
        folder = os.path.join(self._to_host(self.download_folder), "downloadstation")
        os.makedirs(folder, exist_ok=True)
        dst = os.path.join(folder, os.path.basename(src))
        root, ext = os.path.splitext(dst)
        n = 0
        while os.path.exists(dst):
            n += 1
            dst = f"{root} ({n}){ext}"
        try:
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)
        return dst

    async def _ds_finished(self, t, client, peer):
        from .history_manager import hsize
        from .rename_manager import MIN_VIDEO_BYTES, is_video_file
        title = t.get('title', '?')
        destination = ((t.get('additional') or {}).get('detail') or {}).get('destination', '')
        path = self._ds_local_path(destination, title)
        text = f"🧲 **Terminado en Download Station**\n📄 `{title}`\n📦 {hsize(t.get('size') or 0)}"
        place = bool(path) and getattr(self, 'auto_rename', False)
        if not path:
            text += (f"\n⚠️ No veo la carpeta `{destination}` desde el bot. Para que la coloque, "
                     "monta ese volumen en el contenedor con la misma ruta.")
        waiting = "\n⏳ _Esperando a que el archivo deje de cambiar para colocarlo…_"
        msg = await client.send_message(peer, text + (waiting if place else ""))
        if hasattr(self, 'add_history'):
            self.add_history('downloaded', title, size=t.get('size') or 0, path=path or destination)
        if not place:
            return

        # Que nadie lo siga escribiendo: mismo tamaño y fecha durante un rato
        if not await self._ds_wait_stable(path):
            await self._ds_edit(client, peer, msg, text + "\n⚠️ _El archivo no ha dejado de cambiar (o ha "
                                "desaparecido): no lo coloco._")
            return
        videos = []
        if os.path.isdir(path):
            for root, _d, files in os.walk(path):
                videos += [os.path.join(root, f) for f in sorted(files)]
        else:
            videos = [path]
        videos = [v for v in videos if is_video_file(v) and os.path.getsize(v) >= MIN_VIDEO_BYTES]
        if not videos:
            await self._ds_edit(client, peer, msg, text)
            return
        await self._ds_edit(client, peer, msg, text + "\n🏷️ _Identificando…_")
        all_placed = True
        for v in videos:
            copy = await asyncio.to_thread(self._ds_copy_for_library, v)
            copy = await self.maybe_remux(copy)
            final = await self.rename_media_file(copy, client, peer, notify_msg=msg if len(videos) == 1 else None,
                                                 base_text=text if len(videos) == 1 else "")
            if not self._in_library(final):
                all_placed = False
        if all_placed:
            # solo así se puede borrar luego el original sin perder nada
            self.ds_conf.setdefault('placed', {})[t['id']] = True

    def _in_library(self, final):
        """¿Ha acabado en la biblioteca? (no en renombrados/ ni sin identificar)."""
        if not final or not os.path.exists(final):
            return False
        out = getattr(self, 'rename_output_folder', None)
        return not (out and os.path.abspath(final).startswith(os.path.abspath(out) + os.sep))

    @staticmethod
    async def _ds_edit(client, peer, msg, text):
        try:
            await client.edit_message(peer, msg, text)
        except Exception as e:
            logger.debug("No se pudo editar el aviso de Download Station: %s", e)

    # ---------- panel ----------
    def ds_downloads_text(self, bar, hsize, short):
        if not self.ds_configured():
            return ""
        if self.ds_error:
            return f"🧲 **Download Station:** ⚠️ {self.ds_error}\n\n"
        active = [t for t in self.ds_tasks if t.get('status') not in DONE]
        if not active:
            return "🧲 Download Station sin descargas\n\n"
        text = f"🧲 **Download Station ({len(active)})**\n"
        for t in active[:6]:
            text += f"• {short(t.get('title', '?'))}\n{task_line(t, bar, hsize)}\n"
        if len(active) > 6:
            text += f"• … y {len(active) - 6} más\n"
        return text + "\n"

    def ds_view(self, note=""):
        text = "🧲 **Download Station**\n\n" + (note + "\n\n" if note else "")
        if self.ds_configured():
            c = self.ds_conf
            text += (f"🔌 `{base_url(c)}` · 👤 `{c['user']}`" + (" · 🔐 2FA" if c.get('device') else "") + "\n"
                     f"Estado: {'⚠️ ' + self.ds_error if self.ds_error else '✅ conectado'}\n"
                     f"Torrents y magnets del canal: {'✅ a Download Station' if self.ds_send_torrents() else '❌ no se envían'}\n\n"
                     f"Compartir lo terminado: **{seed_label(c.get('seed_days'))}**\n\n"
                     "_Lo que termine se avisa y, si el renombrado está activado, se coloca en la biblioteca._")
            buttons = [
                [Button.inline("🧪 Probar conexión", data="cmd_ds_test"),
                 Button.inline("✏️ Cambiar datos", data="cmd_ds_setup")],
                [Button.inline(f"🧲 Enviar torrents: {'✅' if self.ds_send_torrents() else '❌'}", data="cmd_ds_send")],
                [Button.inline(f"🌱 Compartir: {seed_label(self.ds_conf.get('seed_days'))}", data="cmd_ds_seed"),
                 Button.inline(f"🗑️ Terminados ({len(self._ds_done_tasks())})", data="menu_ds_done")],
                [Button.inline("🔌 Desconectar", data="cmd_ds_forget_ask")],
                [Button.inline("🔙 Volver", data="menu_config")],
            ]
        else:
            text += ("Conecta el bot con Download Station de tu Synology para:\n"
                     "• ver su progreso en Descargas\n"
                     "• mandarle los .torrent y los enlaces magnet del canal\n"
                     "• colocar en la biblioteca lo que termine\n\n"
                     "Te pediré la dirección del NAS, el usuario y la contraseña. Si tienes "
                     "verificación en dos pasos, también el código, pero solo esta vez.")
            buttons = [[Button.inline("🔌 Conectar", data="cmd_ds_setup")],
                       [Button.inline("🔙 Volver", data="menu_config")]]
        return text, buttons

    def _ds_done_tasks(self):
        return [t for t in self.ds_tasks if t.get('status') in DONE]

    def _ds_task(self, tid):
        return next((t for t in self.ds_tasks if t['id'] == tid), None)

    def ds_seed_view(self):
        cur = self.ds_conf.get('seed_days')
        text = ("🌱 **¿Cuántos días se comparte lo terminado?**\n\n"
                "Pasado ese tiempo lo quito de Download Station y borro el archivo, pero solo si ya está "
                "en tu biblioteca. Si no lo está, quito la tarea y dejo el archivo.\n\n"
                f"Ahora: **{seed_label(cur)}**")
        opts = [Button.inline(("✅ " if cur == d else "") + seed_label(d), data=f"cmd_ds_seedset_{d}")
                for d in SEED_OPTIONS]
        rows = [opts[i:i + 4] for i in range(0, len(opts), 4)]
        rows.append([Button.inline(("✅ " if not cur else "") + "♾️ Indefinido (lo borras tú)", data="cmd_ds_seedset_0")])
        rows.append([Button.inline("🔙 Volver", data="menu_ds")])
        return text, rows

    def ds_done_view(self, note=""):
        from .history_manager import hsize
        tasks = self._ds_done_tasks()
        days = self.ds_conf.get('seed_days')
        fin = self.ds_conf.get('finished_at', {})
        text = "🗑️ **Terminados en Download Station**\n\n" + (note + "\n\n" if note else "")
        if not tasks:
            return text + "No hay nada terminado.", [[Button.inline("🔙 Volver", data="menu_ds")]]
        rows = []
        for t in tasks[:12]:
            left = ""
            if days and t['id'] in fin:
                rest = days * DAY - (time.time() - fin[t['id']])
                left = f" · se quita en {max(0, rest) / DAY:.1f} d" if rest > 0 else " · se quita ya"
            lib = "📚" if self.ds_conf.get('placed', {}).get(t['id']) else "📄"
            text += f"{lib} {t.get('title', '?')[:45]} · {hsize(t.get('size') or 0)}{left}\n"
            rows.append([Button.inline(f"🗑️ {t.get('title', '?')[:30]}", data=f"cmd_ds_del_{t['id']}")])
        text += "\n_📚 ya está en tu biblioteca · 📄 no lo está_"
        rows.append([Button.inline("🔙 Volver", data="menu_ds")])
        return text, rows

    # ---------- asistente ----------
    WIZ_CANCEL = [Button.inline("✖️ Cancelar", data="cmd_ds_cancel")]

    def _wiz_prompt(self, step, warn=""):
        w = self.ds_wizard
        head = "🧲 **Conectar Download Station**\n\n" + (f"⚠️ {warn}\n\n" if warn else "")
        if step == 'host':
            return head + ("**1/4 · Dirección del NAS**\n\nEnvía en un mensaje al chat la IP o el nombre "
                           "del NAS.\nEjemplo: `192.168.1.10`"), [self.WIZ_CANCEL]
        if step == 'scheme':
            return head + f"**2/4 · ¿Cómo entras a DSM en `{w['host']}`?**", [
                [Button.inline("http (normal)", data="cmd_ds_scheme_http"),
                 Button.inline("https (seguro)", data="cmd_ds_scheme_https")], self.WIZ_CANCEL]
        if step == 'port':
            default = 5001 if w.get('scheme') == 'https' else 5000
            return head + ("**3/4 · Puerto**\n\nSi usas uno distinto, envíalo en un mensaje al chat. "
                           f"Si no, pulsa el botón."), [
                [Button.inline(f"Por defecto ({default})", data="cmd_ds_port_default")], self.WIZ_CANCEL]
        if step == 'user':
            return head + f"🔌 `{base_url(w)}`\n\n**4/4 · Usuario**\n\nEnvía el usuario del NAS en un mensaje al chat.", [self.WIZ_CANCEL]
        if step == 'pass':
            return head + (f"🔌 `{base_url(w)}` · 👤 `{w['user']}`\n\n**Contraseña**\n\nEnvíala en un mensaje al chat. "
                           "La borro del chat nada más leerla y se guarda cifrada."), [self.WIZ_CANCEL]
        if step == 'otp':
            return head + (f"🔌 `{base_url(w)}` · 👤 `{w['user']}`\n\n🔐 **Verificación en dos pasos**\n\n"
                           "Envía en un mensaje al chat el código de tu app de autenticación.\n\n"
                           "_Solo te lo pido esta vez: el NAS recordará el bot como dispositivo de "
                           f"confianza («{DEVICE_NAME}»)._"), [self.WIZ_CANCEL]
        raise ValueError(step)

    async def _wiz_show(self, step, event=None, warn=""):
        self.awaiting_input = {'key': 'ds', 'type': f'ds_{step}'}
        text, buttons = self._wiz_prompt(step, warn)
        if event is not None and hasattr(event, 'data'):
            await self._safe_edit(event, text, buttons)
        else:
            await self.post_panel(text, buttons, view='other')

    def _wiz_next(self):
        w = self.ds_wizard
        if 'scheme' not in w:
            return 'scheme'
        if 'port' not in w:
            return 'port'
        return 'user' if 'user' not in w else 'pass'

    async def _wiz_finish(self, password, otp=None):
        conf = {'host': self.ds_wizard['host'], 'scheme': self.ds_wizard['scheme'],
                'port': self.ds_wizard.get('port'), 'user': self.ds_wizard['user']}
        self.awaiting_input = None
        await self.post_panel("🧲 **Conectando con Download Station…**", [], view='other')
        test = DSClient(base_url(conf), conf['user'], password)
        try:
            tasks = await asyncio.to_thread(lambda: (test.login(otp), test.tasks())[1])
        except DSError as e:
            if e.code == 403 and not otp:
                # pide el código: la contraseña ya es correcta y se guarda solo en memoria
                self.ds_wizard['_password'] = password
                await self._wiz_show('otp')
                return
            if e.code == 404 and otp:
                await self._wiz_show('otp', warn="Ese código no vale o ha caducado. Envía el que marque ahora la app.")
                return
            self.ds_wizard.pop('user', None)
            self.ds_wizard.pop('_password', None)
            await self._wiz_show('user', warn=f"No he podido entrar: {e}. Revisa el usuario y la contraseña.")
            return
        old = self.ds_conf
        conf['password'] = encrypt_secret(password)
        if test.device_id:
            conf['device'] = encrypt_secret(test.device_id)
        conf['send_torrents'] = old.get('send_torrents', True)
        # lo que ya estaba terminado no se vuelve a procesar
        conf['done_ids'] = sorted(t['id'] for t in tasks if t.get('status') in DONE)
        self.ds_conf = conf
        self._ds_client = test
        self.ds_tasks = tasks
        self.ds_error = None
        self.ds_wizard = {}
        self._ds_save()
        self._ds_wake.set()
        text, buttons = self.ds_view(f"✅ **Conectado.** Download Station tiene ahora {len(tasks)} tarea{'s' if len(tasks) != 1 else ''}.")
        await self.post_panel(text, buttons, view='other')

    async def handle_ds_text(self, event):
        """Pasos del asistente y enlaces magnet. Devuelve True si ha usado el mensaje."""
        if not event.message or (event.media and not hasattr(event.media, 'webpage')):
            return False
        text = (event.message.message or "").strip()
        aw = getattr(self, 'awaiting_input', None)
        if aw and aw.get('type', '').startswith('ds_') and text and not text.startswith('/'):
            await self._delete_message(event)
            step = aw['type'][3:]
            if step == 'host':
                parsed = parse_address(text)
                if not parsed:
                    await self._wiz_show('host', warn="Eso no parece una dirección. Prueba con la IP, por ejemplo `192.168.1.10`.")
                    return True
                host, scheme, port = parsed
                self.ds_wizard = {'host': host}
                if scheme:
                    self.ds_wizard['scheme'] = scheme
                if port:
                    self.ds_wizard['port'] = port
            elif step == 'port':
                if not text.isdigit() or not 0 < int(text) < 65536:
                    await self._wiz_show('port', warn="El puerto es un número, por ejemplo `5000`.")
                    return True
                self.ds_wizard['port'] = int(text)
            elif step == 'user':
                self.ds_wizard['user'] = text
            elif step == 'pass':
                await self._wiz_finish(text)
                return True
            elif step == 'otp':
                code = text.replace(' ', '')
                if '_password' not in self.ds_wizard:
                    self.ds_wizard.pop('user', None)
                    await self._wiz_show('user', warn="Se ha perdido la contraseña (¿reinicio?). Vuelve a escribir el usuario.")
                    return True
                if not OTP_RE.match(code):
                    await self._wiz_show('otp', warn="El código son 6 cifras, por ejemplo `123456`.")
                    return True
                await self._wiz_finish(self.ds_wizard['_password'], otp=code)
                return True
            else:
                await self._wiz_show(step, warn="Usa los botones.")
                return True
            await self._wiz_show(self._wiz_next())
            return True
        uri, kind, name = torrent_link(text)
        if uri:
            await self._delete_message(event)
            if not self.ds_send_torrents():
                await self.client.send_message(event.chat_id, f"🧲 Para los enlaces {kind} conecta Download Station "
                                                              "en **Ajustes → Download Station**.")
                return True
            try:
                await self.ds_add_magnet(uri)
                await self.client.send_message(event.chat_id, f"🧲 **Enlace {kind} enviado a Download Station**" +
                                               (f"\n📄 `{name}`" if name else ""))
            except Exception as e:
                await self.client.send_message(event.chat_id, f"❌ Download Station no ha aceptado el enlace: {e}")
            return True
        return False

    async def handle_ds_callback(self, event, data):
        if data == 'menu_ds':
            text, buttons = self.ds_view()
        elif data == 'cmd_ds_setup':
            self.ds_wizard = {}
            await self._wiz_show('host', event)
            return True
        elif data == 'cmd_ds_cancel':
            self.awaiting_input = None
            self.ds_wizard = {}
            text, buttons = self.ds_view("Cancelado, no he cambiado nada.")
        elif data.startswith('cmd_ds_scheme_'):
            if 'host' not in self.ds_wizard:
                return await self.handle_ds_callback(event, 'cmd_ds_setup')
            self.ds_wizard['scheme'] = data.rsplit('_', 1)[1]
            await self._wiz_show(self._wiz_next(), event)
            return True
        elif data == 'cmd_ds_port_default':
            if 'host' not in self.ds_wizard:
                return await self.handle_ds_callback(event, 'cmd_ds_setup')
            self.ds_wizard['port'] = None
            await self._wiz_show(self._wiz_next(), event)
            return True
        elif data == 'cmd_ds_test':
            await event.answer("Probando…")
            try:
                self.ds_tasks = await self.ds_call('tasks')
                self.ds_error = None
                note = f"✅ **Conexión correcta.** {len(self.ds_tasks)} tarea{'s' if len(self.ds_tasks) != 1 else ''} en Download Station."
            except Exception as e:
                self.ds_error = str(e)
                note = f"❌ **No conecta:** {e}"
            text, buttons = self.ds_view(note)
        elif data == 'cmd_ds_seed':
            text, buttons = self.ds_seed_view()
        elif data.startswith('cmd_ds_seedset_'):
            n = int(data.rsplit('_', 1)[1])
            self.ds_conf['seed_days'] = n if n in SEED_OPTIONS else None
            self._ds_save()
            self._ds_wake.set()
            text, buttons = self.ds_seed_view()
        elif data == 'menu_ds_done':
            text, buttons = self.ds_done_view()
        elif data.startswith('cmd_ds_del_'):
            t = self._ds_task(data[len('cmd_ds_del_'):])
            if not t:
                text, buttons = self.ds_done_view("Esa tarea ya no está.")
            else:
                placed = self.ds_conf.get('placed', {}).get(t['id'])
                text = (f"🗑️ **¿Quitar** `{t.get('title', '?')}`**?**\n\n"
                        + ("Ya está en tu biblioteca, así que puedes borrar también el archivo."
                           if placed else "⚠️ No está en tu biblioteca: si borras el archivo, se pierde."))
                buttons = [[Button.inline("🗑️ Quitar y borrar el archivo", data=f"cmd_ds_delf_{t['id']}")],
                           [Button.inline("➖ Solo quitar de Download Station", data=f"cmd_ds_delt_{t['id']}")],
                           [Button.inline("❌ No", data="menu_ds_done")]]
        elif data.startswith(('cmd_ds_delf_', 'cmd_ds_delt_')):
            t = self._ds_task(data[len('cmd_ds_delf_'):])
            if not t:
                text, buttons = self.ds_done_view("Esa tarea ya no está.")
            else:
                await event.answer("Quitando…")
                try:
                    note = await self._ds_remove(t, delete_files=data.startswith('cmd_ds_delf_'))
                    self.ds_tasks = [x for x in self.ds_tasks if x['id'] != t['id']]
                except Exception as e:
                    note = f"❌ No he podido quitarla: {e}"
                text, buttons = self.ds_done_view(note)
        elif data == 'cmd_ds_send':
            self.ds_conf['send_torrents'] = not self.ds_send_torrents()
            self._ds_save()
            text, buttons = self.ds_view()
        elif data == 'cmd_ds_forget_ask':
            text = ("🔌 **¿Desconectar Download Station?**\n\nSe borran la dirección, el usuario y la contraseña "
                    "guardados. Las descargas de Download Station no se tocan.")
            buttons = [[Button.inline("✅ Sí, desconectar", data="cmd_ds_forget"),
                        Button.inline("❌ No", data="menu_ds")]]
        elif data == 'cmd_ds_forget':
            self.ds_conf = {}
            self._ds_client = None
            self.ds_tasks = []
            self.ds_error = None
            try:
                os.remove(self.ds_file)
            except FileNotFoundError:
                pass
            text, buttons = self.ds_view("Desconectado. He borrado los datos guardados.")
        else:
            return False
        await self._safe_edit(event, text, buttons)
        return True
