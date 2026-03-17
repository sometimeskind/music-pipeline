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

- **`music-ingest`** — Core daily workflow. Loops `.spotdl` files, runs `spotdl sync`, diffs before/after snapshots with `jq` to detect Spotify removals, imports to beets with `source=<playlist_name>` tag, generates `.m3u` playlists, quarantines unimported files.
- **`music-setup`** — Interactive. Creates local directories, runs `spotdl save` to create the initial `.spotdl` sync file, validates credentials.
- **`music-import`** — Called by `music-ingest`. Imports all audio from inbox to beets; moves unmatched files to quarantine.
- **`music-remove`** — Interactive. Removes `.spotdl` file, download dir, `.m3u`, and clears `source=<name>` beets tags. Does not delete library files.

### Key Design Decisions

**`source=<playlist_name>` beets tag** — This drives the entire lifecycle. It's written during import, used to generate `.m3u` playlists, and is how `music-remove` identifies which tracks belong to a playlist. All scripts depend on it.

**Snapshot diff for soft deletes** — `music-ingest` snapshots the `.spotdl` file before and after sync, diffs with `jq` to find removed tracks, then removes only their `source=` tags (files stay in the library).

**Strict MusicBrainz threshold** — `strong_rec_thresh: 0.05` in `config/beets/config.yaml`. Files that don't match confidently go to `/root/Music/quarantine/` for manual review. Raise to `0.10` if too many good tracks are quarantined.

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

- `CRON_SCHEDULE` — Cron expression for the ingest schedule. Default: `0 3 * * *` (03:00 UTC daily). Written to `/etc/cron.d/pipeline-cron` by `entrypoint.sh`.
- `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` — Injected via 1Password at runtime.

### `.m3u` Playlists

Generated in `/root/Music/playlists/` with paths relative to that directory, for Navidrome compatibility. Regenerated on every ingest run.
