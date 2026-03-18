FROM python:3.14-slim

# System dependencies: ffmpeg for audio processing, cron for scheduled ingest.
# jq removed — JSON processing is now done in Python.
RUN apt-get update && apt-get install -y \
    ffmpeg \
    cron \
    libchromaprint-tools \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies: pinned in requirements.txt for Dependabot tracking
COPY requirements.txt /requirements.txt
RUN pip install --no-cache-dir -r /requirements.txt

# Install the pipeline package — entry points land in /usr/local/bin/
COPY pipeline/ /app/pipeline/
COPY pyproject.toml /app/pyproject.toml
RUN pip install --no-cache-dir -e /app

# Entrypoint: writes cron schedule from env then execs CMD
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Pre-create directories that will be populated by volume mounts
RUN mkdir -p \
    /root/Music/inbox/spotdl \
    /root/Music/library \
    /root/Music/quarantine \
    /root/Music/playlists \
    /root/.config/beets \
    /root/.config/spotdl \
    /root/.config/music-pipeline

ENTRYPOINT ["/entrypoint.sh"]
CMD ["cron", "-f"]
