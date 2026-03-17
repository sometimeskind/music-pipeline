FROM python:3.12-slim

# System dependencies: ffmpeg for audio processing, jq for .spotdl JSON diffing,
# cron for scheduled ingest
RUN apt-get update && apt-get install -y \
    ffmpeg \
    jq \
    cron \
    chromaprint-tools \
    && rm -rf /var/lib/apt/lists/*

# beets: audio library manager + tagger
# spotdl: Spotify-to-local downloader
# pyacoustid: Python bindings for Chromaprint/AcoustID (beets chroma plugin)
RUN pip install --no-cache-dir beets spotdl pyacoustid

# Scripts go on PATH
COPY scripts/ /usr/local/bin/
RUN chmod +x \
    /usr/local/bin/music-setup \
    /usr/local/bin/music-ingest \
    /usr/local/bin/music-import \
    /usr/local/bin/music-remove

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
    /root/.config/spotdl

ENTRYPOINT ["/entrypoint.sh"]
CMD ["cron", "-f"]
