# fetch: spotdl sync, Spotify/YouTube network calls.
FROM python:3.13-slim AS prod

# ffmpeg required by spotdl (audio download and format handling)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

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

CMD ["music-ingest"]

# dev stage: extends prod with test deps; used by `just test`, never pushed to registry
FROM prod AS dev
COPY requirements-dev.txt /requirements-dev.txt
RUN pip install --no-cache-dir -r /requirements-dev.txt
COPY tests/ /app/tests/
WORKDIR /app
ENTRYPOINT []
CMD ["pytest"]
