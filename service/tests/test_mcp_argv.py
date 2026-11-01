"""Every MCP tool's argv is accepted by its CLI's argparse (homelab#2412).

Parsing only: ``parse_args`` is wrapped so that once the real parser has
accepted the argv, it raises before the CLI does anything.  A flag the CLI
renames or drops fails here, not in production.
"""

from __future__ import annotations

import argparse
import sys
from importlib.metadata import entry_points

import pytest

from music_service.mcp_tools import TOOLS, WRITE


class Parsed(Exception):
    pass


# Normalised args per tool, every flag the builder can emit covered once.
CASES = {
    "audit_dupes": [{"fingerprint_ids": None}, {"fingerprint_ids": []}, {"fingerprint_ids": [3, 7]}],
    "audit_lengths": [{"silence": False}, {"silence": True}],
    "audit_releases": [{"replaced_query": None}, {"replaced_query": ""}, {"replaced_query": "Artist — Album"},
                       {"replaced_query": "--apply"}],
    "audit_want_albums": [{}],
    "recheck_recordings": [{"ids": [], "resume": None}, {"ids": [5672, 6994], "resume": None},
                           {"ids": [], "resume": "3f2a9c0d1e2b4a5c"}],
    "apply_recheck": [{"run_id": "3f2a9c0d1e2b4a5c"}],
    "search_releases": [{"query": "band the album", "limit": 5}, {"query": "--apply -x", "limit": 10}],
    "reset_album": [{"album": "0ETFjACtuP2ADo6LFhL6HN", "reimport": False},
                    {"album": "0ETFjACtuP2ADo6LFhL6HN", "reimport": True},
                    {"album": "-m-/le baptême", "reimport": False}],  # a local album's key
    "requeue_albums": [{"ids": ["0ETFjACtuP2ADo6LFhL6HN"], "since": None},
                       {"ids": [], "since": "2026-10-20T00:00:00+00:00"},
                       {"ids": ["A", "B"], "since": "2026-10-20T00:00:00+00:00"},
                       {"ids": ["--apply", "-x"], "since": None}],  # hostile IDs stay positional
    "merge_items": [{"pairs": [[1, 2]]}, {"pairs": [[1, 2], [3, 4]]}],
    "replace_item": [{"id": 10001, "youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}],
    "unlink_spotify_id": [{"id": 9674, "track": "2o49Twc3qrNMOt8gq9W06L"}],
    "embed_covers": [{}],
    "canon_albums": [{"refresh": False, "search_budget": None, "mb_budget": None},
                     {"refresh": True, "search_budget": 5, "mb_budget": 0}],
    "set_search_override": [{"album": "0ETFjACtuP2ADo6LFhL6HN", "words": "four tet", "reason": ""},
                            {"album": "-m-/le baptême", "words": "--apply", "reason": "-x --apply"}],
    "clear_search_override": [{"album": "--apply"}],
    "hold_album": [{"album": "0ETFjACtuP2ADo6LFhL6HN", "reason": "--apply curated"}],
    "release_album": [{"album": "0ETFjACtuP2ADo6LFhL6HN"}],
    "pin_track": [{"track": "68ozhSrI4eLbnUgih1SlOT", "url": "https://music.youtube.com/watch?v=29Sh3yEaBP8",
                   "label": "", "reason": ""},
                  {"track": "68ozhSrI4eLbnUgih1SlOT", "url": "https://music.youtube.com/watch?v=29Sh3yEaBP8",
                   "label": "-x", "reason": "--apply"}],
    "unpin_track": [{"track": "68ozhSrI4eLbnUgih1SlOT"}],
    "request_album": [{"albums": [{"release_group": "6e0a6c2a-2d38-3d3c-9d38-4f0c2b5b7a11", "preferred_release": None}],
                       "later": False},
                      {"albums": [{"release_group": "0b4ed3a1-7d3c-4d9e-9b8e-0c5c6a8d2f10",
                                   "preferred_release": "11111111-1111-4111-8111-111111111111"},
                                  {"release_group": "6e0a6c2a-2d38-3d3c-9d38-4f0c2b5b7a11", "preferred_release": None}],
                       "later": True}],
    "remove_from_later": [{"release_groups": ["6e0a6c2a-2d38-3d3c-9d38-4f0c2b5b7a11"], "albums": [], "through": None},
                          {"release_groups": [], "albums": [], "through": 2},
                          {"release_groups": [], "albums": ["1734", "--apply"], "through": None},
                          {"release_groups": ["0b4ed3a1-7d3c-4d9e-9b8e-0c5c6a8d2f10"], "albums": ["1734"],
                           "through": 1}],
}


def _argvs():
    for name, cases in CASES.items():
        tool = TOOLS[name]
        modes = (False, True) if tool.tier == WRITE else (False,)
        for args in cases:
            for apply in modes:
                for argv in tool.commands(args, apply):
                    yield pytest.param(argv, apply, id=" ".join(argv))


def test_every_tool_has_cases():
    assert set(CASES) == set(TOOLS)


@pytest.mark.parametrize("argv,apply", list(_argvs()))
def test_cli_parses_the_tool_argv(argv, apply, monkeypatch):
    scripts = {ep.name: ep for ep in entry_points(group="console_scripts")}
    assert argv[0] in scripts, f"{argv[0]} is not an installed console script"
    real = argparse.ArgumentParser.parse_args

    parsed = []

    def parse_only(self, args=None, namespace=None):
        parsed.append(real(self, args, namespace))  # exits 2 on an argv it rejects
        raise Parsed

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", parse_only)
    monkeypatch.setattr(sys, "argv", argv)
    main = scripts[argv[0]].load()
    try:
        main()
    except SystemExit as exc:
        pytest.fail(f"{' '.join(argv)} rejected by argparse (exit {exc.code})")
    except Parsed:
        # Only an apply's argv applies, whatever the arguments hold.
        assert getattr(parsed[0], "apply", False) is apply
        return
    pytest.fail(f"{argv[0]} returned without parsing its arguments")
