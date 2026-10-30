import os
import json
from telethon import TelegramClient
from .session_manager import get_session, save_session

def setup_user_login(args):
    print("🔑 Iniciando proceso de login interactivo para cuenta de usuario...")
    print("Este proceso requiere que introduzcas los datos solicitados en esta terminal.\n")
    
    # Obtener sesión DownloadDaemon_user
    session = get_session("DownloadDaemon_user")
    
    # Inicializar cliente de Telethon para el login
    client = TelegramClient(session, args.api_id, args.api_hash)
    
    try:
        # start() pedirá interactivamente teléfono, código SMS y 2FA
        client.start()
        
        # Guardar la sesión persistente en disco
        save_session(client.session, "DownloadDaemon_user")
        
        # Obtener información del usuario logueado
        me = client.loop.run_until_complete(client.get_me())
        phone = me.phone if hasattr(me, 'phone') and me.phone else "Desconocido"
        user_name = f"{me.first_name or ''} {me.last_name or ''}".strip()
        print(f"\n✅ ¡Login completado con éxito!")
        print(f"👤 Cuenta: {user_name} (Teléfono: +{phone})")
        
        # Guardar el teléfono en el config.json para que aparezca en el menú del bot
        config_file = "/session/config.json" if os.path.exists("/session") else os.path.join("session", "config.json")
        config = {}
        if os.path.exists(config_file):
            try:
                with open(config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
            except Exception as e:
                print(f"⚠️ Advertencia al leer config.json: {e}")
                
        config['user_phone'] = f"+{phone}" if phone != "Desconocido" else None
        
        try:
            os.makedirs(os.path.dirname(config_file), exist_ok=True)
            with open(config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            print(f"✅ Configuración actualizada con el teléfono de la cuenta.")
        except Exception as e:
            print(f"⚠️ Error al guardar config.json: {e}")
            
        print("\n✨ La sesión de usuario se ha guardado de forma persistente.")
        print("Ya puedes ir al menú del bot en Telegram y seleccionar 'Cambiar a modo Usuario'.")
        
    except Exception as e:
        print(f"\n❌ Ocurrió un error durante el inicio de sesión: {e}")
    finally:
        if client.is_connected():
            client.disconnect()
