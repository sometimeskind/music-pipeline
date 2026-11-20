# Workflow

## Tiers

| Tier | Who | Tools |
|------|-----|-------|
| Read | anyone signed in to the connector, and Claude Code's static token | the structured reads, the audits, `check_release`, `recheck_recordings`, `recording_candidates`, `compare_items`, `get_job`, `get_guide` |
| Recoverable write | the claude.ai connector (a Pocket ID user in the `music` group) | `reset_album`, `requeue_albums`, `blocklist_release`, `unblocklist_release`, `merge_items`, `replace_item`, `unlink_spotify_id`, `set_recording`, `fingerprint_items`, `keep_quarantined`, `restore_kept`, `retire_item`, `embed_covers`, `canon_albums`, `backfill_album_ids`, `clear_bad_isrcs`, `clear_release_tracks`, `apply_recheck`, `set_search_override`, `clear_search_override`, `hold_album`, `release_album`, `pin_track`, `unpin_track`, `trigger_scan`, `trigger_album_tick` |
| Irreversible | not exposed | the workstation commands below |

The tier belongs to the token, not to the client: the static token is read-only, and an OAuth
login in the `music` group writes, whichever client holds it. Don't guess the tier from what
you are; the server enforces it. A write refused as read-only is handed over as "in the
claude.ai Music connector: `reset_album` …".

## Claims

State only what tool output shows, and "the tools can't tell me" is an answer: never fill the gap
with a likely cause. Read the state again before advising on it, as it may have moved. Propose a
fix only once its dry run has run or it has been tested, and say a release or an override matches
only once `check_release` has answered.

## Dry run, then apply

Every write tool is a dry run unless `apply=true`:

1. Run it with `apply=false` and show the user the output.
2. When they agree, run it again with **the same arguments** and `apply=true`, within 15
   minutes. The dry run may come from any caller.
3. The apply first runs the dry run again. If the output changed (the library moved), it ends
   `refused` with the new dry run as its output, and that becomes the dry run to apply. Show it
   and ask again.

