# Telegram Download Daemon Plus

Daemon de Telegram que descarga los archivos que le reenvías (o los enlaces `t.me` que le pegas),
los descomprime, los renombra con FileBot y coloca las pelis y series en tu biblioteca.
Todo se maneja desde un panel con botones dentro de Telegram.

## Qué hace

- **Descargas** desde un canal/grupo: archivos reenviados, subidos directamente o enlaces `https://t.me/...`.
  Cola con varios workers, reintentos, pausa/reanudación y progreso en vivo.
- **Modo híbrido (opcional)**: además del bot, puede usar tu cuenta de usuario para descargar
  de canales donde el bot no tiene acceso. Se configura desde el propio panel.
- **Extracción automática** de `.zip`, `.rar`, `.7z` (incluidos multiparte) y limpieza de las partes.
- **Renombrado con FileBot** (TheMovieDB / TheTVDB):
  - Películas: `Título (Año) [1080p] [x265]`
  - Series: `Serie (Año)/Serie - 01x02 - Título del capítulo`
- **Biblioteca en varios discos**: cada capítulo va a la carpeta de su serie si ya existe;
  lo nuevo va al disco con más espacio libre, respetando una reserva. Distingue 4K e infantil.
  Nunca sobrescribe nada de lo que ya tienes.
- **IA opcional (Groq)** para adivinar el título cuando el nombre del archivo es raro; FileBot siempre confirma.
- Te **pregunta** por lo que no consigue identificar, guarda un historial y manda un resumen diario.

## Comandos

| Comando | Qué hace |
|---|---|
| `/menu`, `/start` | Panel de control |
| `/descargas`, `/estado` | Activas, en cola y pausadas |
| `/cola` | Gestionar la cola |
| `/biblioteca` | Espacio de los discos y últimas añadidas |
| `/buscar nombre` | Dónde está una serie o peli |
| `/pendientes` | Lo que no se pudo identificar o colocar |
| `/historial` | Últimos movimientos |
| `/archivos` | Archivos descargados |
| `/config` | Ajustes |
| `/limpiar` | Borra temporales |
| `/limpiarchat` | Deja el chat limpio con solo el panel |
| `/reanudar` | Reanuda lo pendiente |
| `/ayuda` | Ayuda |

## Instalación (Docker)

Necesitas Docker con docker compose.

```sh
git clone https://github.com/rubenzori86/telegram-download-daemon-plus.git
cd telegram-download-daemon-plus
cp .env.example .env
nano .env            # rellena tus datos
docker compose up -d --build
docker compose logs -f
```

### Qué rellenar en `.env`

Todo está explicado dentro de [`.env.example`](.env.example). Lo mínimo:

| Variable | De dónde sale |
|---|---|
| `TELEGRAM_DAEMON_API_ID` / `TELEGRAM_DAEMON_API_HASH` | https://my.telegram.org/apps |
| `TELEGRAM_DAEMON_BOT` | Token que te da [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_DAEMON_OWNER_ID` | Tu ID de usuario (p.ej. con [@userinfobot](https://t.me/userinfobot)) |
| `TELEGRAM_DAEMON_CHANNEL` | ID del canal/grupo donde está el bot (empieza por `-100`) |
| `DOWNLOADS_DIR` | Carpeta de tu máquina donde se guardan las descargas |

El bot tiene que ser administrador del canal.

### Opcional

- **FileBot**: copia tu instalación de FileBot (tiene licencia propia, no se incluye) en `./filebot-pro`
  de forma que exista `./filebot-pro/bin/filebot-pro`. Sin FileBot el daemon descarga y extrae, pero no renombra.
- **Biblioteca**: copia `docker-compose.override.example.yml` como `docker-compose.override.yml`,
  monta ahí tus discos y ponlos en `LIBRARY_DISKS` del `.env`. Prueba primero con `LIBRARY_DRY_RUN=1`.
- **IA**: crea una clave gratis en https://console.groq.com y ponla en `GROQ_API_KEY`.

### Datos que se guardan

| Carpeta | Contenido |
|---|---|
| `session/` | Sesiones de Telegram, configuración e historial. **Es privada, no la compartas.** |
| `temp/` | Descargas en curso |
| `filebot-data/` | Caché e historial de FileBot |

## Créditos y licencia

Este proyecto empezó como fork de
[Nyaran/telegram-download-daemon-ng](https://github.com/Nyaran/telegram-download-daemon-ng),
que a su vez parte de [alfem/telegram-download-daemon](https://github.com/alfem/telegram-download-daemon).
Gracias a sus autores.

Se distribuye bajo la licencia **GNU GPL v3**, igual que el original. Consulta [LICENSE.md](LICENSE.md).
