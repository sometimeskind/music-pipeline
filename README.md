# music-pipeline

Dockerized music pipeline: Spotify playlists → spotdl downloads → beets import/tag → music library.

```
Spotify playlists → spotdl → beets → ~/Music/library
```

A single long-running service container orchestrates everything via Prefect. Two flows run on configurable schedules:

| Flow | Default schedule | Does |
|---|---|---|
| `fetch` | Daily 03:00 UTC (`FETCH_CRON`) | Spotify/YouTube sync — downloads new tracks, queues removals |
| `scan` | On demand (file watcher or HTTP API) | Import inbox → beets, refresh metadata, regenerate .m3u |

---

## Pipeline internals

A full sync cycle (`music-ingest` followed by `music-scan`) runs 8 discrete steps. Steps 1–2 are the **fetch phase**; steps 3–8 are the **scan phase**.

```
┌─ FETCH PHASE (music-ingest) ──────────────────────────────────────────┐
│                                                                        │
│  [1] Playlist reconciliation                                           │
│        Reads playlists.conf; provisions new .spotdl files, syncs      │
│        .nosync sentinels, queues playlists removed from config.        │
│        │                                                               │
│        ▼                                                               │
│  [2] spotdl sync                                                       │
│        Downloads new tracks from Spotify/YouTube into inbox/.          │
│        Diffs snapshot URLs to detect tracks removed from Spotify.      │
│        Returns a PendingRemovals struct (removed tracks + playlists).  │
│                                                                        │
└────────────────────────────────────────┬───────────────────────────────┘
                                         │ PendingRemovals
┌─ SCAN PHASE (music-scan) ─────────────▼───────────────────────────────┐
│                                                                        │
│  [3] Pending-removal cleanup           ◄── (no-op if scan runs alone) │
│        Clears beets source= tags for tracks/playlists that were        │
│        removed from Spotify or from playlists.conf.                    │
│        │                                                               │
│        ▼                                                               │
│  [4] Beets import                                                      │
│        Matches inbox audio to MusicBrainz/AcoustID; moves matched      │
│        files to library/. Low-confidence matches go to quarantine/.    │
│        │                                                               │
│        ▼                                                               │
│  [5] Quarantine + asis pass                                            │
│        Moves unmatched inbox leftovers to quarantine/. Then attempts   │
│        a second beet import --asis for quarantine files that already   │
│        have sufficient embedded tags (title, artist, album, track#).   │
│        │                                                               │
│        ▼                                                               │
│  [6] Library metadata refresh                                          │
│        Runs beet update to refresh metadata on existing library items. │
│        │                                                               │
│        ▼                                                               │
│  [7] Snapshot reconciliation                                           │
│        Diffs each .spotdl file against the beets library + quarantine. │
│        Drops URLs absent from both so spotdl re-downloads them next    │
│        fetch rather than silently skipping forever.                    │
│        │                                                               │
│        ▼                                                               │
│  [8] Playlist generation + Navidrome trigger                           │
│        Regenerates .m3u files (in Spotify playlist order). Calls the   │
│        Navidrome Subsonic API to trigger a library rescan.             │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
```

Steps 3, 7, and 8 are no-ops when `music-scan` runs on its 5-minute schedule with no preceding fetch (no removals to apply, Navidrome already up to date). The scan phase is idempotent and safe to run at any time.

---

## Requirements

