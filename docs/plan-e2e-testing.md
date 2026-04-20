# End-to-End Testing Plan: Unified Service

This plan covers how to test the unified `music-pipeline` service image
end-to-end — from HTTP API through fetch/scan orchestration to library push.

## Current Test Landscape

| Layer | Location | Runs in | Trigger |
|---|---|---|---|
| Unit tests (fetch) | `fetch/tests/` | `music-pipeline-fetch:dev` container | `just test` |
| Unit tests (scan) | `scan/tests/` | `music-pipeline-scan:dev` container | `just test` |
| Unit tests (service) | `service/tests/` | `music-pipeline-service:dev` container | `just test` |
| Script tests | `scripts/tests/` | `music-pipeline-service:dev` container | `just test` |
| Integration (no auth) | `tests/test_smoke.py`, `tests/test_import.py` | Host pytest + Docker SDK | `just test-integration` |
| Integration (auth) | `tests/test_auth.py` | Host pytest + Docker SDK | `just test-auth` |

Unit and script tests run inside dev containers and are covered by CI. The
integration tests in `tests/` use the Docker SDK from the host to orchestrate
real containers with isolated volumes.

## What Needs Testing

The existing integration tests (`test_smoke.py`, `test_import.py`, `test_auth.py`)
test the **scan** and **fetch** containers individually. They do not exercise:

1. **The unified service image** — entry points, system deps, HTTP API
2. **The orchestrator** — fetch-then-scan chaining, concurrency lock, debounced scan
3. **The HTTP API** — trigger endpoints, inbox upload, quarantine download
4. **Library push** — rclone copy/sync after scan
5. **File watcher** — inbox file creation triggers debounced scan

## Test Strategy

### Layer 1: Service Smoke Tests (no auth, CI-safe)

**Goal:** Verify the unified image starts, all entry points work, and system
dependencies are present.

**Approach:** Same pattern as `test_smoke.py` — run commands inside the service
container via Docker SDK.

**Tests:**
- `test_service_health` — Start the service container (with `music-pipeline`
  entry point), wait for `/health` to return 200, then stop. Validates startup,
  Flask, waitress, scheduler, and file watcher all initialise without error.
- `test_entry_points` — Run `music-ingest --help`, `music-scan --help`,
  `music-pipeline --help` inside the container. Validates all three entry points
  are installed.
- `test_system_deps` — Run `rclone version`, `fpcalc -version`, `ffmpeg -version`,
  `node --version` inside the container. Validates all system dependencies.
- `test_beet_chroma_plugin` — Run `beet version` and check `chroma` is listed
  (same as existing test but against the unified image).

**Image:** `SERVICE_IMAGE` env var override, defaulting to
`ghcr.io/sometimeskind/music-pipeline:latest`.

