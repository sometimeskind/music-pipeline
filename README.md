# music-pipeline

Dockerized music pipeline: Spotify playlists → spotdl downloads → beets import/tag → music library.

```
Spotify playlists → spotdl → beets → ~/Music/library
```

Two top-level jobs run on separate schedules:

| Job | Default schedule | Does |
|---|---|---|
| `music-ingest` | Daily 03:00 UTC | Spotify/YouTube sync — downloads new tracks, queues removals |
| `music-scan` | Every 5 min | Import inbox → beets, refresh metadata, regenerate .m3u |

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

1. Install the browser extension **"Get cookies.txt LOCALLY"**
2. Sign in to [music.youtube.com](https://music.youtube.com) with your YouTube Premium account
3. Export cookies in Netscape format
4. Save as `cookies.txt` in the repo root (already in `.gitignore`)

Cookies expire periodically. Re-export when downloads start failing at quality.

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
just up
```

The container runs two cron jobs: `music-scan` every 5 minutes and `music-ingest` daily at 03:00 UTC. Override with `SCAN_CRON_SCHEDULE` and `SYNC_CRON_SCHEDULE` env vars.

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

Two `CronJob` resources sharing two `PersistentVolumeClaim`s:

| CronJob | Schedule | Command |
|---|---|---|
| `music-scan` | `*/5 * * * *` | `/usr/local/bin/music-scan` |
| `music-ingest` | `0 3 * * *` | `/usr/local/bin/music-ingest` |

### PersistentVolumeClaims

| PVC name | Contents | Notes |
|---|---|---|
| `music-working` | `inbox/`, `quarantine/` | Ephemeral working data; shared between fetch and scan |
| `music-library` | `library/`, `playlists/` | Permanent library; back this up |
| `beets-data` | `library.db`, `import.log` | Small SQLite DB; back this up |

Mounted with `subPath` so each PVC root maps to the correct sub-directory under `/root/Music`:

| PVC | `subPath` | `mountPath` | Pods |
|---|---|---|---|
| `music-working` | `inbox` | `/root/Music/inbox` | fetch, scan |
| `music-working` | `quarantine` | `/root/Music/quarantine` | scan only |
| `music-library` | `library` | `/root/Music/library` | scan only |
| `music-library` | `playlists` | `/root/Music/playlists` | scan only |
| `beets-data` | _(none)_ | `/root/.config/beets` | scan only |

The fetch container only needs `music-working` (inbox mount). The scan container mounts all five.

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

### Environment variables (all pods)

| Variable | Source | Default | Notes |
|---|---|---|---|
| `SPOTIFY_CLIENT_ID` | Secret `music-pipeline-spotify/client-id` | — | Required for music-ingest pods |
| `SPOTIFY_CLIENT_SECRET` | Secret `music-pipeline-spotify/client-secret` | — | Required for music-ingest pods |
| `PUSHGATEWAY_URL` | Plain value | `""` | e.g. `http://prometheus-pushgateway.monitoring:9091` |
| `SYNC_JITTER_SECONDS` | Plain value | `"300"` | Random pre-sync sleep to stagger retries |
| `SYNC_TRACK_LIMIT` | Plain value | `""` | Max new tracks downloaded across all playlists per session. Unset = no limit. Useful for large playlists (e.g. liked songs); the pipeline resumes where it left off next run. Fetch pods only. |
| `BEET_SKIP_LIMIT` | Plain value | `""` | Terminate beet import early after this many skipped tracks. Unset = no limit. Useful for threshold testing or capping import time per run. Scan pods only. |

`SCAN_CRON_SCHEDULE` and `SYNC_CRON_SCHEDULE` are only relevant to Docker Compose (written to `/etc/cron.d`). In k8s, the schedule is set on the `CronJob` spec directly.

### Volume: YouTube cookies

`cookies.txt` is **not** managed as a ConfigMap (it's binary-adjacent and expires frequently).
Mount it as a `Secret`:

| Secret name | Key | Mount path |
|---|---|---|
| `music-pipeline-cookies` | `cookies.txt` | `/root/.config/spotdl/cookies.txt` |

Mount `readOnly: true`. Update by patching the Secret; the next pod start picks it up.

### CronJob spec notes

```yaml
# music-scan CronJob
schedule: "*/5 * * * *"
concurrencyPolicy: Forbid          # never run two scans at once
successfulJobsHistoryLimit: 3
failedJobsHistoryLimit: 3
spec:
  backoffLimit: 0                  # don't retry scans; next cron run will retry
  activeDeadlineSeconds: 240       # kill if scan takes > 4 min
  containers:
    - command: ["/usr/local/bin/music-scan"]
      # Spotify credentials NOT needed — music-scan makes no network calls

# music-ingest CronJob
schedule: "0 3 * * *"
concurrencyPolicy: Forbid
successfulJobsHistoryLimit: 3
failedJobsHistoryLimit: 7          # keep a week of failures for debugging
spec:
  backoffLimit: 2                  # retry up to 2x on rate-limit/transient failures
  activeDeadlineSeconds: 7200      # 2-hour hard deadline
  containers:
    - command: ["/usr/local/bin/music-ingest"]
      # Needs: SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, cookies Secret
```

### Typical k8s playlist workflow

Playlist management is fully declarative: edit `config/playlists.conf` and update the `music-pipeline-playlists` ConfigMap. The next `music-ingest` CronJob run reconciles disk state automatically.

**Add a new playlist:**
1. Add the entry to `config/playlists.conf`, commit and push.
2. Update the `music-pipeline-playlists` ConfigMap (or let GitOps do it).
3. The next `music-ingest` CronJob run provisions the `.spotdl` file and begins syncing.

**Freeze a playlist (stop syncing):**
1. Add `nosync` as the third field on the playlist's line in `config/playlists.conf`.
2. Update the `music-pipeline-playlists` ConfigMap.
3. The next `music-ingest` run creates the `.nosync` sentinel automatically.

**Remove a playlist:**
1. Remove the entry from `config/playlists.conf`, commit and push.
2. Update the `music-pipeline-playlists` ConfigMap.
3. The next `music-ingest` run queues beets tag cleanup and deletes the `.spotdl` file.

**Run a manual ingest now:**
```bash
kubectl create job music-ingest-manual --from=cronjob/music-ingest
kubectl logs -f job/music-ingest-manual
```

**Recover after PVC loss:**
1. Restore `beets-data` PVC from backup (restores `library.db`).
2. `kubectl create job music-ingest-recovery --from=cronjob/music-ingest` — re-provisions all `.spotdl` files from `playlists.conf` and re-downloads.

---

## Notes and gotchas

- **`source` is single-value.** A track imported by two playlists only carries the source from whichever ran first.
- **`beet update` does not prune deleted files.** Use `beet remove <query>` with a specific query. Never run `beet remove` without a query.
- **Cookies expire.** Re-export from browser when downloads fail at quality.
- **Spotify rate limits.** Always use your own app credentials — the spotdl defaults are shared and hit limits quickly. For large playlists, set `SYNC_TRACK_LIMIT` to cap new downloads per session (e.g. `50`); the pipeline picks up where it left off each run.
- **MusicBrainz threshold.** `strong_rec_thresh: 0.10` in `config/beets/config.yaml`. Tracks that don't match confidently land in quarantine. A second `--asis` pass then imports any quarantined file that already has sufficient embedded tags (title, artist, album, tracknumber); the rest stay in quarantine for manual review. Raise the threshold if too many valid tracks are being quarantined.
- **`.spotdl` files are the sync state.** Never delete them manually. They are backed by the PVC and re-created by `music-ingest` from `config/playlists.conf` on first run or after PVC loss. When `SYNC_TRACK_LIMIT` is active, the snapshot intentionally contains only downloaded tracks — deferred tracks are absent so they re-appear as new on the next run.
