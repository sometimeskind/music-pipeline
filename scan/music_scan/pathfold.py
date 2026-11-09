"""Library paths as Navidrome tells them apart (homelab#2742).

Navidrome v0.64.2 links a ``.m3u`` line to a file with SQLite's ``NOCASE``
over the NFC and NFD forms of the path (``core/playlists/parse_m3u.go``), so
two files whose paths differ only in ASCII case, or only in Unicode
normalisation, are one path to it: a playlist links whichever the query
returns.  :func:`fold` is that comparison.  The library's filesystem is case
sensitive, so ``os.path.exists`` and beets' ``unique_path`` don't see such a
*case twin*: the import (:func:`music_scan.music_pipeline.unique_folded`)
and canon (:mod:`music_scan.canon`) check the folded paths of the library's
items too, and ``music-audit-case-twins`` lists the twins already there.
"""

from __future__ import annotations

import os
import unicodedata

_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def fold(path) -> str:
    """*path* as Navidrome matches a ``.m3u`` line to a file (v0.64.2,
    ``core/playlists/parse_m3u.go``): Unicode-normalized, compared with SQLite's
    ``NOCASE``, which folds ASCII letters only."""
    if isinstance(path, (bytes, memoryview)):
        path = os.fsdecode(bytes(path))
    return unicodedata.normalize("NFC", str(path)).translate(_ASCII_LOWER)


def library_paths(lib) -> dict[str, dict[int, str]]:
    """Every item of the beets library *lib* by the :func:`fold` of its path:
    ``{folded path: {item ID: absolute path}}``.  One query, the paths as
    stored (relative to the library root since beets 2.10, made absolute)."""
    directory = os.fsdecode(lib.directory)
    with lib.transaction() as tx:
        rows = tx.query("SELECT id, path FROM items")
    out: dict[str, dict[int, str]] = {}
    for item_id, raw in rows:
        if not raw:
            continue
        path = os.path.join(directory, os.fsdecode(bytes(raw)) if not isinstance(raw, str) else raw)
        out.setdefault(fold(path), {})[item_id] = path
    return out


def held_by(paths: dict[str, dict[int, str]], dest, own: int | None = None) -> dict[int, str]:
    """The items other than *own* whose path folds to *dest*'s, from :func:`library_paths`."""
    return {i: p for i, p in paths.get(fold(dest), {}).items() if i != own}
