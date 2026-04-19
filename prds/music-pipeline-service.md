# PRD: Music Pipeline — Unified Service Package

## Overview

This PRD covers the changes to the `music-pipeline` repository to consolidate
the two-container pipeline (fetch + scan) into a single, persistent service. It
does not cover Kubernetes manifests or homelab-side deployment — those are
tracked in the homelab repo.

## Background

The pipeline currently ships two separate container images:
- `music-pipeline-fetch` — spotdl sync, Spotify/YouTube downloads
- `music-pipeline-scan` — beets import, tagging, .m3u generation

Each image installs a Python package called `pipeline`, so they cannot coexist
in a single image — the second install silently overwrites the first. They
coordinate via a `.pending-removals.json` file written by fetch and read by
scan. Both images are consumed by ephemeral CronJob pods.

The target state is a single persistent service that runs both subsystems
in-process, exposes an HTTP API, and reacts to both a cron schedule and
inbox file events.

## Goals

- Ship a single unified container image containing both fetch and scan logic.
- Expose an HTTP API for operational control (triggers, inbox management, quarantine access).
- Replace the `.pending-removals.json` file handoff with in-process data passing.
- Provide a standalone CLI script (`scripts/music-files`) for interacting with the API.
- Update local development tooling (compose, justfile) to match the new layout.

## Non-Goals

- Kubernetes manifests, ConfigMaps, Secrets, PVCs — tracked in the homelab repo.
- Configuration of any external rclone remote target — defined by deployment environment.
- Data migration or PVC rebinding procedures — homelab repo.
- Changing beets behaviour, spotdl behaviour, or any existing pipeline logic.

## Functional Requirements

### Package renaming — DONE (#63, merged via #69)

1. ~~`fetch/pipeline/` MUST be renamed to `fetch/music_fetch/` and installed as
   the `music_fetch` package. The entry point MUST remain `music-ingest`.~~
2. ~~`scan/pipeline/` MUST be renamed to `scan/music_scan/` and installed as
   the `music_scan` package. The entry point MUST remain `music-scan`. The beets
   plugin path in `config/beets/config.yaml` MUST be updated accordingly.~~
3. ~~All internal imports in both packages MUST be updated to reflect the rename.~~
4. ~~All test files MUST be updated to import from the new package names.~~

### In-process data passing — DONE (#64, merged via #70)

5. ~~A `PendingRemovals` dataclass (containing track list and source URLs) MUST be
   defined in `music_fetch`. `music_scan` imports it from `music_fetch` — this
   creates a build-time dependency of scan on fetch, which is acceptable since
   both are always installed together in the unified image.~~
6. ~~`music_fetch.ingest.run()` MUST return a `PendingRemovals` instance instead
   of writing `.pending-removals.json` to disk.~~
7. ~~`music_scan.scan.run()` MUST accept an optional `PendingRemovals` argument.
   When `None` (standalone scan invocation), removal processing is skipped.~~
8. ~~The `.pending-removals.json` file format and all code that reads or writes it
   MUST be removed.~~

Implementation notes: Also introduced a `RemovedTrack` dataclass for type safety
(replaces raw dicts). Scan Dockerfile build context changed to repo root to
install the `music_fetch` dependency. CI workflow and compose.yml updated accordingly.

### Staging directory — DONE (#66, merged via #72)

9. ~~Beets' `directory` setting in `config/beets/config.yaml` MUST be changed
   from `/root/Music/library` to `/root/Music/staging`. In production this is
   an `emptyDir` volume — ephemeral and cleared on pod restart. This is safe
   because imported tracks are immediately pushed to Navidrome via rclone
   (see library push requirements below), and `library.db` on `pipeline-state`
   is the authoritative record of what has been imported.~~

### New `service/` package — DONE (#65, merged via #71)

10. ~~A new top-level `service/` directory MUST be added, containing a
    `music_service` Python package with entry point `music-pipeline`.~~
