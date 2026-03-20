# PR Summary — for next agent to post as a comment, then delete this file

## What this PR does

Adds container integration tests that orchestrate real Docker containers to verify the full scan pipeline end-to-end. Tests live in `tests/` and run on the host via the Docker SDK (`docker-py`), spinning up the scan image with isolated volumes per test.

## Scenarios covered

| Scenario | File | Description |
|---|---|---|
| 1 | `test_smoke.py` | Empty inbox → `music-scan` exits 0 |
| 1a | `test_smoke.py` | `fpcalc`, `pyacoustid`, and `chroma` plugin all present in image |
| 1c | `test_smoke.py` | Recording-ID lookup path: `beet import --search-id <mbid>` lands track in library and full scan succeeds |
| 2 | `test_import.py` | Silent MP3 with correct tags imports to library; noise file goes to quarantine |
| 3 | `test_import.py` | Track imported from spotdl playlist dir gets `source=<playlist>` tag; `.m3u` generated with relative paths |
| 4 | `test_import.py` | Re-importing the same track produces exactly one beets entry |
| 5 | `test_auth.py` | Full fetch → scan ingest against a real Spotify playlist (auth-gated) |

## Key design decisions worked out during this session

**Audio fixture**: A silent MP3 generated at test time by `ffmpeg` inside the scan container. No downloaded files, no checked-in binaries. Tagged with `artist=Nine Inch Nails`, `title=7 Ghosts I`, `album=Ghosts I-IV`, duration=121s (matching the real MusicBrainz track length).

**Why `singletons=True` in the test beets config**: A single file in a subdirectory triggers beets' album-group mode, which searches MusicBrainz by album name ("Ghosts I-IV"). This returns 0 candidates — likely a Lucene query issue where the hyphen in "I-IV" is misinterpreted as a NOT operator. Singleton mode searches by recording title/artist instead and reliably finds "7 Ghosts I" by Nine Inch Nails. Singletons mode is also the correct production mode for spotdl imports (individual tracks, not full albums).

**Why `tracks` parameter is not the issue**: Investigated whether beets was filtering by track count. The `tracks` parameter is only sent to MusicBrainz if `extra_tags: tracks` is in the beets config — our production config doesn't set this, so it's never sent.

**Scenario 1c — AcoustID mock approach**: The production chroma path is: fingerprint → AcoustID → recording MBID → MusicBrainz lookup → import. We can't fingerprint silence (no AcoustID match), and mocking acoustid.org over HTTPS inside a subprocess is complex. Instead, `beet import --search-id <recording_mbid>` feeds beets exactly what AcoustID would provide, testing the recording-ID → MusicBrainz lookup → import path without any network mocking or custom plugins. `beet_import_verbose` was extended with an `extra_flags` parameter to support this.

## Current state

All scenarios are implemented and committed. The branch is `claude/automate-container-tests-kxlsO`. Tests have not been run against a live image in this session (CI should handle that). The one known gap is that Scenario 1c hasn't been validated against the actual image yet — `--search-id` with `--quiet` in singletons mode should work per beets docs, but if beets requires interactive confirmation even with `--quiet --search-id`, the test may need `timid: yes` added to the test config or `--yes` flag added.
