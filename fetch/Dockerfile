# fetch: spotdl sync, Spotify/YouTube network calls.
# No ffmpeg or chromaprint needed — beets is not installed here.
FROM python:3.13-slim AS prod

# Python dependencies
COPY requirements.txt /requirements.txt
RUN pip install --no-cache-dir -r /requirements.txt

# Install the pipeline package — entry points land in /usr/local/bin/
COPY pipeline/ /app/pipeline/
COPY pyproject.toml /app/pyproject.toml
RUN pip install --no-cache-dir -e /app

# Pre-create directories that will be populated by volume mounts
RUN mkdir -p \
    /root/Music/inbox/spotdl \
    /root/.config/spotdl \
    /root/.config/music-pipeline


# dev stage: extends prod with test deps; used by `just test`, never pushed to registry
FROM prod AS dev
COPY requirements-dev.txt /requirements-dev.txt
RUN pip install --no-cache-dir -r /requirements-dev.txt
COPY tests/ /app/tests/
WORKDIR /app
ENTRYPOINT []
CMD ["pytest"]
