# Test Audio Fixture

The audio file used by `test_import.py` is **not committed** to this repository.
It is downloaded automatically on the first test run and cached here.

## Track in use

**"7 Ghosts I" by Nine Inch Nails**, from *Ghosts I–IV* (2008)

| Field | Value |
|---|---|
| Artist | Nine Inch Nails |
| Title | 7 Ghosts I |
| Album | Ghosts I–IV |
| Duration | 2:01 |
| License | [CC BY-NC-SA 3.0](https://creativecommons.org/licenses/by-nc-sa/3.0/) |
| MusicBrainz Recording ID | [1d1bb32a-5bc6-4b6f-88cc-c043f6c52509](https://musicbrainz.org/recording/1d1bb32a-5bc6-4b6f-88cc-c043f6c52509) |
| Source | [Internet Archive](https://archive.org/details/nineinchnails_ghosts_I_IV) |
| Download URL | `https://archive.org/download/nineinchnails_ghosts_I_IV/07_Ghosts_I.mp3` |

The file is saved as `Nine Inch Nails - 7 Ghosts I.mp3` so beets' `fromfilename`
plugin extracts the correct artist and title for MusicBrainz text matching.

## Why this track?

- Verified in MusicBrainz with AcoustID fingerprints registered
- CC BY-NC-SA 3.0 — compatible with non-commercial test infrastructure
- Hosted on Internet Archive (long-term preservation; no authentication required)
- Short (2:01) — keeps test download time low
- Ghosts I–IV was a pioneering CC music release; the metadata is exemplary

## Attribution (CC BY-NC-SA 3.0)

"7 Ghosts I" by Nine Inch Nails is licensed under
[Creative Commons Attribution-NonCommercial-ShareAlike 3.0](https://creativecommons.org/licenses/by-nc-sa/3.0/).

## Cache behaviour

The `fixture_audio` pytest fixture in `conftest.py` checks for the cached file
before downloading. Delete it to force a re-download:

```bash
rm "tests/fixtures/audio/Nine Inch Nails - 7 Ghosts I.mp3"
```
