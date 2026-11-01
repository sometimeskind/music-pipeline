# MCP API (`/mcp-api/*`)

The contract between the pipeline service (`service/`) and the Music MCP adapter (`mcp/`), homelab#2412. The adapter is the only client. It never runs in the pipeline pod, and this API never returns a credential: every JSON response passes `music_fetch.redact` on its way out, which drops `download_url` fields (a Prowlarr download link carries Prowlarr's API key) and masks credential query parameters (`apikey=REDACTED`) and URL user info in every string, records, events and job output alike (#343).

Every timestamp in a request or a response is ISO 8601; a response's are always UTC (`2026-10-27T14:03:11Z`).

## Authentication and headers

| Header | Required | Meaning |
|---|---|---|
| `Authorization: Bearer <MCP_API_TOKEN>` | every route | `MCP_API_TOKEN` opens `/mcp-api/*` and nothing else; `API_BEARER_TOKEN` and `ALBUM_IMPORT_TOKEN` don't open `/mcp-api/*`. With `MCP_API_TOKEN` unset, every `/mcp-api/*` request is 401. |
| `X-MCP-Caller` | every route | Who asked: the OIDC `sub`, or `pilot`. Logged with every job and stored on its record. 1–200 characters. |
| `X-MCP-Tier` | write routes | `read` or `write`. The pipeline trusts the adapter's claim. A write route returns 403 when it is missing or `read`. Read routes accept either value or none. |

| Status | Body |
|---|---|
| 401 | `{"error": "unauthorized"}` |
| 400 | `{"error": "X-MCP-Caller required"}` / `{"error": "X-MCP-Tier must be read or write"}` |
| 403 | `{"error": "write tier required"}` |

## Common shapes

**Errors.** A bad request is `400 {"error": "<what is wrong>"}`.

**Busy library.** Structured reads open beets' SQLite read-only with a 10 s busy timeout. When it stays locked: `503 {"status": "busy", "retry": true, "error": "beets library is locked"}`.

**Ambiguous album.** An album query uses the CLI's `<query>` semantics (`music_fetch.albums.find_albums`): the album-store key (Spotify album ID); else an exact case-insensitive `artist — album`; else the records whose `artist — album` contains it. A query that matches more than one album does nothing and returns:

```json
{"status": "ambiguous", "query": "abbey",
 "candidates": [{"key": "0ETFjACtuP2ADo6LFhL6HN", "artist": "The Beatles", "name": "Abbey Road", "status": "imported"},
                {"key": "...", "artist": "...", "name": "Abbey Road (Remastered)", "status": "wanted"}]}
```

One that matches none: `404 {"status": "not_found", "query": "..."}`.

**Paging.** A list or an output is capped at **200 lines (or entries) and 32 KB** per response. `offset` (default 0) starts the page and `next_offset` continues it; `next_offset` is `null` when nothing more is (or, for a finished job, will be) there. A single output line over 32 KB is cut, ending in `…`.

## Structured reads (any tier)

### `GET /mcp-api/library_status` (`library_status`)

```json
{"status": "ok",
 "items": 10234,
 "albums": {"total": 412, "by_status": {"imported": 300, "wanted": 80, "fallback": 20, "failed": 12}},
 "album_mode": "on",
 "quarantine": {"unmatched": 14, "rejected": 3, "replaced": 120},
 "playlists": ["chill", "running"],
 "jobs": {"running": {"job_id": "…", "tool": "canon_albums", "status": "running"}, "queued": 1, "depth": 5,
          "long": {"job_id": "…", "tool": "recheck_recordings", "status": "running", "lane": "long"}}}
```

### `GET /mcp-api/albums?query=Q` (`find_albums`)

Every album record the query matches (no ambiguity check), each with its library item count:

```json
{"status": "ok", "query": "abbey",
 "albums": [{"key": "0ETF…", "artist": "The Beatles", "name": "Abbey Road", "status": "imported", "playlists": ["albums"], "items": 17}]}
```

### `GET /mcp-api/album?query=Q` (`get_album`)

One album: ambiguous or not found as above, otherwise:

```json
{"status": "ok", "query": "abbey road",
 "album": {"key": "0ETF…", "spotify_id": "0ETF…", "record": {"artist": "…", "name": "…", "status": "imported", "attempts": 1, "blocklist": [], "…": "…"}},
 "beets": {"album_ids": [812], "item_ids": [10001, 10002]},
 "items": [{"id": 10001, "title": "Come Together", "artist": "The Beatles", "track": 1, "path": "The Beatles/Abbey Road/01 - Come Together.m4a", "via": "usenet"}]}
```

