# scan: beets import, AcoustID fingerprinting, .m3u generation.
# No Spotify or YouTube calls — reads inbox written by the fetch container.
FROM python:3.13-slim AS prod

# System dependencies: ffmpeg for audio processing, chromaprint for AcoustID fingerprinting.
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libchromaprint-tools \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY requirements.txt /requirements.txt
RUN pip install --no-cache-dir -r /requirements.txt

# Install the pipeline package — entry points land in /usr/local/bin/
COPY music_scan/ /app/music_scan/
COPY pyproject.toml /app/pyproject.toml
RUN pip install --no-cache-dir -e /app

# Pre-create directories that will be populated by volume mounts
RUN mkdir -p \
    /root/Music/inbox/spotdl \
    /root/Music/library \
    /root/Music/quarantine \
    /root/Music/playlists \
    /root/.config/beets \
    /root/.config/spotdl

CMD ["music-scan"]

# dev stage: extends prod with test deps; used by `just test`, never pushed to registry
FROM prod AS dev
COPY requirements-dev.txt /requirements-dev.txt
RUN pip install --no-cache-dir -r /requirements-dev.txt
COPY tests/ /app/tests/
WORKDIR /app
ENTRYPOINT []
CMD ["pytest"]
