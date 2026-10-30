# -*- coding: utf-8 -*-
"""
Ayudante con IA (Groq) para interpretar nombres de archivo raros. Solo SUGIERE:
lo que devuelve se usa como búsqueda exacta para FileBot (que debe confirmarlo) o como
botón propuesto en la pregunta al usuario. Nunca coloca nada por sí solo.
"""
import json
import logging
import os
import urllib.request

logger = logging.getLogger(__name__)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

PROMPT = """Eres un experto en catalogar películas y series (a menudo en castellano o latino).
Te doy el nombre de un archivo de vídeo descargado de Telegram y, a veces, el texto del mensaje.
Deduce qué es. Corrige faltas, tildes y palabras juntas (p.ej. "DragonBall" -> "Dragon Ball").
Usa el título oficial más conocido (preferiblemente el que usa TheTVDB/TheMovieDB en español si existe).
Responde SOLO un JSON con estas claves:
  "type": "series" | "movie" | "unknown",
  "title": título de la serie o película (sin año, sin calidad, sin idioma),
  "year": año de estreno (número) o null si no lo sabes con seguridad,
  "season": número de temporada o null,
  "episode": número de capítulo o null,
  "confidence": número de 0 a 1.
Si el nombre es solo un número (p.ej. "3.mp4") y no hay más contexto, usa "unknown"."""


def ai_enabled():
    return bool(GROQ_API_KEY)


def ask_ai(filename, caption=None, library_hint=None, timeout=20):
    """Devuelve un dict con type/title/year/season/episode/confidence, o None."""
    if not GROQ_API_KEY:
        return None
    user = f"Archivo: {filename}"
    if caption:
        user += f"\nTexto del mensaje: {caption[:500]}"
    if library_hint:
        user += f"\nSeries que ya hay en la biblioteca y podrían encajar: {', '.join(library_hint[:15])}"
    body = json.dumps({
        "model": GROQ_MODEL,
        "messages": [{"role": "system", "content": PROMPT}, {"role": "user", "content": user}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }).encode()
    req = urllib.request.Request(GROQ_URL, data=body, headers={
        "Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json",
        "User-Agent": "telegram-download-daemon-plus"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
        out = json.loads(data["choices"][0]["message"]["content"])
        if out.get("type") not in ("series", "movie") or not out.get("title"):
            return None
        for k in ("year", "season", "episode"):
            try:
                out[k] = int(out[k]) if out.get(k) not in (None, "") else None
            except (TypeError, ValueError):
                out[k] = None
        logger.info("🤖 IA: %s → %s", filename, out)
        return out
    except Exception as e:
        logger.warning("IA no disponible para %s: %s", filename, e)
        return None
