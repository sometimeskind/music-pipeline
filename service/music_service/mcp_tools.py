"""The MCP's CLI tools (homelab#2412): their arguments, normalised, and the argv they run.

Normalised arguments are what the dry-run → apply guard compares, so two
spellings of one request are one request: album queries become the album's
key, ID lists are deduplicated and sorted, ``since`` is UTC.  The argv
builders are the only place a tool's command line is written; the argparse
test runs every one of them through its CLI's parser.
"""

from __future__ import annotations

import dataclasses
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


class NotImplementedYet(Exception):
    """A tool waiting on another change: a 501."""


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
    """The album query as the CLI reads it."""
    # TODO(homelab#2411): music_fetch.ids.spotify_key(query, "album"), as
    # music-album-reset's argparse does once #2411 lands: reduces an
    # open.spotify.com/album URL to its ID and raises ValueError on other URLs.
    return query


def resolve_album(query, albums: Albums) -> str:
    """The key of the one album *query* names (the CLI's ``<query>``)."""
    from music_fetch.albums import find_albums  # noqa: PLC0415

    if not isinstance(query, str) or not query.strip():
        raise BadRequest("query must be a non-empty string")
    try:
        query = album_ref(query.strip())
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    hits = find_albums(albums(), query)
    if not hits:
        raise NotFound(query)
    if len(hits) > 1:
        raise Ambiguous(query, [candidate(k, r) for k, r in hits])
    return hits[0][0]


def candidate(key: str, record: dict) -> dict:
    return {"key": key, "artist": record.get("artist"), "name": record.get("name"), "status": record.get("status")}


def youtube_url(ref) -> str:
    """The canonical watch URL for a YouTube video ID or URL."""
    # TODO(homelab#2411): return music_fetch.ids.youtube_url(ref), raising
    # BadRequest on its ValueError; replace_item stays a 501 until then.
    raise NotImplementedYet("replace_item waits for homelab#2411's YouTube ID parser")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _apply(argv: list[str], apply: bool) -> list[str]:
    return [*argv, "--apply"] if apply else argv


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
    return [["music-album-audit-releases", "--replaced", query]] if query else [["music-album-audit-releases", "--replaced"]]


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
    ids = sorted({i.strip() for i in ids or []})
    since = None if since is None else utc_iso(since)
    if not ids and since is None:
        raise BadRequest("give ids, since, or both")
    return {"ids": ids, "since": since}


def _requeue_argv(args: dict, apply: bool) -> list[list[str]]:
    argv = ["music-album-requeue", *args["ids"]]
    if args["since"]:
        argv += ["--since", args["since"]]
    return [_apply(argv, apply)]


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
         lambda a, apply: [_apply(["music-album-reset", a["album"], *(["--reimport"] if a["reimport"] else [])],
                                  apply)],
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
]}
