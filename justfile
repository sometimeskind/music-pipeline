# Run the test suite in a dev container (never pushed to registry)
# --network=host: build container shares host network stack so systemd-resolved DNS works
test:
    docker build --network=host --target dev -f service/Dockerfile -t music-pipeline-service:dev .
    docker run --rm music-pipeline-service:dev

# Build the unified service image
build:
    docker build -f service/Dockerfile -t music-pipeline:local .

# Run container integration tests against the current GHCR image (no auth needed)
# Override the image with: SCAN_IMAGE=music-pipeline-scan:local just test-integration
test-integration:
    [ -d .venv ] || python3 -m venv .venv
    .venv/bin/pip install -q -r tests/requirements.txt
    .venv/bin/pytest tests/ -m "not auth" -v

# Run service integration tests (no auth needed)
# Override the image with: SERVICE_IMAGE=music-pipeline:local just test-service
test-service:
    [ -d .venv ] || python3 -m venv .venv
    .venv/bin/pip install -q -r tests/requirements.txt
    .venv/bin/pytest tests/test_service_*.py -m "not auth" -v

# Run auth-required integration tests (local only; requires Spotify credentials via 1Password)
# Set TEST_PLAYLIST_URL to a small playlist before running: export TEST_PLAYLIST_URL=https://...
test-auth:
    [ -d .venv ] || python3 -m venv .venv
    .venv/bin/pip install -q -r tests/requirements.txt
    op run --env-file .env.tpl -- .venv/bin/pytest tests/ -m auth -v

# Install git hooks (run once after cloning)
hooks:
    git config core.hooksPath .githooks

# Run spotdl sync (fetch via service container)
fetch:
    op run --env-file .env.tpl -- docker compose run --rm service music-ingest

# Run a local scan
scan:
    docker compose run --rm service music-scan

# Run full ingest
sync:
    just fetch && just scan

# Show tracks currently in MISS backoff (backed off after repeated 'no source found' failures)
list-failures:
    docker compose run --rm service sh -c "cat /root/Music/inbox/.spotdl-failures.json 2>/dev/null | python3 -m json.tool || echo '(no backoff state)'"

# Clear all MISS backoff state — all tracks will be retried on the next run
clear-failures:
    docker compose run --rm service sh -c "rm -f /root/Music/inbox/.spotdl-failures.json && echo Cleared"

# One-time migration: populate spotify_url flex attr for items imported before #100 fix.
# Pass --dry-run to preview, --playlist <name> to limit to one playlist.
backfill-spotify-urls *args:
    docker compose run --rm \
        --volume {{justfile_directory()}}/scripts/backfill-spotify-urls.py:/tmp/backfill-spotify-urls.py \
        service python3 /tmp/backfill-spotify-urls.py {{args}}

# Fingerprint all beets items lacking mb_trackid via AcoustID. Requires ACOUSTID_APIKEY env var.
# Safe to re-run: skips items that already have mb_trackid.
mb-fingerprint:
    op run --env-file .env.tpl -- docker compose run --rm service music-mb-fingerprint

# One-time migration (issue #117): copy source= → sources= for all beets items.
# IMPORTANT: deploy new code first, then run this. Safe to re-run.
migrate-sources:
    docker compose run --rm service python3 -c "
from beets.library import Library
lib = Library('/root/.config/beets/library.db', '/root/Music/library')
for item in lib.items('source:'):
    item['sources'] = item.get('source') or ''
    item['source'] = ''
    item.store()
lib._close()
print('Migration complete.')
"

# Dump beets DB and export JSON from the container
backup:
    docker compose run --rm service sh -c "beet export > /root/.config/beets/library-export.json"
    docker compose run --rm service sh -c "sqlite3 /root/.config/beets/library.db .dump > /root/.config/beets/library-dump.sql"