`key` is the album-store key, which is the Spotify album ID; `record` is the album record as the album store holds it (the `music` database, homelab#2410), with `release_group` and `unresolved`; the items are those whose `spotify_album_id` is the key.

### `GET /mcp-api/items/<id>` (`get_item`)

```json
{"status": "ok",
 "item": {"id": 10001, "title": "…", "artist": "…", "albumartist": "…", "album": "…", "track": 1, "length": 259.9,
          "bitrate": 256000, "format": "AAC", "path": "The Beatles/Abbey Road/01 - Come Together.m4a",
          "added": "2026-10-01T03:12:40Z",
          "fields": {"spotify_ids": "…", "isrc": "…", "sources": "albums", "via": "usenet", "…": "…"}},
 "listed_on": ["albums"]}
```

`path` is relative to the library root. `fields` holds every other fixed and flexible beets attribute. `listed_on` is the playlists whose store lists the item's recording (`scan.store_listings`), and `sources` is provenance only. An unknown ID returns `404 {"status": "not_found", "id": 1}`.

### `GET /mcp-api/quarantine?kind=K[&query=Q][&offset=N]` (`list_quarantine`)

`kind` is `unmatched` (the files the scan quarantined: everything outside `rejected/` and `replaced/`), `rejected` (the length guard's) or `replaced` (superseded and merged copies). `query` filters by a case-insensitive substring of the path.

```json
{"status": "ok", "kind": "rejected", "query": null, "total": 3, "offset": 0, "next_offset": null,
 "files": [{"path": "rejected/chill/Artist - Title.m4a", "size": 4012345, "modified": "2026-10-20T03:40:00Z"}]}
```

### `GET /mcp-api/playlists/<name>[?offset=N]` (`get_playlist`)

The playlist store in Spotify order, each entry resolved against the whole library by Spotify ID or ISRC:

```json
{"status": "ok", "name": "chill", "updated": "2026-10-27T03:10:00Z", "source": "spotify",
 "counts": {"entries": 120, "resolved": 117, "missing": 3},
 "offset": 0, "next_offset": null,
 "entries": [{"position": 1, "kind": "song", "title": "…", "artist": "…", "album": "…", "song_id": "…", "isrc": "…", "item_id": 10001},
             {"position": 2, "kind": "song", "title": "…", "artist": "…", "album": "…", "song_id": "…", "isrc": null, "item_id": null},
             {"position": 121, "kind": "local", "title": "…", "artist": "…", "item_id": 9001}]}
```

An unknown playlist returns `404 {"status": "not_found", "name": "…"}`.

### `GET /mcp-api/jobs/<job_id>[?offset=N]` (`get_job`)

A job record (below) with one page of its output. An unknown ID returns `404 {"status": "unknown", "error": "unknown (pruned or lost to a restart)"}`.

`get_guide` is served by the adapter from `mcp/guide/`, not by this API.

## History (any tier, homelab#2413)

History comes from the event store (`music_fetch.events`, the `music` database's `events` table). Each tag the pipeline logs for an album or item (`[GRAB]`, `[DONE]`, `[RETIRE]`, `[CLASH]`…) is also recorded as an event: `kind`, `message` (the log line), `source` (the component that saw it), the album's `spotify_album_id` and `release_group` (once an import identified its release), the `item_id`, the MCP `caller` and `job_id` that caused it, and `data`. A path clash is recorded once a day while it stands. Events are kept 90 days, and each album's latest 20 longer. Event writes are best-effort: a lost one stamps `music_events_write_failure_timestamp_seconds`, which homelab alerts on.

When the event store can't be read (unset, or the database is down): `503 {"status": "unavailable", "error": "…"}`.

### `GET /mcp-api/album_history?query=Q[&since=T][&offset=N]` (`album_history`)

One album's events, newest first. The query resolves as for `get_album` (ambiguous, not found). Events match by the album's Spotify ID, by its release group once known, or for a local files' album by its store key. `since` (ISO 8601) is optional: without it, all kept history.

```json
{"status": "ok", "query": "abbey road",
 "album": {"key": "0ETF…", "artist": "The Beatles", "name": "Abbey Road", "status": "imported", "release_group": "9162580e-…"},
 "since": null, "total": 3, "offset": 0, "next_offset": null,
 "events": [{"at": "2026-10-28T03:12:40Z", "kind": "import_done", "source": "albums", "spotify_album_id": "0ETF…",
             "release_group": "9162580e-…", "item_id": null, "caller": null, "job_id": null,
             "message": "[DONE] The Beatles — Abbey Road: 17 file(s) from …", "data": {"album": "…", "key": "0ETF…", "origin": "album"}}]}
```

### `GET /mcp-api/digest_summary[?since=T]` (`digest_summary`)

What happened since `since` (default: 24 hours ago), and the library issues standing now, for the nightly digest. Every list is `{"total": n, "listed": [first 25]}`.

```json
{"status": "ok", "since": "2026-10-28T05:00:00Z", "until": "2026-10-29T05:00:00Z",
 "history": {"counts": {"grab": 2, "import_done": 1, "retire": 14},
             "events": {"grab": [{"at": "…", "message": "[GRAB] …"}], "failure": [{"at": "…", "message": "[FAIL] …", "gave_up": true}]}},
 "issues": {"mixed": {"total": 1, "listed": [{"key": "…", "album": "Artist — Album", "origin": "album"}]},
            "stuck": {"failed": {…}, "missing": {…}, "grabbed": {…}, "fallback": {…}},
            "only_copies": {"total": 2, "listed": ["replaced/3-three.m4a"]},
            "path_clashes": {"total": 1, "listed": [{"id": 10001, "path": "…", "album": "…"}]},
            "empty_slots": {"at": "2026-10-29T03:40:00+00:00", "playlists": {"chill": {"total": 1, "entries": ["Someone — Absent"]}}},
            "rejects": {"total": 1, "listed": ["rejected/chill/two.m4a"]}},
 "requests": {"missing": {"total": 0, "listed": []}, "failed": {"total": 0, "listed": []}, "fallback": {"total": 0, "listed": []}}}
```

- `history.events` holds each kind's latest 25 in the window, oldest first; `counts` every kind's total. A `merge`, `moved`, `local` or `recording_replaced` event also carries its `item_id` and `data`, which the digest judges: a merge or a move between unrelated recordings or albums, an extra left to `music-audit-dupes`, a guessed recording ID `music-mb-fingerprint --recheck` replaced (`data.old`, `data.new`, `data.score`, homelab#2543). With the event store unavailable, `history` is `{"error": "…"}` and the rest still answers.
- `issues.stuck`: `failed` and `missing` albums (a feed's, which never fall back), grabs SABnzbd hasn't finished in 12 hours, and fallbacks spotdl hasn't filled in 14.
- `issues.mixed` is the last album tick's; `only_copies` are the files in `quarantine/replaced/` holding a recording the library doesn't; `path_clashes` the items canon tagged in place (`canon_clash`); `empty_slots` the last scan's (the `empty-slots` document), playlists with none left out; `rejects` the length guard's files still in `quarantine/rejected/`.
- `requests` (homelab#2478) are the album requests whose album ended `missing` or `failed`, or is in `fallback` with tracks left, each as `list_requests` shows it (`fallback_rejected` and `acoustid_unknown_only` included). With the database down all three are empty and an `error` says so.

## Overrides (any tier, homelab#2484)

### `GET /mcp-api/overrides[?kind=K][&offset=N]` (`list_overrides`)

The search overrides, holds and track pins in the `music` database (`music_fetch.overrides`), oldest first; `kind` is `search`, `hold` or `pin` (default: all three). An override or hold is on an album row: `album` is that row's record, or `null` with `detached: true` when the row is gone (the album left its playlists, or a merge's survivor has one of its own); `refs` are the album IDs, names and release group it re-attaches by.

```json
{"status": "ok", "kind": null, "total": 3, "offset": 0, "next_offset": null,
 "overrides": [{"kind": "search", "album": {"key": "6Ox…", "artist": "Four Tet", "name": "…", "status": "missing", "release_group": null},
                "detached": false, "refs": ["6Ox…"], "words": "wingdings four tet", "reason": "…",
                "set_by": "album-overrides.conf", "set_at": "2026-10-29T15:00:00Z"},
               {"kind": "hold", "album": null, "detached": true, "refs": ["3Sp…"], "reason": "…", "set_by": "sub-…", "set_at": "…"},
               {"kind": "pin", "spotify_track_id": "68oz…", "isrc": "GB…", "track": "Four Tet — …",
                "url": "https://music.youtube.com/watch?v=…", "label": "track 1", "reason": "…", "set_by": "…", "set_at": "…"}]}
```

Before `music-overrides import` has run, or with the database down: `503 {"status": "unavailable", "error": "…"}`.

## Requests (any tier, homelab#2478)

### `GET /mcp-api/requests[?status=S][&offset=N]` (`list_requests`)

The album requests in the `music` database (`music_fetch.album_requests`), oldest first. A request names a MusicBrainz release group; `album` is the album row that has it (its key, status and origin), or `null` while the next album tick hasn't queued it, when `status` is `pending`. Otherwise `status` is the album's (`wanted`, `dry-run`, `grabbed`, `missing`, `have`, `imported`, `failed`, `fallback`, `filled`); `?status=` keeps only those. `tracklist_release` is the release the tracklist was read from: the `preferred_release`, else one with the release group's most common track count. A `fallback` album also has `fallback_left`, the tracks spotdl still has to get, and (homelab#2546) `fallback_rejected`, those of them whose download is held in `quarantine/rejected/request/`, by reason: `fingerprint` (AcoustID named other recordings, or ours below 0.85, or the file couldn't be fingerprinted), `acoustid-unknown` (AcoustID has no MusicBrainz recording for the audio: no result, or only results with no recording), `duration`, `silence`. `acoustid_unknown_only` is true when there is at least one and every one is `acoustid-unknown`. The scan re-checks an `acoustid-unknown` file against AcoustID once a day for 30 days, so a confirmed one leaves `fallback_rejected` and a mismatch moves to `fingerprint`. Rejects from before homelab#2546 all read `fingerprint`.

```json
{"status": "ok", "filter": null, "total": 1, "offset": 0, "next_offset": null,
 "requests": [{"release_group": "6e0a…", "artist": "…", "name": "…", "preferred_release": null,
               "tracklist_release": "b84e…", "tracks": 10, "requested_by": "sub-…", "requested_at": "2026-10-30T09:00:00Z",
               "status": "missing",
               "album": {"key": "2120", "status": "missing", "origin": "request", "searched_at": "…", "query": "…"}},
              {"release_group": "…", "…": "…", "status": "fallback",
               "album": {"key": "2131", "status": "fallback", "origin": "request", "searched_at": "…", "query": "…",
                         "fallback_left": 22, "fallback_rejected": {"acoustid-unknown": 18}, "acoustid_unknown_only": true}}]}
```

With the database down: `503 {"status": "unavailable", "error": "…"}`.

## The Later queue (any tier, homelab#2478)

### `GET /mcp-api/later[?offset=N]` (`get_later`)

The Later queue in the `music` database (`music_fetch.later_queue`): the albums in `later.m3u`, in arrival order (`available_at`, the first held whole first), each with its `position` from 1, then the `pending` ones the library doesn't hold whole yet, in the order they were added (`position` null). `album` is the album row that has the release group (its key, status and origin), or `null` while the album tick hasn't queued the request, when `status` is `pending`. `available` counts the albums in the `.m3u`; `requested` says whether an album request names the release group.

```json
{"status": "ok", "total": 2, "available": 1, "offset": 0, "next_offset": null,
 "albums": [{"position": 1, "state": "available", "release_group": "6e0a…", "artist": "…", "name": "…",
             "status": "imported", "album": {"key": "2120", "status": "imported", "origin": "request"},
             "requested": true, "added_at": "2026-10-30T09:00:00Z", "available_at": "2026-10-30T11:02:13Z"},
            {"position": null, "state": "pending", "release_group": "0b4e…", "artist": "…", "name": "…",
             "status": "wanted", "album": {"key": "2121", "status": "wanted", "origin": "request"},
             "requested": true, "added_at": "2026-10-30T09:00:00Z", "available_at": null}]}
```

With the database down: `503 {"status": "unavailable", "error": "…"}`.

## Jobs

The CLI tools run as jobs in a **single-slot queue** of depth 5 (the short lane): at most 5 jobs are queued or running at once. A 6th call returns `503 {"status": "busy", "queue": [<job summary>…]}`. Jobs run one at a time, in order, each one a subprocess of the CLI (`argv` below).

**The long lane** (homelab#2553) runs at most one long job (`recheck_recordings`) beside the short lane, so the short tools keep working while it runs. A second long job while one is queued or running returns `503 {"status": "busy", "queue": [<the long job>]}`. A long job holds no lock of its own: a CLI that writes in a long job takes its locks itself, for that phase only.

### Running a tool

`POST /mcp-api/run/<tool>`, with the tool's arguments as a JSON object (`{}` for none). The call waits up to `MCP_INLINE_SECONDS` (default 20) for the job:

- **finished:** `200` with the job record and the first page of output;
- **not yet:** `202` with the job record (`queued`, `waiting_for_lock` or `running`); poll `GET /mcp-api/jobs/<job_id>`.

### Job record

```json
{"job_id": "3f2a…", "tool": "reset_album", "args": {"album": "0ETF…", "reimport": false}, "mode": "dry_run",
 "status": "done", "caller": "a1b2…", "tier": "write",
 "argv": [["music-album-reset", "--", "0ETF…"]], "lane": "short", "resumable": false, "resumes": 0,
 "created_at": "…", "started_at": "…", "finished_at": "…", "exit_code": 0,
 "dry_run_hash": "sha256:…", "error": null, "progress": null,
 "output": "…one page…", "offset": 0, "next_offset": null, "total_lines": 12}
```

| `status` | Meaning |
|---|---|
| `queued` | waiting for the slot |
| `waiting_for_lock` | has the slot, waiting for Prefect's `pipeline` (and for reset/requeue, `albums`) lock |
| `running` | its CLI is running (set before it starts) |
| `done` | every command exited 0 |
| `failed` | a command exited non-zero (`exit_code`), or the job could not run (`error`) |
| `refused` | an apply whose fresh dry run no longer matches: `output` is the new dry run, which now counts as the dry run to apply |
| `interrupted` | it was queued or running when the service restarted and is not resumable, or was resumed 5 times already; never retried |

`args` are the **normalised** arguments: album queries resolved to their key, ID lists deduplicated and sorted, `since` in UTC. `argv` lists each command the job runs (`merge_items` runs one per pair). `dry_run_hash` is set on a successful dry run and on a refused apply.

`lane` is `short` or `long`. `progress` is the output's latest `[PROGRESS] <done>/<total>…` line as `{"done": n, "total": n, "line": "…", "at": "<its log timestamp>"}`, or `null`: a long job's progress without paging through its output.

Records live on pipeline-state (`inbox/spotdl/.mcp-jobs.json`, output in `.mcp-jobs/<job_id>.log`). The last 50 jobs and every job of the last 7 days are kept, whichever is more. On boot, `queued`, `waiting_for_lock` and `running` jobs become `interrupted`, except a **resumable** one (`resumable: true`: a long job, and an apply of `apply_recheck` once its dry-run comparison has passed, `applying: true`; one cut short before that ends `interrupted` with nothing applied). That job is queued again with the same `job_id`, `resumes` one higher and `resumed_at` set, and its output goes on after what it printed before (`[MCP] resumed after a restart (n of 5)`). A long job's CLI continues from its own checkpoint. A resumed apply takes its locks again and runs only its `--apply` commands, since the dry-run comparison passed before the restart. The CLI checks each item again, and an item applied before the restart is counted as done. After 5 resumes a job is `interrupted` (`error` says so), so a job that kills the pod is not run forever.

Every job logs `[MCP]` lines to the pod log: one at the start with its argv, caller, tier and mode, every output line, and one at the end with its status and exit code.

### Locks

A write tool's **apply** takes Prefect's `pipeline` lock (and first `albums`, for `reset_album` and `requeue_albums`) for its whole run: the fresh dry run, the comparison and the `--apply` command. The CLI subprocess gets `MUSIC_LOCKS_HELD=albums,pipeline`, so it does not wait for a lock its job already holds. Dry runs and the read-tier audits take no lock. In direct mode (no `PREFECT_API_URL`) the locks are the service's threading locks.

`MUSIC_LOCKS_HELD` is set **only** in a job subprocess's environment, and only for the locks that job holds. It must never be set in the Deployment, a ConfigMap or the image: a CLI run by hand (`kubectl exec`) from that environment would silently skip its locks. The service refuses to start with it set, and a job with no locks strips it from its subprocess's environment.

## Test harness

`service/tests/mcp_harness.py` serves this API over a seeded beets library, album store, playlist store and quarantine, with faked CLI commands (no CLI runs): `python service/tests/mcp_harness.py --port N --dir D` (`/app/tests/mcp_harness.py` in the dev image). The stores are in a new database it creates on `MUSIC_TEST_DATABASE_URL`'s server (or in `MUSIC_DATABASE_URL`'s, when set), so it needs one. `MCP_API_TOKEN` defaults to `mcp-secret`. The adapter's contract test runs against it.

## Read-tier CLI dry runs (any tier)

| Tool | Body | argv |
|---|---|---|
| `audit_dupes` | `{"fingerprint_ids": [1, 2]}` (optional; `[]` = every item without a fingerprint) | `music-audit-dupes` / `music-audit-dupes --fingerprint [ID…]` |
| `audit_lengths` | `{"silence": false}` | `music-audit-lengths [--silence]` |
| `audit_releases` | `{"replaced_query": "…"}` (optional; `""` lists every only copy) | `music-album-audit-releases` / `… --replaced` / `… --replaced=QUERY` |
| `audit_want_albums` | `{}` | `music-audit-want-albums` |
| `audit_titles` | `{}` | `music-audit-titles` |
| `recheck_recordings` | `{"ids": [6994, 5672]}` (optional; none = every item with an `mb_trackid`) or `{"resume": "<run ID>"}` | `music-mb-fingerprint --recheck --run-id=<job_id> [-- ID…]` / `… --run-id=<run ID>` (long lane) |
| `search_releases` | `{"query": "…", "limit": 5}` | `music-requests search --limit=N -- QUERY` |

`audit_releases --replaced` takes the CLI's query as given, ambiguity included: the CLI itself refuses more than one match.

`recheck_recordings` (homelab#2553) is a recorded `music-mb-fingerprint --recheck` in the long lane: each item's guessed MusicBrainz recording (`mb_trackid`) checked against the recordings AcoustID links to its audio, at 3 lookups a second (about an hour for the whole library). Its run ID is its `job_id`. The run stores a checkpoint every 25 items and its findings in the `music` database, so a restart resumes it (above) and `apply_recheck` applies it. It changes nothing in the library. `ids` (positive integers, deduplicated and sorted) limits it to those items. `resume` (a run ID: 1 to 64 letters, digits, `-` or `_`, starting with a letter or digit) continues a run that ended `failed` or `interrupted`, with the items it was started with, so it can't be combined with `ids`. A finished run, an unknown one, or one another process holds fails the call (exit 1).

`search_releases` (homelab#2478) reads only MusicBrainz, never the library: it sends `query` (1 to 200 characters, whitespace collapsed) as given, in MusicBrainz's release-group search syntax, so it must be what the user typed, never a title read from the library. It lists the best `limit` matches (1 to 10, default 5), each release group with its primary and secondary types, first year and its releases, earliest first (at most 15 listed, the rest counted): date, country, media, disambiguation and track count. A release group whose secondary types include Live, Remix, Compilation, Demo, DJ-mix or Mixtape/Street is `Flagged: …: not a studio album`; a release not official, or whose disambiguation says live, demo, remix or compilation, is `[flagged: …]`. Each match costs a release browse at one MusicBrainz read a second, so a long list outlasts the inline wait. A query MusicBrainz rejects, or MusicBrainz down, fails the call (exit 1).

## Write tools (write tier)

Every write tool's body takes `"apply": false` (the default) or `true`.

| Tool | Body | argv (dry run; apply adds `--apply` before any `--`) | Locks |
|---|---|---|---|
| `reset_album` | `{"query": "…", "reimport": false}` | `music-album-reset [--reimport] -- <key>` | albums, pipeline |
| `requeue_albums` | `{"ids": ["…"], "since": "2026-10-20T00:00:00"}` (one or both) | `music-album-requeue [--since <UTC ISO>] -- [ID…]` | albums, pipeline |
| `merge_items` | `{"pairs": [[keep_id, drop_id], …]}` | `music-audit-dupes --merge <keep> <drop>`, one per pair | pipeline |
| `replace_item` | `{"id": 10001, "youtube_url": "…"}` | `music-audit-lengths --replace <id> --youtube https://www.youtube.com/watch?v=<video id>` | pipeline |
| `unlink_spotify_id` | `{"id": 9674, "track": "…"}` | `music-audit-dupes --unlink <id> <track id>` | pipeline |
| `embed_covers` | `{}` | `music-embed-covers` | pipeline |
| `canon_albums` | `{"refresh": false, "search_budget": null, "mb_budget": null}` | `music-canon-albums [--refresh] [--search-budget N] [--mb-budget N]` | pipeline |
| `apply_recheck` | `{"run_id": "…"}` | `music-mb-fingerprint --replace-run=<run ID>` | pipeline |
| `set_search_override` | `{"album": "…", "words": "…", "reason": ""}` | `music-overrides search [--reason=R] -- <key> <words>` | none |
| `clear_search_override` | `{"album": "…"}` | `music-overrides clear-search -- <key>` | none |
| `hold_album` | `{"album": "…", "reason": "…"}` | `music-overrides hold --reason=R -- <key>` | none |
| `release_album` | `{"album": "…"}` | `music-overrides release -- <key>` | none |
| `pin_track` | `{"track": "…", "url": "…", "label": "", "reason": ""}` | `music-overrides pin [--label=L] [--reason=R] -- <track id> <url>` | none |
| `unpin_track` | `{"track": "…"}` | `music-overrides unpin -- <track id>` | none |
| `request_album` | `{"albums": [{"release_group": "…", "preferred_release": null}, …], "later": false}` | `music-requests add [--later] -- <release group>[=<release>]…` | none |
| `remove_from_later` | `{"release_groups": ["…"], "albums": ["<key>"], "through": null}` | `music-later remove [--through=N] [--album=<key>]… -- <release group>…` | none |

Free-text values never parse as flags: positional values follow `--`, and the `--replaced` query is passed as `--replaced=QUERY`. The argparse test checks that a dry run's argv never parses with `--apply`, whatever its arguments hold.

Validation (400): `reset_album` needs a non-empty `query`, which must name exactly one album (ambiguous and not found as above, and no job is queued); `requeue_albums` needs `ids`, `since` or both; `merge_items` needs at least one pair of two different positive integer IDs, and an ID one pair drops can't appear in another pair; budgets are non-negative integers.

`replace_item` accepts only a YouTube video ID, or a youtube.com / music.youtube.com / youtu.be URL reduced to one (`music_fetch.ids.youtube_url`, homelab#2411), and passes the canonical watch URL; anything else is a 400. An album query or a requeue ID may be an `open.spotify.com/album` URL, reduced to its ID as the CLIs do (`music_fetch.ids.spotify_key`); a Spotify URL of another kind is a 400.

The override tools (homelab#2484) write rows of their own tables, which the tick and the nightly read at their start, so they take no lock. `album` is an album query as for `reset_album` (resolved to the key; ambiguous and not found as above). `words` are normalised as the tick searches (`usenet.normalise`) and must keep a readable word; `reason` and `label` are whitespace-collapsed, at most 500 characters, and `hold_album` needs a reason. `track` is a Spotify track ID or `open.spotify.com/track` URL, reduced to the ID; `url` must be a YouTube or YouTube Music video (an ID, or a `youtube.com`, `music.youtube.com` or `youtu.be` URL), reduced to its canonical watch URL on its own host; another site or a playlist is a 400, since the URL reaches yt-dlp. Each apply records an `override_set` or `override_cleared` event with the caller, in the same transaction as the row. Until `music-overrides import --apply` has run, every write fails (exit 1): the tick still reads the ConfigMap's files.

`request_album` (homelab#2478) takes 1 to 25 albums, each a `release_group` and an optional `preferred_release`: an MBID or a `musicbrainz.org/release-group/…` (`/release/…`) URL, reduced to the MBID; the list is deduplicated in the order given, and one release group given twice with different preferred releases is a 400. The dry run reads each tracklist from MusicBrainz (two or three reads each, at one a second, so a long list outlasts the inline wait) and says what the next album tick does: a new album row, or the row that has the release group taken over (a wants or library row's wanted tracks become the whole album) or kept when it leaves its playlists. A release group requested already is listed and left as it is. A release group or release MusicBrainz doesn't have, a preferred release of another release group, or no official release with tracks fails the call (exit 1), and nothing is written. The apply adds every request, each with its `album_requested` event and the caller, in one transaction. The request's own table is read by the tick at its start, so it takes no lock. With `"later": true` the albums are also appended to the Later queue in that order, each with a `later_added` event, in the same transaction; an album requested already is only appended, and one on Later already stays where it is. The dry run's `Later:` line per album says which, and whether the library holds it already (it joins the end of `later.m3u` at the next scan) or it waits for its import.

`remove_from_later` (homelab#2478) takes `release_groups` (up to 25, MBIDs or URLs, deduplicated in order), `albums` (up to 25 album queries as for `reset_album`, each resolved to its key, deduplicated in order; ambiguous and not found as above, and nothing runs; homelab#2534) and `through`, a positive integer: Later's first N albums, `get_later`'s positions 1 to N. At least one is needed. An album reaches its Later entry whether the entry holds it by release group or by ref (queued by the cutover before its row had a release group), which a release group can't. A release group or album not on Later is listed and left as it is; a `through` past the end of Later fails the call (exit 1). The apply deletes the queue rows, each with a `later_removed` event and the caller, in one transaction; `later.m3u` drops the albums at the next scan, and their files stay in the library. The scan's regen reads the queue, so it takes no lock.

`unlink_spotify_id` (homelab#2530) takes one Spotify track ID (`track`: an ID or `open.spotify.com/track` URL, reduced to the ID) off one item's `spotify_ids`. A playlist entry resolves by Spotify ID before ISRC, so an item that wrongly holds an entry's ID takes its slot even when the right item holds the entry's ISRC. An ID the item doesn't hold, or its `spotify_url` ID (its file was downloaded for that entry: `replace_item` it instead), fails the call (exit 1). The dry run prints the field before and after, and each playlist entry listing the ID with the item it resolves to afterwards (`resolver`'s rule), or that its slot goes empty. The apply stores the item, records a `wrong_version` event, regenerates the `.m3u` files and triggers a Navidrome scan. It is undone by hand: `beet modify id:<id> spotify_ids=<the dry run's before value>`.

`apply_recheck` (homelab#2553) takes the `job_id` of a finished `recheck_recordings` run. Its dry run reads the run's findings, not AcoustID. For each replaceable item it says what the apply does as the library stands: `[WOULD-REPLACE] <id> — <title>: mb_trackid <old> → <new>`, `[SKIP] <id>: <why>` (gone from the library, its `mb_trackid` changed, or its file's path, size or mtime changed since the check: `replace_item` swaps the audio and keeps `mb_trackid`), or `[DONE]` (it holds the replacement already). The apply stores each replacement with its `recording_replaced` event (`data.run`) and the finding's outcome in one transaction, then writes the file's tags. It is guarded like any write (the dry run's hash, 15 minutes). Items with several recordings are left alone. A run that isn't finished, or that finished more than 7 days ago, fails the call (exit 1); runs are pruned 30 days after they finish. The apply is resumable (above): resumed or run again, it re-checks every item, so a partly applied run finishes without a second event for an item it already replaced.

`merge_items` runs its pairs in order and stops at the first that fails; each pair is its own CLI run, so the pairs before it stay merged. Within a pair, beets commits each store and removal on its own (no transaction spans the merge), so a crash mid-pair can leave the keeper with the loser's IDs and the loser still in the library: rerun the pair.

### The dry-run → apply guard

`apply: true` is accepted only when a successful dry run of the **same tool with the same normalised args**, by **any caller**, finished within the last **15 minutes**. Otherwise:

```json
409 {"status": "refused", "reason": "no_dry_run", "error": "run reset_album with apply=false first (same args, within 15 min)", "args": {…}}
```

Accepted, the apply job, holding its locks, runs the dry run again and compares its output hash with that dry run's. Lines are compared without their leading log timestamp (`[2026-10-27T14:03:11+0000] `), and lines that are nothing but a timestamp are dropped. A mismatch ends the job `refused`, with the new dry run as its output and no change made. That new dry run is now the one to apply. A match runs the `--apply` commands, and the job's output is theirs.

### Triggers (write tier, no dry run)

`POST /mcp-api/trigger/scan` (`trigger_scan`) and `POST /mcp-api/trigger/album_tick` (`trigger_album_tick`) submit the `music-scan` or `music-albums` flow run. Each is a no-op when that flow already has a run pending or running (for `music-albums`, a scheduled run that is due counts as pending, but the next interval run does not):

```json
200 {"status": "submitted", "flow": "music-scan"}
200 {"status": "already_pending", "flow": "music-scan", "state": "RUNNING"}
503 {"status": "failed", "error": "failed to submit run — is the Prefect server reachable?"}
```
