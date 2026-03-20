# Test Audio Fixture: track-a.m4a

`track-a.m4a` is **not committed** to this repository. You must provide it before running Scenarios 2–4.

## Requirements

The file must be:
- A short (5–10 second) clip is sufficient — full-length tracks work too
- CC-licensed (Creative Commons) or otherwise freely redistributable
- From a release indexed in [MusicBrainz](https://musicbrainz.org) so that beets can identify it with high confidence via AcoustID fingerprinting + metadata matching

## Sourcing a suitable file

Good sources:
- **[Jamendo](https://www.jamendo.com)** — large catalogue of CC releases, many indexed in MusicBrainz. Download any track, verify it appears in MusicBrainz via `https://musicbrainz.org/search`.
- **[ccMixter](https://ccmixter.org)** — CC-BY and CC0 tracks. Search for the artist/title in MusicBrainz to confirm it's indexed.
- **[Free Music Archive](https://freemusicarchive.org)** — CC-licensed catalogue.

## After adding the file

1. Place the file here as `track-a.m4a`
2. Update this README with the track details:

---

**Track:** _(fill in)_
**Artist:** _(fill in)_
**Album:** _(fill in)_
**License:** _(fill in — e.g. CC BY 4.0)_
**MusicBrainz Recording ID:** _(fill in — e.g. https://musicbrainz.org/recording/<uuid>)_

---

## Verifying the match threshold

If `test_file_drop_known_track_imported_to_library` fails with "No files found in library", the track likely went to quarantine because the match confidence fell below the threshold in `config/beets/config.yaml`:

```yaml
match:
  strong_rec_thresh: 0.05
```

Try raising it to `0.10` and re-running. If the track still doesn't match, choose a different source file with better MusicBrainz coverage.
