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

**Album playlists sync at the album level (#268).** Spotify decides *which* albums are on the playlist; the local release decides *what is on* each album. An album is on the playlist while at least one of its tracks is, so adding one song on Spotify brings in the whole album: the album counts as `have` only when the library holds every playlist track and at least Spotify's track count of the album's tracks. The `.m3u` lists every local track of every album on the playlist (the items its entries resolve to, and every item canon filed on that Spotify album or, under the same name, on its #214 twin), album by album in playlist order, each in disc/track order, so a release's tracks without a Spotify ID are listed too. A track canon moved to another album (a single's track filed on its standard album) is listed alone. Removing one of an album's tracks on Spotify does nothing; when the last one goes, the tick logs `[UNLINK] <artist> — <album>: no track left on <playlist>`, the album's lines leave the `.m3u` at the next regen, and its former entries are unlinked from `sources` by the scan as before. Normal playlists stay track level. `music-audit-playlists --albums` (read-only) prints, per album playlist, the `.m3u` on disk, the track-level listing (before #268) and the album-level one, with the lines added (`+`) and dropped (`-`); then the album records whose old `partial`/`gap` fallback the next tick settles (`[SETTLE]`), and the `have`/`filled` albums whose library copy is not whole, which are left as they are (`[NOTWHOLE]`). `--clear-tail` refuses an album playlist: its other tracks belong on it, tagged or not.

When SABnzbd finishes an album job, its post-processing script POSTs to `/trigger-album-import` (bearer `ALBUM_IMPORT_TOKEN`, which opens only that route). The `music-album-import` flow then:
1. moves the job into `inbox/usenet/<playlist>/`, so beets tags it `sources=<playlist>` and `via=usenet`;
2. runs the scan under the `pipeline` lock;
3. matches the release as a whole to a MusicBrainz release (beets' album matcher, without importing: `[RELEASE] <job> is <album> (<mbid>): N of its M track(s) have no file`, or `[NOMB]`), imports the files beets couldn't match as they are (`[REST]`), stamps the release's items `album_record=<spotify album id>` and `release=<guid>`, and gives them Spotify track IDs as a best effort: by ISRC, then disc and track number when the title agrees (edition notes dropped, #278), never by title words alone;
4. judges the release (#279): **on album playlists the album is the unit, not its tracks.** The release is the album whatever its tracklist (another edition than Spotify's, missing or extra tracks, a file beets couldn't match): `[DONE] … N file(s) from <release>; K of Spotify's M titles not in this release`. Nothing of it is quarantined and nothing it lacks goes to spotdl. Its items take the album's tags from the record (canon, below), and every other copy of the album (an earlier release, items from before #279) moves to `quarantine/replaced/` (`[RETIRE]`).

A release fails only for a broken download, for being another album (`[WRONG]`: fewer than half of the album's Spotify titles, edition notes dropped, among its items; albums of fewer than 4 tracks aren't judged; its items go to `quarantine/replaced/`), or for lacking tracks of its own MusicBrainz release (`[INCOMPLETE]`). A failed release is blocklisted and the next one tried. An incomplete copy stays in the library until a more complete one has imported, and the next grab must be the same edition (the edition words in its title: deluxe, remaster, anniversary…); when no copy of that edition is left, or after 3 releases, the most complete copy is the album (`[DONE] … lacks N track(s) of its own release`). Searches prefer the Spotify album's edition, and take another edition when Usenet has none (`usenet.rank`). After 3 failed releases with no copy kept the album is `failed`, and the last release's quarantined files are kept for review. Usenet tracks never go through the scan's asis pass. A trigger that never arrives is recovered from SABnzbd history by the next tick, an hour after the grab. A failure that isn't the release's (#261) costs no attempt: an exception during the import moves the job back to SABnzbd's completed dir and leaves the album `grabbed`, so that recovery imports it again (`[RETRY]`; the third crash in a row counts against the release like a failed import), and a completed job whose dir is gone is grabbed again without a blocklist entry (`[LOST]`). MusicBrainz being down is not the release's fault either (#264): beets gives up on a request after its own retries (six, with a short backoff, not configurable), skips the track and exits 0, so the scan watches beets' output for `Max retries exceeded` on `musicbrainz.org`. It then leaves the skipped files in the inbox instead of quarantining them, fails with `music_scan_last_failure_reason{reason="musicbrainz_unavailable"}`, and the album import retries the job the same way (`[RETRY] n/12`, about six hours of ticks) before the release takes the blame. `music-album-reset <artist or album>` undoes an album's last attempt by hand (back to `wanted`, its last release off the blocklist, one attempt fewer, its spotdl queue cleared); `--reimport` starts it over from one release instead (blocklist and attempts cleared, grabbed although the library has its tracks, the old copies retired on import); dry run unless `--apply`. `music-album-audit-releases` lists the imported albums the track-level rules before #279 mixed from several releases or topped up from spotdl (`[MIXED]`), each with its `--reimport` command; `--replaced '<artist or album>'` lists that album's copies in `quarantine/replaced/` (by their album and artist tags) and `--apply` deletes them. An original edition and its remaster on the playlists are two album records that can match the same release (#243): a release another record has in flight is not grabbed again (`[WAIT]`, searched again next tick), and one another record imported or keeps is skipped.

Searches use the names as release names spell them: diacritics folded, `$` as `s`, apostrophes deleted (`WHACK'S MUSEUM` searches and matches as `whacks museum`). A search with no results is retried once with the artist alone, which counts as a second indexer hit. A title made of glyphs is searched by the artist and matched on size alone (`[PICK] … (artist-only)`). An album whose artist and title are both glyphs is not searched; it logs `[NOWORDS] <album id>: … add a search override`. Overrides live in `album-overrides.conf`, one `<spotify album id>  <search words>` per line; the words are both the query and what a release title must contain. A missing album is searched again as soon as its query changes (a new override, or a normalisation fix). `[MISS]` lines list the queries sent, so a miss can be checked in the indexer's own search. An indexer in Prowlarr's failure backoff gets instant empty answers rather than errors, so the tick checks `/api/v1/indexerstatus` before its first search and after every empty result (#263): an indexer in backoff stops the tick with one `[ERR] … indexer in Prowlarr backoff` line, like a failed search, and the album keeps its status. As a guard against an indexer failing silently, 3 albums in a row with no results at all also stop the tick, and the third keeps its status.

**spotdl fallback (#205).** In `on`, what Usenet can't supply goes to spotdl: a `missing` album (from its first miss) or a `failed` one. An imported release never falls back, and sends nothing to spotdl (#268, #279). spotdl fetches the Spotify album's whole tracklist, not just the playlist's tracks: the tick reads it once per album (two Spotify calls, `album_songs` in `.albums.json`, up to 10 albums a tick), and until then the playlist's tracks stand in. The album becomes `fallback` (`[FALLBACK] … N track(s) left to spotdl (<why>)`), its absent tracks' Spotify IDs are kept in `fallback_tracks` under its first playlist, and album mode no longer searches it. A record from before #268 whose fallback came from an imported release (`fallback_from: partial` or `gap`) is settled by the next tick: `imported`, `settled_from` keeps why, the tracks spotdl already fetched stay and the rest leave its queue (`[SETTLED]`). The nightly sync downloads those tracks into the album playlist's inbox at the playlist's place in `playlists.conf` order, from the same `SYNC_TRACK_LIMIT` budget, with the usual `[HAVE]` check, backoff and length guard, and never rewrites the album playlist's `.spotdl`. Once the library holds every track of the album's tracklist the tick marks the album `filled` (`[FILLED]`). `music_albums{status="fallback"|"filled"}` and `music_albums_fallback_tracks` (tracks waiting) are on the `music_albums` push. `music-album-requeue [<album id>…] [--since <ISO time>]` puts albums a miss demoted (`fallback_from: missing`) back to `wanted`, by key or by when they fell back, and takes their tracks spotdl hasn't downloaded yet off its queue; dry run unless `--apply`. It is the recovery for misses that were really an indexer outage.

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

**Duplicates (#210):** `music-audit-dupes` finds duplicates already in the library, in two tiers. **Certain:** a shared Spotify ID, ISRC or MusicBrainz recording ID, or an AcoustID match (shared `acoustid_id`, or fingerprints compared between items that share an artist word) with lengths within 2s and the same title once edition suffixes are dropped. **Uncertain:** title+artist only (the album matcher's normalisation), or an AcoustID match with lengths further apart, or between different titles (likely a wrong download: fix it with `--replace` above, never a merge); listed, never merged by `--apply`, since a demo or live take must stay its own item. Title+artist pairs with disjoint ISRCs are the duplicate hook's `[SPLIT]` decision and are only counted. `--apply` merges each certain group onto the item whose length is closest to Spotify's (then bitrate, playlists, the older item): it takes the others' `spotify_ids`, `isrc`, `sources` and a missing `mb_trackid`, their files move to `quarantine/replaced/` (never deleted), the `.m3u` files are regenerated, then Navidrome rescans. The keeper isn't moved; run `music-canon-albums` afterwards to file it under its album. **`--merge <keep> <drop>...` merges a pair you have confirmed (#276)**, usually from the uncertain tier, which prints the command for each of its pairs (`if one recording: music-audit-dupes --merge 2898 92`, keeper by the same rule; a group of three prints one per pair, so copy only the pairs that are one recording). It goes through the same merge as `--apply` (the keeper takes the drops' IDs, playlists and a missing `mb_trackid`, their files go to `quarantine/replaced/`, the `.m3u` files are regenerated and Navidrome rescans), under the pipeline lock. Without `--apply` it prints the keep/drop block and what moves. It refuses unknown IDs, the keeper among the drops, and a manually imported drop (any `via` but spotdl/usenet) unless `--allow-manual`. Each audit pushes `music_dupes_groups{tier}` (Pushgateway job `music_audit_dupes`). `chroma` fingerprints only autotagged imports, so asis imports had none; the plugin now fingerprints them at import, and `music-audit-lengths --replace` refingerprints the new audio. For the items already in the library, `--fingerprint --apply` stores one for each item without (database only, files untouched), so run it first; `--fingerprint <id>...` recomputes items replaced before this, which still carry the old audio's fingerprint.
```bash
kubectl exec -n <ns> deploy/music-pipeline -- music-audit-dupes --fingerprint           # count items without a fingerprint
kubectl exec -n <ns> deploy/music-pipeline -- music-audit-dupes --fingerprint --apply   # fingerprint and store them
kubectl exec -n <ns> deploy/music-pipeline -- music-audit-dupes --fingerprint 873 615 --apply  # refingerprint these items
kubectl exec -n <ns> deploy/music-pipeline -- music-audit-dupes                         # dry run: both tiers with evidence
kubectl exec -n <ns> deploy/music-pipeline -- music-audit-dupes --apply                 # merge the certain tier
kubectl exec -n <ns> deploy/music-pipeline -- music-audit-dupes --merge 2898 92         # dry run: merge 92 onto 2898
kubectl exec -n <ns> deploy/music-pipeline -- music-audit-dupes --merge 2898 92 --apply # merge it
```

**Failed releases imported before #238:** `music-rollback-releases` lists the `via=usenet` items that match no playlist entry and came from a failed release, and with `--apply` quarantines them like the rollback above. Each item is attributed to an album record in `.albums.json` by album name (edition suffixes dropped) and artist: it came with the successful release when it was added between the record's last grab and its import (a full or partial import), otherwise from a failed one. Items no record claims are listed as `[UNKNOWN]` and kept, and a successful release's extras as `[KEPT]`. A failed release's item that is one of its album's tracks the library still lacks (same title, artist within a `feat.` credit, #240) is listed as `[LINK] … → <spotify id>` and gets that ID instead of being quarantined. With `--apply` a linked item is canonicalised right away (#244); the scan also picks up any item that got a Spotify ID after its import.

```sh
kubectl exec -n <ns> deploy/music-pipeline -- music-rollback-releases          # dry run: [LINK], [FAILED], [KEPT], [UNKNOWN]
kubectl exec -n <ns> deploy/music-pipeline -- music-rollback-releases --apply  # link [LINK], quarantine [FAILED]
```

**Album covers (#204):** Usenet album tracks get Spotify's album cover embedded after import (the convert command drops a FLAC's picture, and singleton imports get no `fetchart`/`embedart`). The cover URL comes from the `.spotdl` files, so no Spotify calls. `music-embed-covers` backfills every `via=usenet` item with no embedded art, then triggers a Navidrome rescan:
```bash
kubectl exec -n <ns> deploy/music-pipeline -- music-embed-covers           # dry run: [ART] line per album
kubectl exec -n <ns> deploy/music-pipeline -- music-embed-covers --apply   # embed, then rescan Navidrome
```

**Canonical albums (#209):** Spotify decides album grouping. An item with a Spotify ID takes `album`, `albumartist`, the release date and its track/disc numbering from the Spotify album its playlist entry is on (an `album` release over a `single` or `compilation`, then one a playlist names today, then the earliest), and beets moves the file. A track on a `single` moves to a standard album by the same album artist holding the same recording (an EP, which Spotify files as `single`, only when the track is already filed under it), found by ISRC with a Spotify track search (cached in `.canon-isrc.json`, one search a second, 20 per scan; a track still waiting for its search is marked `canon_wait` and picked up by the next scan). Its cover follows when the album name or album artist changes, or when the file has none. An item whose move would land on another file's path is left as it is (`[CLASH]`, a duplicate for #210). Spotify sometimes lists one album twice (#214): releases of the same type with the same album artist and name form a twin group whose representative (most playlist entries, then the earliest, then the most tracks) takes every item once its recording is known to be on it, by a playlist entry's ISRC or the same ISRC search (`[TWIN]` lines in the dry run; `[NOTWIN]` when the search shows the recording only on the other release, as for two self-titled albums). A change to beets-only fields (`spotify_album_id`, `mb_album_via`) updates the database without rewriting the file. Navidrome groups by the MusicBrainz album ID first, so the album-level MusicBrainz tags follow too: the release MusicBrainz links to the Spotify album URL, else one holding the album's ISRCs with its track count and title, else one with its barcode (one Spotify album call), else they are cleared (`[MB-NONE]`, with a Harmony link to add the release to MusicBrainz). Track-level IDs and ISRCs stay. Lookups are cached in `.mb-releases.json`; each scan resolves up to 20 albums and leaves the rest `mb_album_via=pending` for the next, and misses are retried weekly. A Usenet release is the album (#266, #279): an item stamped `album_record` takes its record's Spotify album (album, album artist, date, cover, `spotify_album_id`) and the MusicBrainz album tags of the release its files are (`mb_album_via=files`), and keeps the release's own disc/track, whatever its Spotify track IDs say or whether it has any; a `.album` playlist lists those items alone, in disc/track order. Older release items keep #278's rule: on their album record's album when one of their IDs is (`[HOME]`). Album tracks get Spotify IDs only from ISRC or disc/track, never by matching an item's title to an entry. **The album copy supersedes loose copies that predate it** (`music_scan.supersede`): a non-Usenet item with the same Spotify ID, ISRC or MusicBrainz recording ID as one of the release's items, or at the slot of a release item with no Spotify ID on the same Spotify album when the release has Spotify's track count, is merged into the album item (`[SUPERSEDE]`). A loose item canon moved onto the album from a twin release holds the twin's Spotify ID, so the album's tracklist resolves it (#274): the entry with its ISRC, else the entry at its disc/track (vetoed by disjoint ISRCs), and the album item holding that entry's Spotify ID replaces it. The album item takes its playlists, IDs and `mb_trackid` (so Navidrome keeps plays and stars), its file goes to `quarantine/replaced/`, the album item moves to the freed path when it sat beside it as a `.1` copy (`[MOVE]`), and the `.m3u` files are regenerated. A Spotify ID from the title rung never supersedes. A loose item on the album that matches nothing is kept and listed as `[KEEP]` with the reason (no Spotify ID, no tracklist, its track held by no release item or only by title words, no entry at its slot, disjoint ISRCs) for `music-audit-dupes`; a manual import is never replaced (`[MANUAL]`). The duplicate hook imports a Usenet track whose same-recording duplicates are all spotdl items (`[ALBUM]`) so that the supersede can run. Runs at every scan and after each Usenet album import (supersede after every import). `music-canon-albums` backfills, supersedes included, the library from the playlist pages (cached for a day, so the `--apply` after a dry run reads Spotify once):
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

- **Track identity.** `spotify_ids` (comma-separated Spotify track IDs: one recording has different IDs on the single, the album and compilations) and `isrc` (`;`-separated, MusicBrainz's plus Spotify's) decide whether a playlist entry is a library item: Spotify ID, then ISRC, then MusicBrainz recording ID, and title+artist only as a logged last resort (`[WORDS]`, whole titles: a remaster is another track, #252). The title+artist resort only reaches items with no Spotify ID (#257): an item that has one is that track, so a same-title entry with another ID never takes it (the slots, `.m3u` regen, and a removal's title fallback alike). Two recordings are one track only when one of those identifiers agrees (#255): the duplicate hook merges an incoming track only on a shared Spotify ID, ISRC or recording ID (`[SAME]`), and a same-title track imports as its own item, `[SPLIT]` when the ISRCs differ and `[APART]` when one side has none. Real duplicates that arrive without IDs are found by `music-audit-dupes` (fingerprints).
- **`sources` is comma-separated.** A track imported by multiple playlists carries all playlist names (e.g. `sources=playlist-a,playlist-b`). It is one of the two ways a track reaches a playlist's `.m3u`; see playlist slots below.
- **Playlist slots (#228).** Each playlist entry is a slot that every scan fills by resolving it against the library, never by recording anything: first to an item tagged with the playlist in `sources` (Spotify ID, ISRC, then title+artist, `[WORDS]`), else to any library item by Spotify ID, then ISRC (`[SLOT] <playlist>: N entr(ies) filled from the library by ID/ISRC`, per track at debug). So a track that arrived by any route (a Usenet album, another playlist's download, a manual import or replacement) is listed by every playlist that names it at the next scan, with no download and no tag write. The album hooks' `missing` and `have` read the same resolution. The scan pushes `music_playlist_slots_empty{playlist}` (entries with no item; for a spotdl playlist the `.spotdl` holds only downloaded and linked entries, so its count stays near 0 until homelab#1914's store). `music-audit-playlists` is the read-only proof: per playlist, the `.m3u` on disk, today's path, the slots, the added entries and any the file on disk would lose (exit 1 if any). This is the first slice of homelab#1914, which replaces `.spotdl` and `sources` with one playlist store.
- **Pending removals survive a crash (#259).** `.pending-removals.json` stays on disk until the scan has applied it, and then only the applied entries are dropped (ones the album tick queued meanwhile stay). An entry that raises is logged as `[SKIP]` and skipped, not fatal. Removals lost before this fix leave tagged items no entry resolves to (the audit's tail lines): `music-audit-playlists --clear-tail <playlist>` lists the ones with a Spotify ID as `[UNTAG]` and those without (local-only extras, #245) or that an entry agrees with by ID or ISRC as `[KEEP]`; `--apply` clears the playlist's tag on the `[UNTAG]` items, and the next scan rewrites the `.m3u`.
- **`beet update` does not prune deleted files.** Use `beet remove <query>` with a specific query. Never run `beet remove` without a query.
- **Cookies expire.** Re-export from browser when downloads fail at quality.
- **Spotify rate limits.** Always use your own app credentials — the spotdl defaults are shared and hit limits quickly. For large playlists, set `SYNC_TRACK_LIMIT` to cap new downloads per session (e.g. `50`); the pipeline picks up where it left off each run. The budget is spent on one want list across playlists in `playlists.conf` order (new entries the library lacks, minus backoff); once spent, the remaining playlists are still fetched, `[HAVE]`-linked and diffed for removals, and only their downloads wait (`[DEFER]`), so no playlist goes unchecked (#228).
- **Long Spotify rate limits fail the run.** A 429 whose `Retry-After` exceeds 120s is not slept through (it once said 17.5h): the run fails with `Spotify rate-limited: Retry-After <n>s, until <UTC>`, the end goes to `.spotify-rate-limit.json` on pipeline-state and to the `music_spotify_rate_limited_until_seconds` gauge (Pushgateway job `music_spotify`), and every Spotify reader fails without calling Spotify until then. Shorter ones are waited out.
- **MusicBrainz threshold.** `strong_rec_thresh: 0.10` in `config/beets/config.yaml`. Tracks that don't match confidently land in quarantine. A second `--asis` pass then imports any quarantined file that already has sufficient embedded tags (title, artist, album, tracknumber); the rest stay in quarantine for manual review. Raise the threshold if too many valid tracks are being quarantined.
- **`.spotdl` files are the sync state.** Never delete them manually. They are backed by the PVC and re-created by `music-ingest` from `config/playlists.conf` on first run or after PVC loss. When `SYNC_TRACK_LIMIT` is active, the snapshot intentionally contains only downloaded tracks — deferred tracks are absent so they re-appear as new on the next run.
