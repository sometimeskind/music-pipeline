# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A Dockerized music pipeline: Spotify playlists → spotdl downloads → beets import/tagging → music library. One long-running service container orchestrates everything via Prefect.

**Data flow:**
```
Spotify playlists → spotdl sync → /root/Music/inbox/spotdl/<name>/ → beets import → /root/Music/library/$albumartist/$album/$track - $title.m4a
```

One Docker image is pushed to GHCR: `ghcr.io/sometimeskind/music-pipeline`. It contains three Python packages:

| Package | Source | Role |
|---|---|---|
| `music_fetch` | `fetch/` | spotdl sync, Spotify/YouTube calls |
| `music_scan` | `scan/` | beets import, AcoustID fingerprinting, .m3u generation |
| `music_service` | `service/` | Prefect runner, Flask HTTP API, watchdog file watcher |

The `music-pipeline` entry point starts all three subsystems and runs indefinitely. Scheduling is handled internally by Prefect (`FETCH_CRON` env var), not by external CronJobs.

## Common Commands

All commands are wrapped in `just` recipes (see `justfile` in the repo root).

```bash
# One-time setup after cloning: install git hooks
just hooks

# Run the test suite (builds the dev container, runs all unit tests)
just test

# Build the service image
just build

# Run spotdl sync (fetch flow via service container)
just fetch

# Run a local scan (scan flow via service container)
just scan

# Run full ingest: just fetch && just scan
just sync
```

**Do not run Python commands directly on the host.** All Python tooling (pytest, beet, spotdl) runs inside the container. Use `just test` to run tests.

## Repo Structure

```
fetch/              — music_fetch package: spotdl sync, Spotify/YouTube calls
  music_fetch/
  tests/
  Dockerfile        — dev-only stages (never pushed to registry)
  pyproject.toml
  requirements.txt / requirements-dev.txt

scan/               — music_scan package: beets import, fingerprinting, .m3u generation
  music_scan/
  tests/
  Dockerfile        — dev-only stages (never pushed to registry)
  pyproject.toml
  requirements.txt / requirements-dev.txt

service/            — music_service package: Prefect runner, Flask API, file watcher
  music_service/
  tests/
  Dockerfile        — prod image pushed to GHCR; build context is repo root
  pyproject.toml
  requirements.txt / requirements-dev.txt

scripts/            — CLI helper scripts (music-files HTTP API client, etc.)
  tests/

config/             — bind-mounted config files (beets, spotdl, playlists.conf)
tests/              — container integration tests (run against the service image)
compose.yml         — prefect-server + service
justfile
```

## Testing

All unit tests (fetch, scan, service, scripts) run in a single dev container built from `service/Dockerfile`:

```bash
just test   # builds service dev container → runs all unit tests in one shot
```

The dev stage copies all four test directories into the container and runs pytest on them together. Integration tests live in `tests/` (repo root) and run against the real service container image:

```bash
just test-service   # runs tests/test_service_*.py against the GHCR image
```

Each Dockerfile has two stages:
- **`prod` stage** — the image pushed to GHCR. Contains only runtime dependencies.
- **`dev` stage** — extends `prod` with test dependencies. Built locally by `just test`. Never pushed to the registry.

A `pre-push` git hook runs `just test` automatically before every push. Install it once with `just hooks`.

## Architecture

### Entry Points

**`music-pipeline`** (`service/music_service/cli.py`) — main service entry point. Runs indefinitely:
- Starts a Flask HTTP API on port 8080
- Starts a watchdog file watcher on the inbox (triggers debounced scan on new audio)
- Registers and serves two Prefect deployments via `prefect.serve()`

**`music-ingest`** (`fetch/music_fetch/cli.py`) — one-shot fetch. Reconciles `playlists.conf`, runs spotdl sync, diffs snapshots to detect Spotify removals.

**`music-scan`** (`scan/music_scan/cli.py`) — one-shot scan. Imports inbox → beets (MusicBrainz/AcoustID), regenerates `.m3u` playlists, triggers Navidrome rescan.

### Prefect Flows

Defined in `service/music_service/flows.py`:

| Flow | Trigger | Steps |
|---|---|---|
| `fetch` | Cron (`FETCH_CRON`), or `POST /fetch/trigger` | preflight → reconcile playlists → spotdl sync → save removals |
| `scan` | File watcher (new audio in inbox), or `POST /scan/trigger` | apply removals → beet import → quarantine → asis import → beet update → regen playlists → Navidrome → snapshot reconcile |

### HTTP API

The service exposes a Flask API on port 8080. All routes except `/health` require `Authorization: Bearer <API_BEARER_TOKEN>`.

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Health check |
| `GET` | `/inbox` | List inbox files |
| `POST` | `/inbox/upload` | Upload a zip of audio files to inbox; triggers debounced scan |
| `GET` | `/quarantine` | List quarantine files |
| `GET` | `/quarantine/download/<name>` | Download a quarantine file or directory (as zip) |
| `POST` | `/fetch/trigger` | Submit a fetch Prefect run |
| `POST` | `/scan/trigger` | Submit a scan Prefect run |

