# fetch: spotdl sync, Spotify/YouTube network calls.
FROM python:3.13-slim AS prod

# ffmpeg required by spotdl
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Deno >= 2.3 is required by yt-dlp's EJS JS-challenge solver (Deno is yt-dlp's default runtime).
# Debian's nodejs (20.x) is below yt-dlp's Node >= 22 floor, so Node is not an option here.
COPY --from=docker.io/denoland/deno:bin-2.9.5 /deno /usr/local/bin/deno

# Python dependencies
COPY requirements.txt /requirements.txt
RUN pip install --no-cache-dir -r /requirements.txt

# Install the pipeline package — entry points land in /usr/local/bin/
COPY music_fetch/ /app/music_fetch/
COPY pyproject.toml /app/pyproject.toml
RUN pip install --no-cache-dir -e /app

# Pre-create directories that will be populated by volume mounts
RUN mkdir -p \
    /root/Music/inbox/spotdl \
    /root/.config/spotdl \
    /root/.config/music-pipeline

CMD ["music-ingest"]

# dev stage: extends prod with test deps; used by `just test`, never pushed to registry
FROM prod AS dev
COPY requirements-dev.txt /requirements-dev.txt
RUN pip install --no-cache-dir -r /requirements-dev.txt
COPY tests/ /app/tests/
WORKDIR /app
ENTRYPOINT []
CMD ["pytest"]
