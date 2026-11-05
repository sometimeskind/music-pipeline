"""music-check-release: the matcher's verdict on one release for one album (homelab#2661).

Takes an album and a release title, and runs :func:`~music_fetch.usenet.rejection`
on them with what the tick passes (:func:`~music_fetch.albums.plan`,
:func:`~music_fetch.albums.track_count`, :func:`~music_fetch.albums.match_args`),
so the answer is the tick's: accepted, or the first rule the title fails.  It also
says what the tick would search and whether a stored override applied.

``--words`` simulates a search override through ``plan()``, in place of any stored
one, so the verdict shows what the override would really do: no artist, so the
``artist words`` rule is off, and the title must hold every word.

Without ``--size`` the size rule can't be judged, and the output says so: the
release is given the bottom of the album's range, which that rule passes whatever
order the rules run in.

The matcher is all it runs.  The blocklist and the releases other albums took go
by GUID, which a title doesn't have, and an accepted release still has to outrank
the others.  Read-only: the album store, no indexer call, no writes, no events.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging

from music_fetch import albums
from music_fetch.usenet import Rejection, Release, edition, normalise, rejection, size_range

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class Check:
    """One release title against one album."""

    key: str
    record: dict
    title: str
    search: albums.Plan
    stored: str | None
    """The album's stored override."""
    words: str
    """The override simulated, empty for none."""
    why: Rejection | None
    size: int | None
    low: int
    high: int

    @property
    def size_judged(self) -> bool:
        """Whether the size rule ran on a real size: it is the matcher's last, so a refusal by any other came first."""
        return self.size is not None and self.high > 0 and (self.why is None or self.why.rule == "size")


def check(key: str, record: dict, title: str, overrides: dict[str, str], size: int | None = None,
          words: str = "") -> Check:
    """The matcher's verdict on *title* for the album, with *overrides* (by album
    key) as the tick reads them, or the override *words* in their place."""
    search = albums.plan(key, record, {key: words} if words else overrides)
    tracks = albums.track_count(record)
    matching = albums.match_args(record, search)
    low, high = size_range(tracks, matching["seconds"])
    release = Release(guid="", title=title, size=low if size is None else size, indexer_id=0, download_url="",
                      categories=[])
    why = rejection(release, search.artist, search.album, tracks, **matching)
    return Check(key, record, title, search, albums.override_words(key, record, overrides), words, why, size, low, high)


def _megabytes(n: int) -> str:
    return f"{n / 1e6:.0f} MB"


def _verdict(c: Check) -> str:
    if c.why:
        return f"refused by {c.why}. The matcher stops at the first rule a title fails, so later rules weren't run"
    return "accepted" if c.size_judged or not c.high else "accepted on the title; size not checked"


def _search(c: Check) -> str:
    if not c.search.queries:
        return "none: the album's name has no readable words, so the tick won't search it ([NOWORDS]) without an override"
    queries = ", then ".join(f'"{q}"' for q in c.search.queries)
    return queries + (": any release by the artist matches, on type and size alone" if c.search.any_album else "")


def _override(c: Check) -> str:
    if c.words:
        return f'simulated "{c.words}", ' + (f'in place of the stored "{c.stored}"' if c.stored else "none stored")
    return f'stored "{c.stored}" applied' if c.stored else "none stored"


def _size(c: Check) -> str:
    fits = f"this album takes {_megabytes(c.low)} to {_megabytes(c.high)}"
    if not c.high:
        return "not checked: the album's length and track count are unknown, so the tick has no size rule for it"
    if c.size_judged:
        return f"checked: {_megabytes(c.size or 0)}, and {fits}"
    return f"not checked: {'the title was refused first' if c.size is not None else 'no size given'}; {fits}"


def log_check(c: Check) -> None:
    """The verdict, what it was run on, then what it doesn't cover."""
    logger.info('[CHECK] %s: %s — %s (%s): "%s"', c.key, c.record.get("artist"), c.record.get("name"),
                c.record.get("status"), c.title)
    logger.info("Verdict: %s", _verdict(c))
    logger.info("Search: %s", _search(c))
    logger.info("Override: %s", _override(c))
    if c.words or c.stored:
        logger.info("With an override there is no artist: a title must start with one of its words and hold them all "
                    "(a leading - excludes nothing), and the artist words and self-titled rules don't run")
    logger.info("Size: %s", _size(c))
    if c.record.get("edition") is not None:
        logger.info("Edition: an incomplete copy is in the library, so the tick takes only its edition (%s); this "
                    "title's is (%s)", " ".join(c.record["edition"]) or "plain", " ".join(sorted(edition(c.title))) or "plain")
    logger.info("Not covered: the blocklist and releases other albums took (they go by GUID), and whether the indexer "
                "has this release. An accepted release still has to outrank the others")


def run(album: str, title: str, size: int | None = None, words: str = "") -> Check:
    """Check *title* against the one album *album* names."""
    state = albums.State.load()
    hits = albums.find_albums(state.albums, albums._album_ref(album))
    if len(hits) != 1:
        logger.error("%d album(s) match %r", len(hits), album)
        raise SystemExit(1)
    key, record = hits[0]
    result = check(key, record, title, albums._search_overrides(state), size, words)
    log_check(result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(prog="music-check-release", description=__doc__.splitlines()[0])
    parser.add_argument("--album", required=True, help="the album: its key, Spotify ID, release group or name")
    parser.add_argument("--title", required=True, help="the release title")
    parser.add_argument("--size", type=int, help="the release's size in bytes (default: the size rule isn't checked)")
    parser.add_argument("--words", help="simulate this search override, in place of any stored one")
    args = parser.parse_args()
    words = "" if args.words is None else normalise(args.words)
    if args.words is not None and not words:
        parser.error("--words holds no readable search words")
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    run(args.album, args.title, args.size, words)