An apply job shows its dry run first. While `get_job`'s `phase` says `checking dry run`, `Dry run
— nothing …` in the output and a null dry run hash are expected and nothing has been applied:
this is not a failed apply, so keep polling. Once it matches, the output restarts at
`[MCP] dry run re-run matches (…); applying`, and `phase` says `applying`.

Writes are recoverable: the library and pipeline state are backed up, and a merged or replaced
file goes to `quarantine/replaced/` for 14 days (`keep_quarantined` moves one out of the purge's
reach, to `quarantine/keep/`). A file `retire_item` took off an album goes the same way, and
nothing in the library holds its recording, so `keep_quarantined` is the only way back.
`restore_kept` puts a kept file back on an album, and `retire_item` takes it off again. Still,
apply only what the user agreed to.

## Jobs

The CLI tools run one at a time in a queue of depth 5. A call answers inline within about
60 s. Otherwise it returns a job (`queued`, `waiting_for_lock`, `running`): poll `get_job` with
its `job_id`. A sixth call gets `busy` and the queue. A job interrupted by a pod restart is
never retried: run it again.

**Long jobs.** `recheck_recordings` runs in a lane of its own, one at a time beside the queue,
so the other tools keep working while it runs (about an hour for the whole library). A second
one gets `busy` with the one running. It survives a pod restart: it is resumed from its
checkpoint (`resumes` counts how often, at most 5) with its output kept. `get_job`'s `progress`
is its latest `[PROGRESS]` line (`done`, `total`, `at`), so poll that rather than paging the
output. A run that ends `failed` (AcoustID down) or `interrupted` continues with
`recheck_recordings` and `resume` set to its `job_id`.

**`canon_albums` for a few albums** (homelab#3025): give `albums` a list of album queries (key, Spotify album ID, release group, exact "artist — album", or a part matching one album). An ambiguous or unknown entry makes the call do nothing and returns `ambiguous` (with the candidates) or `not_found`, naming that entry. The run touches only what belongs to those albums: the retags, moves and covers of the items canon files under them (their release's items, and loose items it moves onto them; one filed there today that canon moves to another album is left to a run over that album), and the loose copies their release supersedes, which it finds in the whole library, since a copy to supersede is not on the album yet. It looks up, searches and plans only those albums, so it takes a small part of a whole run's time: what is left is reading the library once per phase, and the playlist pages when none were read in the last day. The apply needs a dry run with the same `albums` (any spelling of the same albums is the same call). Use it after a change to one album (its items placed or renumbered, an extra retired, a reimport) or to check one album's canon before a whole run. A track on a single that a Spotify search would move onto the album counts only once the search is cached or its ISRC is on a playlist entry of the album; the whole-library run picks up the rest.

**A whole-library `canon_albums`** (no `albums`) runs in the queue, not the long lane: tools called after it wait until it ends, and a pod restart interrupts it (run it again). A first dry run of the day takes about 20 minutes, most of it MusicBrainz lookups the cache doesn't hold yet; one after it about 4 to 5 minutes. Its apply runs the dry run again before it writes, so allow for the dry run's time twice. The apply's comparison leaves out what the dry run itself cached or counted (the playlist pages, MusicBrainz lookups, Spotify searches, the items scanned), so a library that only grew doesn't refuse it; an import that changes a `[RETAG]`, `[SUPERSEDE]` or `[KEEP]` line still does, and so does a MusicBrainz lookup that failed in one run (`[MB-WAIT]`, its items left `pending`) and succeeded in the other; the refused run is the new dry run to show. The more albums a run covers, the likelier that is: scope the call when only a few albums need it. Before homelab#3025 its supersede phase (`[ALBUM]` … `[SUPERSEDE]` / `[KEEP]` lines, after the `[RETAG]` list) took hours on the whole library; a dry run that again spends long there is a regression to report.

**Applying a recheck.** The finished run's `job_id` is its run ID. `apply_recheck` with it is a
write like the others, dry run first. Its dry run reads the run's findings, not AcoustID, so it
takes seconds and lists each replacement (`[WOULD-REPLACE]`) or why an item is skipped (it left
the library, its `mb_trackid` changed, or its file changed since the check: recheck those by ID).
The apply also refreshes each replaced item's `trackdisambig` and `title` from its new recording
on MusicBrainz (`[REFRESH]`, about a second an item); a Usenet album track keeps its file's own
title unless it names a version (5.1, live, demo…) the recording doesn't. A
`[NOREFRESH]` item (its MusicBrainz lookup failed) is not replaced, since nothing checked that
its new recording is the same song: apply the run again later to retry it. A `[SUSPECT]` item's
new recording names another song: it is not replaced either (`[SKIP]`). The dry run doesn't
know either kind yet (it makes no MusicBrainz call) and lists them as `[WOULD-REPLACE]`. Show a
`[SUSPECT]` to the user: either the recheck matched the wrong recording (an AcoustID collision)
or the file is a wrong download (`replace_item`); never retitle it to the other song.
The apply is refused for a run not finished or finished more than 7 days ago: run a new one. An
apply cut short by a restart is resumed and checks each item again. Run again by hand, one that
already holds its replacement shows `[DONE]`.

Output comes 200 lines or 32 KB at a time. Pass `next_offset` as `offset` to `get_job` for the
rest. Lists (`get_playlist`, `list_quarantine`) page the same way.

`busy, retry` from a structured read means the beets library is locked by an import. Retry in
a minute.

## Workstation-only operations

These stay on the workstation by design: most delete or rewrite for good, so they are not tools. Name the command and the reason,
and let the operator run it. In the CLAUDE.md admin form, it is:
`KUBECONFIG=~/.kube/admin.yaml kubectl -n music exec deploy/music-pipeline -- <command>`.

| Command | What it does |
|---------|--------------|
| `music-album-audit-releases --replaced '<artist — album>' --apply --only-copies` | Deletes only copies for good |
| `music-audit-dupes --apply` | Merges every certain group at once. It can't skip one, and a group can merge two takes that share an ISRC. Prefer `merge_items` for chosen pairs |
| `music-audit-dupes --merge … --allow-manual --apply` | Drops a manually imported item too |
| `music-audit-lengths --replace <id> --youtube <url> --force --apply` | Swaps the file in even though the length or silence check rejects it |
| `music-audit-lengths --quarantine` | Also runs the length and silence checks over quarantine's files |
| `music-mb-fingerprint --refresh-replaced [--since <time>] --apply` | Refreshes `title` and `trackdisambig` from the new recording on items the recheck replaced before its apply did that, or whose lookup failed then (homelab#2544). A dry run without `--apply`; never changes `mb_trackid` |
| `music-rollback-releases --apply` | The legacy cleanup: moves pre-#279 Usenet items that match no playlist entry to `quarantine/replaced/` |

Groups that would take a track off an album are listed `[ALBUM]` and left alone by
`--apply`. `merge_items` one of their pairs only if the album may lose that track.
