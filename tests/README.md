# Container Integration Tests

End-to-end tests that orchestrate real Docker containers to verify the full pipeline behaviour. These complement the unit tests in `fetch/tests/` and `scan/tests/`, which run inside dev containers.

## How it works

Tests run **on the host** using the Python Docker SDK (`docker-py`). Each test spins up the service container with isolated Docker volumes, injects test data, runs operations against the HTTP API or entry points, and asserts on filesystem state and log output.

The image under test is controlled by an environment variable so CI can point tests at a freshly-built local image before the push to GHCR:

| Variable | Default | CI value |
|---|---|---|
| `SERVICE_IMAGE` | `ghcr.io/sometimeskind/music-pipeline:latest` | `music-pipeline:ci-local` |

## Prerequisites

- Docker running locally
- Python 3.x on the host (for the test harness — not the pipeline itself)
- `pip install -r tests/requirements.txt`

For auth tests only (Layer 5):
- `cookies.txt` present at the repo root
- `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` (injected via 1Password — see below)
- `TEST_PLAYLIST_URL` set to a small Spotify playlist URL (≤10 tracks)

## Running locally

### No-auth tests (Layers 1–4)

```bash
just test-integration
```

This runs against the current image in GHCR. To test a local build instead:

```bash
docker build --target prod -t music-pipeline:local service
SERVICE_IMAGE=music-pipeline:local pytest tests/ -m "not auth" -v
```

### Auth tests (Layer 5)

```bash
export TEST_PLAYLIST_URL=https://open.spotify.com/playlist/<id>
just test-auth
```

`just test-auth` uses `op run --env-file .env.tpl` to inject Spotify credentials. Ensure:
- `.env.tpl` has `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` vault references
- `cookies.txt` exists at the repo root
- `TEST_PLAYLIST_URL` is exported in your shell (or added to `.env.tpl`)

## Test audio fixture

Layers 2–4 use a synthetic silent MP3 generated at runtime by ffmpeg inside
the service container. No file needs to be provided — the `fixture_audio` session
fixture handles generation automatically. See
[`tests/fixtures/audio/README.md`](fixtures/audio/README.md) for details.

## Scenarios covered

| File | Layer | Auth required |
|---|---|---|
| `test_smoke.py` | 1 (smoke), 1a (chroma plugin) | No |
| `test_import.py` | 2 (file drop), 3 (playlist + .m3u), 4 (duplicate handling) | No |
| `test_service_smoke.py` | Layer 1 (service smoke) | No |
| `test_service_api.py` | Layer 2 (HTTP API) + Layer 3 (scan-via-service) | No |
| `test_service_push.py` | Layer 4 (library push) | No |
| `test_service_e2e.py` | Layer 5 (full pipeline via API) | Yes |
