# Verify a fix

Say what should change and when, and offer to check then.

- **Album tick:** every 30 min, counted from pod start, so the next one comes 30 min after the
  last `Album tick` line. `trigger_album_tick` runs one now. A reset album is grabbed by the
  next tick and logs `[GRAB]`, `[IMPT]`, then `[DONE]` with its `[RETIRE]`s. Download and
  import take minutes to hours, depending on SABnzbd. `get_album`'s `status` moves `wanted` →
  `grabbed` → `imported`.
- **Scan:** runs after an import, after the 03:00 UTC fetch, or when the inbox watcher sees new
  files. There is no timer. A scan-time fix (an extra retired as now held, a purge, playlist
  slots) can wait until about 03:05 UTC, unless `trigger_scan` starts one now.
- **A write's own effect** shows in its job output at once: `merge_items` (`[MERGE] keep …`),
  `replace_item`, `embed_covers`. Check the item afterwards with `get_item`, or the album with
  `get_album`.
- **Overrides, holds and pins** apply from the next run that reads them: a search override at
  the next tick (`get_album`'s `query` changes once it searches; a `missing` album is searched
  again at once), a hold from the next tick's reimport pass, a pin at the next nightly
  (`[PIN] … → downloading from …`, then the item in `get_playlist`). `list_overrides` shows the
  row at once, and `album_history` the event.
- **A request** is queued by the next tick (`list_requests`: `pending`, then `wanted`; the
  tick logs `[REQUESTS] N request(s): …`), and searched by the same tick when a slot is free.
- **Playlists** catch up with Spotify at the next nightly (album playlists: the next tick after
  a Spotify edit). `get_playlist` shows an entry's `item_id` once an item holds it.
- **Metrics** move on the next push: `music_albums*` each tick, `music_scan_*` each scan.
  `library_status` is live.
