# -*- coding: utf-8 -*-
"""
Gestión de descargas completadas y configuración de progreso individual.
"""
import datetime
import logging
import time

# Robustez 8: logger de módulo
logger = logging.getLogger(__name__)


class CompletionManagerMixin:
    """Mixin para gestionar descargas completadas y progreso individual."""

    def __init_completion_manager__(self):
        """Inicializa las estructuras de datos para gestión de completadas."""
        self.completed_downloads = {}  # link -> {'filename': str, 'completed_at': timestamp, 'extraction_message_id': int}
        self.individual_progress_messages = False  # Por defecto desactivado

    def register_completed_download(self, link, filename, download_message_id=None, origin_message_id=None):
        """Registra una descarga completada con sus mensajes asociados."""
        if link not in self.completed_downloads:
            self.completed_downloads[link] = {
                'filename': filename,
                'completed_at': time.time(),
                'extraction_message_id': None,
                'download_message_id': download_message_id,
                'origin_message_id': origin_message_id
            }
        else:
            # Actualizar datos existentes
            self.completed_downloads[link]['filename'] = filename
            if download_message_id:
                self.completed_downloads[link]['download_message_id'] = download_message_id
            if origin_message_id:
                self.completed_downloads[link]['origin_message_id'] = origin_message_id

    def register_extraction_message(self, origin_link, message_id):
        """Registra el ID del mensaje de extracción asociado a una descarga completada."""
        # 1. Intentar registrar usando el link proporcionado (clave primaria)
        if origin_link in self.completed_downloads:
            self.completed_downloads[origin_link]['extraction_message_id'] = message_id
            print(f"DEBUG: Mensaje de extracción registrado para {origin_link}")
            return

        # 2. Buscar si el origin_link coincide con algún link de descarga original guardado
        # (A veces el link de descarga es el archivo, pero el origin_link es el mensaje)
        for link, data in self.completed_downloads.items():
            # Si tenemos guardado el link de origen...
            if 'origin_link' in data and data['origin_link'] == origin_link:
                data['extraction_message_id'] = message_id
                print(f"DEBUG: Mensaje de extracción asociado a {link} (via origin_link)")
                return
            
        # 3. Fallback: Registrar como nueva entrada solo con este mensaje
        # Esto permite que delete_completed_messages lo encuentre y borre
        print(f"DEBUG: Creando entrada aislada para mensaje de extracción {origin_link}")
        self.completed_downloads[origin_link] = {
            'filename': 'Extraction Message',
            'completed_at': time.time(),
            'extraction_message_id': message_id,
            'download_message_id': None, # No asociado a descarga conocida
            'origin_message_id': None
        }

    async def delete_completed_messages(self, client):
        """Borra todos los mensajes asociados a descargas completadas."""
        if not self.completed_downloads:
            return "📋 **No hay descargas completadas para borrar**"
        
        deleted_count = 0
        failed_count = 0
        chat_id = self.raw_channel_id
        
        all_messages_to_delete = set()
        
        for link, data in self.completed_downloads.items():
            # 1. Mensaje de notificación de descarga completada (el que dice "Descarga completada: archivo.rar")
            if data.get('download_message_id'):
                all_messages_to_delete.add(data['download_message_id'])
            
            # 2. Mensaje de extracción exitosa (si existe)
            if data.get('extraction_message_id'):
                all_messages_to_delete.add(data['extraction_message_id'])
            
            # 3. Mensaje original (forward o source)
            # Primero intentar usar el ID guardado explícitamente
            if data.get('origin_message_id'):
                all_messages_to_delete.add(data['origin_message_id'])
            else:
                # Si no, intentar parsear del link
                try:
                    parts = link.split('/')
                    if len(parts) >= 5 and parts[-1].isdigit():
                        all_messages_to_delete.add(int(parts[-1]))
                except Exception:
                    pass

        if not all_messages_to_delete:
            self.completed_downloads.clear()
            return "✅ Registro limpiado (no había mensajes borrables detectados)."

        # Convertir a lista para batching
        msg_ids_list = list(all_messages_to_delete)
        
        # Eliminar en lotes de 100
        for i in range(0, len(msg_ids_list), 100):
            batch = msg_ids_list[i:i+100]
            try:
                await client.delete_messages(chat_id, batch, revoke=True)
                deleted_count += len(batch)
            except Exception as e:
                print(f"Error eliminando lote de mensajes completados: {e}")
                failed_count += len(batch)
        
        # Limpiar registro
        self.completed_downloads.clear()
        
        return f"🗑️ **Limpieza completada:**\n✅ {deleted_count} mensajes borrados\n❌ {failed_count} fallidos"

    def set_individual_progress(self, enabled: bool):
        """Configura si se muestran mensajes individuales de progreso."""
        old = self.individual_progress_messages
        self.individual_progress_messages = enabled
        if hasattr(self, 'save_config'):
            self.save_config()
        return f"✅ Progreso individual: {old} → {self.individual_progress_messages}"
