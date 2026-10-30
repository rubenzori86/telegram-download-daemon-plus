# -*- coding: utf-8 -*-
"""
Colocación inteligente de pelis y series ya renombradas en la biblioteca (los discos de LIBRARY_DISKS).

Reglas:
  - Capítulo de una serie que ya existe en algún disco -> a esa carpeta, con el nombre de la carpeta.
  - Serie nueva -> Series o Series_Infantiles (animación), en el disco con más espacio libre.
  - Película -> Peliculas / Peliculas_Infantiles, o Peliculas_4k / Peliculas_4k_Infantiles si es 2160p,
    en el disco con más espacio. Si ya existe en la misma calidad (4K o no 4K), no se toca.
  - Nunca se renombra, mueve ni sobrescribe nada de lo que ya hay en la biblioteca.
"""
import logging
import os
import re
import shutil
import subprocess
import unicodedata

logger = logging.getLogger(__name__)

# "NOMBRE=/ruta,NOMBRE2=/ruta2"  (vacío = sin biblioteca: todo se queda en renombrados/)
LIBRARY_DISKS = [
    tuple(x.split('=', 1)) for x in
    os.environ.get("LIBRARY_DISKS", "").split(',')
    if '=' in x
]
RESERVE_BYTES = int(float(os.environ.get("LIBRARY_RESERVE_GB", "100")) * 1024 ** 3)

SERIES_CATEGORY = {False: "Series", True: "Series_Infantiles"}
MOVIE_CATEGORY = {
    (False, False): "Peliculas", (True, False): "Peliculas_Infantiles",
    (False, True): "Peliculas_4k", (True, True): "Peliculas_4k_Infantiles",
}

FOLDER_RE = re.compile(r'^(?P<title>.*?)\s*\(?(?P<year>(?:19|20)\d{2})\)?\s*$')
MOVIE_RE = re.compile(r'^(?P<title>.+?) \((?P<year>\d{4})\) \[(?P<res>[^\]]+)\]')
EPISODE_RE = re.compile(r' - (?P<code>\d{2,3}x\d{2,4}) - ')


def normalize(text):
    """Minúsculas, sin acentos ni signos: 'X-Men '97' -> 'xmen97'."""
    text = unicodedata.normalize('NFKD', text or '')
    text = ''.join(c for c in text if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]', '', text.lower())


def split_title_year(name):
    m = FOLDER_RE.match(name)
    if m:
        return m.group('title').strip(), m.group('year')
    return name.strip(), None


def _listdir(path):
    try:
        return os.listdir(path)
    except OSError:
        return []


def _category_dirs(prefix):
    """(disco, categoría, ruta) de las carpetas de categoría que empiezan por prefix."""
    for disk, root in LIBRARY_DISKS:
        for cat in sorted(_listdir(root)):
            path = os.path.join(root, cat)
            if cat.startswith(prefix) and os.path.isdir(path):
                yield disk, cat, path


def free_bytes(path):
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def choose_disk(category, size):
    """Disco con más espacio libre que tenga la categoría y respete la reserva."""
    best = None
    for disk, root in LIBRARY_DISKS:
        path = os.path.join(root, category)
        if not os.path.isdir(path):
            continue
        free = free_bytes(path)
        if free - size < RESERVE_BYTES:
            continue
        if best is None or free > best[2]:
            best = (disk, path, free)
    return best


def find_series(show_title, show_year):
    """Busca la serie en todas las categorías Series* de todos los discos.
    Devuelve (disco, ruta_carpeta, titulo_carpeta) o None."""
    key = normalize(show_title)
    by_name = []
    for disk, _cat, cat_path in _category_dirs("Series"):
        for folder in _listdir(cat_path):
            full = os.path.join(cat_path, folder)
            if folder.startswith('@') or not os.path.isdir(full):
                continue
            title, year = split_title_year(folder)
            if normalize(title) != key:
                continue
            if show_year and year == show_year:
                return disk, full, title
            by_name.append((disk, full, title))
    # mismo nombre con otro año (o sin año): solo si es inequívoco
    if len(by_name) == 1:
        return by_name[0]
    return None


def movie_exists(title, year, is_4k):
    """Rutas de la misma peli (título+año) en la misma calidad (4K / no 4K)."""
    key = (normalize(title), year)
    found = []
    for _disk, _cat, cat_path in _category_dirs("Peliculas"):
        for f in _listdir(cat_path):
            m = MOVIE_RE.match(f)
            if not m or (normalize(m.group('title')), m.group('year')) != key:
                continue
            if (m.group('res').lower() == '2160p') == is_4k:
                found.append(os.path.join(cat_path, f))
    return found


def episode_exists(folder, code):
    pattern = re.compile(r' - ' + re.escape(code) + r'( - |\.)')
    # los .part son copias a medias (p.ej. cortadas por un reinicio): no cuentan
    return [f for f in _listdir(folder) if pattern.search(f) and not f.endswith('.part')]


