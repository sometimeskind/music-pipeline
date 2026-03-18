# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A Dockerized music pipeline: Spotify playlists → spotdl downloads → beets import/tagging → Navidrome music server. Runs on a cron schedule inside the container.

**Data flow:**
```
Spotify playlists → spotdl sync → /root/Music/inbox/spotdl/<name>/ → beets import → /root/Music/library/$albumartist/$album/$track - $title.m4a → Navidrome (via NFS)
```

## Common Commands

All commands require 1Password CLI (`op`) to inject Spotify credentials:

```bash
# Build the container image
docker compose build

# Interactive: add a new playlist
op run --env-file=.env.tpl -- docker compose run --rm -it pipeline music-setup

# Start the service (cron-based daily ingest)
op run --env-file=.env.tpl -- docker compose up -d

# Run a full ingest immediately
op run --env-file=.env.tpl -- docker compose exec pipeline music-ingest

# Follow logs
docker compose logs -f pipeline
```

There is no test suite. Shell script validation is manual.

## Architecture

### Scripts (`scripts/`)

- **`music-scan`** — Fast local path (runs every 5 min). Imports inbox → beets (makes MusicBrainz/AcoustID calls during import), refreshes metadata, regenerates `.m3u` playlists, triggers Navidrome rescan, pushes Prometheus metrics. No Spotify or YouTube calls. Called by `music-ingest` after sync.
- **`music-ingest`** — Daily network sync. Loops `.spotdl` files, runs `spotdl sync`, diffs snapshots with `jq` to detect Spotify removals, then calls `music-scan`. Skips `.nosync` playlists.
- **`music-provision`** — Non-interactive. Reads `config/playlists.conf` and calls `music-setup` for each entry. Idempotent. Use in k8s Jobs and for PVC recovery.
- **`music-setup`** — Creates local directories, runs `spotdl save` to create the initial `.spotdl` sync file. Interactive by default; non-interactive with `--name <name> --url <url>`. Idempotent — skips if `.spotdl` already exists.
- **`music-import`** — Called by `music-scan`. Imports all audio from inbox to beets; moves unmatched files to quarantine.
- **`music-remove`** — Interactive. Removes `.spotdl` file, download dir, `.m3u`, and clears `source=<name>` beets tags. Does not delete library files.

### Key Design Decisions

**`source=<playlist_name>` beets tag** — This drives the entire lifecycle. It's written during import, used to generate `.m3u` playlists, and is how `music-remove` identifies which tracks belong to a playlist. All scripts depend on it.

**Snapshot diff for soft deletes** — `music-ingest` snapshots the `.spotdl` file before and after sync, diffs with `jq` to find removed tracks, then removes only their `source=` tags (files stay in the library).

**Strict MusicBrainz threshold** — `strong_rec_thresh: 0.05` in `config/beets/config.yaml`. Files that don't match confidently go to `/root/Music/quarantine/` for manual review. Raise to `0.10` if too many good tracks are quarantined.

**`config/playlists.conf`** — Declarative registry of playlists: one `name spotify-url` line per playlist. Committed to the repo. Enables PVC recovery (`music-provision` re-creates all `.spotdl` files from this file) and non-interactive k8s provisioning Jobs. In k8s, mount as a ConfigMap at `/root/.config/music-pipeline/playlists.conf`.

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

- `SCAN_CRON_SCHEDULE` — Cron expression for `music-scan`. Default: `*/5 * * * *` (every 5 min).
- `SYNC_CRON_SCHEDULE` — Cron expression for `music-ingest`. Default: `0 3 * * *` (03:00 UTC daily).
- `SYNC_JITTER_SECONDS` — If set > 0, `music-ingest` sleeps a random number of seconds (up to this value) before starting. Reduces thundering herd when multiple k8s pods start simultaneously. Default: `0`.
- `PUSHGATEWAY_URL` — Prometheus Pushgateway URL (e.g. `http://pushgateway:9091`). If unset, metrics are not pushed.
- `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` — Injected via 1Password at runtime.
- `NAVIDROME_URL` / `NAVIDROME_API_KEY` — Optional Navidrome rescan trigger. `NAVIDROME_API_KEY` format: `user:password`.

### `.m3u` Playlists

Generated in `/root/Music/playlists/` with paths relative to that directory, for Navidrome compatibility. Regenerated by `music-scan` on every run.

### Static Playlists (`.nosync`)

Mark a playlist as static by adding `nosync` as a third field in `config/playlists.conf`:

```
my-playlist  https://open.spotify.com/playlist/...  nosync
```

`music-provision` creates (or removes) the `.nosync` sentinel file on the PVC to match the config — making it declarative and recoverable after PVC loss. The sentinel file itself lives at:

```
inbox/spotdl/my-playlist.nosync
```

`music-ingest` skips `spotdl sync` for any playlist with a matching `.nosync` file. The tracks remain in the library and continue to appear in m3u generation.

The sentinel file can also be created directly (e.g. via `kubectl exec`) without changing `playlists.conf`, but that state won't survive PVC loss.

### Managing Playlists in k8s

There is no always-running pod to exec into. The idiomatic pattern is an ephemeral pod that mounts the same PVCs.

**Add a new playlist:**
```bash
kubectl apply -f job-music-setup.yaml   # job template in homelab repo
kubectl attach -it job/music-setup
# interactive setup writes .spotdl file to the PVC
# next music-ingest CronJob run picks it up automatically
```

**Remove a playlist:**
```bash
kubectl apply -f job-music-remove.yaml
kubectl attach -it job/music-remove
```

**Run a manual sync now:**
```bash
kubectl create job music-ingest-manual --from=cronjob/music-ingest
kubectl logs -f job/music-ingest-manual
```

`music-setup` and `music-remove` already work as interactive scripts reading from env vars — no container changes needed.
