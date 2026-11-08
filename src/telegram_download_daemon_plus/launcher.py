# -*- coding: utf-8 -*-
"""
Actualizaciones desde el bot, sin reconstruir la imagen.

La versión nueva se descarga de GitHub a session/code/<versión>/ y el bot se reinicia. Al
arrancar, este lanzador mira si allí hay una versión más nueva que la de la imagen y, si la
hay, vuelve a lanzar el bot con ella. Si esa versión falla al arrancar 3 veces seguidas,
vuelve sola a la anterior.

Si una versión necesita cambiar la imagen (programas del sistema, como mkvmerge), sube
REQUIRED_IMAGE y el Dockerfile su TDD_IMAGE: el bot no se actualiza solo y explica cómo
reconstruir. Las librerías de Python nuevas sí se instalan solas en session/code/pydeps.

Este archivo solo usa la biblioteca estándar: lo carga también la versión antigua.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

PACKAGE = "telegram_download_daemon_plus"
REQUIRED_IMAGE = 1          # súbelo junto con TDD_IMAGE del Dockerfile al cambiar la imagen
MAX_BOOTS = 3
TIMEOUT = 60


def image_version():
    try:
        return int(os.environ.get("TDD_IMAGE", "1"))
    except ValueError:
        return 1


def session_dir():
    path = os.environ.get("TELEGRAM_DAEMON_SESSION_PATH") or ("/session" if os.path.isdir("/session") else "session")
    return path


def code_root():
    return os.path.join(session_dir(), "code")


def state_file():
    return os.path.join(code_root(), "state.json")


def parse_version(text):
    m = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', (text or "").strip())
    return tuple(int(n) for n in m.groups()) if m else None


def newer(a, b):
    pa, pb = parse_version(a), parse_version(b)
    return bool(pa and pb) and pa > pb


def load_state():
    try:
        with open(state_file(), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    os.makedirs(code_root(), exist_ok=True)
    tmp = state_file() + ".tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, state_file())


def version_dir(version):
    return os.path.join(code_root(), version)


def has_code(version):
    return bool(version) and os.path.isfile(os.path.join(version_dir(version), PACKAGE, "__init__.py"))


def pydeps_dir():
    return os.path.join(code_root(), "pydeps")


def running_version():
    """Versión descargada con la que se ejecuta ahora (None = la de la imagen)."""
    return os.environ.get("TDD_RUNNING_OVERLAY") or None


# ---------- arranque ----------
def choose(state, baked):
    """Qué versión descargada usar (o None para la de la imagen). Puede cambiar el estado:
    si la activa ha fallado demasiadas veces, pasa a la anterior."""
    for _ in range(3):
        active = state.get('active')
        if not active or not newer(active, baked) or not has_code(active):
            return None
        if state.get('boots', 0) < MAX_BOOTS:
            return active
        # no arranca: se vuelve a la anterior (o a la de la imagen)
        failed = state.setdefault('failed', [])
        if active not in failed:
            failed.append(active)
        prev = state.get('previous')
        state['rolled_back'] = {'from': active, 'to': prev or baked}
        state['active'] = prev if prev and newer(prev, baked) and has_code(prev) else None
        state['previous'] = None
        state['boots'] = 0
    return None


def maybe_reexec(baked_version):
    """Llamado al principio de __main__. Si hay una versión descargada más nueva, se
    reemplaza este proceso por el bot con esa versión."""
    if running_version():
        return
    try:
        state = load_state()
        before = json.dumps(state, sort_keys=True)
        version = choose(state, baked_version)
        if version:
            state['boots'] = state.get('boots', 0) + 1
        if json.dumps(state, sort_keys=True) != before:
            save_state(state)
    except Exception as e:  # el lanzador nunca debe impedir arrancar
        print(f"⚠️ Lanzador: {e}; arranco con la versión de la imagen", flush=True)
        return
    if not version:
        return
    env = dict(os.environ)
    paths = [version_dir(version)]
    if os.path.isdir(pydeps_dir()):
        paths.append(pydeps_dir())
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["TDD_RUNNING_OVERLAY"] = version
    print(f"🔁 Arrancando la versión descargada {version}", flush=True)
    # -P: que la carpeta actual (código de la imagen) no tape la versión descargada
    argv = [sys.executable, "-P", "-m", PACKAGE] + sys.argv[1:]
    if os.name == 'nt':  # en Windows execve no reemplaza el proceso
        sys.exit(subprocess.call(argv, env=env))
    os.execve(sys.executable, argv, env)


def mark_healthy():
    """El bot ha arrancado bien: se reinicia la cuenta de intentos."""
    version = running_version()
    state = load_state()
    if version and state.get('active') == version and state.get('boots'):
        state['boots'] = 0
        save_state(state)


def pop_start_notes():
    """Avisos del último arranque (actualizado / vuelto atrás). Se muestran una vez."""
    state = load_state()
    notes = {k: state.pop(k) for k in ('updated', 'rolled_back') if k in state}
    if notes:
        save_state(state)
    return notes


# ---------- instalar ----------
class UpdateError(Exception):
    pass


def download(repo, version, dest):
    url = f"https://codeload.github.com/{repo}/tar.gz/refs/tags/v{version}"
    req = urllib.request.Request(url, headers={'User-Agent': 'telegram-download-daemon'})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r, open(dest, 'wb') as f:
        shutil.copyfileobj(r, f)


def extract(archive, work):
    """Saca del .tar.gz la carpeta del paquete y requirements.txt. Devuelve su raíz."""
    with tarfile.open(archive, 'r:gz') as tar:
        members = []
        for m in tar.getmembers():
            parts = m.name.split('/', 1)
            if len(parts) < 2:
                continue
            rel = parts[1]
            if rel == "requirements.txt" or rel.startswith(f"src/{PACKAGE}/"):
                if m.issym() or m.islnk() or '..' in rel.split('/') or rel.startswith('/'):
                    raise UpdateError(f"archivo sospechoso en la descarga: {m.name}")
                members.append(m)
        if not any(m.name.endswith(f"src/{PACKAGE}/__init__.py") for m in members):
            raise UpdateError("la descarga no trae el código del bot")
        kwargs = {'filter': 'data'} if hasattr(tarfile, 'data_filter') else {}
        tar.extractall(work, members=members, **kwargs)
    tops = [d for d in os.listdir(work) if os.path.isdir(os.path.join(work, d))]
    if len(tops) != 1:
        raise UpdateError("estructura de la descarga inesperada")
    return os.path.join(work, tops[0])


def required_image_of(package_dir):
    try:
        text = open(os.path.join(package_dir, "launcher.py"), encoding='utf-8').read()
    except OSError:
        return 1
    m = re.search(r'^REQUIRED_IMAGE\s*=\s*(\d+)', text, re.M)
    return int(m.group(1)) if m else 1


def version_of(package_dir):
    text = open(os.path.join(package_dir, "panel.py"), encoding='utf-8').read()
    m = re.search(r'^DAEMON_VERSION\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else None


def missing_requirements(req_file):
    """Líneas 'paquete==versión' que no están instaladas con esa versión."""
    from importlib import metadata
    missing = []
    try:
        lines = open(req_file, encoding='utf-8').read().splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        m = re.match(r'^([A-Za-z0-9_.\-]+)==([^\s;]+)$', line)
        if not m:
            missing.append(line)
            continue
        try:
            if metadata.version(m.group(1)) != m.group(2):
                missing.append(line)
        except metadata.PackageNotFoundError:
            missing.append(line)
    return missing


def install_requirements(lines, log):
    log(f"📦 Instalando librerías: {', '.join(lines)}")
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--no-cache-dir", "--upgrade",
                        "--target", pydeps_dir()] + lines,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors='ignore', timeout=900)
    if r.returncode != 0:
        raise UpdateError("no se pudieron instalar las librerías nuevas:\n" + (r.stdout or '')[-400:])


def check_imports(code_dir):
    """Prueba en otro proceso que la versión nueva se puede cargar."""
    env = dict(os.environ)
    paths = [code_dir] + ([pydeps_dir()] if os.path.isdir(pydeps_dir()) else [])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env.pop("TDD_RUNNING_OVERLAY", None)
    r = subprocess.run([sys.executable, "-P", "-c",
                        f"import {PACKAGE}, {PACKAGE}.__main__, {PACKAGE}.panel as p; print(p.DAEMON_VERSION)"],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors='ignore',
                       timeout=120, env=env, cwd=tempfile.gettempdir())
    if r.returncode != 0:
        raise UpdateError("la versión nueva no carga:\n" + (r.stdout or '')[-600:])
    return r.stdout.strip().splitlines()[-1] if r.stdout.strip() else None


def install(repo, version, current_version, log=print, archive=None):
    """Descarga, comprueba y deja lista la versión. No reinicia: eso lo hace quien llama."""
    if not parse_version(version):
        raise UpdateError(f"versión no válida: {version}")
    os.makedirs(code_root(), exist_ok=True)
    work = tempfile.mkdtemp(prefix="update-", dir=code_root())
    try:
        if archive is None:
            log(f"⬇️ Descargando la {version} de GitHub…")
            archive = os.path.join(work, "src.tar.gz")
            download(repo, version, archive)
        root = extract(archive, os.path.join(work, "x"))
        package_dir = os.path.join(root, "src", PACKAGE)
        found = version_of(package_dir)
        if found != version:
            raise UpdateError(f"la descarga dice ser la {found}, no la {version}")
        need = required_image_of(package_dir)
        if need > image_version():
            raise UpdateError("REBUILD")
        missing = missing_requirements(os.path.join(root, "requirements.txt"))
        if missing:
            install_requirements(missing, log)
        target = version_dir(version)
        staging = target + ".new"
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging)
        shutil.copytree(package_dir, os.path.join(staging, PACKAGE),
                        ignore=shutil.ignore_patterns('__pycache__'))
        log("🧪 Comprobando que la versión nueva carga…")
        try:
            check_imports(staging)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        shutil.rmtree(target, ignore_errors=True)
        os.replace(staging, target)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    state = load_state()
    running = running_version()
    state.update(active=version, previous=running if running and running != version else None,
                 boots=0, updated={'from': current_version, 'to': version})
    state.pop('rolled_back', None)
    if version in state.get('failed', []):
        state['failed'].remove(version)
    save_state(state)
    prune(state)


def prune(state):
    """Borra versiones descargadas que ya no hacen falta."""
    keep = {state.get('active'), state.get('previous')}
    for name in os.listdir(code_root()):
        path = os.path.join(code_root(), name)
        if parse_version(name) and name not in keep and os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)


def rollback(baked_version):
    """Vuelve a la versión anterior. Devuelve a cuál, o None si no hay a cuál volver."""
    state = load_state()
    running = running_version()
    if not running:
        return None
    prev = state.get('previous')
    target = prev if prev and has_code(prev) and newer(prev, baked_version) else None
    state.update(active=target, previous=None, boots=0,
                 updated={'from': running, 'to': target or baked_version})
    save_state(state)
    return target or baked_version
