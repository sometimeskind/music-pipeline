"""Album import hook: Spotify covers on Usenet tracks (#204). No Prefect server needed."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def _complete_with_tag_ids(embed_covers):
    """Run the album hooks' complete with albums.complete calling tag_ids once."""
    from music_service.flows import _album_library_hooks

    fresh, old = MagicMock(added=200.0), MagicMock(added=10.0)
    lib = MagicMock()
    lib.items_by_source.return_value = [fresh, old]

    def fake_complete(state, completion, import_inbox, missing, add_source, tag_ids):
        tag_ids("later", [], 100.0, 2)
        return "imported"

    with patch("music_fetch.albums.complete", side_effect=fake_complete), \
         patch("music_service.flows.scan.tag_album_ids") as tag_album_ids, \
         patch("music_scan.cover.covers_by_id", return_value={"T1": "https://i.scdn.co/x"}), \
         patch("music_scan.cover.embed_covers", side_effect=embed_covers) as embed, \
         patch("music_scan.navidrome.trigger_scan") as rescan:
        _, _, complete = _album_library_hooks(lib)
        assert complete(MagicMock(), MagicMock()) == "imported"
    tag_album_ids.assert_called_once_with(lib, "later", [], 100.0, 2)
    return embed, rescan, fresh


def test_album_import_embeds_covers_on_fresh_items_and_rescans():
    embed, rescan, fresh = _complete_with_tag_ids(lambda items, covers: 1)
    assert embed.call_args.args[0] == [fresh]
    rescan.assert_called_once()


def test_album_import_cover_failure_does_not_fail_the_import():
    def boom(items, covers):
        raise OSError("disk full")

    _, rescan, _ = _complete_with_tag_ids(boom)
    rescan.assert_not_called()


def test_album_import_canonicalises_after_tag_ids_and_before_covers():
    """#209: canon needs the Spotify IDs tag_album_ids set, and runs before the
    cover backfill so that only covers what the canonical album had no art for."""
    order = []
    with patch("music_scan.canon.canonicalize_items",
               side_effect=lambda items: order.append(("canon", list(items)))):
        _, _, fresh = _complete_with_tag_ids(lambda items, covers: order.append(("covers", items)) or 0)
    assert order == [("canon", [fresh]), ("covers", [fresh])]


def test_album_import_canon_failure_does_not_fail_the_import():
    with patch("music_scan.canon.canonicalize_items", side_effect=OSError("disk full")):
        embed, _, _ = _complete_with_tag_ids(lambda items, covers: 0)
    embed.assert_called_once()
