# UDB-Sonarr: auto-download missing Sonarr episodes via UDB site clients
# Base: slim Python — UDB needs ffmpeg + Chrome (undetected_chromedriver)
FROM python:3.11-slim

# Install system deps:
#  - ffmpeg: required by UDB's HLSDownloader (check_ffmpeg fails without it)
#  - chromium/chromedriver: required by undetected_chromedriver (AnimePahe client)
#  - build tools: needed to compile quickjs-ng if no wheel is available
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        chromium \
        chromium-driver \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Config directory — mount your config_sonarr.yaml here.
# Using a directory (/config) instead of a single file so the container
# works cleanly with Unraid's GUI, docker-compose, and plain docker run.
RUN mkdir -p /config /tv /downloads
ENV PYTHONUNBUFFERED=1
ENV CONFIG_FILE=/config/config_sonarr.yaml

# Entrypoint: run the daemon with the mounted config
ENTRYPOINT ["python3", "udb_sonarr.py"]
CMD ["-c", "/config/config_sonarr.yaml"]
