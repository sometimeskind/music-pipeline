# service: unified music-pipeline — fetch + scan + HTTP API
# Build context: repo root
FROM python:3.13-slim AS prod

# System deps: all of fetch (ffmpeg, nodejs) + scan (ffmpeg, chromaprint) + rclone
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ffmpeg nodejs libchromaprint-tools rclone \
    && rm -rf /var/lib/apt/lists/*

# Install all three packages
COPY fetch/music_fetch/ /build/fetch/music_fetch/
COPY fetch/pyproject.toml /build/fetch/pyproject.toml
COPY fetch/requirements.txt /build/fetch/requirements.txt
RUN pip install --no-cache-dir -r /build/fetch/requirements.txt /build/fetch

COPY scan/music_scan/ /build/scan/music_scan/
COPY scan/pyproject.toml /build/scan/pyproject.toml
COPY scan/requirements.txt /build/scan/requirements.txt
RUN pip install --no-cache-dir -r /build/scan/requirements.txt /build/scan
# Expose scan source at the beets pluginpath location expected by config.yaml
RUN mkdir -p /app && cp -r /build/scan/music_scan /app/music_scan

COPY service/music_service/ /build/service/music_service/
COPY service/pyproject.toml /build/service/pyproject.toml
COPY service/requirements.txt /build/service/requirements.txt
RUN pip install --no-cache-dir -r /build/service/requirements.txt /build/service

# Pre-create directories
RUN mkdir -p \
    /root/Music/inbox/spotdl \
    /root/Music/staging \
    /root/Music/quarantine \
    /root/Music/playlists \
    /root/.config/beets \
    /root/.config/spotdl \
    /root/.config/music-pipeline \
    /root/.config/yt-dlp

RUN echo --js-runtimes node > /root/.config/yt-dlp/config

CMD ["music-pipeline"]

FROM prod AS dev
COPY service/requirements-dev.txt /tmp/reqs-dev.txt
RUN pip install --no-cache-dir -r /tmp/reqs-dev.txt
COPY service/tests/ /app/tests/
COPY scripts/ /app/scripts/
WORKDIR /app
ENTRYPOINT []
CMD ["pytest", "tests/", "scripts/tests/"]
