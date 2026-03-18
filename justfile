# Run the test suite in a dev container (never pushed to registry)
test:
    docker build --target dev -t music-pipeline:dev .
    docker run --rm music-pipeline:dev

# Install git hooks (run once after cloning)
hooks:
    git config core.hooksPath .githooks

# Run full ingest (download + import + M3U + quarantine)
sync:
    op run --env-file .env.tpl -- docker compose exec pipeline music-ingest

# Add a new playlist (interactive)
setup:
    op run --env-file .env.tpl -- docker compose run --rm -it pipeline music-setup

# Provision all playlists from config/playlists.conf (idempotent)
provision:
    op run --env-file .env.tpl -- docker compose run --rm -it pipeline music-provision

# Remove a playlist: just remove <name>
remove name:
    docker compose exec pipeline music-remove {{name}}

# Import files dropped into inbox
import:
    docker compose exec pipeline music-import

# Tail container logs
logs:
    docker compose logs -f pipeline

# Start the pipeline container
up:
    op run --env-file .env.tpl -- docker compose up -d

# Stop the pipeline container
down:
    docker compose down

# Dump beets DB and export JSON from the container
backup:
    docker compose exec pipeline sh -c "beet export > /root/.config/beets/library-export.json"
    docker compose exec pipeline sh -c "sqlite3 /root/.config/beets/library.db .dump > /root/.config/beets/library-dump.sql"
