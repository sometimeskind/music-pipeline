# Run the test suite in dev containers (never pushed to registry)
test:
    docker build --target dev -t music-pipeline-fetch:dev fetch
    docker run --rm music-pipeline-fetch:dev
    docker build --target dev -f scan/Dockerfile -t music-pipeline-scan:dev .
    docker run --rm music-pipeline-scan:dev
    docker build --target dev -f service/Dockerfile -t music-pipeline-service:dev .
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
    .venv/bin/pytest tests/test_service_smoke.py tests/test_service_api.py -m "not auth" -v

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

# Dump beets DB and export JSON from the container
backup:
    docker compose run --rm service sh -c "beet export > /root/.config/beets/library-export.json"
    docker compose run --rm service sh -c "sqlite3 /root/.config/beets/library.db .dump > /root/.config/beets/library-dump.sql"
