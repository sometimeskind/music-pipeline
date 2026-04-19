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

The service holds `music-data` (RWO) and `pipeline-state` (not RWO, held for Velero backup) permanently. Fetch runs on an internal schedule; scan is event-driven. Navidrome accesses the library via an rclone sidecar (this piece of homelab#566 is still required).

---

## Storage Layout

| PVC | Access mode | Sole mounter | Contains |
|---|---|---|---|
| `music-data` | RWO | `music-pipeline` Deployment | inbox (user-dropped tracks + spotdl downloads), quarantine |
| `pipeline-state` | not RWO | `music-pipeline` Deployment | `library.db`, `import.log` (subPath `beets/`); `.spotdl` snapshot files (subPath `spotdl/`) |
| `music-library` | RWO | Navidrome pod (rclone sidecar) | library files + playlists, pushed by `music-pipeline` via rclone WebDAV |

**Why spotdl state lives on `pipeline-state`:** `.spotdl` files are application state (they record which Spotify URLs have been processed and drive soft-delete logic), not user data. They belong alongside `library.db`. The PVC is mounted at two subPaths — `beets/` → `/root/.config/beets/` and `spotdl/` → `/root/Music/inbox/spotdl/`.

Beets imports into an `emptyDir` volume mounted at `/root/Music/staging/`. After import, rclone pushes staging → Navidrome sidecar. The staging emptyDir is ephemeral: on pod restart it is empty, which is correct — anything already imported is in `library.db` and already pushed to Navidrome. Playlists are also written to staging and pushed.

This keeps `music-data` small: inbox during a fetch run + quarantine only.

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
POST /inbox/upload                    — accepts a zip archive; extracts into /root/Music/inbox/
                                        preserving directory structure. triggers debounced scan.
GET  /quarantine                      — list files in quarantine (name, size, modified)
GET  /quarantine/download/<path:name> — if path is a file: stream directly.
                                        if path is a directory: zip on the fly and stream.
POST /fetch/trigger                   — enqueue fetch (returns 409 if already running)
POST /scan/trigger                    — enqueue scan (returns 409 if already running)
```

Upload and download both use Python's stdlib `zipfile` — no extra deps. The zip boundary aligns with how the user works: upload an album directory, download a quarantine subdirectory.

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
music-files upload <path>          — zips <path> (file or directory) and POSTs to /inbox/upload
music-files list-quarantine
music-files download <path>        — downloads <path> from quarantine; unzips directories in place
music-files trigger-fetch
music-files trigger-scan
```

`list-inbox` and `list-quarantine` output a table: name, size, modified date.
`upload` zips transparently — caller passes a plain path, never touches zip files directly.
`download` saves to the current directory; if the response is a zip it is extracted there.

---

## Phase 5 — Namespace Split

Currently `media` namespace contains Nextcloud, Navidrome, and music-pipeline. Nextcloud now has no connection to the music services — the inbox/quarantine handoff via Nextcloud WebDAV is being replaced by the HTTP API. Splitting into separate namespaces removes the coupling and makes each service independently manageable.

**Split:**
- `music` namespace: music-pipeline Deployment + Navidrome (tightly coupled via rclone sidecar + Subsonic API)
- `media` namespace: Nextcloud + Redis (no change to existing manifests)

**Homelab changes required:**
- Create `kubernetes/music/namespace.yaml` — new `music` namespace with same pod-security labels as current `media`
- Move `kubernetes/music-pipeline/` resources to `music` namespace
- Move `kubernetes/navidrome/` resources to `music` namespace — update all `namespace: media` → `namespace: music`
- Update Velero backup schedule: add `music` to `includedNamespaces`, keep `media` (for Nextcloud)
- Update `NAVIDROME_URL` env var in music-pipeline Deployment: `http://navidrome.music.svc`
- Update `PUSHGATEWAY_URL` references (still `monitoring` namespace — no change needed there)
- NetworkPolicy for the rclone sidecar is scoped within the new `music` namespace (simplifies selector)

**Migration note:** Service DNS names change from `*.media.svc` to `*.music.svc`. Navidrome's HTTPRoute namespace label changes; nothing outside the namespace references it directly. The `music-library` PVC is currently in `media` — it must be recreated in `music` (PVCs are namespace-scoped). Because the PVC is RWO and bound to a pre-provisioned LVM PV (`volumeName: pvc-45ab61ec-...`), the procedure is: delete the old PVC in `media`, recreate it in `music` referencing the same `volumeName`. The LV on the node is untouched; the new PVC binds to it immediately.

---

## Phase 6 — Kubernetes Resources

### Namespace

All resources in `music` namespace (per Phase 5).

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
# Note: current CronJob mounts this at /root/.spotdl/config.json (legacy path).
# Deployment below mounts at /root/.config/spotdl/config.json — confirm which
# path the image reads before deploying (or set SPOTDL_CONFIG env var).

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
  namespace: music              # Phase 5 namespace split
spec:
  replicas: 1
  strategy:
    type: Recreate          # RWO PVC — only one pod at a time
  template:
    metadata:
      annotations:
        # inbox/quarantine is transient — exclude from Velero fs-backup.
        # pipeline-state (beets db, spotdl files) is included by default.
        # staging is emptyDir — auto-excluded by Velero.
        backup.velero.io/backup-volumes-excludes: music-data
    spec:
      containers:
      - name: music-pipeline
        image: ghcr.io/sometimeskind/music-pipeline:latest
        ports:
        - containerPort: 8080   # HTTP API
        resources:
          requests:
            cpu: "100m"
            memory: "256Mi"
          limits:
            memory: "2Gi"       # fetch can spike to ~2Gi; carry over from fetch CronJob limit
        env:
        - name: FETCH_CRON
          value: "0 3 * * *"
        - name: PUSHGATEWAY_URL
          value: "http://prometheus-pushgateway.monitoring.svc.cluster.local:9091"
        - name: NAVIDROME_URL
          value: "http://navidrome.music.svc"   # music namespace (Phase 5)
        - name: SYNC_TRACK_LIMIT
          value: ""
        - name: SYNC_JITTER_SECONDS
          value: ""
        - name: SYNC_TIMEOUT_SECONDS
          value: "6300"
        envFrom:
        - secretRef:
            name: music-pipeline-credentials
        volumeMounts:
        - name: music-data
          mountPath: /root/Music
        - name: pipeline-state
          mountPath: /root/.config/beets
          subPath: beets
        - name: pipeline-state
          mountPath: /root/Music/inbox/spotdl
          subPath: spotdl
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
      - name: pipeline-state
        persistentVolumeClaim:
          claimName: pipeline-state   # formerly beets-data; rename PVC or create new + migrate
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
  namespace: music
spec:
  selector:
    app: music-pipeline
  ports:
  - name: http
    port: 8080
    targetPort: 8080
```

### HTTPRoute

The homelab uses Gateway API, not traditional Ingress. Use an `HTTPRoute` on the `private` gateway:

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: music-pipeline
  namespace: music
spec:
  parentRefs:
  - name: private
    namespace: gateways
  hostnames:
  - music-pipeline.prins.id    # TBD — confirm hostname
  rules:
  - backendRefs:
    - name: music-pipeline
      port: 8080
```

Auth is the bearer token — no additional gateway-level auth needed given VPN constraint.

**Required after applying:** Add OPNsense DNS host override for the chosen hostname → `192.168.11.21` (IPv4) and `fd00:0:3::2` (IPv6).

---

## Phase 7 — justfile Updates

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

## Phase 8 — What to Retain from homelab#566

**Still needed:**

- **Navidrome rclone sidecar** — add to `kubernetes/navidrome/navidrome.yaml`: a second container running `rclone serve webdav /music --addr :8081`, mounting `music-library`. music-pipeline pushes via `rclone copy --webdav-url http://navidrome.music.svc:8081`. Navidrome itself continues to read from the mounted PVC volume directly (no change to its mount or config).

- **NetworkPolicy** — add `kubernetes/music/network-policy.yaml` restricting inbound to the rclone sidecar port (8081) to only pods with `app: music-pipeline` in the `music` namespace. With the namespace split, the selector is clean and self-contained.

- **ConfigMaps** for beets config, spotdl config, playlists.conf (per Phase 6 above)

- **Provision new `music-data` PVC** (RWO, `openebs-hostpath`, sized for inbox during fetch + quarantine — 50Gi is sufficient given `music-working` is 50Gi today)

**No longer needed:**
- Kubernetes Lease + shell entrypoint scripts for mutual exclusion
- rclone in fetch/scan images for WebDAV operations
- emptyDir for fetch/scan working directories
- RBAC for kubectl access from CronJob pods
- `.pending-removals.json` on WebDAV
- Nextcloud WebDAV access for inbox/quarantine

---

## Migration Steps

1. **Create `music` namespace** (homelab repo) — `kubernetes/music/namespace.yaml` with pod-security labels

2. **Apply Navidrome rclone sidecar** (homelab repo) — add sidecar container and NetworkPolicy to Navidrome manifests; move Navidrome to `music` namespace (requires deleting and recreating the `music-library` PVC in `music` — see Phase 5 note on LVM PV rebind). Update Velero schedule to include `music` namespace.

3. **Migrate `music-library` PVC to `music` namespace:**
   a. Scale Navidrome to 0 replicas
   b. Delete `music-library` PVC in `media` (the underlying LV on the node is untouched)
   c. Recreate `music-library` PVC in `music`, with the same `volumeName: pvc-45ab61ec-...` to rebind to the existing LV
   d. Redeploy Navidrome in `music` namespace

4. **Provision `music-data` PVC** (`openebs-hostpath`, RWO, 50Gi, in `music` namespace)

5. **Provision `pipeline-state` PVC** — `openebs-hostpath`, RWO. `beets-data` cannot be renamed in place; procedure:
   a. Scale `beets-keeper` to 0
   b. Create new `pipeline-state` PVC in `music` namespace
   c. Run a one-off copy pod mounting both `beets-data` and `pipeline-state`; copy beets state to `beets/` subPath, migrate existing spotdl `.spotdl` files to `spotdl/` subPath
   d. Verify contents, then proceed

6. **Migrate inbox/quarantine data** from `music-working` to `music-data` via a one-off copy pod

7. **Seal new combined secret** `music-pipeline-credentials` (Spotify + bearer token + Navidrome creds); update `scripts/reseal-all.sh` and `kubernetes/secrets/RESEAL.md`; delete old `music-pipeline-spotify` and `music-pipeline-navidrome` sealed secrets

8. **Perform package rename** (Phase 0) — update imports, tests, beets config pluginpath

9. **Implement Phase 1** (remove pending-removals) **and Phase 2** (service/ directory)

10. **Build and push** unified image to GHCR

11. **Apply** ConfigMaps, Secrets, Deployment, Service, HTTPRoute in `music` namespace; **add OPNsense DNS host override** for the pipeline hostname

12. **Verify:** fetch runs on schedule, scan triggers on completion and on inbox file drop, HTTP API + CLI work from laptop

13. **Delete** `beets-keeper` Deployment, `music-ingest` CronJob, `music-scan` CronJob (from `media` namespace)

14. **Decommission `music-working` PVC** (once inbox data confirmed migrated) — delete PVC in `media`, then clean up the underlying LVM LV on the node

15. **Clean up `beets-data` PVC** in `media` once `pipeline-state` is verified

---

## Open Questions

- **HTTPRoute hostname**: TBD — `music-pipeline.prins.id` is a reasonable default. Requires OPNsense host override once chosen.
- **spotdl config path**: Confirm whether the container image reads `/root/.spotdl/config.json` (legacy) or `/root/.config/spotdl/config.json` before finalising the volume mount. May require updating the Dockerfile or setting `SPOTDL_CONFIG` env var.
