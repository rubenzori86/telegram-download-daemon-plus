FROM python:3.13

# Variables de entorno
ENV TELEGRAM_DAEMON_DEST="/downloads"
ENV TELEGRAM_DAEMON_SESSION_PATH="/session"
ENV TELEGRAM_DAEMON_TEMP="/temp"

# FileBot Pro (montado en /opt/filebot-pro) para renombrar pelis y series
ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    FILEBOT_BIN=/opt/filebot-pro/bin/filebot-pro \
    JAVA_TOOL_OPTIONS="-Djava.awt.headless=true -Xmx512m -Dapplication.dir=/filebot-data -Duser.home=/filebot-data -Dsun.jnu.encoding=UTF-8 -Dfile.encoding=UTF-8"

# Carpeta de trabajo
WORKDIR /usr/src/app

# Copia solo requirements.txt primero para aprovechar la caché de Docker
COPY requirements.txt .

# Instala utilidades del sistema para extracción y dependencias de Python
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       p7zip-full \
       unzip \
       unrar-free \
       unar \
       libmediainfo0v5 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install -r requirements.txt

# Copia el resto del código
COPY src/ .

# Comando por defecto al levantar el contenedor
CMD ["python3", "-m", "telegram_download_daemon_plus"]
