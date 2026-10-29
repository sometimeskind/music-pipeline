"""Tests for music_fetch.redact (#343)."""

import pytest

from music_fetch.redact import redact, redact_text


@pytest.mark.parametrize("text, expected", [
    ("http://prowlarr:9696/1/download?apikey=abc123&link=xyz&file=Album",
     "http://prowlarr:9696/1/download?apikey=REDACTED&link=xyz&file=Album"),
    ("https://indexer/api?t=get&id=9&APIKEY=abc123", "https://indexer/api?t=get&id=9&APIKEY=REDACTED"),
    ("getnzb/9.nzb&i=42&r=abc123", "getnzb/9.nzb&i=42&r=REDACTED"),
    ("…?token=abc123#frag and more", "…?token=REDACTED#frag and more"),
    ("http://x/?link=https%3A%2F%2Findexer%2Fapi%3Ft%3Dget%26apikey%3Dabc123%26id%3D9",
     "http://x/?link=https%3A%2F%2Findexer%2Fapi%3Ft%3Dget%26apikey%3DREDACTED%26id%3D9"),
    ("https://user:hunter2@news.example:563/", "https://REDACTED@news.example:563/"),
    ("postgresql://music:p@ss@db:5432/music", "postgresql://REDACTED@db:5432/music"),
    ("http://x/?X-Api-Key=abc123&access_token=def&passwd=ghi",
     "http://x/?X-Api-Key=REDACTED&access_token=REDACTED&passwd=REDACTED"),
    ("http://x/?link=https%253A%252F%252Fi%252Fapi%253Fapikey%253Dabc123%2526id%253D9",
     "http://x/?link=https%253A%252F%252Fi%252Fapi%253Fapikey%253DREDACTED%2526id%253D9"),
    ("failed: Max retries exceeded with url: /1/download?apikey=abc123 (Caused by …)",
     "failed: Max retries exceeded with url: /1/download?apikey=REDACTED (Caused by …)"),
])
def test_redact_text_masks_url_credentials(text, expected):
    assert redact_text(text) == expected


@pytest.mark.parametrize("text", [
    "https://music.youtube.com/watch?v=abc123&si=xyz",
    "https://open.spotify.com/album/0ETFjACtuP2ADo6LFhL6HN?si=1",
    "The Beatles — Abbey Road: key=value is not a URL parameter",
    "[GRAB] The Beatles — Abbey Road → Abbey.Road.FLAC (412 MB)",
])
def test_redact_text_leaves_other_text_alone(text):
    assert redact_text(text) == text


def test_redact_masks_the_environments_credentials_in_any_shape(monkeypatch):
    monkeypatch.setenv("PROWLARR_API_KEY", "0123456789abcdef")
    monkeypatch.setenv("ALBUM_PASSTHROUGH", "/root/Music/library")  # not a credential's name
    text = "key 0123456789abcdef, encoded x%3D0123456789abcdef, under /root/Music/library"
    assert redact_text(text) == "key REDACTED, encoded x%3DREDACTED, under /root/Music/library"
    assert redact({"line": text})[1] == 1


def test_redact_walks_json_and_drops_download_urls():
    value = {"candidate": {"guid": "https://indexer/details/9?apikey=abc123", "title": "Abbey Road",
                           "download_url": "http://prowlarr/1/download?apikey=abc123"},
             "blocklist": ["https://indexer/details/8?apikey=abc123", "plain-guid"],
             "attempts": 2, "ok": True, "none": None}
    clean, changed = redact(value)
    assert clean == {"candidate": {"guid": "https://indexer/details/9?apikey=REDACTED", "title": "Abbey Road"},
                     "blocklist": ["https://indexer/details/8?apikey=REDACTED", "plain-guid"],
                     "attempts": 2, "ok": True, "none": None}
    assert changed == 3
    assert redact({"a": ["b", 1]}) == ({"a": ["b", 1]}, 0)
