# Requesting albums

`request_album` asks for whole albums by MusicBrainz **release group**. The album tick then
searches Usenet for each one and imports it whole, as it does an album playlist's. A request
is not tied to Spotify: the tracklist the search and the import check against comes from
MusicBrainz.

## Choosing the release group

- A release group is the album in every edition: the original, its remasters and deluxe
  versions. Usenet decides which edition arrives. Request the album's own release group: not a
  single, EP, compilation or live album of the same songs, unless that is what is wanted.
- The user gives it as an MBID or a musicbrainz.org release-group URL. Don't look a library
  title up on MusicBrainz or the web yourself; ask the user for the link.
- Several albums go in one call (at most 25). Each costs two or three MusicBrainz reads at one
  a second, so a long list outlasts the inline wait: poll `get_job`.

## A preferred release

Give a preferred release (a release of that group, as an MBID or musicbrainz.org release URL)
only when the edition matters: a deluxe edition's bonus tracks, an original mix rather than a
remaster. Its tracklist is then the album's: the track count the search ranks releases by, and
the titles the import checks a download against. Never prefer a demo, live, remix or
bootleg release for a studio album.

Without one, the tracklist is the earliest official release with the group's most common track
count. The dry run names that release and the other counts it saw (`also: 14 tracks ×3`):
when the count you want is among the others, request again with a release that has it.

## The dry run, then the apply

The dry run shows, per album: its artist, title, type and year; the tracklist (tracks, how
many have an ISRC, the release it came from and why); and what the next tick does with it:

| Album line | Meaning |
|------------|---------|
| none has the release group yet | A new album, `wanted` at the next tick, searched by the query shown |
| … the whole album is wanted, not only its N wanted track(s) | A playlist's want list had queued some of its tracks; the request takes that album over |
| … on <playlist>; the library holds it | Already held: nothing is downloaded. The request keeps the album when it leaves the playlist |
| requested already | Nothing changes |

A missing release group or release, or a preferred release of another group, fails the whole
call and writes nothing. Apply only once the user agrees with the dry run.

## After the request

`list_requests` shows each request with its album's status: `pending` until the next tick
(within 30 min, or `trigger_album_tick`), then the album's own (`wanted`, `grabbed`,
`imported`, `have`, …). Each request is an event in `album_history`, which
finds the album by its release group once the tick has queued it.

A request that misses stays **missing**: there is no spotdl fallback for a request yet. It is
searched again after 7 days. When the search words are wrong (the log's `[MISS]` names the
query), `set_search_override` on the album fixes them; `digest_summary` lists missing and
failed requests under `requests`.

## Later

`request_album` with `later` also appends the albums to Later, in the order given. An album
requested already is only appended, so `later` is also how an album already asked for (or
already in the library) goes on Later. The dry run's `Later:` line per album says what happens:

| Later line | Meaning |
|------------|---------|
| appended; the library holds it | It joins the end of Later's `.m3u` at the next scan |
| appended; it joins the end of Later once the library holds it whole | Pending: listed in `get_later`, not in the `.m3u` until its import |
| on it already | Nothing changes: an album never moves |

Later's `.m3u` is in **arrival order**: an album is placed when the library first holds it
whole, not when it was asked for, so one that downloads late lands at the end rather than
behind where the user is listening. A reset or reimport doesn't move it.

`get_later` shows the queue: positions 1, 2, … are the `.m3u`'s albums, oldest arrival first,
then the pending ones with their album's status. `remove_from_later` takes albums off it, by
release group, or `through=N` for the first N (the ones listened to). Removing touches no files:
the album stays in the library. Both writes are a dry run first; the `.m3u` changes at the next
scan (`trigger_scan` to have it now).

Until the operator's cutover from the Spotify `later` playlist (homelab#2478 stage 3), that
playlist's albums come first in `later.m3u` and the queue's follow; `get_later` and
`remove_from_later` see only the queue's.
