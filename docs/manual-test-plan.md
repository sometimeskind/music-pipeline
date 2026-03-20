# Manual Test Plan

End-to-end verification for the music pipeline. Run these scenarios top-to-bottom against a running container to confirm the full data flow is working.

## Prerequisites

- Images pulled from the registry: `docker compose pull`
- `cookies.txt` present on the host (YouTube Premium cookies)
- Spotify credentials available for Scenario 5 (via `op run` or set `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` directly)
- A few test audio files ready to drop in — see [Test Audio Files](#test-audio-files) below

---

## Test Audio Files

You need two types of files:

**Case A — Well-known track (will match MusicBrainz):**
A freely licensed .mp3 or .m4a from a release indexed in MusicBrainz. Good sources:
- [Jamendo](https://www.jamendo.com) — Creative Commons releases, many with MusicBrainz entries
- Any CC-BY or CC0 album you can find on MusicBrainz directly

**Case B — Unknown track (will be quarantined):**
A short noise or silence file that won't match anything. Generate one with:
```bash
ffmpeg -f lavfi -i "anoisesrc=d=5" -ar 44100 noise.mp3
```

---

## Scenario 1 — Smoke Test (Container Starts & Scan Runs Clean)

**Goal:** Confirm the image runs and `music-scan` exits cleanly against empty volumes.

```bash
just scan
```

**Pass criteria:**
- Container starts, `music-scan` runs, and exits 0
- Output shows `No files imported from /root/Music/inbox`
- `beet update exited with code 1 (non-fatal)` is expected on an empty library — not a failure

---

## Scenario 1a — Chroma Plugin Verification

**Goal:** Confirm that the chroma plugin and its runtime dependencies are correctly installed, and that AcoustID fingerprinting can be invoked during import.

> Run this once after pulling a new image. Silent failure is possible: if `fpcalc` is missing or `pyacoustid` is broken, beets silently falls back to MusicBrainz-only matching with no error.

### Layer 1 — Installation checks

```bash
# 1. Verify the fpcalc binary (from libchromaprint-tools) is present and functional
docker compose run --rm scan fpcalc -version
# Expected: "fpcalc version X.X.X" — exits 0

# 2. Verify pyacoustid is importable
docker compose run --rm scan python -c "import acoustid; print(acoustid.__version__)"
# Expected: prints a version string — exits 0

# 3. Verify beets loads the chroma plugin without errors
docker compose run --rm scan beet version
# Expected: "chroma" appears in the plugins list — no "plugin not found" errors
```

**Pass criteria:** All three exit 0. Any failure points to the image: check `scan/Dockerfile` for `libchromaprint-tools` and `scan/requirements.txt` for `pyacoustid`.

### Layer 2 — Runtime verification (fingerprinting invoked during import)

After completing Scenario 2, check the import log for AcoustID activity:

```bash
docker compose run --rm scan grep -i "fingerprint\|acoustid\|chroma" /root/.config/beets/import.log
```

If the grep returns nothing, beets matched entirely via MusicBrainz metadata (valid for well-tagged spotdl files). To force the fingerprint path, strip tags before import:

```bash
ffmpeg -i track-a.mp3 -map_metadata -1 -c:a copy track-a-notags.mp3
docker compose cp track-a-notags.mp3 scan:/root/Music/inbox/
just scan
docker compose run --rm scan grep -i "fingerprint\|acoustid\|chroma" /root/.config/beets/import.log
```

**Pass criteria:** Import log contains at least one fingerprint/AcoustID reference when a tag-stripped file is imported.

---

## Scenario 2 — Manual File Drop into Inbox

**Goal:** Verify beets import, library placement, quarantine, and inbox cleanup — without Spotify or YouTube.

> This is the most important scenario to run first. It does not require credentials.

### Steps

**1. Copy test files into the inbox:**
```bash
docker compose cp track-a.mp3 scan:/root/Music/inbox/
docker compose cp noise.mp3 scan:/root/Music/inbox/
```

**2. Trigger an import:**
```bash
just scan
```

**3. Verify Case A (well-known track) was imported to the library:**
```bash
docker compose run --rm scan beet ls -a
# should list the track with artist/album/title populated

docker compose run --rm scan find /root/Music/library -type f
# should show: /root/Music/library/<albumartist>/<album>/<track> - <title>.*
```

**4. Verify Case B (unknown track) went to quarantine:**
```bash
docker compose run --rm scan find /root/Music/quarantine -type f
# noise.mp3 should appear here
```

**5. Verify inbox is clear:**
```bash
docker compose run --rm scan find /root/Music/inbox -maxdepth 1 -type f
# should be empty — files have been moved out
```

**6. Check the import log for match decisions:**
```bash
docker compose run --rm scan tail -50 /root/.config/beets/import.log
# shows confidence scores and import/skip/quarantine decisions per file
```

**Pass criteria:**
- Case A in library at `$albumartist/$album/$track - $title.*`
- Case B in quarantine (`/root/Music/quarantine/`)
- Inbox empty after import
- Import log shows confidence scores and a decision for each file

**If too many good tracks go to quarantine:** raise `strong_rec_thresh` in `config/beets/config.yaml` from `0.05` toward `0.10` and re-test.

---

## Scenario 3 — Simulated spotdl Playlist Import

**Goal:** Verify `source=<name>` tagging and `.m3u` generation — without Spotify or YouTube.

### Steps

**1. Create a fake spotdl playlist directory:**
```bash
docker compose run --rm scan mkdir -p /root/Music/inbox/spotdl/test-playlist
```

**2. Drop a well-known track into it (simulating a spotdl download):**
```bash
docker compose cp track-a.mp3 scan:/root/Music/inbox/spotdl/test-playlist/
```

**3. Create a minimal `.spotdl` state file:**
```bash
docker compose run --rm scan sh -c 'echo "[]" > /root/Music/inbox/spotdl/test-playlist.spotdl'
```

**4. Run a scan:**
```bash
just scan
```

**5. Verify the `source` tag was applied:**
```bash
docker compose run --rm scan beet ls -a source:test-playlist
# should list the imported track
```

**6. Verify the `.m3u` was generated:**
```bash
docker compose run --rm scan cat /root/Music/playlists/test-playlist.m3u
# should contain a relative path to the imported track
```

**Pass criteria:**
- Track tagged `source=test-playlist` in beets DB
- `/root/Music/playlists/test-playlist.m3u` exists and contains a relative path to the track

---

## Scenario 4 — Duplicate Handling

**Goal:** Verify that re-importing an already-present track is skipped correctly.

> Run after Scenario 2 or 3.

### Steps

**1. Drop the same well-known track into the inbox again:**
```bash
docker compose cp track-a.mp3 scan:/root/Music/inbox/
```

**2. Run import again:**
```bash
just scan
```

**3. Check the import log for a skip/duplicate decision:**
```bash
docker compose run --rm scan tail -20 /root/.config/beets/import.log
```

**4. Confirm there is only one copy in the library:**
```bash
docker compose run --rm scan beet ls -a title:<track-title>
# should show exactly one entry
```

**Pass criteria:**
- Import log shows the file was skipped or identified as a duplicate
- Beets DB has exactly one entry for the track
- No second file written to the library

---

## Scenario 5 — Full Ingest with Spotify

**Goal:** Verify the complete `spotdl sync → import → .m3u` flow with a real playlist.

> Requires Spotify credentials. Use a small playlist (≤10 tracks) to keep the test fast.
> Pass credentials via `op run` or export `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` directly.

### Steps

**1. Add a test playlist to `config/playlists.conf`:**
```
test-small  https://open.spotify.com/playlist/<id>
```

**2. Provision the playlist (first `just fetch` run):**
```bash
just fetch
```

This provisions the `.spotdl` state file (recording all current songs as already known) and
then runs a sync — which downloads 0 tracks because all songs are already in the snapshot.

> To force a download of existing playlist tracks (e.g. to test the full download path), write
> an empty-songs snapshot before running `just fetch`:
> ```bash
> PLAYLIST_URL="https://open.spotify.com/playlist/<id>"
> docker compose run --rm fetch sh -c "printf '{\"type\":\"sync\",\"query\":[\"$PLAYLIST_URL\"],\"songs\":[]}' > /root/Music/inbox/spotdl/test-small.spotdl"
> ```
> Then run `just fetch` — all tracks will appear as new and be downloaded.

**3. Run a full sync:**
```bash
just sync
```

**4. Verify tracks are in the library with the correct tag:**
```bash
docker compose run --rm scan beet ls -a source:test-small
```

**5. Verify the `.m3u` was generated:**
```bash
docker compose run --rm scan cat /root/Music/playlists/test-small.m3u
```

**6. If `PUSHGATEWAY_URL` is set — check Prometheus for metrics:**
Look for `music_scan_*` metrics in the Pushgateway UI.

**Pass criteria:**
- All playlist tracks imported to library
- Each track tagged `source=test-small`
- `.m3u` populated with correct relative paths
- Optional integration (Prometheus) confirmed in Pushgateway UI

---

## Cleanup After Testing

Remove test data to leave the environment clean:

```bash
# Remove fake spotdl dirs created in Scenario 3
docker compose run --rm scan rm -rf /root/Music/inbox/spotdl/test-playlist
docker compose run --rm fetch rm -f /root/Music/inbox/spotdl/test-playlist.spotdl

# Clear the source tag from any test-playlist tracks in beets
docker compose run --rm scan beet modify -y source= source:test-playlist

# Remove test-small from config/playlists.conf (edit the file), then:
docker compose run --rm fetch rm -rf /root/Music/inbox/spotdl/test-small /root/Music/inbox/spotdl/test-small.spotdl
docker compose run --rm scan rm -f /root/Music/playlists/test-small.m3u

# Clear quarantine
docker compose run --rm scan rm -rf /root/Music/quarantine/*
```

---

## Key Files for Debugging

| File | Purpose |
|---|---|
| `/root/.config/beets/import.log` | Per-file import decisions, confidence scores, and AcoustID lookups |
| `/root/.config/beets/library.db` | Beets SQLite DB — query with `beet ls` or `sqlite3` |
| `/root/Music/quarantine/` | Files that didn't meet the MusicBrainz match threshold |
| `/root/Music/playlists/` | Generated `.m3u` files |
| `config/beets/config.yaml` | Match threshold (`strong_rec_thresh`), library paths, plugin list |
| `scan/Dockerfile` | Installs `libchromaprint-tools` (provides `fpcalc`) — required by chroma plugin |
| `scan/requirements.txt` | Lists `pyacoustid` — Python binding used by beets chroma plugin |
