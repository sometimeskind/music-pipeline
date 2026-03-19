# Run the test suite in a dev container (never pushed to registry)
test:
    docker build --target dev -t music-pipeline:dev .
    docker run --rm music-pipeline:dev

# Install git hooks (run once after cloning)
hooks:
    git config core.hooksPath .githooks

# Run full ingest: spotdl sync → import → .m3u → Navidrome rescan
sync:
    op run --env-file .env.tpl -- docker compose run --rm pipeline music-ingest

# Run a local scan: import inbox → .m3u → Navidrome rescan (no Spotify/YouTube)
scan:
    docker compose run --rm pipeline music-scan

# Import files dropped into inbox (subset of scan)
import:
    docker compose run --rm pipeline music-import

# Add a new playlist (interactive)
setup:
    op run --env-file .env.tpl -- docker compose run --rm -it pipeline music-setup

# Provision all playlists from config/playlists.conf (idempotent)
provision:
    op run --env-file .env.tpl -- docker compose run --rm pipeline music-provision

# Remove a playlist: just remove <name>
remove name:
    docker compose run --rm pipeline music-remove {{name}}

# Dump beets DB and export JSON from the container
backup:
    docker compose run --rm pipeline sh -c "beet export > /root/.config/beets/library-export.json"
    docker compose run --rm pipeline sh -c "sqlite3 /root/.config/beets/library.db .dump > /root/.config/beets/library-dump.sql"
