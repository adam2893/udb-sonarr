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

# Default config is provided via the example; real config should be
# volume-mounted at /app/config_sonarr.yaml (see docker-compose.yml)
ENV PYTHONUNBUFFERED=1

# Entrypoint: run the daemon with a mounted config
ENTRYPOINT ["python3", "udb_sonarr.py"]
CMD ["-c", "/app/config_sonarr.yaml"]
