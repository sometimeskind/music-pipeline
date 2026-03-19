# Run the test suite in dev containers (never pushed to registry)
test:
    docker build --target dev -t music-pipeline-fetch:dev fetch
    docker run --rm music-pipeline-fetch:dev
    docker build --target dev -t music-pipeline-scan:dev scan
    docker run --rm music-pipeline-scan:dev

# Install git hooks (run once after cloning)
hooks:
    git config core.hooksPath .githooks

# Run spotdl sync (fetch container: Spotify/YouTube → inbox)
fetch:
    op run --env-file .env.tpl -- docker compose run --rm fetch music-ingest

# Run a local scan: import inbox → .m3u (no Spotify/YouTube)
scan:
    docker compose run --rm scan music-scan

# Run full ingest: spotdl sync → import → .m3u
sync:
    just fetch && just scan

# Dump beets DB and export JSON from the container
backup:
    docker compose run --rm scan sh -c "beet export > /root/.config/beets/library-export.json"
    docker compose run --rm scan sh -c "sqlite3 /root/.config/beets/library.db .dump > /root/.config/beets/library-dump.sql"
