# music-pipeline

Dockerized music pipeline: Spotify playlists → spotdl downloads → beets import/tag → music library.

```
Spotify playlists → spotdl → beets → ~/Music/library
```

A single long-running service container orchestrates everything via Prefect. Two flows run on configurable schedules:

| Flow | Default schedule | Does |
|---|---|---|
| `music-fetch` | Daily 03:00 UTC (`FETCH_CRON`) | Spotify/YouTube sync — downloads new tracks, queues removals |
| `music-scan` | On demand (file watcher or HTTP API) | Import inbox → beets, refresh metadata, regenerate .m3u |

---

## Pipeline internals

A full sync cycle (`music-ingest` followed by `music-scan`) runs 9 discrete steps. Steps 1–2 are the **fetch phase**; steps 3–9 are the **scan phase**.

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
│  [4] Length guard (LENGTH_GUARD)                                       │
│        Checks each spotdl download against the Spotify duration (±10%) │
│        and for mid-track silence; moves wrong audio to                 │
│        quarantine/rejected/ (asis pass skips it, no re-download).      │
│        │                                                               │
│        ▼                                                               │
│  [5] Beets import                                                      │
│        Matches inbox audio to MusicBrainz/AcoustID; moves matched      │
│        files to library/. Low-confidence matches go to quarantine/.    │
│        │                                                               │
│        ▼                                                               │
│  [6] Quarantine + asis pass                                            │
│        Moves unmatched inbox leftovers to quarantine/. Then attempts   │
│        a second beet import --asis for quarantine files that already   │
│        have sufficient embedded tags (title, artist, album, track#).   │
│        │                                                               │
│        ▼                                                               │
│  [7] Library metadata refresh                                          │
│        Runs beet update to refresh metadata on existing library items. │
│        │                                                               │
│        ▼                                                               │
│  [8] Snapshot reconciliation                                           │
│        Diffs each .spotdl file against the beets library + quarantine. │
│        Drops URLs absent from both so spotdl re-downloads them next    │
│        fetch rather than silently skipping forever. Skips album and    │
│        nosync playlists: spotdl never re-adds their entries.           │
│        │                                                               │
│        ▼                                                               │
│  [9] Playlist generation + Navidrome trigger                           │
│        Regenerates .m3u files (in Spotify playlist order). Calls the   │
│        Navidrome Subsonic API to trigger a library rescan.             │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
```

Steps 3, 8, and 9 are no-ops when `music-scan` runs on its 5-minute schedule with no preceding fetch (no removals to apply, Navidrome already up to date). The scan phase is idempotent and safe to run at any time.

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

**Each time cookies expire**, sign in to [music.youtube.com](https://music.youtube.com) in Firefox, then run:

```bash
just cookies
```

This extracts cookies directly from Firefox and saves them to `cookies.txt` (already in `.gitignore`).

Cookies expire every few weeks. Re-export when downloads start failing: the ingest run logs a `YouTube cookies ... look expired` warning, sets the `music_ingest_cookies_expired` gauge to 1, and records the per-track reason (e.g. `HTTP Error 403: Forbidden`) in `/root/Music/inbox/.spotdl-failures.json`.

Each ingest run also reads the expiry stated in `cookies.txt`: it logs `YouTube auth cookies expire <date>` and pushes the soonest expiry among the Google login cookies (`SID`, `HSID`, `SSID`, `APISID`, `SAPISID`, `__Secure-{1,3}PSID`, `__Secure-{1,3}PAPISID`, `LOGIN_INFO`) as `music_ingest_cookies_expiry_timestamp_seconds`. The homelab alert `MusicCookiesExpiringSoon` fires 7 days before that date. A file with no login cookies (exported signed out) logs a warning and pushes no expiry. The stated expiry is about a year out, and Google usually invalidates a session server-side before then, so `music_ingest_cookies_expired` stays the main signal.

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

The optional `album` flag is for playlists that hold only whole albums. spotdl never syncs them (a `.album` sentinel). Instead, the `music-albums` flow downloads each album whole from Usenet through Prowlarr and SABnzbd. Every `ALBUM_POLL_SECONDS` it checks the playlist's Spotify `snapshot_id` and re-reads the tracks only when it has changed, so a playlist edit is picked up within one poll. Then it tops the SABnzbd queue up to `ALBUM_MAX_IN_FLIGHT` within a rolling-24h indexer budget. Its state (album status, release blocklist, indexer use) is `.albums.json` next to the `.spotdl` files. Set `ALBUM_MODE=dry-run` first: it searches and logs `[PICK]` lines but never grabs.

When SABnzbd finishes an album job, its post-processing script POSTs to `/trigger-album-import` (bearer `ALBUM_IMPORT_TOKEN`, which opens only that route). The `music-album-import` flow then:
1. moves the job into `inbox/usenet/<playlist>/`, so beets tags it `sources=<playlist>` and `via=usenet`;
2. runs the scan under the `pipeline` lock;
3. counts the album imported only if every playlist track is now in the library.

Otherwise (a failed download, or tracks beets quarantined) it blocklists the release, deletes its quarantined tracks, and tries the next release; tracks already in the library are skipped as duplicates, so it only fills the gaps. After 3 failed releases the album is `failed`, and the last release's quarantined tracks are kept for review. A release that imports all but a few tracks (at most `ALBUM_PARTIAL_MAX_MISSING`, or `ALBUM_PARTIAL_MAX_PERCENT` of the album's playlist tracks if that is more) is not blocklisted: it logs `[PART]` and its missing tracks go to spotdl, as below. Usenet tracks never go through the asis pass. A trigger that never arrives is recovered from SABnzbd history by the next tick, an hour after the grab.

Searches use the names as release names spell them: diacritics folded, `$` as `s`, apostrophes deleted (`WHACK'S MUSEUM` searches and matches as `whacks museum`). A search with no results is retried once with the artist alone, which counts as a second indexer hit. A title made of glyphs is searched by the artist and matched on size alone (`[PICK] … (artist-only)`). An album whose artist and title are both glyphs is not searched; it logs `[NOWORDS] <album id>: … add a search override`. Overrides live in `album-overrides.conf`, one `<spotify album id>  <search words>` per line; the words are both the query and what a release title must contain. A missing album is searched again as soon as its query changes (a new override, or a normalisation fix). `[MISS]` lines list the queries sent, so a miss can be checked in the indexer's own search.

**spotdl fallback (#205).** In `on`, what Usenet can't supply goes to spotdl: a `missing` album (from its first miss), a `failed` one, and a partial import's missing tracks. The album becomes `fallback` (`[FALLBACK] … N track(s) left to spotdl (<why>)`), its absent tracks' Spotify IDs are kept in `fallback_tracks`, and album mode no longer searches it. The nightly sync downloads those tracks into the album playlist's inbox at the playlist's place in `playlists.conf` order, from the same `SYNC_TRACK_LIMIT` budget, with the usual `[HAVE]` check, backoff and length guard, and never rewrites the album playlist's `.spotdl`. Once the library holds every track the tick marks the album `filled` (`[FILLED]`). `music_albums{status="fallback"|"filled"}` and `music_albums_fallback_tracks` (tracks waiting) are on the `music_albums` push.

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
  album-overrides.conf   ← optional album-mode search overrides (k8s: ConfigMap)
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
| `music-pipeline-playlists` | `album-overrides.conf` (optional) | `/root/.config/music-pipeline/album-overrides.conf` | homelab repo only |

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
| `LENGTH_GUARD` | Plain value | `dry-run` | `on` rejects wrong-length or silence-padded spotdl downloads before import, `dry-run` only logs `[WOULD-REJECT]`, `off` skips the check |
| `ALBUM_MODE` | Plain value | `off` | `off`, `dry-run` (search and log picks, never grab) or `on` |
| `ALBUM_DRY_RUN_LIMIT` | Plain value | `25` | Albums searched in total while `dry-run`; then it only polls. `on` searches every album again, so dry-run is a sample |
| `ALBUM_POLL_SECONDS` | Plain value | `1800` | Interval of the `music-albums` flow (Spotify snapshot poll + queue top-up) |
| `ALBUM_MAX_IN_FLIGHT` | Plain value | `3` | Albums queued in SABnzbd at once; size it to the `music-data` disk |
| `ALBUM_GRABS_PER_DAY` / `ALBUM_HITS_PER_DAY` | Plain value | `18` / `90` | Rolling-24h indexer budget (NZB grabs / API searches) |
| `ALBUM_PARTIAL_MAX_MISSING` / `ALBUM_PARTIAL_MAX_PERCENT` | Plain value | `2` / `20` | A release missing at most this many tracks (or this % of the album's playlist tracks, if more) isn't re-grabbed; spotdl fetches the rest |
| `PROWLARR_URL` / `PROWLARR_API_KEY` | Plain value / Secret | `http://prowlarr.music.svc.cluster.local:9696` / — | Album mode only |
| `SABNZBD_URL` / `SABNZBD_API_KEY` | Plain value / Secret | `http://sabnzbd.music.svc.cluster.local:8080` / — | Album mode only |
| `BEET_SKIP_LIMIT` | Plain value | `""` | Terminate beet import after this many skipped tracks |
| `PREFECT_LOGGING_EXTRA_LOGGERS` | Image `ENV` | `music_fetch,music_scan` | Attaches Prefect's log handlers to the pipeline's own loggers so per-track `[OK]`/`[MISS]`/`[FAIL]` lines reach the flow run logs. Baked into the image; the deployment does not need to set it. |
| `PREFECT_LOGGING_TO_API_WHEN_MISSING_FLOW` | Image `ENV` | `ignore` | Silences Prefect's warning when those loggers emit from a thread without a flow-run context (the `beet` stderr relay). Such lines still reach the pod logs, just not the Prefect UI. |

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

**Backfill track identity (once, #176):** items imported before Spotify IDs and ISRCs were stored get them from the playlists' Spotify pages (about one call per 100 tracks). It also lists **wrong versions**: playlist entries an older artist+title duplicate check merged into a different recording (live, remaster, radio edit). A candidate whose ISRC MusicBrainz lists on the item's recording (`mb_trackid`) is the same recording under another ISRC, not a wrong version (`[SAME]`); only those candidates are looked up, at one MusicBrainz call a second.
```bash
kubectl exec -n <ns> deploy/music-pipeline -- music-backfill-ids                            # dry run: report only
kubectl exec -n <ns> deploy/music-pipeline -- music-backfill-ids --apply                    # write IDs and ISRCs
kubectl exec -n <ns> deploy/music-pipeline -- music-backfill-ids --apply --redownload 10    # also fetch 10 right versions
```

**Audit and replace wrong audio (#165):** the length guard only checks new downloads. `music-audit-lengths` reads every playlist (`nosync` and `album` ones too) from its Spotify pages (about one call per 100 tracks) and lists `[SUSPECT]` items whose length is off from the Spotify entry's by more than 10% and 5s, with the YouTube video spotdl downloaded them from. `--replace` re-downloads one item, pinned to a YouTube video, and swaps the file **in place**: the item keeps its id, tags, playlists and IDs, and the old file goes to `quarantine/replaced/`. Going through the inbox doesn't work, since the duplicate hook would merge the new file into the old item by Spotify ID and drop it.
```bash
kubectl exec -n <ns> deploy/music-pipeline -- music-audit-lengths                         # report suspects
kubectl exec -n <ns> deploy/music-pipeline -- music-audit-lengths --silence --quarantine  # also mid-track silence; guard over quarantine
kubectl exec -n <ns> deploy/music-pipeline -- music-audit-lengths --replace <id> --youtube <url>          # dry run
kubectl exec -n <ns> deploy/music-pipeline -- music-audit-lengths --replace <id> --youtube <url> --apply  # replace
```
Without `--youtube` spotdl searches again and usually picks the same video; the replace then stops. A download that fails the guard is refused unless `--force`.
`--redownload` downloads into the playlist's inbox (works for `nosync` and `album` playlists too) and takes the playlist off the wrong item; the next scan imports the download as its own recording.

**Duplicates (#210):** `music-audit-dupes` finds duplicates already in the library, in two tiers. **Certain:** a shared Spotify ID, ISRC or MusicBrainz recording ID, or an AcoustID match (shared `acoustid_id`, or fingerprints compared between items that share an artist word) with lengths within 2s. **Uncertain:** title+artist only (the album matcher's normalisation), or an AcoustID match with lengths further apart; listed, never merged, since a demo or live take must stay its own item. Title+artist pairs with disjoint ISRCs are the duplicate hook's `[SPLIT]` decision and are only counted. `--apply` merges each certain group onto the item whose length is closest to Spotify's (then bitrate, playlists, the older item): it takes the others' `spotify_ids`, `isrc`, `sources` and a missing `mb_trackid`, their files move to `quarantine/replaced/` (never deleted), the `.m3u` files are regenerated, then Navidrome rescans. The keeper isn't moved; run `music-canon-albums` afterwards to file it under its album. Each audit pushes `music_dupes_groups{tier}` (Pushgateway job `music_audit_dupes`). Items imported asis or replaced in place have no fingerprint; `--fingerprint --apply` stores one for each (database only, files untouched), so run it first.
```bash
kubectl exec -n <ns> deploy/music-pipeline -- music-audit-dupes --fingerprint           # count items without a fingerprint
kubectl exec -n <ns> deploy/music-pipeline -- music-audit-dupes --fingerprint --apply   # fingerprint and store them
kubectl exec -n <ns> deploy/music-pipeline -- music-audit-dupes                         # dry run: both tiers with evidence
kubectl exec -n <ns> deploy/music-pipeline -- music-audit-dupes --apply                 # merge the certain tier
```

**Album covers (#204):** Usenet album tracks get Spotify's album cover embedded after import (the convert command drops a FLAC's picture, and singleton imports get no `fetchart`/`embedart`). The cover URL comes from the `.spotdl` files, so no Spotify calls. `music-embed-covers` backfills every `via=usenet` item with no embedded art, then triggers a Navidrome rescan:
```bash
kubectl exec -n <ns> deploy/music-pipeline -- music-embed-covers           # dry run: [ART] line per album
kubectl exec -n <ns> deploy/music-pipeline -- music-embed-covers --apply   # embed, then rescan Navidrome
```

**Canonical albums (#209):** Spotify decides album grouping. An item with a Spotify ID takes `album`, `albumartist`, the release date and its track/disc numbering from the Spotify album its playlist entry is on (an `album` release over a `single` or `compilation`, then one a playlist names today, then the earliest), and beets moves the file. A track on a `single` moves to a standard album by the same album artist holding the same recording (an EP, which Spotify files as `single`, only when the track is already filed under it), found by ISRC with a Spotify track search (cached in `.canon-isrc.json`, one search a second, 20 per scan; a track still waiting for its search is marked `canon_wait` and picked up by the next scan). Its cover follows when the album name or album artist changes, or when the file has none. An item whose move would land on another file's path is left as it is (`[CLASH]`, a duplicate for #210). A change to beets-only fields (`spotify_album_id`, `mb_album_via`) updates the database without rewriting the file. Navidrome groups by the MusicBrainz album ID first, so the album-level MusicBrainz tags follow too: the release MusicBrainz links to the Spotify album URL, else one holding the album's ISRCs with its track count and title, else one with its barcode (one Spotify album call), else they are cleared (`[MB-NONE]`, with a Harmony link to add the release to MusicBrainz). Track-level IDs and ISRCs stay. Lookups are cached in `.mb-releases.json`; each scan resolves up to 20 albums and leaves the rest `mb_album_via=pending` for the next, and misses are retried weekly. Runs at every scan and after each Usenet album import. `music-canon-albums` backfills the library from the playlist pages (cached for a day, so the `--apply` after a dry run reads Spotify once):
```bash
kubectl exec -n <ns> deploy/music-pipeline -- music-canon-albums           # dry run: [ALBUM]/[RETAG] lines and totals
kubectl exec -n <ns> deploy/music-pipeline -- music-canon-albums --apply   # retag, move, regenerate the .m3u files (#218), then rescan Navidrome
```
The dry run still looks albums up on MusicBrainz (about 1–6 s each) and caches the results. `--mb-budget N` caps the lookups per run. The dry run also runs the single → album ISRC searches (one a second, cached for the `--apply`); `--search-budget N` caps them.

**Recover after PVC loss:**
1. Restore `beets-data` PVC from backup (restores `library.db`).
2. Trigger a fetch — it re-provisions all `.spotdl` files from `playlists.conf` and re-downloads.

---

## Notes and gotchas

- **Track identity.** `spotify_ids` (comma-separated Spotify track IDs: one recording has different IDs on the single, the album and compilations) and `isrc` (`;`-separated, MusicBrainz's plus Spotify's) decide whether a playlist entry is a library item: Spotify ID, then ISRC, then MusicBrainz recording ID, and title+artist only as a logged last resort (`[WORDS]`). A same-title track with different ISRCs imports as its own recording (`[SPLIT]`).
- **`sources` is comma-separated.** A track imported by multiple playlists carries all playlist names (e.g. `sources=playlist-a,playlist-b`). It will appear in all relevant `.m3u` files once each playlist has been synced at least once.
- **`beet update` does not prune deleted files.** Use `beet remove <query>` with a specific query. Never run `beet remove` without a query.
- **Cookies expire.** Re-export from browser when downloads fail at quality.
- **Spotify rate limits.** Always use your own app credentials — the spotdl defaults are shared and hit limits quickly. For large playlists, set `SYNC_TRACK_LIMIT` to cap new downloads per session (e.g. `50`); the pipeline picks up where it left off each run.
- **Long Spotify rate limits fail the run.** A 429 whose `Retry-After` exceeds 120s is not slept through (it once said 17.5h): the run fails with `Spotify rate-limited: Retry-After <n>s, until <UTC>`, the end goes to `.spotify-rate-limit.json` on pipeline-state and to the `music_spotify_rate_limited_until_seconds` gauge (Pushgateway job `music_spotify`), and every Spotify reader fails without calling Spotify until then. Shorter ones are waited out.
- **MusicBrainz threshold.** `strong_rec_thresh: 0.10` in `config/beets/config.yaml`. Tracks that don't match confidently land in quarantine. A second `--asis` pass then imports any quarantined file that already has sufficient embedded tags (title, artist, album, tracknumber); the rest stay in quarantine for manual review. Raise the threshold if too many valid tracks are being quarantined.
- **`.spotdl` files are the sync state.** Never delete them manually. They are backed by the PVC and re-created by `music-ingest` from `config/playlists.conf` on first run or after PVC loss. When `SYNC_TRACK_LIMIT` is active, the snapshot intentionally contains only downloaded tracks — deferred tracks are absent so they re-appear as new on the next run.
