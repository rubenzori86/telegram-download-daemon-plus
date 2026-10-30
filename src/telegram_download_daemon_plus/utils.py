import os
import random
import re
import string
from mimetypes import guess_extension

from telethon.tl.types import DocumentAttributeFilename, DocumentAttributeVideo

# Sufijo compartido para archivos temporales
TELEGRAM_DAEMON_TEMP_SUFFIX = "tdd"

EXTENSION_EMOJI = {
    '.mkv': '🎬', '.mp4': '🎬', '.avi': '🎬', '.mov': '🎬',
    '.rar': '📦', '.zip': '📦', '.7z': '📦',
    '.mp3': '🎵', '.flac': '🎵', '.wav': '🎵',
    '.pdf': '📄', '.epub': '📚',
    '.jpg': '🖼️', '.jpeg': '🖼️', '.png': '🖼️',
}


def get_file_emoji(filename):
    """Devuelve el emoji correspondiente a la extensión del archivo."""
    ext = os.path.splitext(filename)[1].lower()
    return EXTENSION_EMOJI.get(ext, '📁')


def sanitize_path_component(name: str, fallback: str = "Desconocido") -> str:
    """Sanitiza un nombre de carpeta/archivo derivado de una fuente externa (p.ej. título de canal).

    - Elimina separadores de ruta y secuencias '..'
    - Elimina caracteres de control no imprimibles
    - Recorta espacios en los extremos
    - Devuelve `fallback` si el resultado queda vacío
    - Limita la longitud a 200 caracteres
    """
    if not name:
        return fallback
    # Eliminar separadores de ruta y '..'
    name = name.replace('/', '_').replace('\\', '_')
    name = re.sub(r'\.\.+', '.', name)
    # Eliminar caracteres de control no imprimibles (U+0000–U+001F y U+007F)
    name = re.sub(r'[\x00-\x1f\x7f]', '', name)
    name = name.strip()
    # Limitar longitud
    name = name[:200]
    return name if name else fallback


class UtilsMixin:
    """Utilidades genéricas y helpers de nombres de archivo."""

    def get_random_id(self, length):
        chars = string.ascii_lowercase + string.digits
        return ''.join(random.choice(chars) for _ in range(length))

    @staticmethod
    def get_filename(event_or_message):
        """Obtiene un nombre de archivo seguro a partir de un evento o mensaje de Telethon.
        
        Funciona igual que el original: acepta eventos o mensajes directamente.
        """
        media_file_name = "unknown"

        # Acceder al media directamente como en el original
        # Si es un mensaje, tiene 'media' directamente
        # Si es un evento, puede tener 'media' o 'message.media'
        if hasattr(event_or_message, 'media') and event_or_message.media:
            media = event_or_message.media
            message_obj = event_or_message
        elif hasattr(event_or_message, 'message'):
            # Puede ser un evento con event.message.media
            if hasattr(event_or_message.message, 'media') and event_or_message.message.media:
                media = event_or_message.message.media
                message_obj = event_or_message.message
            else:
                return "unknown"
        else:
            return "unknown"

        if hasattr(media, 'photo'):
            media_file_name = str(media.photo.id) + ".jpeg"
        elif hasattr(media, 'document'):
            for attribute in media.document.attributes:
                if isinstance(attribute, DocumentAttributeFilename):
                    media_file_name = attribute.file_name
                    break
                if isinstance(attribute, DocumentAttributeVideo):
                    # Obtener el texto del mensaje como en el original
                    message_text = getattr(message_obj, 'message', '')
                    if message_text != '':
                        media_file_name = message_text.split(os.linesep)[0]
                    else:
                        media_file_name = str(media.document.id)
                    media_file_name += guess_extension(media.document.mime_type)
                    break

        media_file_name = "".join(c for c in media_file_name if c.isalnum() or c in "()._- ")
        return media_file_name