11. ~~On startup, the service MUST:~~
   ~~a. Validate that `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, and
      `API_BEARER_TOKEN` are set.~~
   ~~b. Start a scheduler that runs fetch on the `FETCH_CRON` schedule
      (default `0 3 * * *`).~~
   ~~c. Start a file watcher on `/root/Music/inbox` that triggers a debounced
      scan (30-second quiet window) when audio files are created.~~
   ~~d. Start an HTTP server on `0.0.0.0:8080`.~~

### Orchestrator — DONE (#65, merged via #71)

12. ~~The orchestrator MUST use a `threading.Lock` to prevent fetch and scan from
    running concurrently.~~
13. ~~After fetch completes, the orchestrator MUST immediately invoke scan,
    passing the returned `PendingRemovals` in-process.~~
14. ~~A fetch or scan triggered via the HTTP API while the lock is held MUST
    return HTTP 409.~~

### HTTP API — DONE (#65, merged via #71)

15. ~~The service MUST expose a Flask HTTP API on port 8080, served by waitress
    (pure-Python WSGI server).~~
16. ~~All endpoints except `/health` MUST require `Authorization: Bearer <token>`
    authentication. The token is read from `API_BEARER_TOKEN` at startup.~~
17. ~~The API MUST provide the following endpoints:~~

| Method | Path | Behaviour |
|---|---|---|
| `GET` | `/health` | Returns 200; no auth required |
| `GET` | `/inbox` | Lists audio files in the inbox tree: name, size, modified |
| `POST` | `/inbox/upload` | Accepts a zip; extracts into `/root/Music/inbox/`; triggers debounced scan |
| `GET` | `/quarantine` | Lists files in quarantine: name, size, modified |
| `GET` | `/quarantine/download/<path>` | Streams file; or zips directory and streams |
| `POST` | `/fetch/trigger` | Enqueues fetch; returns 409 if already running |
| `POST` | `/scan/trigger` | Enqueues scan; returns 409 if already running |

18. ~~Zip handling (both upload extraction and directory download) MUST use
    Python's stdlib `zipfile` — no additional dependencies.~~

Implementation notes: SIGTERM handler added for graceful shutdown. Trigger endpoints
return 202 (accepted) and run in background threads. Path traversal protection on
quarantine download. MUSIC_INBOX and MUSIC_QUARANTINE env vars added for testability
(default to container paths).

### Library push — DONE (#66, merged via #72)

19. ~~After each scan, the orchestrator MUST use `rclone copy` to push
    `/root/Music/staging/` to the remote configured by `LIBRARY_REMOTE`.~~
20. ~~After each scan, the orchestrator MUST use `rclone sync` to push
    `/root/Music/playlists/` to the playlists subdirectory on the same remote.
    `sync` (not `copy`) ensures deleted playlists are removed from the remote.~~
21. ~~rclone MUST be invoked with CLI flags (e.g. `--webdav-url`) rather than a
    config file — credentials and remote type come from environment variables.~~
22. ~~After the push, the orchestrator MUST trigger a library rescan on the
    configured music server via the existing Subsonic API call
    (`music_scan.navidrome.trigger_scan`).~~

Implementation notes: `_push_library()` returns bool — `trigger_scan()` only fires
when a push was actually performed (LIBRARY_REMOTE set). `trigger_scan()` moved from
`scan.run()` to the orchestrator so it fires after the rclone push, not before.
MUSIC_STAGING and MUSIC_PLAYLISTS env vars added for testability. Integration tests
updated for the staging path change.

### CLI script (`scripts/music-files`) — DONE (#67, merged via #74)

23. ~~A standalone Python script MUST be added at `scripts/music-files`.~~
24. ~~It MUST have no dependencies beyond the standard library and `requests`.~~
25. ~~It MUST read `MUSIC_PIPELINE_URL` and `MUSIC_PIPELINE_TOKEN` from the
    environment.~~
26. ~~It MUST support the following subcommands:~~

| Subcommand | Behaviour |
|---|---|
| `list-inbox` | Print a table: name, size, modified |
| `upload <path>` | Zip the given file or directory; POST to `/inbox/upload` |
| `list-quarantine` | Print a table: name, size, modified |
| `download <path>` | GET `/quarantine/download/<path>`; if response is a zip, extract in CWD |
| `trigger-fetch` | POST to `/fetch/trigger` |
| `trigger-scan` | POST to `/scan/trigger` |

Implementation notes: Test suite added at `scripts/tests/test_music_files.py` (loaded
via `importlib` since the script has no `.py` extension). Service Dockerfile dev stage
updated to run script tests alongside service tests.

### Unified Dockerfile — DONE (#68, merged via #73)

27. ~~The `service/Dockerfile` MUST produce a single image containing all three
    packages (`music_fetch`, `music_scan`, `music_service`).~~
28. ~~The image MUST include all system dependencies from both current images:
    ffmpeg, nodejs, libchromaprint-tools, and rclone.~~
29. ~~The image MUST use a two-stage build (`prod` + `dev`) matching the existing
    fetch/scan pattern. The `dev` stage MUST run the service test suite.~~
30. ~~The `CMD` MUST be `music-pipeline`.~~

### Local development tooling — DONE (#68, merged via #73)

31. ~~`compose.yml` MUST be updated to replace the `fetch` and `scan` service
    definitions with a single `service` container built from `service/`.~~
32. ~~`justfile` MUST be updated:~~
    - ~~`test` recipe MUST build and run all three dev containers (fetch, scan, service).~~
    - ~~`fetch` and `scan` recipes MUST invoke their respective commands on the
      `service` container. `sync` MUST run `just fetch && just scan`.~~
    - ~~A `build` recipe MUST build the unified service image.~~

Implementation notes: New `.github/workflows/service.yml` CI workflow with unit tests
and build/push to `ghcr.io/<owner>/music-pipeline:latest`. Health checks cover all
entry points (music-pipeline, music-ingest, music-scan) and tools (rclone, beet, spotdl,
fpcalc). Python bumped to 3.13 across all CI workflows. Scan CI paths trigger updated
to include `fetch/**`. `API_BEARER_TOKEN` defaults to `dev-token` in compose for local dev.

## Configuration

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SPOTIFY_CLIENT_ID` | Yes | — | Spotify Developer credentials |
| `SPOTIFY_CLIENT_SECRET` | Yes | — | Spotify Developer credentials |
| `API_BEARER_TOKEN` | Yes | — | Bearer token for HTTP API auth |
| `NAVIDROME_USER` | Yes (if scan trigger enabled) | — | Subsonic API credentials |
| `NAVIDROME_PASSWORD` | Yes (if scan trigger enabled) | — | Subsonic API credentials |
| `NAVIDROME_URL` | No | — | Base URL for Subsonic rescan trigger |
| `LIBRARY_REMOTE` | No | — | rclone remote for library push; used with CLI flags, not a config file |
| `FETCH_CRON` | No | `0 3 * * *` | APScheduler cron for automatic fetch |
| `PUSHGATEWAY_URL` | No | — | Prometheus Pushgateway URL |
| `SYNC_TRACK_LIMIT` | No | — | Cap on tracks downloaded per run |
| `SYNC_JITTER_SECONDS` | No | — | Random pre-sync sleep |
| `SYNC_TIMEOUT_SECONDS` | No | — | Hard deadline for spotdl sync (passed through to `music_fetch`) |

## Test Requirements — DONE (#65 via #71, #68 via #73)

33. ~~The `service/` package MUST include a test suite runnable via the `dev`
    stage of its Dockerfile.~~
34. ~~Existing fetch and scan test suites MUST continue to pass after the package
    rename.~~

## Open Questions

1. **spotdl config path** — The container may read `/root/.spotdl/config.json`
   (legacy) or `/root/.config/spotdl/config.json`. Must be confirmed before
   finalising the Dockerfile.
