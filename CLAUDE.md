# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A Dockerized music pipeline: Spotify playlists → spotdl downloads → beets import/tagging → music library. Scheduling is handled externally (k8s CronJobs); each container runs a single task and exits.

The pipeline runs as two containers:
- **fetch** (`music-pipeline-fetch`) — spotdl sync, Spotify/YouTube network calls; no beets/ffmpeg
- **scan** (`music-pipeline-scan`) — beets import, AcoustID fingerprinting, .m3u generation

**Data flow:**
```
Spotify playlists → [fetch] spotdl sync → /root/Music/inbox/spotdl/<name>/ → [scan] beets import → /root/Music/library/$albumartist/$album/$track - $title.m4a
```

Removed-track info is passed between containers via `/root/Music/inbox/.pending-removals.json` on the shared volume. The fetch container writes it; music-scan reads and processes it, then deletes it.

## Common Commands

All commands are wrapped in `just` recipes (see `justfile` in the repo root).

```bash
# One-time setup after cloning: install git hooks
just hooks

# Run the test suite (builds the dev container, runs pytest)
just test

# Build both container images
docker compose build

# Run spotdl sync only (fetch container: Spotify/YouTube → inbox)
# Reconciles playlists.conf: provisions new, queues removed
just fetch

# Run a local scan only (scan container: import inbox → .m3u)
just scan

# Run full ingest: just fetch && just scan
just sync
```

**Do not run Python commands directly on the host.** All Python tooling (pytest, beet, spotdl) runs inside the container. Use `just test` to run tests.

## Repo Structure

```
fetch/          — fetch container: spotdl sync, Spotify/YouTube calls
  pipeline/     — Python package (ingest, spotdl_ops, config, metrics, cli)
  tests/
  Dockerfile    — prod + dev stages
  pyproject.toml
  requirements.txt / requirements-dev.txt

scan/           — scan container: beets import, .m3u generation
  pipeline/     — Python package (scan, library, process, music_pipeline, metrics, cli)
  tests/
  Dockerfile    — prod + dev stages
  pyproject.toml
  requirements.txt / requirements-dev.txt

config/         — bind-mounted config files (beets, spotdl, playlists.conf)
compose.yml     — references fetch/ and scan/ build contexts
justfile
```

## Testing

Tests live in `fetch/tests/` and `scan/tests/`. Both suites run inside Docker dev containers:

```bash
just test   # builds fetch dev container → runs pytest, then same for scan
```

Each Dockerfile uses a two-stage build:
- **`prod` stage** — the image pushed to GHCR. Contains only runtime dependencies.
- **`dev` stage** — extends `prod` with `requirements-dev.txt` (pytest) and the `tests/` directory. Built locally by `just test`. Never pushed to the registry.

A `pre-push` git hook runs `just test` automatically before every push. Install it once with `just hooks`.

## Architecture

### Entry Points

**fetch container** (`fetch/pipeline/cli.py`):
- **`music-ingest`** — Declarative reconciliation loop. Reconciles disk state against `playlists.conf` (provisions new playlists, queues removed ones), loops `.spotdl` files, runs `spotdl sync`, diffs snapshots to detect Spotify removals. Skips `.nosync` playlists.

**scan container** (`scan/pipeline/cli.py`):
- **`music-scan`** — Imports inbox → beets (MusicBrainz/AcoustID calls during import), regenerates `.m3u` playlists, pushes Prometheus metrics. No Spotify or YouTube calls.

### Key Design Decisions

**`source=<playlist_name>` beets tag** — This drives the entire lifecycle. It's written during import, used to generate `.m3u` playlists, and is how `music-remove` identifies which tracks belong to a playlist. All scripts depend on it.

**Snapshot diff for soft deletes** — `music-ingest` snapshots the `.spotdl` file before and after sync, diffs URL sets in Python to find removed tracks, then removes only their `source=` tags (files stay in the library).

**Strict MusicBrainz threshold** — `strong_rec_thresh: 0.05` in `config/beets/config.yaml`. Files that don't match confidently go to `/root/Music/quarantine/` for manual review. Raise to `0.10` if too many good tracks are quarantined.

