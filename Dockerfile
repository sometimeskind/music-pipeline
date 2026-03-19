# fetch stage: spotdl sync, Spotify/YouTube network calls.
# No ffmpeg or chromaprint needed — beets is not installed here.
FROM python:3.14-slim AS fetch

# Python dependencies: pinned in requirements-fetch.txt for Dependabot tracking
COPY requirements-fetch.txt /requirements-fetch.txt
RUN pip install --no-cache-dir -r /requirements-fetch.txt

# Install the pipeline package — entry points land in /usr/local/bin/
COPY pipeline/ /app/pipeline/
COPY pyproject.toml /app/pyproject.toml
RUN pip install --no-cache-dir -e /app

# Pre-create directories that will be populated by volume mounts
RUN mkdir -p \
    /root/Music/inbox/spotdl \
    /root/.config/spotdl \
    /root/.config/music-pipeline


# scan stage: beets import, AcoustID fingerprinting, .m3u generation.
# No Spotify or YouTube calls — reads inbox written by the fetch container.
FROM python:3.14-slim AS scan

# System dependencies: ffmpeg for audio processing, chromaprint for AcoustID fingerprinting.
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libchromaprint-tools \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies: pinned in requirements-scan.txt for Dependabot tracking
COPY requirements-scan.txt /requirements-scan.txt
RUN pip install --no-cache-dir -r /requirements-scan.txt

# Install the pipeline package — entry points land in /usr/local/bin/
COPY pipeline/ /app/pipeline/
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


# dev stage: scan + all fetch deps + test deps; used by `just test`, never pushed to registry
FROM scan AS dev
COPY requirements-dev.txt /requirements-dev.txt
RUN pip install --no-cache-dir -r /requirements-dev.txt
COPY tests/ /app/tests/
WORKDIR /app
ENTRYPOINT []
CMD ["pytest"]
