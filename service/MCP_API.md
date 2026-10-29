# MCP API (`/mcp-api/*`)

The contract between the pipeline service (`service/`) and the Music MCP adapter (`mcp/`), homelab#2412. The adapter is the only client. It never runs in the pipeline pod, and this API never returns a credential.

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
 "jobs": {"running": {"job_id": "…", "tool": "canon_albums", "status": "running"}, "queued": 1, "depth": 5}}
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

`key` is the album-store key, which is the Spotify album ID; `record` is the album record as the album store holds it (the `music` database once switched, homelab#2410; `.albums.json` before), with `release_group` and `unresolved`; the items are those whose `spotify_album_id` is the key.

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
 "requests": {"missing": {"total": 0, "listed": []}, "failed": {"total": 0, "listed": []}}}
```

- `history.events` holds each kind's latest 25 in the window, oldest first; `counts` every kind's total. A `merge`, `moved` or `local` event also carries its `item_id` and `data`, which the digest judges: a merge or a move between unrelated recordings or albums, an extra left to `music-audit-dupes`. With the event store unavailable, `history` is `{"error": "…"}` and the rest still answers.
- `issues.stuck`: `failed` and `missing` albums (a feed's, which never fall back), grabs SABnzbd hasn't finished in 2 days, and fallbacks spotdl hasn't filled in 14.
- `issues.mixed` is the last album tick's; `only_copies` are the files in `quarantine/replaced/` holding a recording the library doesn't; `path_clashes` the items canon tagged in place (`canon_clash`); `empty_slots` the last scan's (`inbox/spotdl/.empty-slots.json`), playlists with none left out; `rejects` the length guard's files still in `quarantine/rejected/`.
- `requests` (homelab#2478) are the requests that ended `missing` or `failed`; none exist yet, so both are empty.

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

## Jobs

The CLI tools run as jobs in a **single-slot queue** of depth 5: at most 5 jobs are queued or running at once. A 6th call returns `503 {"status": "busy", "queue": [<job summary>…]}`. Jobs run one at a time, in order, each one a subprocess of the CLI (`argv` below).

### Running a tool

`POST /mcp-api/run/<tool>`, with the tool's arguments as a JSON object (`{}` for none). The call waits up to `MCP_INLINE_SECONDS` (default 20) for the job:

- **finished:** `200` with the job record and the first page of output;
- **not yet:** `202` with the job record (`queued`, `waiting_for_lock` or `running`); poll `GET /mcp-api/jobs/<job_id>`.

### Job record

```json
{"job_id": "3f2a…", "tool": "reset_album", "args": {"album": "0ETF…", "reimport": false}, "mode": "dry_run",
 "status": "done", "caller": "a1b2…", "tier": "write",
 "argv": [["music-album-reset", "--", "0ETF…"]],
 "created_at": "…", "started_at": "…", "finished_at": "…", "exit_code": 0,
 "dry_run_hash": "sha256:…", "error": null,
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
| `interrupted` | it was queued or running when the service restarted; never retried |

`args` are the **normalised** arguments: album queries resolved to their key, ID lists deduplicated and sorted, `since` in UTC. `argv` lists each command the job runs (`merge_items` runs one per pair). `dry_run_hash` is set on a successful dry run and on a refused apply.

Records live on pipeline-state (`inbox/spotdl/.mcp-jobs.json`, output in `.mcp-jobs/<job_id>.log`). The last 50 jobs and every job of the last 7 days are kept, whichever is more. On boot, `queued`, `waiting_for_lock` and `running` jobs become `interrupted`.

Every job logs `[MCP]` lines to the pod log: one at the start with its argv, caller, tier and mode, every output line, and one at the end with its status and exit code.

### Locks

A write tool's **apply** takes Prefect's `pipeline` lock (and first `albums`, for `reset_album` and `requeue_albums`) for its whole run: the fresh dry run, the comparison and the `--apply` command. The CLI subprocess gets `MUSIC_LOCKS_HELD=albums,pipeline`, so it does not wait for a lock its job already holds. Dry runs and the read-tier audits take no lock. In direct mode (no `PREFECT_API_URL`) the locks are the service's threading locks.

`MUSIC_LOCKS_HELD` is set **only** in a job subprocess's environment, and only for the locks that job holds. It must never be set in the Deployment, a ConfigMap or the image: a CLI run by hand (`kubectl exec`) from that environment would silently skip its locks. The service refuses to start with it set, and a job with no locks strips it from its subprocess's environment.

## Test harness

`service/tests/mcp_harness.py` serves this API over a seeded beets library, album store, playlist store and quarantine, with faked CLI commands (no CLI runs): `python service/tests/mcp_harness.py --port N --dir D` (`/app/tests/mcp_harness.py` in the dev image). `MCP_API_TOKEN` defaults to `mcp-secret`. The adapter's contract test runs against it.

## Read-tier CLI dry runs (any tier)

| Tool | Body | argv |
|---|---|---|
| `audit_dupes` | `{"fingerprint_ids": [1, 2]}` (optional; `[]` = every item without a fingerprint) | `music-audit-dupes` / `music-audit-dupes --fingerprint [ID…]` |
| `audit_lengths` | `{"silence": false}` | `music-audit-lengths [--silence]` |
| `audit_releases` | `{"replaced_query": "…"}` (optional; `""` lists every only copy) | `music-album-audit-releases` / `… --replaced` / `… --replaced=QUERY` |
| `audit_want_albums` | `{}` | `music-audit-want-albums` |

`audit_releases --replaced` takes the CLI's query as given, ambiguity included: the CLI itself refuses more than one match.

## Write tools (write tier)

Every write tool's body takes `"apply": false` (the default) or `true`.

| Tool | Body | argv (dry run; apply adds `--apply` before any `--`) | Locks |
|---|---|---|---|
| `reset_album` | `{"query": "…", "reimport": false}` | `music-album-reset [--reimport] -- <key>` | albums, pipeline |
| `requeue_albums` | `{"ids": ["…"], "since": "2026-10-20T00:00:00"}` (one or both) | `music-album-requeue [--since <UTC ISO>] -- [ID…]` | albums, pipeline |
| `merge_items` | `{"pairs": [[keep_id, drop_id], …]}` | `music-audit-dupes --merge <keep> <drop>`, one per pair | pipeline |
| `replace_item` | `{"id": 10001, "youtube_url": "…"}` | `music-audit-lengths --replace <id> --youtube https://www.youtube.com/watch?v=<video id>` | pipeline |
| `embed_covers` | `{}` | `music-embed-covers` | pipeline |
| `canon_albums` | `{"refresh": false, "search_budget": null, "mb_budget": null}` | `music-canon-albums [--refresh] [--search-budget N] [--mb-budget N]` | pipeline |
| `set_search_override` | `{"album": "…", "words": "…", "reason": ""}` | `music-overrides search [--reason=R] -- <key> <words>` | none |
| `clear_search_override` | `{"album": "…"}` | `music-overrides clear-search -- <key>` | none |
| `hold_album` | `{"album": "…", "reason": "…"}` | `music-overrides hold --reason=R -- <key>` | none |
| `release_album` | `{"album": "…"}` | `music-overrides release -- <key>` | none |
| `pin_track` | `{"track": "…", "url": "https://…", "label": "", "reason": ""}` | `music-overrides pin [--label=L] [--reason=R] -- <track id> <url>` | none |
| `unpin_track` | `{"track": "…"}` | `music-overrides unpin -- <track id>` | none |

Free-text values never parse as flags: positional values follow `--`, and the `--replaced` query is passed as `--replaced=QUERY`. The argparse test checks that a dry run's argv never parses with `--apply`, whatever its arguments hold.

Validation (400): `reset_album` needs a non-empty `query`, which must name exactly one album (ambiguous and not found as above, and no job is queued); `requeue_albums` needs `ids`, `since` or both; `merge_items` needs at least one pair of two different positive integer IDs, and an ID one pair drops can't appear in another pair; budgets are non-negative integers.

`replace_item` accepts only a YouTube video ID, or a youtube.com / music.youtube.com / youtu.be URL reduced to one (`music_fetch.ids.youtube_url`, homelab#2411), and passes the canonical watch URL; anything else is a 400. An album query or a requeue ID may be an `open.spotify.com/album` URL, reduced to its ID as the CLIs do (`music_fetch.ids.spotify_key`); a Spotify URL of another kind is a 400.

The override tools (homelab#2484) write rows of their own tables, which the tick and the nightly read at their start, so they take no lock. `album` is an album query as for `reset_album` (resolved to the key; ambiguous and not found as above). `words` are normalised as the tick searches (`usenet.normalise`) and must keep a readable word; `reason` and `label` are whitespace-collapsed, at most 500 characters, and `hold_album` needs a reason. `track` is a Spotify track ID or `open.spotify.com/track` URL, reduced to the ID; `url` must be `https://`, a YouTube link losing its tracking parameters. Each apply records an `override_set` or `override_cleared` event with the caller, in the same transaction as the row. Until `music-overrides import --apply` has run, every write fails (exit 1): the tick still reads the ConfigMap's files.

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