- Docker + Docker Compose
- [1Password CLI (`op`)](https://developer.1password.com/docs/cli/) signed in — used on the **host** to inject Spotify credentials
- YouTube Premium account + cookies export (see below)
- Spotify Developer app (client_id + client_secret) stored in 1Password at `Private/Spotify Developer App`

---

## Setup

### 1. Clone and prepare

```bash
git clone https://github.com/sometimeskind/music-pipeline
cd music-pipeline
```

### 2. Export YouTube Premium cookies

spotdl requires YouTube Premium cookies for M4A 256 kbps quality.

**One-time setup:** install `yt-dlp` on the host:

```bash
pipx install yt-dlp
```

**Each time cookies expire**, sign in to [music.youtube.com](https://music.youtube.com) in Firefox, then run:

```bash
just cookies
```

This extracts cookies directly from Firefox and saves them to `cookies.txt` (already in `.gitignore`).

Cookies expire every few weeks. Re-export when downloads start failing or when `just fetch` logs show all tracks as `no source`.

### 3. Set up Spotify credentials

Store your Spotify Developer app credentials in 1Password:

- **Item:** `Personal/Spotify API`
- **Fields:** `username` (client ID), `credential` (client secret)

Create an `.env.tpl` for `op run`:

```bash
SPOTIFY_CLIENT_ID=op://Personal/Spotify API/username
SPOTIFY_CLIENT_SECRET=op://Personal/Spotify API/credential
```

### 4. Register playlists

Edit `config/playlists.conf` and add one line per playlist:

```
# name             spotify-url                                               [flags]
liked-songs        https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M
archived-mix       https://open.spotify.com/playlist/37i9dQZF1DXd9rLJfaAKCk  nosync
```

The optional `nosync` flag freezes a playlist: `music-ingest` creates a `.nosync` sentinel on the PVC and skips `spotdl sync` for it. Remove the flag and run `music-ingest` again to unfreeze.

This file is the single source of truth for playlists. `music-ingest` reconciles disk state to match it on every run — provisioning new entries, reconciling `.nosync` sentinels, and queuing removed playlists for cleanup.

### 5. Run the first ingest

```bash
just fetch
```

This provisions `.spotdl` files for all entries in `playlists.conf` and begins downloading. To add or remove playlists later, just edit `playlists.conf` and run `just fetch` again.

### 6. Start the service

```bash
op run --env-file=.env.tpl -- docker compose up
```

This starts the Prefect server and the `music-pipeline` service. The fetch flow runs daily at 03:00 UTC by default — override with the `FETCH_CRON` env var. The scan flow runs automatically whenever new audio files arrive in the inbox.

To avoid rate limiting on large playlists, set `SYNC_TRACK_LIMIT` to cap total new downloads per session across all playlists. The pipeline resumes where it left off on the next run.

---

## `just` recipes

A `justfile` lives in the repo root. Run these from the repo directory.

| Recipe | What it does |
|---|---|
| `just sync` | Run full ingest now (fetch + scan) |
| `just fetch` | Run spotdl sync only (reconciles playlists.conf → provisions/removes) |
| `just scan` | Run local scan only (import inbox → .m3u) |
| `just backup` | Dump beets DB + export JSON inside container |

---

## Directory structure (inside container)

```
/root/Music/
  inbox/
    spotdl/
      <name>.spotdl      ← spotdl sync state (do not delete; backed by PVC)
      <name>.nosync      ← optional sentinel to freeze a playlist from re-syncing
      <name>/            ← spotdl downloads (cleared after beet import)
  library/               ← beets-managed: $albumartist/$album/$track - $title.m4a
  quarantine/            ← low-confidence MusicBrainz matches, review manually
  playlists/             ← generated .m3u files (relative paths for Navidrome)
/root/.config/beets/
  library.db             ← SQLite database — back this up
  import.log             ← log of every skipped import
  config.yaml            ← bind-mounted from ./config/beets/config.yaml
/root/.config/music-pipeline/
  playlists.conf         ← bind-mounted from ./config/playlists.conf (k8s: ConfigMap)
```

---

## Kubernetes deployment

This section contains everything an agent needs to write the k8s manifests.
The canonical manifests live in the homelab repo.

### Architecture

One long-running `Deployment` for the `music-pipeline` service, plus an optional `Deployment` for the Prefect server (for the UI and flow run history). Scheduling is handled internally by Prefect via the `FETCH_CRON` env var — no k8s CronJobs needed.

Without `PREFECT_API_URL` set, the service runs in direct mode: flows execute in-process and the Prefect server is not required. Use this for a simpler deployment with no UI.

### PersistentVolumeClaims

| PVC name | Contents | Notes |
|---|---|---|
| `music-data` | `inbox/`, `library/`, `quarantine/`, `playlists/` | Full music volume; back up `library/` |
| `beets-data` | `library.db`, `import.log` | Small SQLite DB; back this up |

### ConfigMaps

| ConfigMap name | Key | Mount path | Source file |
|---|---|---|---|
| `music-pipeline-beets-config` | `config.yaml` | `/root/.config/beets/config.yaml` | `config/beets/config.yaml` |
| `music-pipeline-spotdl-config` | `config.json` | `/root/.config/spotdl/config.json` | `config/spotdl/config.json` |
| `music-pipeline-playlists` | `playlists.conf` | `/root/.config/music-pipeline/playlists.conf` | `config/playlists.conf` |

All three ConfigMaps should be mounted `readOnly: true`.

### Secrets

| Secret name | Key | Env var | Description |
|---|---|---|---|
| `music-pipeline-spotify` | `client-id` | `SPOTIFY_CLIENT_ID` | Spotify Developer app client ID |
| `music-pipeline-spotify` | `client-secret` | `SPOTIFY_CLIENT_SECRET` | Spotify Developer app client secret |
| `music-pipeline-api` | `bearer-token` | `API_BEARER_TOKEN` | Bearer token for the HTTP API |
| `music-pipeline-cookies` | `cookies.txt` | _(file mount)_ | YouTube Premium cookies — expires; re-export from browser |

Mount `cookies.txt` at `/root/.config/spotdl/cookies.txt` read-only. Update by patching the Secret; the next pod restart picks it up.

### Environment variables

| Variable | Source | Default | Notes |
|---|---|---|---|
| `SPOTIFY_CLIENT_ID` | Secret | — | Required |
| `SPOTIFY_CLIENT_SECRET` | Secret | — | Required |
| `API_BEARER_TOKEN` | Secret | — | Required |
| `PREFECT_API_URL` | Plain value | unset | Set to reach the Prefect server (e.g. `http://prefect-server:4200/api`). Unset = direct mode. |
| `FETCH_CRON` | Plain value | `0 3 * * *` | Cron expression for the fetch flow |
| `PUSHGATEWAY_URL` | Plain value | `""` | e.g. `http://prometheus-pushgateway.monitoring:9091` |
| `SYNC_JITTER_SECONDS` | Plain value | `""` | Random pre-sync sleep (seconds) to stagger retries |
| `SYNC_TRACK_LIMIT` | Plain value | `""` | Cap new tracks downloaded per run. Pipeline resumes next run. |
| `BEET_SKIP_LIMIT` | Plain value | `""` | Terminate beet import after this many skipped tracks |

### Typical k8s playlist workflow

Playlist management is fully declarative: edit `config/playlists.conf` and update the `music-pipeline-playlists` ConfigMap. The next fetch flow run reconciles disk state automatically.

**Add a new playlist:**
1. Add the entry to `config/playlists.conf`, commit and push.
2. Update the `music-pipeline-playlists` ConfigMap (or let GitOps do it).
3. The next scheduled fetch run provisions the `.spotdl` file and begins syncing.

**Freeze a playlist (stop syncing):**
1. Add `nosync` as the third field on the playlist's line in `config/playlists.conf`.
2. Update the `music-pipeline-playlists` ConfigMap.
3. The next fetch run creates the `.nosync` sentinel automatically.

**Remove a playlist:**
1. Remove the entry from `config/playlists.conf`, commit and push.
2. Update the `music-pipeline-playlists` ConfigMap.
3. The next fetch run queues beets tag cleanup and deletes the `.spotdl` file.

**Trigger a manual fetch now:**
```bash
kubectl exec -n <ns> deploy/music-pipeline -- \
  curl -s -X POST http://localhost:8080/fetch/trigger \
    -H "Authorization: Bearer <token>"
```

**Recover after PVC loss:**
1. Restore `beets-data` PVC from backup (restores `library.db`).
2. Trigger a fetch — it re-provisions all `.spotdl` files from `playlists.conf` and re-downloads.

---

## Notes and gotchas

- **`sources` is comma-separated.** A track imported by multiple playlists carries all playlist names (e.g. `sources=playlist-a,playlist-b`). It will appear in all relevant `.m3u` files once each playlist has been synced at least once.
- **`beet update` does not prune deleted files.** Use `beet remove <query>` with a specific query. Never run `beet remove` without a query.
- **Cookies expire.** Re-export from browser when downloads fail at quality.
- **Spotify rate limits.** Always use your own app credentials — the spotdl defaults are shared and hit limits quickly. For large playlists, set `SYNC_TRACK_LIMIT` to cap new downloads per session (e.g. `50`); the pipeline picks up where it left off each run.
- **MusicBrainz threshold.** `strong_rec_thresh: 0.10` in `config/beets/config.yaml`. Tracks that don't match confidently land in quarantine. A second `--asis` pass then imports any quarantined file that already has sufficient embedded tags (title, artist, album, tracknumber); the rest stay in quarantine for manual review. Raise the threshold if too many valid tracks are being quarantined.
- **`.spotdl` files are the sync state.** Never delete them manually. They are backed by the PVC and re-created by `music-ingest` from `config/playlists.conf` on first run or after PVC loss. When `SYNC_TRACK_LIMIT` is active, the snapshot intentionally contains only downloaded tracks — deferred tracks are absent so they re-appear as new on the next run.