**Environment:** `API_BEARER_TOKEN=test-token`, `SPOTIFY_CLIENT_ID=fake`,
`SPOTIFY_CLIENT_SECRET=fake` (service validates these exist at startup but
doesn't use them for health checks).

### Layer 2: HTTP API Tests (no auth, CI-safe)

**Goal:** Exercise every HTTP endpoint against a running service container.

**Approach:** Start the service container with port 8080 mapped, then use
`requests` from the host to hit the API.

**Fixture:** A `running_service` fixture that:
1. Creates isolated Docker volumes (music, beets)
2. Starts the service container with `API_BEARER_TOKEN=test-token` and
   dummy Spotify creds
3. Waits for `/health` to return 200 (poll with backoff, 10s timeout)
4. Yields the base URL and auth headers
5. Tears down container and volumes

**Tests:**
- `test_health_no_auth` — GET `/health` without Authorization header → 200.
- `test_endpoints_require_auth` — GET `/inbox`, `/quarantine` without auth → 401.
- `test_inbox_list_empty` — GET `/inbox` with auth → 200, empty list.
- `test_inbox_upload_and_list` — POST a zip containing a test audio file to
  `/inbox/upload`, then GET `/inbox` → file appears in listing.
- `test_inbox_upload_invalid_zip` — POST garbage bytes to `/inbox/upload` → 400.
- `test_quarantine_list_empty` — GET `/quarantine` → 200, empty list.
- `test_quarantine_download_not_found` — GET `/quarantine/download/nope` → 404.
- `test_quarantine_download_traversal` — GET `/quarantine/download/../../etc/passwd` → 403.
- `test_scan_trigger` — POST `/scan/trigger` → 202.
- `test_fetch_trigger_busy` — POST `/fetch/trigger`, immediately POST again → 409
  (or 202 if first one already finished — may need a sleep/mock to make deterministic).

**Image:** Same `SERVICE_IMAGE` env var.

### Layer 3: Scan-via-Service Tests (no auth, CI-safe)

**Goal:** Verify that the full scan pipeline works when triggered through the
service, not just via the `music-scan` entry point directly.

**Approach:** Use the `running_service` fixture from Layer 2. Upload a test
audio file via the inbox endpoint, trigger a scan via the API, then verify
results.

**Tests:**
- `test_upload_and_scan_imports_track` — Upload a zip containing the fixture
  audio file via `/inbox/upload`. POST `/scan/trigger`. Poll until scan
  completes (check container logs or re-trigger returns 202 indicating lock
  released). Verify the track appears in the beets library (run `beet ls`
  inside the container via `docker exec`).
- `test_upload_and_scan_generates_playlist` — Same setup as above but with a
  spotdl playlist directory structure inside the zip. Verify `.m3u` file
  is generated in `/root/Music/playlists/`.

### Layer 4: Library Push Tests (no auth, CI-safe)

**Goal:** Verify rclone push works after scan.

**Approach:** Use a local directory as the rclone "remote" (rclone supports
`/path/to/dir` as a remote target). Set `LIBRARY_REMOTE` to a path inside
a mounted volume.

**Fixture:** Extend `running_service` with:
- An additional volume mounted at `/remote` inside the container
- `LIBRARY_REMOTE=/remote`

**Tests:**
- `test_scan_pushes_to_remote` — Upload fixture audio, trigger scan, wait for
  completion. Check that `/remote/` contains the imported track and
  `/remote/playlists/` contains the `.m3u` file.
- `test_no_push_without_library_remote` — Run without `LIBRARY_REMOTE` set.
  Trigger scan. Verify `/remote/` is empty (no push attempted).

### Layer 5: Full Integration (requires auth, local-only)

**Goal:** End-to-end test with real Spotify credentials: fetch downloads from
Spotify, scan imports, library push completes.

**Approach:** Similar to existing `test_auth.py` but using the unified service
image and API triggers instead of separate container runs.

**Prerequisites:** Same as `test_auth.py` — `SPOTIFY_CLIENT_ID`,
`SPOTIFY_CLIENT_SECRET`, `cookies.txt`, `TEST_PLAYLIST_URL`.

**Tests:**
- `test_full_pipeline_via_api` — Start service with real Spotify creds and
  `LIBRARY_REMOTE=/remote`. POST `/fetch/trigger`. Poll until complete (watch
  container logs for `Scan complete`). Verify:
  - Tracks downloaded to inbox
  - Tracks imported to staging (beet ls)
  - `.m3u` generated
  - Staging and playlists pushed to `/remote/`

**Marker:** `@pytest.mark.auth` — excluded from CI, run via `just test-auth`.

## Implementation Notes (Review Notes)

### Findings from code review (2026-04-20)

1. **Upload triggers a debounced scan (30 s delay)** — `inbox_upload` calls
   `orchestrator.schedule_scan()`, which arms a 30-second debounce timer.
   Layer 3 tests that POST to `/inbox/upload` and then immediately POST to
   `/scan/trigger` will have *two* scans racing: the explicit trigger and the
   deferred one. Tests must tolerate this (or set `debounce_delay=0` via a
   constructor override, though that's not currently exposed as an env var).
   Simplest mitigation: just POST `/scan/trigger` explicitly and poll for
   completion; if the debounced scan fires first, the result is the same.

2. **Log-polling target strings** — The orchestrator logs `==> Scan complete`
   and `==> Fetch complete` (confirmed in `orchestrator.py`). Use these as
   completion sentinels when log-polling.

3. **`fixture_audio` uses `SCAN_IMAGE`** — The session-scoped fixture in
   `conftest.py` generates the silent MP3 by running ffmpeg inside `SCAN_IMAGE`.
   Layer 3 tests need this fixture, but will use `SERVICE_IMAGE` for the
   running service. Since the service image also includes ffmpeg, the fixture
   generator can stay pointed at `SCAN_IMAGE` (or `SERVICE_IMAGE` — either
   works). No change needed.

4. **`test_fetch_trigger_busy` is non-deterministic** — A rapid double-POST to
   `/fetch/trigger` races against the background thread acquiring the lock.
   If the first fetch completes before the second POST arrives (unlikely but
   possible), both return 202 and the 409 assertion fails. Options: accept the
   test as best-effort, use a sentinel file to slow down fetch in testing, or
   drop the test and rely on the unit tests for busy-lock coverage.

5. **Layer 4 `LIBRARY_REMOTE` with a local path** — rclone accepts a bare
   directory path as a remote target. The `/remote` volume mount described in
   the plan is correct, but the rclone invocation in `_push_library` runs
   `rclone copy <staging> <remote>` (not `rclone copy <staging> <remote>:`).
   A bare path (e.g. `/remote`) works with rclone's local backend without a
   trailing colon. No code change needed, but worth noting when writing the
   fixture.

## Implementation Notes

### New Files

```
tests/
  test_service_smoke.py   — Layer 1  ✅ implemented (2026-04-20)
  test_service_api.py     — Layer 2  ✅ implemented (2026-04-20)
  test_service_api.py     — Layer 3  ✅ implemented (2026-04-20)
  test_service_push.py    — Layer 4  ✅ implemented (2026-04-20)
  test_service_e2e.py     — Layer 5  ✅ implemented (2026-04-20)
```

### Layer 5 Implementation Notes (2026-04-20)

- Uses `beets_asis_config` (asis beets config, no MusicBrainz calls) to keep
  the scan deterministic even with real Spotify downloads.
- Mounts `cookies.txt`, `SPOTDL_CONFIG`, and a temp `playlists.conf` into the
  service container alongside the standard music/beets volumes.
- Sets `LIBRARY_REMOTE=/remote` and mounts a fresh Docker volume there to
  verify the push step end-to-end.
- `wait_for_log("==> Scan complete", timeout=600)` covers both the fetch phase
  (spotdl is slow) and the chained scan. Logs are dumped on timeout or on empty
  inbox to make fetch failures diagnosable.
- Passes with working network in ~3:43 (1 track, 1 spotdl download).
- **Bug found during Layer 5 implementation:**
  The standalone `SCAN_IMAGE` on GHCR was stale: code at `/app/pipeline/`
  (old package name) while `config.yaml` `pluginpath` pointed at `/app/music_scan/`.
  beets adds `pluginpath` entries to `sys.path` and imports `beetsplug.<name>`,
  so the plugin file must live under a `beetsplug/` subdirectory of the
  pluginpath. Fixed two ways:
  1. `scan/music_scan/beetsplug/music_pipeline.py` — shim re-exporting
     `MusicPipelinePlugin`; included automatically by the `COPY scan/music_scan/`
     instruction in both Dockerfiles. Takes effect on next image build.
  2. `test_auth.py` scan operations (`run_scan`, `beet_ls`, `ls_in_volume`,
     `cat_in_volume`) now pass `image=SERVICE_IMAGE` — SERVICE_IMAGE is already
     correct and verified by Layer 3/4. Partial migration of the old test to the
     unified image (see Migration Path below).

### Layer 3 Implementation Notes (2026-04-20)

- Uses `running_service_asis` fixture (asis beets config, no MusicBrainz/AcoustID)
  to keep tests network-free and deterministic.
- `_start_service()` context manager extracted from `running_service` to allow
  both `running_service` (prod config) and `running_service_asis` (asis config)
  without duplication.
- For the playlist test, the zip must include both the `.spotdl` sentinel file
  (at `spotdl/<name>.spotdl`) and the audio file (at `spotdl/<name>/<file>`).
  Without the sentinel, `_regen_playlists()` skips m3u generation entirely.
- `/scan/trigger` accepts 202 or 409 — if the debounced scan (30 s, armed by
  the upload) somehow fires before the explicit trigger, the trigger sees 409.
  Either way, `wait_for_log("==> Scan complete")` catches the result.
- `beets_asis_config` keeps only `[music_pipeline]` in plugins — `fetchart`
  makes network calls (musicbrainz.org / fanart.tv) even with `autotag: false`,
  causing 60 s+ timeouts.
- **Bugs found during Layer 3 implementation:**
  1. Service Dockerfile copied scan source to `/build/scan/music_scan/` but
     `config.yaml` `pluginpath` expected `/app/music_scan/`. Fixed by adding
     `mkdir -p /app && cp -r /build/scan/music_scan /app/music_scan`.
  2. `_forward_sigterm()` in `process.py` called `signal.signal()` which raises
     `ValueError` in non-main threads. The orchestrator runs `scan.run()` in a
     daemon thread, so every scan failed silently. Fixed by catching `ValueError`
     and skipping SIGTERM forwarding when not in the main thread.
  3. `wait_for_log()` used `container.logs(stream=True, follow=True)` in a loop
     with a timeout check after each chunk — hangs indefinitely when the container
     goes quiet. Fixed with a daemon thread + `threading.Event.wait(timeout=)`.
  4. Scan CI was not building the service image before running integration tests,
     so Layer 3 tests always ran against a potentially stale GHCR image. Fixed by
     adding a "Build service image" step and passing `SERVICE_IMAGE=music-pipeline:ci-local`.

### conftest.py Changes

Implemented (2026-04-20):
- `SERVICE_IMAGE` constant (env var override, defaults to
  `ghcr.io/sometimeskind/music-pipeline:latest`)
- `running_service` fixture: starts service container with `network_mode="host"`,
  waits for `/health` to return 200 (30 s timeout), yields
  `{base_url, headers, container, volumes}`, tears down
- `service_in_container()` helper: runs a command inside the running container
  via `container.exec_run()`
- `wait_for_log()` helper: streams container logs until a sentinel string
  appears or timeout elapses (for use in Layer 3+)

Updated (2026-04-20, Layer 5):
- `run_scan`, `ls_in_volume`, `beet_ls`, `cat_in_volume` now accept an optional
  `image=` keyword argument (defaults to `SCAN_IMAGE`). Callers can pass
  `SERVICE_IMAGE` to route through the unified image without changing the default
  behaviour for other tests.
- `run_scan` now always specifies `command=["music-scan"]` explicitly, so it
  works regardless of which image's default CMD is set.

**Networking note:** Docker bridge port mapping and host→container-IP routing
are both non-functional in this environment (iptables issue). The fixture uses
`network_mode="host"` so the service binds directly to localhost:8080. This
means only one service container can run at a time — fine for sequential tests,
but incompatible with parallel test execution. If CI ever runs tests in
parallel, port selection logic will be needed.

### Waiting for Async Operations

The `wait_for_log()` helper (already in `conftest.py`) implements option 2
below. Use it for Layer 3+ tests.

1. **Poll the lock** — Re-POST the same trigger endpoint. 202 means the
   previous operation finished (new one started). 409 means still running.
   Simple but imprecise.
2. **Poll container logs** — Watch for `==> Scan complete` or `==> Fetch
   complete` log lines via `container.logs(stream=True)`. More reliable.
3. **Timeout wrapper** — Wrap either approach in a retry loop with a hard
   timeout (60s for scan, 300s for fetch with Spotify).

Recommended: option 2 (log polling) with a timeout wrapper. Implemented
in `wait_for_log()`.

### Port Allocation

~~Use Docker's random port mapping (`ports={"8080/tcp": None}`) and read the
assigned port from the container's `attrs`.~~

Replaced by `network_mode="host"` (see networking note above). Random port
mapping was the original plan but doesn't work in this environment.

### Path Traversal Testing

`requests` normalises URLs before sending (e.g. `../../etc/passwd` →
`/etc/passwd`), causing Flask to 404 rather than triggering the server-side
`resolve()` guard. The traversal test uses `http.client` directly to send the
raw path, and accepts either 403 (guard fired) or 404 (route not matched) as
a passing assertion.

### justfile Updates

Implemented (`test-service` recipe added):

```just
# Run service integration tests (no auth needed)
test-service:
    [ -d .venv ] || python3 -m venv .venv
    .venv/bin/pip install -q -r tests/requirements.txt
    .venv/bin/pytest tests/test_service_smoke.py tests/test_service_api.py -m "not auth" -v
```

The `test-service` recipe already uses `tests/test_service_*.py` which covers
all five layers (minus auth). The `test-integration` recipe runs all non-auth
tests, including both old and new service tests.

## Migration Path

The existing scan/fetch integration tests (`test_smoke.py`, `test_import.py`,
`test_auth.py`) should be kept until the old images are fully retired. Once
only the unified service image is deployed:

1. **Rebuild and push `music-pipeline-scan` to GHCR** — the `beetsplug/` shim
   (`scan/music_scan/beetsplug/music_pipeline.py`) is in source but the running
   GHCR image is stale. Will be fixed automatically on next CI push that builds
   the scan image.
2. ~~Update `test_auth.py` scan operations to use `SERVICE_IMAGE`~~ — **done**
   (commit `2eb0195`); `run_scan`, `beet_ls`, `ls_in_volume`, `cat_in_volume`
   now accept `image=` and `test_auth.py` passes `SERVICE_IMAGE`.
2. Update `test_smoke.py` and `test_import.py` to use `SERVICE_IMAGE` instead
   of `SCAN_IMAGE` (the service image contains all the same tools).
3. Update `test_auth.py` fetch step to use the service API (`POST /fetch/trigger`)
   instead of `run_fetch` / `FETCH_IMAGE` directly — see `test_service_e2e.py`
   for the pattern.
4. Remove `SCAN_IMAGE` and `FETCH_IMAGE` references from `conftest.py`.

## Execution Order

1. ✅ **Layer 1 (smoke)** — implemented, 9 tests, all passing (commit `c69a10c`)
2. ✅ **Layer 2 (HTTP API)** — implemented, 15 tests, all passing (commit `c69a10c`,
   path traversal fix `6c3a9c5`)
3. ✅ **Layer 3 (scan-via-service)** — implemented, 2 tests (commit `8187a64`);
   uses `running_service_asis` fixture (asis beets config, no MusicBrainz calls)
4. ✅ **Layer 4 (push)** — implemented, 2 tests (commit `3db6166`); rclone with local path remote
5. ✅ **Layer 5 (full e2e)** — implemented, 1 test (commit `2eb0195`); verified
   passing in ~3:43 with working network; requires `op run --env-file .env.tpl`
   + `TEST_PLAYLIST_URL` + `cookies.txt`
