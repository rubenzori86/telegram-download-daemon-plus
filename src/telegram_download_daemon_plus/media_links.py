# -*- coding: utf-8 -*-
"""
Enlaces de TMDB y TheTVDB pegados en Telegram para decir qué es un archivo sin ambigüedad.

  themoviedb.org/movie/1559984-my-weird-school  -> película por ID de TMDB
  themoviedb.org/tv/1402-the-walking-dead       -> serie por ID de TMDB
  thetvdb.com/series/the-walking-dead           -> serie por ID de TVDB
  thetvdb.com/movies/the-matrix                 -> película (TVDB enlaza con su ID de TMDB)

Las URL de TVDB llevan un nombre y no el número: el ID se lee de su página. FileBot no
tiene pelis en TVDB, por eso una película de TVDB acaba siempre buscándose en TMDB.
"""
import html
import logging
import re
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

TMDB_URL = re.compile(r'(?i)(?:https?://)?(?:www\.)?themoviedb\.org/(?:[a-z]{2}(?:-[a-z]{2})?/)?'
                      r'(?P<kind>movie|tv)/(?P<id>\d+)(?:-(?P<slug>[^\s/?#]+))?[^\s]*')
TVDB_URL = re.compile(r'(?i)(?:https?://)?(?:www\.)?thetvdb\.com/(?:[a-z]{2,3}/)?'
                      r'(?P<kind>series|movies)/(?P<slug>[^\s/?#]+)[^\s]*')

TVDB_PAGE_ID = re.compile(r'TheTVDB\.com\s+(?:Series|Movie)\s+ID</strong>\s*<span>\s*(\d+)')
TVDB_PAGE_TMDB_MOVIE = re.compile(r'themoviedb\.org/movie/(\d+)')
TVDB_PAGE_TITLE = re.compile(r'<title>\s*(.+?)\s*-\s*TheTVDB\.com', re.S)

USER_AGENT = "telegram-download-daemon"


class LinkError(Exception):
    """El enlace no se puede usar; el texto se muestra tal cual al usuario."""


def find_link(text):
    """Primer enlace de TMDB o TVDB del texto, o None."""
    if not text:
        return None
    m = TMDB_URL.search(text)
    if m:
        return {'site': 'tmdb', 'kind': 'movie' if m.group('kind').lower() == 'movie' else 'series',
                'id': m.group('id'), 'slug': m.group('slug'), 'url': m.group(0)}
    m = TVDB_URL.search(text)
    if m:
        return {'site': 'tvdb', 'kind': 'movie' if m.group('kind').lower() == 'movies' else 'series',
                'slug': m.group('slug'), 'url': m.group(0)}
    return None


def text_without_link(text, link):
    return text.replace(link['url'], ' ').strip()


def _slug_title(slug):
    return re.sub(r'[-_]+', ' ', urllib.parse.unquote(slug)).strip().title() if slug else None


def _fetch(url):
    if not url.lower().startswith('http'):
        url = 'https://' + url
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode('utf-8', 'ignore')


def resolve(link):
    """Convierte el enlace en lo que necesita FileBot:
    {'kind': 'movie'|'series', 'db': …, 'id': '123', 'title': nombre o None}.
    Hace una petición de red con los enlaces de TVDB: llamar fuera del bucle de eventos."""
    if link['site'] == 'tmdb':
        if not link.get('slug'):
            # TMDB bloquea las peticiones automáticas y FileBot necesita el nombre para buscar
            raise LinkError("A ese enlace le falta el nombre. Copia la URL completa de la ficha, "
                            "como `themoviedb.org/movie/603-the-matrix`.")
        return {'kind': link['kind'], 'db': 'TheMovieDB' if link['kind'] == 'movie' else 'TheMovieDB::TV',
                'id': link['id'], 'title': _slug_title(link.get('slug'))}

    slug = link['slug']
    if link['kind'] == 'series' and slug.isdigit():
        return {'kind': 'series', 'db': 'TheTVDB', 'id': slug, 'title': None}
    try:
        page = _fetch(f"https://thetvdb.com/{'movies' if link['kind'] == 'movie' else 'series'}/{slug}")
    except Exception as e:
        logger.warning("No se pudo leer %s en TVDB: %s", slug, e)
        raise LinkError("No he podido abrir esa página de TheTVDB. Comprueba el enlace.") from e
    m = TVDB_PAGE_TITLE.search(page)
    title = html.unescape(m.group(1)).strip() if m else _slug_title(slug)
    if link['kind'] == 'movie':
        m = TVDB_PAGE_TMDB_MOVIE.search(page)
        if not m:
            raise LinkError("TheTVDB no enlaza esa película con TMDB. Pásame el enlace de themoviedb.org.")
        return {'kind': 'movie', 'db': 'TheMovieDB', 'id': m.group(1), 'title': title}
    m = TVDB_PAGE_ID.search(page)
    if not m:
        raise LinkError("No encuentro el ID de la serie en esa página de TheTVDB.")
    return {'kind': 'series', 'db': 'TheTVDB', 'id': m.group(1), 'title': title}
