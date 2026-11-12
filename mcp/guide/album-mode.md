# Album mode

Album mode grabs whole albums from Usenet for every album a Spotify playlist touches, and
falls back to spotdl when no release works. The `music-albums` flow (the **tick**) runs every
30 min, counted from pod start. It searches, grabs, and resets up to 3 mixed albums a day.
When SABnzbd finishes a job, the `music-album-import` flow imports it. `get_album` shows an
album's record: `status`, `attempts`, its `blocklist` of rejected releases. `blocklisted` shows
each one's title, size and reason, read from the events. A reason of `unknown` means no event
holds it: events began on 2026-10-29, and an `[ERR]` grab from before homelab#2661 lacks its GUID.

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
finished, and `digest_summary` lists the albums under `stuck.grabbed` once they have been
grabbed for 12 h. `SabnzbdQueueStalled` fires after 2 h with nothing downloaded. Two causes:

- **A job paused or aborted.** The operator resumes it in SABnzbd. It then fails and is
  blocklisted like `[WRONG]`.
- **SABnzbd's URL grabber hung on one NZB fetch**: its queue shows the jobs as `Grabbing`
  with nothing left to download, and only the first has a `Grabbing URL` line. The grabber is
  one thread, so every grab queues behind it (homelab#2516). The operator restarts the pod,
  and the new one fetches the NZBs again. No reset or requeue is needed.

## 4. A download doesn't import

Search the album's name in the log. A job holding the same tracks in two formats logs `[FORMAT]` first:
the best format is imported and the rest wait in `quarantine/replaced/retired/`, where `keep_quarantined`
rescues a file until the purge. The import's event (`album_history`) says which MusicBrainz release the files are in `identified`
(`null`: none close enough) and how many files beets couldn't match and imported as they are in
`as_is`, then one outcome:

- `[DONE]`: imported. `[RETIRE]` lines before canon's `[RETAG]` are older copies moving to
  `quarantine/replaced/`. `[LOCAL]` keeps copies the release lacks as extras.
- `[WRONG]`: the release is blocklisted and the next tick grabs the next one, up to 3
  releases. Then the album is `failed` and falls back to spotdl. `[INCOMPLETE]`: the copy is
  kept while a more complete copy of the same edition is tried (not a repost of it: `-1`,
  `-FTP`). Once a retry lacks the same tracks, the edition lacks them: the kept copy is the
  album (`[DONE] … the edition lacks them`, its event's `why`). No action for either.
- `[RETRY] … (n/12)` with `musicbrainz_unavailable`: MusicBrainz is answering 503. The job
  stays grabbed and is retried once it has been grabbed for 1 h. The 13th deferral blames the
  release.
- `[RESUME]`: a pod replaced mid-import left the job's remaining files in the inbox. At start
  the service moves them back to SABnzbd's complete dir, so no scan imports them loose, and
  the lost-trigger recovery finishes the import as the release. No action.
- `[LOST]`: the completed job folder is gone. The album is searched again at the next
  top-up, ahead of the queue, with no blocklist entry. A `[LOST]` right after a pod restart
  with no `[RESUME]` was an import cut short before homelab#2603, which saves the import's
  path before beets runs. Loose items from such an import (no
  album, no cover) are retired when the new release imports.

## 5. An album is wrong

- **Start one album over** from a single release: `reset_album` with `reimport=true`. The next
  tick grabs it. It clears every attempt and the blocklisted releases refused for what they held
  (another album, an incomplete copy). A plain reset takes off only the last candidate, if it was
  refused that way, and one attempt, and an album the library holds then turns `have` with nothing
  grabbed.
- **No reset clears any other failure.** A broken download, a job beets imported nothing from, a
  crashed import or a reason nobody recorded stays on the blocklist: such a release is never
  grabbed again, even if the fault was a one-off, and the album goes without rather than be broken.
- **Before applying a reset**, read its dry run. It lists what it clears, why each was
  blocklisted, and which ones the matcher accepts. Those can be grabbed again, and each may be
  refused the same way, at the cost of one of the album's 3 attempts. Nothing is deleted
  by a reset: the old items stay until an import, and that import retires the ones whose
  recording the new release also holds. With `reimport=true` the album's old release also goes
  whole to `quarantine/replaced/` when it holds under half of the new release's titles, so a wrong
  album leaves (`[RETIRE] … another album: holds 3 of the kept release's 13 titles`). Otherwise the
  rest stay on the album as local extras (`[LOCAL]`), as a deluxe's bonus tracks do. A reimport
  doesn't judge extras an earlier import left, even a wrong album's: `retire_item` takes those off.
  The rule compares titles, so a right release can fall under half (a 16-track edition under a
  92-track super deluxe). Its files stay in `quarantine/replaced/` as `[ONLYCOPY]` until the user
  decides, and a wrong album sharing half the titles stays as `[LOCAL]`.
- **A playlist entry only a retired item held** goes empty, and the `retire` event's `unheld`
  names the playlists. The import's ID linking may give it to the new release. If not, a spotdl
  playlist's next nightly downloads it, as it does any entry no item holds. An album playlist's
  slot stays empty: the release is the album.
- **Keep a hand-curated album out** of automatic reimports: `hold_album` with the reason
  (`release_album` undoes it). `reset_album` still works on a held album.
- **Old copies:** `audit_releases` with `replaced_query='<artist — album>'` lists the album's
  files in `quarantine/replaced/` as `[REPLACED]` (the library holds the recording) or
  `[ONLYCOPY]`. Superseded copies are purged automatically after 14 days. Only copies never
  are, so the user decides on those. Deleting them is workstation-only. A superseded copy the
  user wants kept anyway (the library holds its recording only by a wrong ID): `keep_quarantined`
  before the purge, which moves it to `quarantine/keep/`.
- **Two copies on one album:** the album's older items whose recording the release lacked
  stayed as `[LOCAL]` extras. Once the release's tracks get their Spotify IDs, the next scan
  retires those a release track holds and merges those whose audio one release track holds.
  `[LOCAL] … left to music-audit-dupes` means the audio matched several release tracks: run
  `audit_dupes` and `merge_items` the pair the user confirms. Other extras still there after a
  scan are another take (an alternate version, a 5.1 mix): keep them, or start the album over.
- **A track with no Spotify ID:** an album track gets an ID by ISRC, or by disc, track and title
  together, never by title alone. A track left without one logs `[NOID]` or `[NUMBER]`, shows in
  the id_linked event's `skipped`, and its playlist entry stays empty, so the fetch may download
  it again. `backfill_album_ids` runs that rung over albums already imported: it only writes
  Spotify IDs, so it moves no file and retires or merges nothing.
- **A wrong extra:** a reimport doesn't take extras off, since the kept release lacks their
  recording: a wrong release's live tracks stay as `[LOCAL]` extras after the right release
  arrives. The user chooses them, and `retire_item` with the IDs and a reason takes them off,
  after a dry run. Their files go to `quarantine/replaced/retired/`, which counts as superseded,
  not only copies: the purge deletes them 14 days after the move, and `keep_quarantined` rescues
  one before that. A release item an extra kept off its path (`[CLASH]`, canon_clash) moves
  there at the next scan. It refuses the whole call, nothing moved, when one item is refused:
  - A release's own track: the album is the unit, so `reset_album` with `reimport=true`.
  - The last item a playlist entry resolves to: the dry run shows the entry. When the extra
    holds the entry's Spotify ID wrongly, `unlink_spotify_id`, then retire; the slot goes empty
    until an item holds the entry. When the user confirms a release item is that track,
    `merge_items`, so the release item takes the entry.
  - A loose copy (a spotdl item on the album with no album_extra): not an extra, so it goes by
    `merge_items` onto the release item the user confirms; `compare_items` says whether they
    are the same audio, and `fingerprint_items` gives one with no fingerprint its own first.
- **Two editions merged:** a scan's `[MERGED] … keeps its own release …` means two album
  records turned out to be one release group after both imported. The scan kept the record's
  own release and retired the other's copies as an import would (`[RETIRE]`, `[LOCAL]`). No
  action. `album_history` shows the decision as a merged_releases event (data.kept, data.why),
  and a retired copy's IDs moved to the item holding its recording (data.ids_to), so its
  playlist entries still resolve. `[MULTI] …`: nothing told which release is the record's own,
  so nothing moved: start the album over with `reset_album` and `reimport=true`.
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
few per run, and `library_status` counts albums by status. `audit_want_albums` is the spotdl
playlists' want list, so an album waiting on the tick isn't in it. A `fallback` album went to spotdl after its releases failed. A name of glyphs only
(`[NOWORDS]`), or one whose `[MISS]` queries the indexer can't match, needs
`set_search_override`. A `[MISS]` with results says which rule rejected them (`rejected: …;
closest: "<title>" (<rule>: <detail>)`): `album words` on the closest means the release
spells the title differently, which an override fixes; `size`, `single` or `leftover words`
are about the release itself, which an override doesn't change. `artist words` (the title holds
an album word only as the artist's name, beside words of its own: `Curtis Mayfield-Live In Europe`
for *Curtis Live!*) is another album by the artist, unless those words are only how the album's
release spells it, which an override fixes. `other release` (a tour, live,
box set or remix release holding the album's name) is too, unless the album itself is one whose
Spotify name doesn't say so: an override with that word (`live`) matches it. `subtitle` is what the
title holds against the subtitle after ` - `, which the search leaves out: another decade (`The 60s`
for *… - The 70's*), or leftover words and none of the subtitle's own. An override with the title's
words matches it if the release is the album. An override is words a
release title holds, which also steer the edition picked. Its dry run shows the query before
and after, and when the album is searched again (a `missing` album at the next tick).

The dry run never searches the indexer: it prints the words and the rules, so it says nothing
about what they would find, and "nothing matches" can only come from comparing it with an earlier
search's results. `check_release` doesn't search either. Only a reset and the next tick test the
words: read that tick's `[GRAB]`, `[MISS]` or `[STAYS]` line. A grab under an override is judged
at import, where a release holding none of the album's titles is `[WRONG]`, and the old items stay.

To say whether a release matches, or to propose an override, run `check_release` with the album
and the release title, and with `words` for an override. It is the tick's own matcher: it answers
accepted, or the rule that refused the title and why. Give the release's size in bytes when you
have it: without it the size rule isn't checked and the output says so. It covers the matcher only,
not the blocklist, not whether the indexer has the release, and not how an accepted release ranks.

An override leaves the matcher no artist, and its dry run says so. Every word must be in a title
(`a`, `and` and `the` excepted), so a year or a word only some releases carry refuses the others:
`Curtis Live 1971` needs `1971` in the title. Quotes mean nothing. Nothing excludes:
`set_search_override` refuses a word starting with `-`, and `check_release` shows why, reading
`-Son` as the required word `son`. A title's first word must be one of the override's (`Harry
Nilsson-…` needs `harry`: `nilsson schmilsson` alone refuses it, with `artist: doesn't start with
an album word`). The `self-titled` and `artist words` checks and the second search by the artist
alone are dropped. `reject word`, `leftover words` (the artist's words count unless they are among
the override's), `other release`, `single` and `size` stay.
