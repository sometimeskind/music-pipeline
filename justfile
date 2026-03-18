# Run full ingest (download + import + M3U + quarantine)
sync:
    op run --env-file .env.tpl -- docker compose exec pipeline music-ingest

# Add a new playlist (interactive)
setup:
    op run --env-file .env.tpl -- docker compose exec -it pipeline music-setup

# Remove a playlist: just remove <name>
remove name:
    docker compose exec pipeline music-remove {{name}}

# Import files dropped into inbox
import:
    docker compose exec pipeline music-import

# Trigger Navidrome library rescan
# TODO: fill in Navidrome host, user, and password (store password in 1Password)
# rescan:
#     curl -s "http://<navidrome-host>/rest/startScan?u=<user>&p=<pass>&v=1.16.1&c=music-pipeline&f=json"

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
    docker compose exec pipeline sh -c \
        "beet export > /root/.config/beets/library-export.json && \
         sqlite3 /root/.config/beets/library.db .dump > /root/.config/beets/library-dump.sql"
