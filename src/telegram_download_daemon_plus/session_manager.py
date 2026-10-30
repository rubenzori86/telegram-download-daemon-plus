from os import getenv, path

from telethon.sessions import StringSession

TELEGRAM_DAEMON_SESSION_PATH = getenv("TELEGRAM_DAEMON_SESSION_PATH")
default_session_name = "DownloadDaemon"


def _get_string_session_if_exists(session_name=default_session_name):
    session_path = path.join(TELEGRAM_DAEMON_SESSION_PATH,
                             "{0}.session".format(session_name))
    if path.isfile(session_path):
        with open(session_path, 'r') as file:
            session = file.read()
            print("Sesion cargada desde {0}".format(session_path))
            return session
    return None


def get_session(session_name=default_session_name):
    if TELEGRAM_DAEMON_SESSION_PATH is None:
        return session_name

    return StringSession(_get_string_session_if_exists(session_name))


def save_session(session, session_name=default_session_name):
    if TELEGRAM_DAEMON_SESSION_PATH is not None:
        session_path = path.join(TELEGRAM_DAEMON_SESSION_PATH, "{0}.session".format(session_name))
        with open(session_path, 'w') as file:
            file.write(StringSession.save(session))
        print("Sesion guardada en {0}".format(session_path))

