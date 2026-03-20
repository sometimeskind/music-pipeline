# Test Audio Fixture

The audio file used by `test_import.py` is **not committed** to this repository.
It is downloaded automatically on the first test run and cached here.

## Track in use

**"Carefree" by Kevin MacLeod**

| Field | Value |
|---|---|
| Artist | Kevin MacLeod |
| Title | Carefree |
| License | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |
| Source | [incompetech.com](https://incompetech.com/music/royalty-free/index.html?isrc=USUAN1100245) |
| MusicBrainz artist | [1f9df192-a621-4f54-8850-2c5373b7eac9](https://musicbrainz.org/artist/1f9df192-a621-4f54-8850-2c5373b7eac9) |
| Download URL | `https://incompetech.com/music/royalty-free/mp3-royaltyfree/Carefree.mp3` |

The file is saved as `Kevin MacLeod - Carefree.mp3` so beets' `fromfilename`
plugin extracts the artist and title for MusicBrainz matching.

## Why this track?

- Widely known and indexed in MusicBrainz
- CC-BY 4.0 — compatible with open-source test infrastructure
- Available via direct download from Kevin MacLeod's own site (stable URL)
- The `Artist - Title` filename format enables reliable MusicBrainz text matching
  even without AcoustID fingerprint lookups

## Attribution requirement (CC-BY 4.0)

Kevin MacLeod (incompetech.com) — Licensed under Creative Commons Attribution 4.0.

## Cache behaviour

The `fixture_audio` pytest fixture in `conftest.py` checks for the cached file
before downloading. Delete it to force a re-download:

```bash
rm tests/fixtures/audio/"Kevin MacLeod - Carefree.mp3"
```
