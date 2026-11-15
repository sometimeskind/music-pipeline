"""Embed Spotify's album cover on Usenet album tracks (#204).

Usenet imports end up with no art: the convert command's ``-vn`` drops a
FLAC's embedded picture, and ``fetchart``/``embedart`` act on album imports
only, never on singletons.  The cover comes from the same place spotdl's
does: every playlist store song carries ``cover_url``, the album's largest
Spotify image, so no Spotify API calls are needed.

An item finds its cover by its ``spotify_ids`` (set after the import by
``tag_album_ids``).  An item with none (a ``[NOID]`` track) borrows the cover
of another item on the same album.  Items that already have art are left
alone.

An album no playlist store lists (homelab#2825: it came in through the wants
or library feed) falls back to the Cover Art Archive's front image of its
``mb_albumid``, one request per release.  A release with no front image (a
404) falls back in turn to the front image the archive picks for its release
group (``mb_releasegroupid``, homelab#2857), often another edition's.  Each
404 is marked on the items, the release's as ``caa_none`` and the group's as
``caa_group_none`` (the MBID and the date), so neither is asked again until
:data:`CAA_RETRY_DAYS` have passed or the item's release (or group) changes;
the two marks are independent, so a release marked before the group fallback
existed gets its group asked at the next run.  A network error or a timeout
marks nothing, and a release that failed so isn't followed by its group.  A
dry run makes no request: it lists the releases and groups it would try.

At import the album completion embeds the covers of the tracks it imported.
``music-embed-covers`` backfills every ``via=usenet`` item with no art: dry
run by default, ``--apply`` writes.  Safe to re-run.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta, timezone

from music_fetch import events
from music_scan.identity import item_spotify_ids, spotify_id

logger = logging.getLogger(__name__)

DOWNLOAD_TIMEOUT = 30
# Sent with every cover download (the Spotify CDN and the Cover Art Archive); no personal data.
USER_AGENT = "music-pipeline-covers/1.0 ( https://github.com/sometimeskind/music-pipeline )"
# The same front-500 image the request feed takes (music_fetch.album_requests).
CAA_URL = "https://coverartarchive.org/release/{}/front-500"
# The release group's front image: the archive's pick among its releases' fronts
# (homelab#2857).  Like the release's, a 307 to the image, or a 404 when there is none.
CAA_GROUP_URL = "https://coverartarchive.org/release-group/{}/front-500"
# Flexible field on an item: "<release MBID> <YYYY-MM-DD>", the day the Cover Art
# Archive answered 404 for the item's release (homelab#2825).
CAA_NONE = "caa_none"
# Flexible field on an item: "<release group MBID> <YYYY-MM-DD>", the day the Cover
# Art Archive answered 404 for the item's release group too (homelab#2857).
CAA_GROUP_NONE = "caa_group_none"
# Art gets added to releases over time: a 404 is asked again after this many days.
CAA_RETRY_DAYS = 30
_MBID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def covers_by_id() -> dict[str, str]:
    """``{spotify track id: cover_url}`` from every playlist store (homelab#1914)."""
    from music_fetch import store  # noqa: PLC0415

    covers: dict[str, str] = {}
    for songs in store.songs_by_playlist().values():
        for song in songs:
            sid = song.get("song_id") or spotify_id(song.get("url"))
            url = song.get("cover_url")
            if sid and isinstance(url, str) and url.startswith("https://"):
                covers.setdefault(sid, url)
    return covers


def _path(item) -> str:
    return os.fsdecode(item.path)


def has_art(item) -> bool:
    """True when the file has an embedded image, or can't be read (nothing to embed into)."""
    from mediafile import MediaFile  # noqa: PLC0415

    try:
        return bool(MediaFile(_path(item)).images)
    except Exception as exc:
        logger.warning("  [NOART] %s: unreadable: %s", _path(item), exc)
        return True


def _album(item) -> tuple[str, str]:
    return ((item.albumartist or item.artist or "").lower(), (item.album or "").lower())


def plan_covers(items: Iterable, covers: dict[str, str]) -> tuple[dict[int, tuple[object, str]], list]:
    """Usenet items with no art → their cover URL.

    Returns ``({item.id: (item, url)}, [items with no cover found])``.
    """
    bare = [i for i in items if i.get("via") == "usenet" and not has_art(i)]
    planned: dict[int, tuple[object, str]] = {}
    album_cover: dict[tuple[str, str], str] = {}
    for item in bare:
        url = next((covers[s] for s in sorted(item_spotify_ids(item)) if s in covers), None)
        if url:
            planned[item.id] = (item, url)
            album_cover.setdefault(_album(item), url)
    missing = []
    for item in bare:
        if item.id in planned:
            continue
        url = album_cover.get(_album(item))
        if url:
            planned[item.id] = (item, url)
        else:
            missing.append(item)
    return planned, missing


def download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:  # noqa: S310 — https only: a store or CAA URL
        return resp.read()


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _mbid(item, field: str) -> str | None:
    mbid = str(item.get(field) or "").strip().lower()
    return mbid if _MBID.fullmatch(mbid) else None


def _release(item) -> str | None:
    return _mbid(item, "mb_albumid")


def _group(item) -> str | None:
    return _mbid(item, "mb_releasegroupid")


def caa_checked(item, mbid: str, today: date, field: str = CAA_NONE) -> str | None:
    """The day the Cover Art Archive had no front image for *mbid*, as marked on
    *item* in *field* (:data:`CAA_NONE` for a release, :data:`CAA_GROUP_NONE` for
    a release group), while that is under :data:`CAA_RETRY_DAYS` old; else None."""
    mark = str(item.get(field) or "").split()
    if len(mark) != 2 or mark[0] != mbid:
        return None
    try:
        when = date.fromisoformat(mark[1])
    except ValueError:
        return None
    return mark[1] if today - when < timedelta(days=CAA_RETRY_DAYS) else None


Lookup = tuple[str | None, str | None]


def plan_caa(missing: Iterable, today: date) -> tuple[dict[Lookup, list], list[tuple[object, str]]]:
    """Items no store cover matched → ``({(release, group): [items]}, [(item, why it has no cover)])``.

    The key names what to ask the Cover Art Archive, in order: the release
    (None once it is marked as having no front image), then the release group
    (None when the item has none or it is marked too).  An item with no
    ``mb_albumid`` or ``mb_releasegroupid`` borrows that of another such item
    on its album.  A mark counts until it is :data:`CAA_RETRY_DAYS` old.
    """
    missing = list(missing)
    album_release: dict[tuple[str, str], str] = {}
    album_group: dict[tuple[str, str], str] = {}
    for item in missing:
        if mbid := _release(item):
            album_release.setdefault(_album(item), mbid)
        if mbid := _group(item):
            album_group.setdefault(_album(item), mbid)
    planned: dict[Lookup, list] = {}
    left: list[tuple[object, str]] = []
    for item in missing:
        release = _release(item) or album_release.get(_album(item))
        group = _group(item) or album_group.get(_album(item))
        if not release and not group:
            left.append((item, "no matching playlist store entry, and no MusicBrainz release"))
            continue
        release_when = release and caa_checked(item, release, today)
        group_when = group and caa_checked(item, group, today, CAA_GROUP_NONE)
        if (release and not release_when) or (group and not group_when):
            planned.setdefault((None if release_when else release, None if group_when else group), []).append(item)
            continue
        checked = [f"release {release} (checked {release_when})"] if release else []
        checked += [f"{'its ' if release else ''}release group {group} (checked {group_when})"] if group else []
        left.append((item, "no matching playlist store entry, and the Cover Art Archive has no front image "
                           f"for {' or '.join(checked)}"))
    return planned, left


def embed(item, data: bytes) -> None:
    """Write *data* as the item file's front cover."""
    from mediafile import Image, ImageType, MediaFile  # noqa: PLC0415

    mf = MediaFile(_path(item))
    mf.images = [Image(data=data, type=ImageType.front)]
    mf.save()


def _label(album: tuple[str, str]) -> str:
    return f"{album[0] or '?'} — {album[1] or '?'}"


def _record(done: list, cover_source: str, mbid: str | None = None, group: str | None = None) -> None:
    """Log the album's ``[ART]`` line and record its ``cover_embedded`` event."""
    first = done[0]
    name = f"{first.get('albumartist') or first.get('artist') or '?'} — {first.get('album') or '?'}"
    if group:
        message = (f"[ART] {name}: embedded the Cover Art Archive front image of release group {group} "
                   f"on {len(done)} track(s)")
    elif mbid:
        message = f"[ART] {name}: embedded the Cover Art Archive front image (release {mbid}) on {len(done)} track(s)"
    else:
        message = f"[ART] {name}: embedded the cover on {len(done)} track(s)"
    logger.info("  %s", message)
    events.record("cover_embedded", message, source="cover", item_id=first.id if len(done) == 1 else None,
                  spotify_album_id=events.spotify_album(first.get("spotify_album_id")),
                  release_group=group or first.get("mb_releasegroupid") or None,
                  data={"album": name, "items": [i.id for i in done], "cover_source": cover_source,
                        **({"mb_albumid": mbid} if mbid else {}),
                        **({"mb_releasegroupid": group} if group else {})})


def _embed_each(items: Iterable, data: bytes) -> list:
    done = []
    for item in items:
        try:
            embed(item, data)
        except Exception as exc:
            logger.warning("  [NOART] %s: embedding failed: %s", _path(item), exc)
            continue
        done.append(item)
    return done


def _mark_no_front(items: Iterable, field: str, mbid: str, today: date) -> None:
    what = "release group" if field == CAA_GROUP_NONE else "release"
    for item in items:
        try:
            item[field] = f"{mbid} {today.isoformat()}"
            item.store()
        except Exception as exc:
            logger.warning("  [NOART] %s: could not mark the %s as having no front image: %s", _path(item), what, exc)


def _ask_caa(fetch: Callable[[str], bytes], url: str, label: str) -> tuple[bytes | None, bool]:
    """``(image, False)``, or ``(None, True)`` on a 404 (no front image), or
    ``(None, False)`` when the request failed (a timeout, a network error, any
    other status): that marks nothing, so the next run asks again."""
    try:
        return fetch(url), False
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None, True
        logger.warning("  [NOART] %s: Cover Art Archive download failed (%s): %s", label, url, exc)
    except Exception as exc:
        logger.warning("  [NOART] %s: Cover Art Archive download failed (%s): %s", label, url, exc)
    return None, False


def embed_covers(
    items: Iterable,
    covers: dict[str, str],
    apply: bool = True,
    fetch: Callable[[str], bytes] | None = None,
) -> int:
    """Embed the album cover on every usenet item in *items* with no art.

    The cover is the playlist store's Spotify cover, else the Cover Art
    Archive's front image of the item's release (homelab#2825), else that of
    its release group (homelab#2857), each fetched once, one after another.
    Logs one ``[ART]`` line per album and
    ``[NOART]`` per item with no cover.  A failed download or write is logged
    and skipped: the backfill retries it.  A dry run fetches nothing and
    writes nothing.  Returns the count of items embedded (on a dry run, that
    would get a store cover: whether the Cover Art Archive has one isn't asked).
    """
    fetch = fetch or download
    today = _today()
    planned, missing = plan_covers(items, covers)
    caa, left = plan_caa(missing, today)
    for item, why in left:
        logger.warning("  [NOART] %s: no cover found (%s)", _path(item), why)

    by_album: dict[tuple[str, str], list[tuple[object, str]]] = {}
    for item, url in planned.values():
        by_album.setdefault(_album(item), []).append((item, url))

    images: dict[str, bytes | None] = {}
    count = 0
    for album, entries in by_album.items():
        if not apply:
            logger.info("  [ART] %s: would embed the cover on %d track(s)", _label(album), len(entries))
            count += len(entries)
            continue
        done = []
        for item, url in entries:
            if url not in images:
                try:
                    images[url] = fetch(url)
                except Exception as exc:
                    logger.warning("  [NOART] %s: cover download failed (%s): %s", _label(album), url, exc)
                    images[url] = None
            if images[url] is None:
                continue
            done += _embed_each([item], images[url])
        if done:
            _record(done, "store")
        count += len(done)

    answers: dict[str, tuple[bytes | None, bool]] = {}  # two lookups can share a release group

    def ask(url: str, label: str) -> tuple[bytes | None, bool]:
        if url not in answers:
            answers[url] = _ask_caa(fetch, url, label)
        return answers[url]

    for (release, group), tracks in caa.items():
        label = _label(_album(tracks[0]))
        if not apply:
            what = " then ".join([*([f"release {release}"] if release else []),
                                  *([f"release group {group}"] if group else [])])
            logger.info("  [ART] %s: would try the Cover Art Archive (%s) for %d track(s)", label, what, len(tracks))
            continue
        if release:
            data, no_front = ask(CAA_URL.format(release), label)
            if data is not None:
                done = _embed_each(tracks, data)
                if done:
                    _record(done, "caa", release)
                count += len(done)
                continue
            if not no_front:  # failed: the next run asks the release again before its group
                continue
            logger.warning("  [NOART] %s: the Cover Art Archive has no front image for release %s "
                           "(asked again in %d days)%s", label, release, CAA_RETRY_DAYS,
                           "; trying its release group" if group else "")
            _mark_no_front(tracks, CAA_NONE, release, today)
        if group:
            data, no_front = ask(CAA_GROUP_URL.format(group), label)
            if data is not None:
                done = _embed_each(tracks, data)
                if done:
                    _record(done, "caa_group", group=group)
                count += len(done)
            elif no_front:
                logger.warning("  [NOART] %s: the Cover Art Archive has no front image for release group %s "
                               "(asked again in %d days)", label, group, CAA_RETRY_DAYS)
                _mark_no_front(tracks, CAA_GROUP_NONE, group, today)
    if caa and not apply:
        logger.info("Would try the Cover Art Archive for %d album(s) (%d track(s)) with no playlist store cover",
                    len(caa), sum(len(g) for g in caa.values()))
    return count


def run(apply: bool = False) -> int:
    from music_scan.library import LIBRARY_DB, LIBRARY_DIR, MusicLibrary  # noqa: PLC0415
    from music_scan.navidrome import trigger_scan  # noqa: PLC0415

    covers = covers_by_id()
    with MusicLibrary(LIBRARY_DB, LIBRARY_DIR) as lib:
        items = [i for i in lib.all_items() if i.get("via") == "usenet"]
        logger.info("%d usenet item(s); %d cover URL(s) in the playlist stores", len(items), len(covers))
        count = embed_covers(items, covers, apply=apply)
    if not apply:
        logger.info("Dry run: %d item(s) would get a playlist store cover. Re-run with --apply to write.", count)
        return count
    logger.info("Embedded the cover on %d item(s)", count)
    if count:
        trigger_scan()
    return count


def main() -> None:
    parser = argparse.ArgumentParser(prog="music-embed-covers", description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the covers (default: dry run)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    run(apply=args.apply)
