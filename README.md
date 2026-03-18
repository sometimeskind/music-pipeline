# music-pipeline

Dockerized music pipeline: Spotify playlists → spotdl downloads → beets import/tag → Navidrome.

```
Spotify playlists → spotdl → beets → ~/Music/library → Navidrome
```

Two jobs run on separate schedules:

| Job | Default schedule | Does |
|---|---|---|
| `music-scan` | Every 5 min | Import inbox → beets, refresh metadata, regenerate m3u, Navidrome rescan |
| `music-ingest` | Daily 03:00 UTC | spotdl sync all playlists → calls music-scan |

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

The optional `nosync` flag freezes a playlist: `music-provision` creates a `.nosync` sentinel on the PVC and `music-ingest` skips `spotdl sync` for it. Remove the flag and re-run `music-provision` to unfreeze.

This file is the authoritative registry of playlists. It enables PVC recovery and non-interactive k8s provisioning.

### 5. Provision playlists (create .spotdl files)

```bash
just provision
```

Or interactively add a single playlist:

```bash
just setup
```

`music-setup` and `music-provision` are idempotent — they skip playlists whose `.spotdl` file already exists on the volume.

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
| `just up` | Start the container |
| `just down` | Stop the container |
| `just sync` | Run full ingest now |
| `just setup` | Add a new playlist (interactive) |
| `just provision` | Provision all playlists from `config/playlists.conf` (idempotent) |
| `just remove <name>` | Remove a playlist |
| `just import` | Import files dropped into inbox |
| `just logs` | Tail container logs |
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

## Navidrome integration

Navidrome lives in a separate stack and reads from the same music volume via NFS.

Required Navidrome settings:
- `ND_MUSICFOLDER` pointing at the music volume root (not just `/library` — so it sees playlists too)
- `ND_AUTOIMPORTPLAYLISTS=true`

After ingest, trigger a rescan via:

```
POST /rest/startScan?u=<user>&p=<pass>&v=1.16.1&c=music-pipeline&f=json
```

The `just rescan` recipe handles this.

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

One-shot `Job` resources for management operations:

| Job | Command | When to run |
|---|---|---|
| `music-provision` | `/usr/local/bin/music-provision` | On first deploy; after adding playlists to `playlists.conf` |
| `music-setup` | `/usr/local/bin/music-setup` | Interactive — attach to pod for single-playlist setup |
| `music-remove` | `/usr/local/bin/music-remove` | Interactive — attach to pod to remove a playlist |

### PersistentVolumeClaims

| PVC name | Mount path | Contents | Notes |
|---|---|---|---|
| `music-data` | `/root/Music` | Library, inbox, quarantine, playlists | Large; back up `library/` |
| `beets-data` | `/root/.config/beets` | `library.db`, `import.log` | Small SQLite DB; back this up |

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
| `music-pipeline-navidrome` | `api-key` | `NAVIDROME_API_KEY` | Navidrome credentials as `user:password` |

### Environment variables (all pods)

| Variable | Source | Default | Notes |
|---|---|---|---|
| `SPOTIFY_CLIENT_ID` | Secret `music-pipeline-spotify/client-id` | — | Required for music-ingest and music-setup pods |
| `SPOTIFY_CLIENT_SECRET` | Secret `music-pipeline-spotify/client-secret` | — | Required for music-ingest and music-setup pods |
| `NAVIDROME_URL` | Plain value | `""` | Base URL e.g. `http://navidrome.media:4533` |
| `NAVIDROME_API_KEY` | Secret `music-pipeline-navidrome/api-key` | `""` | Format: `user:password` |
| `PUSHGATEWAY_URL` | Plain value | `""` | e.g. `http://prometheus-pushgateway.monitoring:9091` |
| `SYNC_JITTER_SECONDS` | Plain value | `"300"` | Random pre-sync sleep to stagger retries |
| `SYNC_TRACK_LIMIT` | Plain value | `""` | Max new tracks downloaded across all playlists per session. Unset = no limit. Useful for large playlists (e.g. liked songs); the pipeline resumes where it left off next run. |

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

### Job spec notes (interactive management)

```yaml
# music-provision Job (non-interactive; run after adding playlists to playlists.conf)
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 3600
  containers:
    - command: ["/usr/local/bin/music-provision"]
      # Needs: SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, cookies Secret, playlists ConfigMap

# music-setup / music-remove Jobs (interactive — kubectl attach -it)
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 300
  containers:
    - command: ["/usr/local/bin/music-setup"]   # or music-remove
      stdin: true
      tty: true
```

### Typical k8s playlist workflow

**Add a new playlist:**
1. Add the entry to `config/playlists.conf`, commit and push.
2. Update the `music-pipeline-playlists` ConfigMap (or let GitOps do it).
3. `kubectl apply -f job-music-provision.yaml` — creates the `.spotdl` file on the PVC.
4. The next `music-ingest` CronJob run picks it up automatically.

**Freeze a playlist (stop syncing):**
1. Add `nosync` as the third field on the playlist's line in `config/playlists.conf`.
2. Update the `music-pipeline-playlists` ConfigMap (or let GitOps do it).
3. `kubectl apply -f job-music-provision.yaml` — creates the `.nosync` sentinel on the PVC.

**Remove a playlist:**
```bash
kubectl apply -f job-music-remove.yaml
kubectl attach -it job/music-remove
```

**Recover after PVC loss:**
1. Restore `beets-data` PVC from backup (restores `library.db`).
2. `kubectl apply -f job-music-provision.yaml` — re-creates all `.spotdl` files from `playlists.conf`.
3. `kubectl create job music-ingest-recovery --from=cronjob/music-ingest` — re-downloads and re-imports.

---

## Notes and gotchas

- **`source` is single-value.** A track imported by two playlists only carries the source from whichever ran first.
- **`beet update` does not prune deleted files.** Use `beet remove <query>` with a specific query. Never run `beet remove` without a query.
- **Cookies expire.** Re-export from browser when downloads fail at quality.
- **Spotify rate limits.** Always use your own app credentials — the spotdl defaults are shared and hit limits quickly. For large playlists, set `SYNC_TRACK_LIMIT` to cap new downloads per session (e.g. `50`); the pipeline picks up where it left off each run.
- **MusicBrainz threshold.** `strong_rec_thresh: 0.05` is strict. Raise to `0.10` in `config/beets/config.yaml` if too many valid tracks land in quarantine.
- **`.spotdl` files are the sync state.** Never delete them manually. They are backed by the PVC and also derivable from `config/playlists.conf` via `music-provision`. When `SYNC_TRACK_LIMIT` is active, the snapshot intentionally contains only downloaded tracks — deferred tracks are absent so they re-appear as new on the next run.
