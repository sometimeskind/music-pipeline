"""The MCP's CLI tools (homelab#2412): their arguments, normalised, and the argv they run.

Normalised arguments are what the dry-run → apply guard compares, so two
spellings of one request are one request: album queries become the album's
key, ID lists are deduplicated and sorted, ``since`` is UTC.  The argv
builders are the only place a tool's command line is written; the argparse
test runs every one of them through its CLI's parser.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import datetime, timezone
from typing import Callable

READ = "read"
WRITE = "write"


class BadRequest(ValueError):
    """Invalid arguments: a 400."""


class Ambiguous(Exception):
    """The album query matched more than one album: nothing is done."""

    def __init__(self, query: str, candidates: list[dict]) -> None:
        super().__init__(query)
        self.query = query
        self.candidates = candidates


class NotFound(Exception):
    def __init__(self, query: str) -> None:
        super().__init__(query)
        self.query = query


Albums = Callable[[], dict[str, dict]]


@dataclasses.dataclass(frozen=True)
class Tool:
    name: str
    tier: str
    # (body without "apply", the album store's records) -> normalised args
    normalise: Callable[[dict, Albums], dict]
    # (normalised args, apply) -> the commands, run in order
    commands: Callable[[dict, bool], list[list[str]]]
    locks: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Argument checks
# ---------------------------------------------------------------------------


def _only(body: dict, *names: str) -> None:
    if unknown := sorted(set(body) - set(names)):
        raise BadRequest(f"unknown argument(s): {', '.join(unknown)}")


def _bool(body: dict, name: str) -> bool:
    value = body.get(name, False)
    if not isinstance(value, bool):
        raise BadRequest(f"{name} must be true or false")
    return value


def _id(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BadRequest(f"{name} must be a positive integer item ID")
    return value


def _budget(body: dict, name: str) -> int | None:
    value = body.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BadRequest(f"{name} must be a non-negative integer or null")
    return value


def _ids(value, name: str) -> list[int]:
    if not isinstance(value, list):
        raise BadRequest(f"{name} must be a list of item IDs")
    return sorted({_id(v, name) for v in value})


def utc_iso(value) -> str:
    """ISO 8601 *value* in UTC (UTC when it has no offset), to the second."""
    if not isinstance(value, str) or not value.strip():
        raise BadRequest("since must be an ISO 8601 time")
    try:
        t = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise BadRequest(f"since is not ISO 8601: {value!r}") from exc
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def album_ref(query: str) -> str:
    """The album argument as the CLIs read it (``albums._album_ref``, homelab#2411):
    an ``open.spotify.com`` album URL reduced to its ID, other text as given."""
    from music_fetch.ids import spotify_key  # noqa: PLC0415

    try:
        return spotify_key(query, "album")
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc


def resolve_album(query, albums: Albums) -> str:
    """The key of the one album *query* names (the CLI's ``<query>``)."""
    from music_fetch.albums import find_albums  # noqa: PLC0415

    if not isinstance(query, str) or not query.strip():
        raise BadRequest("query must be a non-empty string")
    query = album_ref(query.strip())
    hits = find_albums(albums(), query)
    if not hits:
        raise NotFound(query)
    if len(hits) > 1:
        raise Ambiguous(query, [candidate(k, r) for k, r in hits])
    return hits[0][0]


def candidate(key: str, record: dict) -> dict:
    return {"key": key, "artist": record.get("artist"), "name": record.get("name"), "status": record.get("status")}


def youtube_url(ref) -> str:
    """The canonical watch URL for a YouTube video ID or URL (homelab#2411), all yt-dlp is given."""
    from music_fetch import ids  # noqa: PLC0415

    if not isinstance(ref, str) or not ref.strip():
        raise BadRequest("youtube_url must be a YouTube video ID or URL")
    try:
        return ids.youtube_url(ref.strip())
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _apply(argv: list[str], apply: bool, *values: str) -> list[str]:
    """*argv*, ``--apply`` with *apply*, then the free-text *values* after
    ``--``: a value never parses as a flag (an album ID ``--apply`` would turn
    a dry run into an apply)."""
    return [*argv, *(["--apply"] if apply else []), *(["--", *values] if values else [])]


def _audit_dupes(body: dict, _: Albums) -> dict:
    _only(body, "fingerprint_ids")
    ids = body.get("fingerprint_ids")
    return {"fingerprint_ids": None if ids is None else _ids(ids, "fingerprint_ids")}


def _audit_dupes_argv(args: dict, _: bool) -> list[list[str]]:
    ids = args["fingerprint_ids"]
    return [["music-audit-dupes"] if ids is None else ["music-audit-dupes", "--fingerprint", *map(str, ids)]]


def _audit_lengths(body: dict, _: Albums) -> dict:
    _only(body, "silence")
    return {"silence": _bool(body, "silence")}


def _audit_releases(body: dict, _: Albums) -> dict:
    _only(body, "replaced_query")
    query = body.get("replaced_query")
    if query is not None and not isinstance(query, str):
        raise BadRequest("replaced_query must be a string or null")
    return {"replaced_query": None if query is None else query.strip()}


def _audit_releases_argv(args: dict, _: bool) -> list[list[str]]:
    query = args["replaced_query"]
    if query is None:
        return [["music-album-audit-releases"]]
    # --replaced=QUERY: a query that starts with "-" stays the option's value.
    return [["music-album-audit-releases", f"--replaced={query}"]] if query else [["music-album-audit-releases", "--replaced"]]


def _none(body: dict, _: Albums) -> dict:
    _only(body)
    return {}


def _reset_album(body: dict, albums: Albums) -> dict:
    _only(body, "query", "reimport")
    reimport = _bool(body, "reimport")
    return {"album": resolve_album(body.get("query"), albums), "reimport": reimport}


def _requeue_albums(body: dict, _: Albums) -> dict:
    _only(body, "ids", "since")
    ids = body.get("ids")
    since = body.get("since")
    if ids is not None and (not isinstance(ids, list) or not all(isinstance(i, str) and i.strip() for i in ids)):
        raise BadRequest("ids must be a list of album IDs")
    ids = sorted({album_ref(i.strip()) for i in ids or []})
    since = None if since is None else utc_iso(since)
    if not ids and since is None:
        raise BadRequest("give ids, since, or both")
    return {"ids": ids, "since": since}


def _requeue_argv(args: dict, apply: bool) -> list[list[str]]:
    argv = ["music-album-requeue"]
    if args["since"]:
        argv += ["--since", args["since"]]
    return [_apply(argv, apply, *args["ids"])]


def _merge_items(body: dict, _: Albums) -> dict:
    _only(body, "pairs")
    pairs = body.get("pairs")
    if not isinstance(pairs, list) or not pairs:
        raise BadRequest("pairs must be a non-empty list of [keep_id, drop_id]")
    out: list[list[int]] = []
    for pair in pairs:
        if not isinstance(pair, list) or len(pair) != 2:
            raise BadRequest("each pair must be [keep_id, drop_id]")
        keep, drop = _id(pair[0], "keep_id"), _id(pair[1], "drop_id")
        if keep == drop:
            raise BadRequest(f"pair [{keep}, {drop}] keeps and drops the same item")
        if [keep, drop] not in out:
            out.append([keep, drop])
    for keep, drop in out:
        if sum(drop in p for p in out) > 1:
            raise BadRequest(f"item {drop} is dropped by one pair and appears in another")
    return {"pairs": out}


def _replace_item(body: dict, _: Albums) -> dict:
    _only(body, "id", "youtube_url")
    item = _id(body.get("id"), "id")
    return {"id": item, "youtube_url": youtube_url(body.get("youtube_url"))}


# ---- search overrides, holds and pins (homelab#2484): music-overrides ----

MAX_TEXT = 500


def _text(body: dict, name: str, required: bool = False) -> str:
    value = body.get(name, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise BadRequest(f"{name} must be a string")
    value = " ".join(value.split())
    if required and not value:
        raise BadRequest(f"{name} must be a non-empty string")
    if len(value) > MAX_TEXT:
        raise BadRequest(f"{name} is over {MAX_TEXT} characters")
    return value


def search_words(value) -> str:
    """The search words as the tick searches them (``usenet.normalise``)."""
    from music_fetch.usenet import normalise  # noqa: PLC0415

    if not isinstance(value, str) or not (words := normalise(value)):
        raise BadRequest("words must hold readable search words")
    return words


def track_ref(value) -> str:
    """A Spotify track ID, from an ID or an ``open.spotify.com/track`` URL."""
    from music_fetch.ids import spotify_key  # noqa: PLC0415

    if not isinstance(value, str) or not value.strip():
        raise BadRequest("track must be a Spotify track ID or URL")
    try:
        track = spotify_key(value.strip(), "track")
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    if not re.fullmatch(r"[0-9A-Za-z]{22}", track):
        raise BadRequest(f"{value!r} is not a Spotify track ID or URL")
    return track


def pin_url(value) -> str:
    """A YouTube or YouTube Music video's canonical URL (``overrides.pin_source``): nothing else reaches yt-dlp."""
    from music_fetch.overrides import pin_source  # noqa: PLC0415

    if not isinstance(value, str) or not value.strip():
        raise BadRequest("url must be a YouTube or YouTube Music video URL")
    try:
        return pin_source(value)
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc


def _set_search(body: dict, albums: Albums) -> dict:
    _only(body, "album", "words", "reason")
    return {"album": resolve_album(body.get("album"), albums), "words": search_words(body.get("words")),
            "reason": _text(body, "reason")}


def _album_only(body: dict, albums: Albums) -> dict:
    _only(body, "album")
    return {"album": resolve_album(body.get("album"), albums)}


def _hold(body: dict, albums: Albums) -> dict:
    _only(body, "album", "reason")
    return {"album": resolve_album(body.get("album"), albums), "reason": _text(body, "reason", required=True)}


def _pin(body: dict, _: Albums) -> dict:
    _only(body, "track", "url", "label", "reason")
    return {"track": track_ref(body.get("track")), "url": pin_url(body.get("url")), "label": _text(body, "label"),
            "reason": _text(body, "reason")}


def _track_only(body: dict, _: Albums) -> dict:
    _only(body, "track")
    return {"track": track_ref(body.get("track"))}


def _overrides(command: str, *flags: str) -> Callable[[dict, bool], list[list[str]]]:
    """``music-overrides <command>`` with *flags* (``--name=value`` from the args), then the positional args."""
    positional = {"search": ("album", "words"), "clear-search": ("album",), "hold": ("album",),
                  "release": ("album",), "pin": ("track", "url"), "unpin": ("track",)}[command]

    def argv(args: dict, apply: bool) -> list[list[str]]:
        options = [f"--{flag}={args[flag]}" for flag in flags if args.get(flag)]
        return [_apply(["music-overrides", command, *options], apply, *(args[p] for p in positional))]
    return argv


def _canon_albums(body: dict, _: Albums) -> dict:
    _only(body, "refresh", "search_budget", "mb_budget")
    return {"refresh": _bool(body, "refresh"), "search_budget": _budget(body, "search_budget"),
            "mb_budget": _budget(body, "mb_budget")}


def _canon_argv(args: dict, apply: bool) -> list[list[str]]:
    argv = ["music-canon-albums"]
    if args["refresh"]:
        argv.append("--refresh")
    if args["search_budget"] is not None:
        argv += ["--search-budget", str(args["search_budget"])]
    if args["mb_budget"] is not None:
        argv += ["--mb-budget", str(args["mb_budget"])]
    return [_apply(argv, apply)]


TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("audit_dupes", READ, _audit_dupes, _audit_dupes_argv),
    Tool("audit_lengths", READ, _audit_lengths,
         lambda a, _: [["music-audit-lengths", "--silence"] if a["silence"] else ["music-audit-lengths"]]),
    Tool("audit_releases", READ, _audit_releases, _audit_releases_argv),
    Tool("audit_want_albums", READ, _none, lambda a, _: [["music-audit-want-albums"]]),
    Tool("reset_album", WRITE, _reset_album,
         lambda a, apply: [_apply(["music-album-reset", *(["--reimport"] if a["reimport"] else [])], apply,
                                  a["album"])],
         ("albums", "pipeline")),
    Tool("requeue_albums", WRITE, _requeue_albums, _requeue_argv, ("albums", "pipeline")),
    Tool("merge_items", WRITE, _merge_items,
         lambda a, apply: [_apply(["music-audit-dupes", "--merge", str(k), str(d)], apply) for k, d in a["pairs"]],
         ("pipeline",)),
    Tool("replace_item", WRITE, _replace_item,
         lambda a, apply: [_apply(["music-audit-lengths", "--replace", str(a["id"]), "--youtube", a["youtube_url"]],
                                  apply)],
         ("pipeline",)),
    Tool("embed_covers", WRITE, _none, lambda a, apply: [_apply(["music-embed-covers"], apply)], ("pipeline",)),
    Tool("canon_albums", WRITE, _canon_albums, _canon_argv, ("pipeline",)),
    # Rows of their own tables, which the tick reads at its start: no lock.
    Tool("set_search_override", WRITE, _set_search, _overrides("search", "reason")),
    Tool("clear_search_override", WRITE, _album_only, _overrides("clear-search")),
    Tool("hold_album", WRITE, _hold, _overrides("hold", "reason")),
    Tool("release_album", WRITE, _album_only, _overrides("release")),
    Tool("pin_track", WRITE, _pin, _overrides("pin", "label", "reason")),
    Tool("unpin_track", WRITE, _track_only, _overrides("unpin")),
]}
