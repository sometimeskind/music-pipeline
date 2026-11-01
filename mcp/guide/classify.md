# Classify a library problem

Find the facts yourself before proposing a fix. The structured reads (`library_status`,
`find_albums`, `get_album`, `get_item`, `get_playlist`, `list_quarantine`) and the audits
answer most questions. Most `[TAG]`s below are also recorded as events: `album_history` shows
an album's, and `digest_summary` the last day's. The full log lines are in Loki: a Claude Code
session can read them through the Grafana MCP, and from claude.ai ask the user to paste them. A
job's own output is returned by the tool and by `get_job`.

More than one row can apply. A reimport that ends `[DONE]` and moves old copies aside also
creates new only copies.

| Symptom | Look at | Fix |
|---------|---------|-----|
| Two copies of tracks on **one album** | `get_album` (its items and their `via`), the album's `[DONE]`, `[RETIRE]`, `[LOCAL]` lines | Usually nothing. The next scan retires `[LOCAL]` extras the release now holds by ID (`[RETIRE] … (extra, now held)`) and merges those whose audio a release track holds (`[RETIRE] … (extra, same audio)`). `[LOCAL] … left to music-audit-dupes`: `audit_dupes`, then `merge_items` for the pair. Another take still there after a scan: `reset_album` with `reimport=true` |
| **Path clashes** (digest "N path clashes", `[CLASH]` on a `NN - Title.1.m4a`) | the latest scan's `[CLASH]` lines; `get_album` | Seen once during a reimport: transient, nothing to do. Repeating every scan: two items for one slot that no ID links. `audit_dupes`, then `merge_items` with `[keep, drop]` (same audio: keep the release's copy). `[CLASH]` names paths, not IDs: take the IDs from `get_album` or the audit |
| The same song on **two albums** | `[BOTH]`, `[SPLIT]`, `[APART]` lines; `get_item` on each | `audit_dupes`, then `merge_items` for a pair the user confirms is one recording |
| An album **mixed** from several releases | `audit_releases`, `[AUTORESET]`, `[MIXED]`, `list_overrides` with `kind=hold` | Nothing while the tick resets 3 a day automatically. Mixed on purpose (a hand-curated album): `hold_album` with the reason, and it stays mixed for good (`release_album` undoes it). By hand: `reset_album` with `reimport=true` for the album `audit_releases` names |
| **Only copies** in `quarantine/replaced/` (`MusicReplacedOnlyCopiesWaiting`) | `list_quarantine` with `kind=replaced`; `audit_releases` with `replaced_query='<artist — album>'` | The user decides per album. Deleting them (`--apply --only-copies`) is irreversible and runs on the workstation only (see `workflow`) |
| **Wrong release** imported (wrong edition, wrong album) | `get_album` (record, blocklist), `[RELEASE]`/`[NOMB]`, `[DONE]`/`[WRONG]` | `reset_album` with `reimport=true`. A hand-curated album: `hold_album` instead |
| Album stuck `wanted` / `fallback` / `failed` | `get_album` (status, attempts), `album_history` (what was tried), `library_status`, `[MISS]`/`[FALLBACK]`/`[ERR]` | Fell back during an indexer outage: `requeue_albums` with `since`. A name of glyphs only (`[NOWORDS]`), or `[MISS]` with queries the indexer can't match, or whose closest result fails `album words`: `set_search_override` with words a release title holds (check them in the indexer's own search first). `failed`: `reset_album` |
| **Wrong take or length** (demo, radio edit, silence) | `audit_lengths` (`silence=true` for padding), `list_quarantine` with `kind=rejected`, `[WOULD-REJECT]` | `replace_item` with the item ID and a YouTube video the user picked. A fingerprint mismatch `audit_dupes` reports is fixed the same way. Re-downloading through the inbox doesn't work: the duplicate hook merges it back into the old item |
| A **track never downloads** (`[MISS] … no source found` on every retry, e.g. a glyph-only title) | `get_playlist` (the entry, `item_id: null`), `list_overrides` with `kind=pin` | `pin_track` with the Spotify track ID and a source URL the user picked (a YouTube Music video): the nightly downloads it from there. The dry run says whether the library holds the track already; then the pin is skipped, and `replace_item` changes the item's audio instead. `unpin_track` once it is in |
| A track shows as **another version** in Navidrome (a 5.1 mix, a live take: its `mb_trackid` names a sibling recording) | `get_item` (`mb_trackid`); the replaced recordings in `digest_summary` | `recheck_recordings` with the item's ID (or none, for the whole library: about an hour), then `apply_recheck` with its `job_id` once the user has seen the replacements. Several recordings for one item are left alone: never pick one |
| **Missing cover** | `[NOART]`; `get_item` (`via`) | `embed_covers` (Usenet items). An album split in two, one without a cover: `canon_albums` |
| **Playlist drift** (the `.m3u` lacks or has extra tracks against Spotify) | `get_playlist` (entries with `item_id: null`), `[SLOT]`, `[STORE-TAIL]` | No tool: the store is the truth, and a Spotify edit reaches the `.m3u` at the next nightly. A lasting gap is an entry no item holds by Spotify ID or ISRC: get the track in (a download, `replace_item`, a Usenet album) |
| A playlist slot plays the **wrong song** (`get_playlist`'s `item_id` is another recording) | `get_item` on the slot's item (`spotify_ids`, `isrc`, `merged_files`) and on the right one; `find_albums` for it | The slot's item holds the entry's Spotify ID wrongly (often from a wrong merge: its `merged_files` names the entry's song), and an entry resolves by Spotify ID before ISRC. The right item in the library, holding the entry's ISRC: `unlink_spotify_id` with the wrong item's ID and the entry's `song_id`; its dry run says where each entry resolves afterwards. The wrong item holds the entry's ISRC too, or the ID is its `spotify_url`: it is a wrong download, so `replace_item`. Never unlink an ID from the right recording |
| Tick failing, not run, downloads stalled | `library_status`, the tick summary line | See `album-mode`. Mostly no tool: the cause is an indexer, SABnzbd or the egress, which are operator matters |
| Album never downloads | `get_album` (status, attempts, blocklist); `audit_want_albums` | See `album-mode` |

## What the MCP can't do

- **Playlist membership** (`playlists.conf`) stays a homelab PR: it is structural, and its
  flags grant Spotify writes. Search overrides, holds and pins are MCP tools (above).
- **Irreversible operations** (deleting only copies, a bare `music-audit-dupes --apply`,
  `--force`, `--quarantine`, `--allow-manual`, rollbacks) are workstation-only. See `workflow`.
- **Pod and egress problems** (SABnzbd paused, Prowlarr down, the Mullvad exit, YouTube
  cookies) are in homelab's runbooks.
