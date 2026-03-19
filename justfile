# Run the test suite in a dev container (never pushed to registry)
test:
    docker build --target dev -t music-pipeline:dev .
    docker run --rm music-pipeline:dev

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

# Import files dropped into inbox (subset of scan)
import:
    docker compose run --rm scan music-import

# Add a new playlist (interactive)
setup:
    op run --env-file .env.tpl -- docker compose run --rm -it fetch music-setup

# Provision all playlists from config/playlists.conf (idempotent)
provision:
    op run --env-file .env.tpl -- docker compose run --rm fetch music-provision

# Remove a playlist: just remove <name>
remove name:
    docker compose run --rm scan music-remove {{name}}

# Dump beets DB and export JSON from the container
backup:
    docker compose run --rm scan sh -c "beet export > /root/.config/beets/library-export.json"
    docker compose run --rm scan sh -c "sqlite3 /root/.config/beets/library.db .dump > /root/.config/beets/library-dump.sql"
