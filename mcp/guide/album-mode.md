# Album mode

Album mode grabs whole albums from Usenet for every album a Spotify playlist touches, and
falls back to spotdl when no release works. The `music-albums` flow (the **tick**) runs every
30 min, counted from pod start. It searches, grabs, and resets up to 3 mixed albums a day.
When SABnzbd finishes a job, the `music-album-import` flow imports it. `get_album` shows an
album's record: `status`, `attempts`, its `blocklist` of rejected releases.

Each tick ends with one summary line: `… N searched, N grabbed, N reset for a reimport;
albums …=n; indexer 24h: …` (Loki: `|= "Album tick"`).

## 1. No tick at all (`MusicAlbumTickNotRun`)

Nothing pushed for 2 h. After a rollout, the first tick comes 30 min after the new pod
started, not at :00 or :30. `trigger_album_tick` starts one now. If it says `failed` (Prefect
unreachable), the pod is the problem, which is an operator matter.

## 2. The tick fails or stops early (`MusicAlbumTickFailing`)

| Line | Cause | Action |
|------|-------|--------|
| `[ERR] … indexer in Prowlarr backoff: <id> until <time>` | The indexer failed and Prowlarr backed it off. Its searches would answer `200 []` | Wait for the backoff to end; nothing is lost. Albums demoted before the guard caught it: `requeue_albums` with `since` set to the start of the outage |
| Three albums in a row with 0 results | An indexer failing without a backoff | Operator: check `ProwlarrHealthError` and the indexer's site. Then the same requeue |
| `Spotify … 429` / `MusicSpotifyRateLimited` | Spotify rate limit with a long `Retry-After` | Wait it out. The flow fails fast instead of sleeping on its lock |
| `Automatic reimports spent` | Not a failure: 3 automatic reimports per rolling 24 h | None |

## 3. Grabs don't download

Usually the app pods (SABnzbd, Prowlarr) or the Usenet egress, all operator matters with
their own homelab runbooks. One case shows in the music data: **the tick logs `grabbed=3` tick
after tick and grabs nothing new**. Every in-flight slot is held by a job SABnzbd never
finished (paused or aborted). The operator resumes it in SABnzbd. It then fails and is
blocklisted like `[WRONG]`.

## 4. A download doesn't import

Search the album's name in the log. The import logs `[RELEASE]`/`[NOMB]`, then one outcome:

- `[DONE]`: imported. `[RETIRE]` lines before canon's `[RETAG]` are older copies moving to
  `quarantine/replaced/`. `[LOCAL]` keeps copies the release lacks as extras.
- `[WRONG]`: the release is blocklisted and the next tick grabs the next one, up to 3
  releases. Then the album is `failed` and falls back to spotdl. `[INCOMPLETE]`: the copy is
  kept while a more complete copy of the same edition is tried. No action for either.
- `[RETRY] … (n/12)` with `musicbrainz_unavailable`: MusicBrainz is answering 503. The job
  stays grabbed and is retried once it has been grabbed for 1 h. The 13th deferral blames the
  release.
- `[RESUME]`: a pod replaced mid-import left the job's remaining files in the inbox. At start
  the service moves them back to SABnzbd's complete dir, so no scan imports them loose, and
  the lost-trigger recovery finishes the import as the release. No action.
- `[LOST]`: the completed job folder is gone. The album is searched again at the next
  top-up, ahead of the queue, with no blocklist entry. Loose items from such an import (no
  album, no cover) are retired when the new release imports.

## 5. An album is wrong

- **Start one album over** from a single release: `reset_album` with `reimport=true`. The next
  tick grabs it, and old copies are retired on import.
- **Keep a hand-curated album out** of automatic reimports: a `hold <spotify album id>` line in
  `album-overrides.conf`, through a homelab PR. Not an MCP tool.
- **Old copies:** `audit_releases` with `replaced_query='<artist — album>'` lists the album's
  files in `quarantine/replaced/` as `[REPLACED]` (the library holds the recording) or
  `[ONLYCOPY]`. Superseded copies are purged automatically after 14 days. Only copies never
  are, so the user decides on those. Deleting them is workstation-only.
- **Two copies on one album:** the album's older items whose recording the release lacked
  stayed as `[LOCAL]` extras. Once the release's tracks get their Spotify IDs, the next scan
  retires those a release track holds and merges those whose audio one release track holds.
  `[LOCAL] … left to music-audit-dupes` means the audio matched several release tracks: run
  `audit_dupes` and `merge_items` the pair the user confirms. Other extras still there after a
  scan are another take (an alternate version, a 5.1 mix): keep them, or start the album over.
- **Duplicates across albums:** `audit_dupes` reports them, and `merge_items` merges a
  confirmed pair.

## 6. quarantine/replaced/ fills up

- **`MusicReplacedOnlyCopiesWaiting`**: only copies (files whose recording no library item
  holds) have sat in `quarantine/replaced/` for 7 days. The scan never purges them, so the alert
  stays until someone decides. Per album: `audit_releases` with `replaced_query`, then the user
  deletes them on the workstation or moves the ones worth keeping out.
- **`MusicReplacedPurgeStalled`**: superseded files are 21 days old and no scan purged one.
  This is operator territory (`REPLACED_RETENTION_DAYS`, the scan's `[PURGE]` lines).
- **Mixed albums** have no alert. `audit_releases` lists them.

## 7. Album never downloads

`get_album`: a `wanted` album with no attempts hasn't been searched yet. The tick searches a
few per run, and `audit_want_albums` shows what is still wanted, grouped by album. A
`fallback` album went to spotdl after its releases failed. A name of glyphs only
(`[NOWORDS]`) needs a search override line (a homelab PR).
