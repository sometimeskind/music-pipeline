# music-pipeline

A Dockerized music pipeline: Spotify playlists → spotdl downloads → beets import/tagging → music library. One long-running service container orchestrates everything via Prefect. Scheduling is internal (`FETCH_CRON`), not external CronJobs.

```
Spotify playlists → spotdl sync → /root/Music/inbox/spotdl/<name>/ → beets import → /root/Music/library/$albumartist/$album/$track - $title.m4a
```

One image is pushed to GHCR (`ghcr.io/sometimeskind/music-pipeline`). It holds three packages: `music_fetch` (`fetch/`: spotdl sync, Spotify/YouTube calls), `music_scan` (`scan/`: beets import, AcoustID fingerprinting, `.m3u` generation) and `music_service` (`service/`: Prefect runner, Flask API, watchdog file watcher). Entry points are `music-pipeline` (the long-running service), `music-ingest` (one-shot fetch) and `music-scan` (one-shot scan).

README.md covers setup, every `just` recipe, volumes, environment variables and the Kubernetes deployment. Don't duplicate it here.

## Commands

Everything goes through `just` (`just --list`). The ones that matter most:

```bash
just hooks               # once after cloning: installs the pre-push hook that runs `just test`
just test                # builds the dev container and runs all unit tests
just fetch && just scan  # or `just sync`
```

**Never run Python tooling (pytest, beet, spotdl) directly on the host.** It all runs inside the container.

## Testing

- All unit tests (fetch, scan, service, scripts) run together in one dev container built from `service/Dockerfile`. That is what `just test` does.
- Integration tests in `tests/` at the repo root run against the real service image: `just test-service`.
- Each Dockerfile has a `prod` stage (pushed to GHCR, runtime deps only) and a `dev` stage (adds test deps, built locally, never pushed). Only `service/Dockerfile` produces the published image; its build context is the repo root. `fetch/` and `scan/` Dockerfiles are dev-only.

## Design Decisions to Preserve

- **`sources=<playlist>` beets flex attribute**: comma-separated list of playlists a track belongs to, written at import and used to generate `.m3u` files. A track shared across playlists only appears in every relevant `.m3u` once `fetch` has synced each of them at least once; `scan` alone cannot infer that.
- **Soft deletes via snapshot diff**: `music-ingest` snapshots the `.spotdl` file before and after sync and diffs the URL sets to find removed tracks, then clears their `sources=` tag. Files stay in the library.
- **Strict MusicBrainz threshold** (`strong_rec_thresh: 0.10` in `config/beets/config.yaml`): low-confidence matches go to `/root/Music/quarantine/` for manual review, after a second `--asis` pass for files with sufficient embedded tags.
- **`config/playlists.conf` is the declarative source of truth**: `music-ingest` reconciles disk state (including `.nosync` sentinels) to match it on every run. Never manage playlists by touching the inbox directly.
- **Prefect direct mode**: with `PREFECT_API_URL` unset, flows run in-process and concurrency is a threading lock instead of Prefect's global limits.
- **Credentials**: Spotify credentials are injected at runtime via `op run --env-file=.env.tpl` (vault references, never secrets). `cookies.txt` (YouTube Premium, needed for 256 kbps M4A) is bind-mounted, gitignored and must be re-exported from the browser when it expires.