**`config/playlists.conf`** — Declarative registry of playlists: one `name spotify-url` line per playlist. Committed to the repo. `music-ingest` reconciles disk state to match this file on every run — provisioning new entries, reconciling `.nosync` sentinels, and queuing removed playlists for cleanup. In k8s, mount as a ConfigMap at `/root/.config/music-pipeline/playlists.conf`.

**Credentials via 1Password** — `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` are injected at runtime via `op run --env-file=.env.tpl`. The `.env.tpl` file holds vault references, not secrets.

**`cookies.txt`** — YouTube Premium cookies for M4A 256 kbps quality. Bind-mounted from the host at `./cookies.txt`. Never committed (in `.gitignore`). Must be re-exported from the browser when expired.

### Volumes

| Volume | Path in container | Purpose |
|---|---|---|
| `music-data` | `/root/Music` | Library, inbox, quarantine, playlists |
| `beets-data` | `/root/.config/beets` | SQLite DB (`library.db`) and import log |
| `./config/beets/config.yaml` | `/root/.config/beets/config.yaml` | Beets config (read-only) |
| `./config/spotdl/config.json` | `/root/.config/spotdl/config.json` | spotdl config (read-only) |
| `./cookies.txt` | `/root/.config/spotdl/cookies.txt` | YouTube cookies (read-only, not committed) |

### Environment Variables

**fetch container:**
- `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` — Injected via 1Password at runtime.
- `SYNC_TRACK_LIMIT` — If set, caps the total number of new tracks downloaded across all playlists in a single `music-ingest` run. Playlists are processed in alphabetical order; the budget is shared. Tracks deferred by the limit are excluded from the `.spotdl` snapshot and re-appear as new on the next run. Useful for large playlists (e.g. liked songs) to avoid rate limiting. Default: unset (no limit).
- `SYNC_JITTER_SECONDS` — If set, sleeps a random 0–N seconds before syncing to spread load. Default: unset.
- `PUSHGATEWAY_URL` — Prometheus Pushgateway URL (e.g. `http://pushgateway:9091`). If unset, metrics are not pushed.

**scan container:**
- `PUSHGATEWAY_URL` — Prometheus Pushgateway URL. If unset, metrics are not pushed.

### `.m3u` Playlists

Generated in `/root/Music/playlists/` with paths relative to that directory. Regenerated by `music-scan` on every run.

### Static Playlists (`.nosync`)

Mark a playlist as static by adding `nosync` as a third field in `config/playlists.conf`:

```
my-playlist  https://open.spotify.com/playlist/...  nosync
```

`music-ingest` creates (or removes) the `.nosync` sentinel file on the PVC to match the config — making it declarative and recoverable after PVC loss. The sentinel file itself lives at:

```
inbox/spotdl/my-playlist.nosync
```

`music-ingest` skips `spotdl sync` for any playlist with a matching `.nosync` file. The tracks remain in the library and continue to appear in m3u generation.

The sentinel file can also be created directly (e.g. via `kubectl exec`) without changing `playlists.conf`, but that state won't survive PVC loss.

### Managing Playlists in k8s

Playlist management is fully declarative: edit `config/playlists.conf` and update the `music-pipeline-playlists` ConfigMap. The next `music-ingest` CronJob run reconciles disk state automatically.

**Add a new playlist:**
1. Add the entry to `config/playlists.conf`, commit and push.
2. Update the `music-pipeline-playlists` ConfigMap (or let GitOps do it).
3. The next `music-ingest` CronJob run provisions the `.spotdl` file and begins syncing.

**Remove a playlist:**
1. Remove the entry from `config/playlists.conf`, commit and push.
2. Update the `music-pipeline-playlists` ConfigMap.
3. The next `music-ingest` run deletes the `.spotdl` file and queues beets tag cleanup.

**Run a manual sync now:**
```bash
kubectl create job music-ingest-manual --from=cronjob/music-ingest
kubectl logs -f job/music-ingest-manual
```
