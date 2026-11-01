# Workflow

## Tiers

| Tier | Who | Tools |
|------|-----|-------|
| Read | anyone signed in to the connector, and Claude Code's static token | the structured reads, the audits, `recheck_recordings`, `get_job`, `get_guide` |
| Recoverable write | the claude.ai connector (a Pocket ID user in the `music` group) | `reset_album`, `requeue_albums`, `merge_items`, `replace_item`, `unlink_spotify_id`, `embed_covers`, `canon_albums`, `apply_recheck`, `set_search_override`, `clear_search_override`, `hold_album`, `release_album`, `pin_track`, `unpin_track`, `trigger_scan`, `trigger_album_tick` |
| Irreversible | not exposed | the workstation commands below |

The tier belongs to the token, not to the client: the static token is read-only, and an OAuth
login in the `music` group writes, whichever client holds it. Don't guess the tier from what
you are; the server enforces it. A write refused as read-only is handed over as "in the
claude.ai Music connector: `reset_album` …".

## Dry run, then apply

Every write tool is a dry run unless `apply=true`:

1. Run it with `apply=false` and show the user the output.
2. When they agree, run it again with **the same arguments** and `apply=true`, within 15
   minutes. The dry run may come from any caller.
3. The apply first runs the dry run again. If the output changed (the library moved), it ends
   `refused` with the new dry run as its output, and that becomes the dry run to apply. Show it
   and ask again.

Writes are recoverable: the library and pipeline state are backed up, and a merged or replaced
file goes to `quarantine/replaced/` for 14 days. Still, apply only what the user agreed to.

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

**Applying a recheck.** The finished run's `job_id` is its run ID. `apply_recheck` with it is a
write like the others, dry run first. Its dry run reads the run's findings, not AcoustID, so it
takes seconds and lists each replacement (`[WOULD-REPLACE]`) or why an item is skipped (it left
the library, its `mb_trackid` changed, or its file changed since the check: recheck those by ID).
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
| `music-audit-lengths --replace <id> --youtube <url> --force --apply` | Swaps the file in even though the length guard rejects it |
| `music-audit-lengths --quarantine` | Also runs the length guard over quarantine's files |
| `music-rollback-releases --apply` | The legacy cleanup: moves pre-#279 Usenet items that match no playlist entry to `quarantine/replaced/` |

Groups that would take a track off an album are listed `[ALBUM]` and left alone by
`--apply`. `merge_items` one of their pairs only if the album may lose that track.