def plan_series(staged_path, is_anim):
    """Decide destino de un capítulo. staged_path: .../Serie (Año)/Serie - 01x02 - Titulo.mkv"""
    show_folder = os.path.basename(os.path.dirname(staged_path))
    filename = os.path.basename(staged_path)
    show_title, show_year = split_title_year(show_folder)
    m = EPISODE_RE.search(filename)
    if not m:
        return {'ok': False, 'reason': f"no se reconoce el número de capítulo en '{filename}'"}
    code = m.group('code')
    rest = filename[m.start():]  # " - 01x02 - Titulo.mkv"

    existing = find_series(show_title, show_year)
    if existing:
        disk, folder, folder_title = existing
        dupes = episode_exists(folder, code)
        if dupes:
            return {'ok': False, 'duplicate': True, 'disk': disk,
                    'existing': os.path.join(folder, dupes[0]),
                    'dest_if_replace': os.path.join(folder, folder_title + rest),
                    'reason': f"el capítulo {code} ya existe en {disk}: {dupes[0]}"}
        return {'ok': True, 'new': False, 'disk': disk,
                'dest': os.path.join(folder, folder_title + rest)}

    category = SERIES_CATEGORY[is_anim]
    size = os.path.getsize(staged_path)
    choice = choose_disk(category, size)
    if not choice:
        return {'ok': False, 'reason': f"ningún disco con '{category}' tiene espacio (reserva {RESERVE_BYTES // 1024**3} GB)"}
    disk, cat_path, _free = choice
    return {'ok': True, 'new': True, 'disk': disk,
            'dest': os.path.join(cat_path, show_folder, filename)}


def plan_movie(staged_path, is_anim):
    filename = os.path.basename(staged_path)
    m = MOVIE_RE.match(filename)
    if not m:
        return {'ok': False, 'reason': f"nombre de película no reconocido: '{filename}'"}
    is_4k = m.group('res').lower() == '2160p'
    dupes = movie_exists(m.group('title'), m.group('year'), is_4k)
    if dupes:
        disk = next((d for d, root in LIBRARY_DISKS if dupes[0].startswith(root + os.sep)), '?')
        return {'ok': False, 'duplicate': True, 'disk': disk, 'existing': dupes[0],
                'dest_if_replace': os.path.join(os.path.dirname(dupes[0]), filename),
                'reason': f"ya existe en {'4K' if is_4k else 'esa calidad'}: {dupes[0]}"}
    category = MOVIE_CATEGORY[(is_anim, is_4k)]
    choice = choose_disk(category, os.path.getsize(staged_path))
    if not choice:
        return {'ok': False, 'reason': f"ningún disco con '{category}' tiene espacio (reserva {RESERVE_BYTES // 1024**3} GB)"}
    disk, cat_path, _free = choice
    return {'ok': True, 'new': True, 'disk': disk, 'dest': os.path.join(cat_path, filename)}


VIDEO_EXT = ('.mkv', '.mp4', '.avi', '.m4v', '.mov', '.wmv', '.ts', '.m2ts', '.mpg', '.mpeg', '.webm')


def search(query, limit=15):
    """Busca series (carpetas) y películas (archivos) cuyo nombre contenga query."""
    key = normalize(query)
    if not key:
        return []
    results = []
    for disk, cat, cat_path in _category_dirs("Series"):
        for folder in sorted(_listdir(cat_path)):
            if folder.startswith('@') or key not in normalize(folder):
                continue
            full = os.path.join(cat_path, folder)
            files = [f for f in _listdir(full) if f.lower().endswith(VIDEO_EXT)]
            seasons = sorted({m.group('code').split('x')[0] for m in (EPISODE_RE.search(f) for f in files) if m})
            results.append({'kind': 'serie', 'disk': disk, 'category': cat, 'name': folder,
                            'episodes': len(files), 'seasons': seasons})
    for disk, cat, cat_path in _category_dirs("Peliculas"):
        for f in sorted(_listdir(cat_path)):
            if f.lower().endswith(VIDEO_EXT) and key in normalize(f):
                results.append({'kind': 'peli', 'disk': disk, 'category': cat, 'name': f})
    return results[:limit]


def stats():
    """Recuento de series, capítulos y películas por disco y categoría."""
    out = {}
    for disk, cat, cat_path in list(_category_dirs("Series")) + list(_category_dirs("Peliculas")):
        d = out.setdefault(disk, {'series': 0, 'episodes': 0, 'movies': 0, 'categories': {}})
        if cat.startswith("Series"):
            folders = [f for f in _listdir(cat_path) if not f.startswith('@') and os.path.isdir(os.path.join(cat_path, f))]
            eps = sum(len([x for x in _listdir(os.path.join(cat_path, f)) if x.lower().endswith(VIDEO_EXT)]) for f in folders)
            d['series'] += len(folders)
            d['episodes'] += eps
            d['categories'][cat] = f"{len(folders)} series · {eps} caps"
        else:
            n = len([f for f in _listdir(cat_path) if f.lower().endswith(VIDEO_EXT)])
            d['movies'] += n
            d['categories'][cat] = f"{n} pelis"
    return out


