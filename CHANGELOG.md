# Cambios

Aquí se anotan los cambios de cada versión.

## [1.6.0] - 2026-11-21

### Añadido

- **Días de compartir en Download Station:** en Ajustes → Download Station eliges cuánto se comparte lo terminado (1, 3, 5, 7, 10, 15, 20 o 30 días, o indefinido). Al cumplirse lo quita de Download Station y borra el archivo, solo si ya está en tu biblioteca; si no, deja el archivo y avisa.
- **Terminados:** lista de lo terminado en Download Station con los días que le quedan y botones para quitarlo a mano (con o sin borrar el archivo).

### Corregido

- **Enviar un .torrent a Download Station** fallaba siempre con el error 101: ahora usa la API DownloadStation2, la que acepta archivos.
- Si la sesión con Download Station caducaba, el bot no volvía a entrar hasta reiniciarse.

### Actualizado

- Python 3.15 en la imagen, y cffi 2.1.1, cryptg 0.6.0, humanize 4.16.0, pyasn1 0.6.5 y pycparser 3.12.
- Acciones de GitHub: checkout v7 y CodeQL v4.

### Para actualizar

- Esta vez hay que reconstruir la imagen, porque cambia la versión de Python: `git pull` y `docker compose up -d --build`.

## [1.5.1] - 2026-11-21

### Corregido

- **Partes incompletas:** un archivo por partes (RAR, 7z, ZIP) ya no se descomprime hasta que el bot comprueba con 7z que están todas y sin errores. Antes, con solo la primera parte, podía extraer una película cortada y colocarla en la biblioteca. Si faltan partes, avisa una vez.
- **Download Station:** antes de colocar lo terminado, espera a que los archivos lleven 2 minutos sin cambiar de tamaño ni de fecha.

## [1.5.0] - 2026-11-08

### Añadido

- **Download Station:** se conecta desde Ajustes. El bot pide por el chat la dirección, el puerto, el usuario y la contraseña, que se borra del chat y se guarda cifrada. Si la cuenta tiene verificación en dos pasos, pide el código una sola vez y el NAS recuerda el bot como dispositivo de confianza. Muestra el progreso en Descargas, manda allí los `.torrent`, los enlaces `magnet:` y los enlaces web a un `.torrent`, y avisa y coloca en la biblioteca lo que termina, sin cortar la compartición.
- **Nombres y MKV:** en Ajustes se puede quitar la calidad y el códec del nombre de las películas, y pasar los MP4 a MKV sin recomprimir. Se guarda y sigue activo tras reiniciar.
- **Actualizar desde el bot:** el aviso de versión nueva trae un botón **Actualizar**. El bot descarga la versión de GitHub, instala las librerías de Python que falten, comprueba que carga y se reinicia con ella, sin reconstruir la imagen. Si no arranca tres veces seguidas, vuelve solo a la anterior. En Sistema también se puede volver a la versión anterior.
- Si una versión cambia los programas del sistema, el bot no se actualiza solo y explica cómo reconstruir.

### Para actualizar

- Esta vez todavía a mano, porque cambia la imagen (la conversión a MKV necesita `mkvmerge`): `git pull` y `docker compose up -d --build`. Las siguientes versiones se actualizan desde el bot.

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

[1.6.0]: https://github.com/rubenzori86/telegram-download-daemon-plus/releases/tag/v1.6.0
[1.5.1]: https://github.com/rubenzori86/telegram-download-daemon-plus/releases/tag/v1.5.1
[1.5.0]: https://github.com/rubenzori86/telegram-download-daemon-plus/releases/tag/v1.5.0
[1.3.0]: https://github.com/rubenzori86/telegram-download-daemon-plus/releases/tag/v1.3.0
[1.2.1]: https://github.com/rubenzori86/telegram-download-daemon-plus/releases/tag/v1.2.1
[1.2.0]: https://github.com/rubenzori86/telegram-download-daemon-plus/releases/tag/v1.2.0
