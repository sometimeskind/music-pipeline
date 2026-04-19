# Plan: Unified Persistent Service

**Supersedes:** music-pipeline#62, homelab#566

## Motivation

The original CronJob model assumed stateless jobs sharing storage. After migrating to OpenEBS LVM LocalPV (RWO), this broke: `music-working` is owned by Nextcloud and `music-library` by Navidrome, so neither CronJob can mount them. The workaround in homelab#566/music-pipeline#62 (WebDAV via rclone, Kubernetes Lease, emptyDir) solves the immediate problem but adds significant complexity (rclone in both images, shell entrypoint scripts for Lease management, WebDAV for every file operation, open question on inbox cleanup).

The root cause is that a persistent pod already exists (`beets-keeper`, holding `beets-data` for Velero). The "stateless jobs" architecture has already been compromised. With that constraint gone, consolidating into a single persistent service is simpler overall.

---

## Resolved Architecture

One persistent `Deployment` (`music-pipeline`) replaces:
- `music-ingest` CronJob
- `music-scan` CronJob
- `beets-keeper` Deployment

The service holds `music-data` (RWO) and `beets-data` (not RWO, held for Velero backup) permanently. Fetch runs on an internal schedule; scan is event-driven. Navidrome accesses the library via an rclone sidecar (this piece of homelab#566 is still required).

---

## Storage Layout

| PVC | Access mode | Sole mounter | Contains |
|---|---|---|---|
| `music-data` | RWO | `music-pipeline` Deployment | inbox, spotdl state, quarantine |
| `beets-data` | not RWO | `music-pipeline` Deployment | `library.db`, `import.log` |
| `music-library` | RWO | Navidrome pod (rclone sidecar) | library files + playlists, pushed by `music-pipeline` via rclone WebDAV |

Beets imports into an `emptyDir` volume mounted at `/root/Music/staging/`. After import, rclone pushes staging → Navidrome sidecar. The staging emptyDir is ephemeral: on pod restart it is empty, which is correct — anything already imported is in `library.db` and already pushed to Navidrome. Playlists are also written to staging and pushed.

This keeps `music-data` small: it only needs to cover the inbox during a fetch run + spotdl state + quarantine. It does not need to hold the full library.

The `music-working` PVC (previously Nextcloud) is no longer needed by the pipeline once `music-data` is provisioned and data migrated. Nextcloud's ownership of inbox/quarantine is replaced by the HTTP API on the service.

---

## Phase 0 — Package Renaming

Both `fetch/` and `scan/` currently install a package called `pipeline`. Installed into the same image, the second overwrites the first.

**Rename before anything else:**

- `fetch/pipeline/` → importable as `music_fetch`
  - Rename directory: `fetch/pipeline/` → `fetch/music_fetch/`
  - Update `fetch/pyproject.toml`: `include = ["music_fetch*"]`
  - Update entry point: `music-ingest = "music_fetch.cli:main"`
  - Update all internal imports: `from pipeline.X` → `from music_fetch.X`

- `scan/pipeline/` → importable as `music_scan`
  - Rename directory: `scan/pipeline/` → `scan/music_scan/`
  - Update `scan/pyproject.toml`: `include = ["music_scan*"]`
  - Update entry point: `music-scan = "music_scan.cli:main"`
  - Update all internal imports: `from pipeline.X` → `from music_scan.X`

Update all test files and any `beets/config.yaml` plugin path references accordingly.
The beets config currently references `pluginpath: /app/pipeline` and `plugins: music_pipeline` — update the pluginpath to `/app/music_scan` after rename.

---

## Phase 1 — Remove `pending-removals.json`

The JSON handoff file exists only because fetch and scan run in separate processes/pods. In the unified service they run in the same process.

**Change `music_fetch/ingest.py`:**
- Define a dataclass: `PendingRemovals(tracks: list[dict], remove_sources: list[str])`
- `run()` returns `PendingRemovals` instead of writing to disk
- Remove `_write_pending_removals()`, `PENDING_REMOVALS` constant

**Change `music_scan/scan.py`:**
- `run(pending: PendingRemovals | None = None)` — accepts removals directly
- Remove `_process_pending_removals()`, `PENDING_REMOVALS` constant
- If `pending` is None (standalone scan run), skip removal processing

The file format backward-compat code can be removed entirely.

---

## Phase 2 — New `service/` Directory

```
service/
  music_service/
    __init__.py
    orchestrator.py   — APScheduler + watchdog + job runner
    api.py            — Flask HTTP endpoints
    auth.py           — bearer token middleware
    cli.py            — entry point: music-pipeline
  tests/
  Dockerfile
  pyproject.toml
  requirements.txt
  requirements-dev.txt
```

### `orchestrator.py`

```python
# Responsibilities:
# - threading.Lock() prevents fetch and scan running concurrently
# - run_fetch(): calls music_fetch.ingest.run(), passes PendingRemovals to run_scan()
# - run_scan(pending=None): calls music_scan.scan.run(pending)
# - APScheduler (BackgroundScheduler): fetch on FETCH_CRON schedule
# - watchdog Observer: watches /root/Music/inbox for FileCreatedEvent on audio extensions
#   → debounced (30s) call to run_scan() to avoid firing on every file in a batch upload
# - on_fetch_complete: after fetch, immediately enqueue run_scan() with the returned PendingRemovals
```

**Environment variable:** `FETCH_CRON` — APScheduler cron expression, default `0 3 * * *`.
Debounce on watchdog events: use a `threading.Timer` reset on each event, fire after 30s of quiet.

### `api.py`

Six endpoints, all protected by bearer token except `/health`:

```
GET  /health                          — liveness/readiness probe (no auth)
GET  /inbox                           — list audio files in inbox tree (name, size, modified)
POST /inbox/upload                    — multipart upload; saves to /root/Music/inbox/
                                        triggers debounced scan on completion
GET  /quarantine                      — list files in quarantine (name, size, modified)
GET  /quarantine/download/<path:name> — stream file for download
POST /fetch/trigger                   — enqueue fetch (returns 409 if already running)
POST /scan/trigger                    — enqueue scan (returns 409 if already running)
```

Use Flask. Run with `waitress` (pure-Python WSGI server, no C deps).

### `auth.py`

```python
# Middleware: check Authorization: Bearer <token> header
# Token read from API_BEARER_TOKEN env var at startup
# Returns 401 on missing/invalid token
# /health exempt
```

### `cli.py`

Entry point `music-pipeline`:
1. Validate required env vars (`SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `API_BEARER_TOKEN`)
2. Start orchestrator (scheduler + file watcher) in background threads
3. Start Flask/waitress on `0.0.0.0:8080`

### `Dockerfile`

```dockerfile
FROM python:3.13-slim AS prod

# System deps: all of fetch (ffmpeg, nodejs) + scan (ffmpeg, chromaprint)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ffmpeg nodejs libchromaprint-tools rclone \
    && rm -rf /var/lib/apt/lists/*

# Install all three packages
COPY fetch/ /build/fetch/
COPY scan/ /build/scan/
COPY service/ /build/service/
RUN pip install --no-cache-dir /build/fetch /build/scan /build/service

# Pre-create directories
RUN mkdir -p \
    /root/Music/inbox/spotdl \
    /root/Music/library \
    /root/Music/quarantine \
    /root/Music/playlists \
    /root/.config/beets \
    /root/.config/spotdl \
    /root/.config/music-pipeline \
    /root/.config/yt-dlp

RUN echo --js-runtimes node > /root/.config/yt-dlp/config

CMD ["music-pipeline"]

FROM prod AS dev
COPY service/requirements-dev.txt /tmp/reqs-dev.txt
RUN pip install --no-cache-dir -r /tmp/reqs-dev.txt
COPY service/tests/ /app/tests/
WORKDIR /app
ENTRYPOINT []
CMD ["pytest"]
```

### `pyproject.toml`

```toml
[project]
name = "music-pipeline-service"
dependencies = [
    "music-pipeline-fetch",
    "music-pipeline-scan",
    "apscheduler>=3.10",
    "watchdog>=4.0",
    "flask>=3.0",
    "waitress>=3.0",
]

[project.scripts]
music-pipeline = "music_service.cli:main"
```

---

## Phase 3 — Navidrome Integration (rclone push)

The `music-library` PVC is owned by Navidrome's pod via an rclone WebDAV sidecar (this is the Navidrome sidecar piece from homelab#566 — still required).

After each scan run, the orchestrator calls a new `push_to_navidrome()` function that uses rclone to push:
- `/root/Music/library/` → Navidrome sidecar WebDAV
- `/root/Music/playlists/` → Navidrome sidecar WebDAV

Environment variable: `NAVIDROME_WEBDAV_URL` (e.g. `http://navidrome:8080/dav`).
The existing `trigger_scan()` (Subsonic API call) remains and fires after the push.

rclone is added to the unified Dockerfile (see Phase 2). No rclone config file needed — use `rclone copy --webdav-url` flags directly, credentials from env vars.

---

## Phase 4 — CLI Script (`scripts/music-files`)

A standalone Python script. No dependencies beyond the standard library + `requests` (already present in the pipeline).

Reads from environment:
- `MUSIC_PIPELINE_URL` — e.g. `http://music-pipeline.homelab.svc:8080` or ingress URL
- `MUSIC_PIPELINE_TOKEN` — bearer token

```
music-files list-inbox
music-files upload <file> [<file> ...]
music-files list-quarantine
music-files download <filename>
music-files trigger-fetch
music-files trigger-scan
```

`list-inbox` and `list-quarantine` output a table: filename, size, modified date.
`download` saves to the current directory.

---

## Phase 5 — Kubernetes Resources

### ConfigMaps

All config previously bind-mounted is promoted to ConfigMaps:

```yaml
# music-pipeline-beets-config
# key: config.yaml
# source: config/beets/config.yaml
# Changes from current:
#   - pluginpath updated to /app/music_scan after package rename
#   - directory updated to /root/Music/staging (emptyDir mount)
#   - import.move: yes remains — beets moves files from inbox into staging

# music-pipeline-spotdl-config
# key: config.json
# source: config/spotdl/config.json

# music-pipeline-playlists
# key: playlists.conf
# source: config/playlists.conf
```

### Secrets

```yaml
# music-pipeline-credentials
SPOTIFY_CLIENT_ID: ...
SPOTIFY_CLIENT_SECRET: ...
API_BEARER_TOKEN: ...        # generate with: openssl rand -hex 32
NAVIDROME_USER: ...
NAVIDROME_PASSWORD: ...

# music-pipeline-cookies
# key: cookies.txt
# source: cookies.txt (not committed)
```

### Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: music-pipeline
spec:
  replicas: 1
  strategy:
    type: Recreate          # RWO PVC — only one pod at a time
  template:
    spec:
      containers:
      - name: music-pipeline
        image: ghcr.io/sometimeskind/music-pipeline:latest
        ports:
        - containerPort: 8080   # HTTP API
        env:
        - name: FETCH_CRON
          value: "0 3 * * *"
        - name: PUSHGATEWAY_URL
          value: "http://pushgateway:9091"
        - name: NAVIDROME_URL
          value: "http://navidrome:4533"
        - name: SYNC_TRACK_LIMIT
          value: ""
        - name: SYNC_JITTER_SECONDS
          value: ""
        envFrom:
        - secretRef:
            name: music-pipeline-credentials
        volumeMounts:
        - name: music-data
          mountPath: /root/Music
        - name: beets-data
          mountPath: /root/.config/beets
        - name: beets-config
          mountPath: /root/.config/beets/config.yaml
          subPath: config.yaml
          readOnly: true
        - name: spotdl-config
          mountPath: /root/.config/spotdl/config.json
          subPath: config.json
          readOnly: true
        - name: playlists
          mountPath: /root/.config/music-pipeline/playlists.conf
          subPath: playlists.conf
          readOnly: true
        - name: cookies
          mountPath: /root/.config/spotdl/cookies.txt
          subPath: cookies.txt
          readOnly: true
        - name: staging
          mountPath: /root/Music/staging
        livenessProbe:
          httpGet:
            path: /health
            port: 8080
          initialDelaySeconds: 10
          periodSeconds: 30
        readinessProbe:
          httpGet:
            path: /health
            port: 8080
          initialDelaySeconds: 5
          periodSeconds: 10
      volumes:
      - name: music-data
        persistentVolumeClaim:
          claimName: music-data
      - name: beets-data
        persistentVolumeClaim:
          claimName: beets-data
      - name: beets-config
        configMap:
          name: music-pipeline-beets-config
      - name: spotdl-config
        configMap:
          name: music-pipeline-spotdl-config
      - name: playlists
        configMap:
          name: music-pipeline-playlists
      - name: cookies
        secret:
          secretName: music-pipeline-cookies
      - name: staging
        emptyDir: {}        # beets import staging; ephemeral, cleared on pod restart
```

Note: `strategy: Recreate` is required with RWO — a rolling update would stall waiting for the old pod to release the volume.

### Service

```yaml
apiVersion: v1
kind: Service
metadata:
  name: music-pipeline
spec:
  selector:
    app: music-pipeline
  ports:
  - name: http
    port: 8080
    targetPort: 8080
```

### Ingress

Expose via existing homelab Ingress controller for VPN-accessible URL. Auth is the bearer token — no additional Ingress-level auth needed given VPN constraint.

---

## Phase 6 — justfile Updates

```makefile
# Build unified service image
build:
    docker build -t music-pipeline:local .

# Run tests: fetch, scan, service
test:
    docker build --target dev -t music-pipeline-fetch:dev fetch
    docker run --rm music-pipeline-fetch:dev
    docker build --target dev -t music-pipeline-scan:dev scan
    docker run --rm music-pipeline-scan:dev
    docker build --target dev -t music-pipeline-service:dev service
    docker run --rm music-pipeline-service:dev

# Run local fetch (against local compose, for dev only)
fetch:
    op run --env-file .env.tpl -- docker compose run --rm service music-ingest

# Run local scan
scan:
    docker compose run --rm service music-scan

# Run full ingest
sync:
    just fetch && just scan
```

The `compose.yml` gains a single `service` container replacing `fetch` and `scan`.

---

## Phase 7 — What to Retain from homelab#566

**Still needed:**
- Navidrome rclone sidecar (rclone serve webdav on music-library PVC)
- NetworkPolicy restricting sidecar WebDAV to music-pipeline pod only
- ConfigMaps for beets config, spotdl config, playlists.conf (per Phase 5 above)
- Provision new `music-data` PVC (RWO, OpenEBS hostpath)

**No longer needed:**
- Kubernetes Lease + shell entrypoint scripts for mutual exclusion
- rclone in fetch/scan images for WebDAV operations
- emptyDir for fetch/scan working directories
- RBAC for kubectl access from CronJob pods
- `.pending-removals.json` on WebDAV
- Nextcloud WebDAV access for inbox/quarantine

---

## Migration Steps

1. Apply Navidrome rclone sidecar (homelab repo) — unblocks Navidrome from library PVC
2. Provision `music-data` PVC (RWO, OpenEBS hostpath, sized for inbox + library + quarantine)
3. Migrate data: copy from current `music-working` and `music-library` PVCs into `music-data`
4. Perform package rename (Phase 0) — update imports, tests, beets config pluginpath
5. Implement Phase 1 (remove pending-removals) and Phase 2 (service/ directory)
6. Build and push unified image to GHCR
7. Apply ConfigMaps, Secrets, Deployment, Service, Ingress
8. Verify: fetch runs on schedule, scan triggers on completion and on inbox file drop, HTTP API works from laptop
9. Delete `beets-keeper` Deployment
10. Delete `music-ingest` CronJob
11. Delete `music-scan` CronJob
12. Decommission `music-working` Nextcloud PVC (once data confirmed migrated)

---

## Open Questions

- **Ingress hostname**: TBD based on homelab ingress setup.