TRASH_DIRNAME = ".reemplazados"   # oculta en Samba (hide dot files); se vacía sola
TRASH_DAYS = int(os.environ.get("LIBRARY_TRASH_DAYS", "14"))


def media_info(path):
    """Resolución, códec, tamaño y duración reales del archivo (MediaInfo)."""
    info = {'size': os.path.getsize(path) if os.path.exists(path) else 0}
    try:
        from pymediainfo import MediaInfo
        mi = MediaInfo.parse(path)
        for t in mi.tracks:
            if t.track_type == 'Video':
                h, w = int(t.height or 0), int(t.width or 0)
                info['res'] = ("2160p" if h > 1100 or w > 2000 else "1080p" if h > 760 or w > 1300
                               else "720p" if h > 500 else f"{h}p")
                fmt = (t.format or "").upper()
                info['codec'] = {'HEVC': 'h265', 'AVC': 'h264', 'AV1': 'AV1'}.get(fmt, fmt.lower())
                if t.hdr_format:
                    info['hdr'] = "HDR"
                break
        for t in mi.tracks:
            if t.track_type == 'General' and t.duration:
                info['duration'] = float(t.duration) / 1000
                break
    except Exception as e:
        logger.debug("MediaInfo no disponible para %s: %s", path, e)
    return info


def _disk_root_of(path):
    for _disk, root in LIBRARY_DISKS:
        if os.path.abspath(path).startswith(root + os.sep):
            return root
    return None


def move_to_trash(path):
    """Aparta un archivo de la biblioteca a <disco>/.reemplazados (mismo subvolumen: instantáneo)."""
    root = _disk_root_of(path)
    if root is None:
        raise ValueError(f"{path} no está en la biblioteca")
    trash = os.path.join(root, TRASH_DIRNAME)
    os.makedirs(trash, exist_ok=True)
    stamp = __import__('time').strftime('%Y%m%d-%H%M%S')
    dest = os.path.join(trash, f"{stamp} {os.path.basename(path)}")
    os.rename(path, dest)
    return dest


def purge_trash(days=TRASH_DAYS):
    """Borra lo apartado hace más de `days` días. Devuelve (archivos, bytes)."""
    import time as _t
    limit = _t.time() - days * 86400
    n = freed = 0
    for _disk, root in LIBRARY_DISKS:
        trash = os.path.join(root, TRASH_DIRNAME)
        for f in _listdir(trash):
            p = os.path.join(trash, f)
            try:
                if os.path.isfile(p) and os.path.getmtime(p) < limit:
                    freed += os.path.getsize(p)
                    os.remove(p)
                    n += 1
            except OSError:
                pass
    return n, freed


def move_into_library(src, dest):
    """Mueve sin sobrescribir nunca. Mismo volumen: rename instantáneo.
    Entre volúmenes: copia a .part, comprueba tamaño, renombra y borra el original."""
    if os.path.exists(dest):
        raise FileExistsError(dest)
    parent = os.path.dirname(dest)
    created_parent = not os.path.isdir(parent)
    os.makedirs(parent, exist_ok=True)
    # propietario de referencia: un archivo hermano si lo hay, si no la carpeta
    owner_ref = parent if not created_parent else os.path.dirname(parent)
    siblings = [os.path.join(parent, f) for f in sorted(_listdir(parent))
                if not f.startswith(('@', '.')) and os.path.isfile(os.path.join(parent, f))]
    if siblings:
        owner_ref = siblings[0]
    try:
        os.rename(src, dest)
    except OSError:
        tmp = dest + ".part"
        # Otro subvolumen btrfs del mismo disco (p.ej. Share → otra carpeta compartida): el clon (reflink)
        # es instantáneo y no ocupa espacio extra; si no se puede, cp hace copia normal
        try:
            subprocess.run(["cp", "--reflink=auto", "--", src, tmp], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except (OSError, subprocess.CalledProcessError):
            if os.path.exists(tmp):
                os.remove(tmp)
            shutil.copyfile(src, tmp)
        if os.path.getsize(tmp) != os.path.getsize(src):
            os.remove(tmp)
            raise IOError(f"copia incompleta hacia {dest}")
        if os.path.exists(dest):
            os.remove(tmp)
            raise FileExistsError(dest)
        os.rename(tmp, dest)
        os.remove(src)
    # mismo propietario que la carpeta de la biblioteca, para verlo bien por SMB
    try:
        st = os.stat(owner_ref)
        targets = [dest] + ([parent] if created_parent else [])
        for t in targets:
            os.chown(t, st.st_uid, st.st_gid)
    except OSError as e:
        logger.debug("No se pudo ajustar propietario de %s: %s", dest, e)
    return dest