### Key Design Decisions

**`sources=<playlist_name>` beets flex attribute** — Comma-separated list of playlists a track belongs to. Written during import, used to generate `.m3u` playlists. Tracks shared across playlists carry all source names (e.g. `sources=playlist-a,playlist-b`).

**Snapshot diff for soft deletes** — `music-ingest` snapshots the `.spotdl` file before and after sync, diffs URL sets in Python to find removed tracks, then clears their `sources=` tags (files stay in the library).

**Strict MusicBrainz threshold** — `strong_rec_thresh: 0.10` in `config/beets/config.yaml`. Files that don't match confidently go to `/root/Music/quarantine/` for manual review.

**`config/playlists.conf`** — Declarative registry of playlists: one `name spotify-url` line per playlist. Committed to the repo. `music-ingest` reconciles disk state to match this file on every run. In k8s, mount as a ConfigMap at `/root/.config/music-pipeline/playlists.conf`.

**Credentials via 1Password** — `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` are injected at runtime via `op run --env-file=.env.tpl`. The `.env.tpl` file holds vault references, not secrets.

**`cookies.txt`** — YouTube Premium cookies for M4A 256 kbps quality. Bind-mounted from the host at `./cookies.txt`. Never committed (in `.gitignore`). Must be re-exported from the browser when expired.

**Prefect direct mode** — When `PREFECT_API_URL` is unset, flows execute in-process without a Prefect server. Concurrency is enforced via a threading lock instead of Prefect's global concurrency limits. Useful for running without the `prefect-server` sidecar.

### Volumes

| Mount | Path in container | Purpose |
|---|---|---|
| host `/home/tom/Music` or PVC | `/root/Music` | Library, inbox, quarantine, playlists |
| `beets-data` volume | `/root/.config/beets` | SQLite DB (`library.db`) and import log |
| `./config/beets/config.yaml` | `/root/.config/beets/config.yaml` | Beets config (read-only) |
| `./config/spotdl/config.json` | `/root/.config/spotdl/config.json` | spotdl config (read-only) |
| `./config/playlists.conf` | `/root/.config/music-pipeline/playlists.conf` | Playlist registry (read-only) |
| `./cookies.txt` | `/root/.config/spotdl/cookies.txt` | YouTube cookies (read-only, not committed) |

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `SPOTIFY_CLIENT_ID` | — | Required. Spotify Developer app client ID. |
| `SPOTIFY_CLIENT_SECRET` | — | Required. Spotify Developer app client secret. |
| `API_BEARER_TOKEN` | — | Required. Bearer token for the HTTP API. |
| `PREFECT_API_URL` | unset | Prefect server URL. If unset, runs in direct (in-process) mode. |
| `FETCH_CRON` | `0 3 * * *` | Cron expression for the fetch Prefect deployment. |
| `SYNC_TRACK_LIMIT` | unset | Cap total new tracks downloaded across all playlists per run. |
| `SYNC_JITTER_SECONDS` | unset | Sleep random 0–N seconds before syncing to spread load. |
| `BEET_SKIP_LIMIT` | unset | Terminate beet import after this many skipped tracks. |
| `PUSHGATEWAY_URL` | unset | Prometheus Pushgateway URL. If unset, metrics are not pushed. |

### `.m3u` Playlists

Generated in `/root/Music/playlists/` with paths relative to that directory. Regenerated by the scan flow on every run.

Playlist membership for tracks shared across multiple playlists is stored in a comma-separated `sources` flex attribute (e.g. `sources=playlist-a,playlist-b`). A track shared across playlists will only appear in all relevant `.m3u` files after `fetch` has synced each playlist at least once — `scan` alone cannot infer cross-playlist membership that `fetch` has not yet recorded in the `.spotdl` snapshots.

### Static Playlists (`.nosync`)

Mark a playlist as static by adding `nosync` as a third field in `config/playlists.conf`:

```
my-playlist  https://open.spotify.com/playlist/...  nosync
```

`music-ingest` creates (or removes) the `.nosync` sentinel file on disk to match the config — making it declarative and recoverable after volume loss. The sentinel file lives at:

```
inbox/spotdl/my-playlist.nosync
```

`music-ingest` skips `spotdl sync` for any playlist with a matching `.nosync` file. The tracks remain in the library and continue to appear in m3u generation.

### Managing Playlists

Playlist management is fully declarative: edit `config/playlists.conf`. The next `music-ingest` run reconciles disk state automatically.

**Add a new playlist:** Add the entry to `config/playlists.conf`.

**Remove a playlist:** Remove the entry from `config/playlists.conf`. The next `music-ingest` run queues beets tag cleanup and deletes the `.spotdl` file.

**Trigger a manual fetch:**
```bash
just fetch                          # locally via docker compose
curl -X POST http://localhost:8080/fetch/trigger \
  -H "Authorization: Bearer <token>"   # via HTTP API
```
