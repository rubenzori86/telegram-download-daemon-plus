# -*- coding: utf-8 -*-
import json
import logging
import os
import time

logger = logging.getLogger(__name__)


class ConfigManagerMixin:
    """Mixin para la gestión de configuración persistente y parámetros del sistema."""

    def load_config(self):
        """Carga la configuración desde el archivo JSON."""
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)

                if 'duplicates' in config:
                    self.duplicates = config['duplicates']
                    print(f"✅ Configuración cargada: duplicates = {self.duplicates}")

                if 'worker_count' in config:
                    self.worker_count = config['worker_count']
                    print(f"✅ Configuración cargada: worker_count = {self.worker_count}")

                if 'max_retries' in config:
                    self.max_retries = config['max_retries']
                    print(f"✅ Configuración cargada: max_retries = {self.max_retries}")

                if 'update_frequency' in config:
                    self.update_frequency = config['update_frequency']
                    print(f"✅ Configuración cargada: update_frequency = {self.update_frequency}")

                if 'auto_extract' in config:
                    self.auto_extract = config['auto_extract']
                    print(f"✅ Configuración cargada: auto_extract = {self.auto_extract}")

                if 'clean_parts_after_extract' in config:
                    self.clean_parts_after_extract = config['clean_parts_after_extract']
                    print(f"✅ Configuración cargada: clean_parts_after_extract = {self.clean_parts_after_extract}")

                if 'daily_summary_enabled' in config:
                    self.daily_summary_enabled = config['daily_summary_enabled']

                if 'auto_rename' in config:
                    self.auto_rename = config['auto_rename']
                    print(f"✅ Configuración cargada: auto_rename = {self.auto_rename}")

                if 'individual_progress_messages' in config:
                    self.individual_progress_messages = config['individual_progress_messages']
                    print(f"✅ Configuración cargada: individual_progress_messages = {self.individual_progress_messages}")

                if 'account_mode' in config:
                    self.account_mode = config['account_mode']
                    print(f"✅ Configuración cargada: account_mode = {self.account_mode}")

                if 'user_phone' in config:
                    self.user_phone = config['user_phone']
                    print(f"✅ Configuración cargada: user_phone = {self.user_phone}")

                print(f"✅ Configuración cargada desde: {self.config_file}")
            else:
                print(" Archivo de configuración no encontrado, usando valores por defecto")
                self.save_config()
        except Exception as e:
            print(f"❌ Error cargando configuración: {e}")
            print(" Usando valores por defecto")

    def save_config(self):
        """Guarda la configuración actual en el archivo JSON."""
        try:
            os.makedirs(os.path.dirname(self.config_file), exist_ok=True)

            config = {
                'duplicates': self.duplicates,
                'worker_count': self.worker_count,
                'max_retries': self.max_retries,
                'update_frequency': self.update_frequency,
                'auto_extract': self.auto_extract,
                'clean_parts_after_extract': self.clean_parts_after_extract,
                'auto_rename': getattr(self, 'auto_rename', True),
                'daily_summary_enabled': getattr(self, 'daily_summary_enabled', True),
                'individual_progress_messages': getattr(self, 'individual_progress_messages', False),
                'account_mode': getattr(self, 'account_mode', 'bot'),
                'user_phone': getattr(self, 'user_phone', None),
                'last_updated': time.strftime('%Y-%m-%d %H:%M:%S')
            }

            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2, ensure_ascii=False)

            print(f"✅ Configuración guardada en: {self.config_file}")
        except Exception as e:
            print(f"❌ Error guardando configuración: {e}")

    def show_duplicates_policy(self):
        """Muestra la política actual de duplicados."""
        policy_descriptions = {
            "ignore": "❌ Ignorar archivos duplicados (no descargar)",
            "rename": " Renombrar archivos duplicados (agregar sufijo)",
            "overwrite": "✅ Sobrescribir archivos duplicados (reemplazar)"
        }

        current_policy = self.duplicates
        description = policy_descriptions.get(current_policy, "❓ Política desconocida")

        return f" Política de duplicados actual:\n• {current_policy.upper()}: {description}\n\nComandos disponibles:\n• duplicados_ignorar\n• duplicados_renombrar\n• duplicados_sobrescribir"

    def set_duplicates_policy(self, new_policy):
        """Cambia la política de duplicados."""
        old_policy = self.duplicates
        self.duplicates = new_policy
        self.save_config()

        policy_names = {
            "ignore": "Ignorar",
            "rename": "Renombrar",
            "overwrite": "Sobrescribir"
        }

        return f"✅ Política de duplicados cambiada:\n• Anterior: {policy_names.get(old_policy, old_policy)}\n• Nueva: {policy_names.get(new_policy, new_policy)}\n• Configuración guardada automáticamente"

    def set_worker_count(self, new_count):
        """Cambia el número de workers."""
        old_count = self.worker_count
        self.worker_count = new_count
        self.save_config()
        return f"✅ Número de workers cambiado:\n• Anterior: {old_count}\n• Nuevo: {new_count}\n\nNota: El cambio completo se aplicará tras reiniciar el bot."

    def show_full_config(self):
        """Muestra toda la configuración actual."""
        policy_descriptions = {
            "ignore": "❌ Ignorar archivos duplicados",
            "rename": " Renombrar archivos duplicados",
            "overwrite": "✅ Sobrescribir archivos duplicados"
        }

        config_info = " Configuración completa del sistema:\n\n"
        config_info += f" Política de duplicados: {self.duplicates.upper()}\n"
        config_info += f"   {policy_descriptions.get(self.duplicates, '❓ Política desconocida')}\n\n"
        config_info += f" Workers de descarga: {self.worker_count}\n"
        config_info += f" Reintentos máximos: {self.max_retries}\n"
        config_info += f"⏱️ Frecuencia de actualización: {self.update_frequency}s\n"
        config_info += f" Auto-extracción: {self.auto_extract}\n"
        config_info += f" Limpieza de partes tras extraer: {self.clean_parts_after_extract}\n"
        config_info += f"🏷️ Renombrado pelis/series (FileBot): {getattr(self, 'auto_rename', False)}\n\n"
        config_info += f" Archivo de configuración: {self.config_file}\n"
        config_info += f" Archivo de descargas pendientes: {self.pending_downloads_file}\n\n"
        config_info += "Comandos disponibles:\n"
        config_info += "• duplicados, duplicados_ignorar, duplicados_renombrar, duplicados_sobrescribir\n"
        config_info += "• workers, retries, config, reset_config\n"
        config_info += "• auto_extract_on, auto_extract_off, limpiar_partes_on, limpiar_partes_off\n"
        config_info += "• renombrar_on, renombrar_off"

        return config_info

    def show_worker_config(self):
        """Muestra la configuración de workers."""
        return f" Configuración de workers:\n• Workers actuales: {self.worker_count}\n• Descargas activas: {len(self.active_downloads)}\n• Descargas en progreso: {len(self.in_progress)}"

    def show_retries_config(self):
        """Muestra la configuración de reintentos."""
        return f" Configuración de reintentos:\n• Reintentos máximos: {self.max_retries}\n• Frecuencia de actualización: {self.update_frequency}s"

    def reset_config_to_defaults(self):
        """Resetea la configuración a los valores por defecto."""
        try:
            old_duplicates = self.duplicates
            old_workers = self.worker_count
            old_retries = self.max_retries
            old_frequency = self.update_frequency

            self.duplicates = "rename"
            self.worker_count = 4
            self.max_retries = 3
            self.update_frequency = 10
            self.auto_extract = True
            self.clean_parts_after_extract = True

            self.save_config()

            return f" Configuración reseteada a valores por defecto:\n\n" \
                   f" Política de duplicados: {old_duplicates} → {self.duplicates}\n" \
                   f" Workers: {old_workers} → {self.worker_count}\n" \
                   f" Reintentos: {old_retries} → {self.max_retries}\n" \
                   f"⏱️ Frecuencia: {old_frequency}s → {self.update_frequency}s\n\n" \
                   f" Auto-extracción: True\n" \
                   f" Limpieza de partes tras extraer: True\n\n" \
                   f"✅ Configuración guardada automáticamente"
        except Exception as e:
            return f"❌ Error reseteando configuración: {str(e)}"

    def set_auto_extract(self, enabled: bool):
        old = self.auto_extract
        self.auto_extract = enabled
        self.save_config()
        return f"✅ Auto-extracción: {old} → {self.auto_extract}"

    def set_clean_parts_after_extract(self, enabled: bool):
        old = self.clean_parts_after_extract
        self.clean_parts_after_extract = enabled
        self.save_config()
        return f"✅ Limpieza de partes tras extraer: {old} → {self.clean_parts_after_extract}"
