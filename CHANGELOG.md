# Cambios

Aquí se anotan los cambios de cada versión.

## [1.4.0] - 2026-11-08

### Añadido

- **Download Station:** se conecta desde Ajustes. El bot pide por el chat la dirección, el puerto, el usuario y la contraseña, que se borra del chat y se guarda cifrada. Si la cuenta tiene verificación en dos pasos, pide el código una sola vez y el NAS recuerda el bot como dispositivo de confianza. Muestra el progreso en Descargas, manda allí los `.torrent`, los enlaces `magnet:` y los enlaces web a un `.torrent`, y avisa y coloca en la biblioteca lo que termina, sin cortar la compartición.
- **Nombres y MKV:** en Ajustes se puede quitar la calidad y el códec del nombre de las películas, y pasar los MP4 a MKV sin recomprimir. Se guarda y sigue activo tras reiniciar.

### Para actualizar

- Hay que reconstruir la imagen, porque la conversión a MKV necesita `mkvmerge`: `docker compose up -d --build`.

## [1.3.0] - 2026-11-06

### Añadido

- **Archivos `.torrent`:** con `TORRENTS_DIR` definida, los `.torrent` enviados al canal van a esa carpeta para que los recoja tu cliente de torrents. Después hay que aplicar el cambio con `docker compose up -d`.

## [1.2.1] - 2026-11-06

### Añadido

- **Aviso de versiones nuevas:** el bot mira GitHub cada 12 horas y avisa en el canal con las novedades y cómo actualizar. También hay un botón en Sistema. Se desactiva con `UPDATE_CHECK=0`.

### Corregido

- El panel mostraba la versión 1.1.0 en vez de la instalada.

## [1.2.0] - 2026-11-06

### Añadido

- **Enlaces de TMDB y TheTVDB:** si no identifica una peli o serie, envía su enlace al chat y la coloca por su ID.
- **Botón «Dar enlace»** en las preguntas y en Biblioteca → Pendientes.
- **Pide el enlace solo** si corriges el nombre y sigue sin encontrarlo.
- **Varios archivos esperando:** pregunta para cuál es el enlace en vez de adivinar.

### Corregido

- Los enlaces con vista previa de Telegram ya no se tratan como comandos.

[1.4.0]: https://github.com/rubenzori86/telegram-download-daemon-plus/releases/tag/v1.4.0
[1.3.0]: https://github.com/rubenzori86/telegram-download-daemon-plus/releases/tag/v1.3.0
[1.2.1]: https://github.com/rubenzori86/telegram-download-daemon-plus/releases/tag/v1.2.1
[1.2.0]: https://github.com/rubenzori86/telegram-download-daemon-plus/releases/tag/v1.2.0
