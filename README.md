# music-pipeline

Dockerized music pipeline: Spotify playlists → spotdl downloads → beets import/tag → Navidrome.

```
Spotify playlists → spotdl → beets → ~/Music/library → Navidrome
```

---

## Requirements

- Docker + Docker Compose
- [1Password CLI (`op`)](https://developer.1password.com/docs/cli/) signed in — used on the **host** to inject Spotify credentials
- YouTube Premium account + cookies export (see below)
- Spotify Developer app (client_id + client_secret) stored in 1Password at `Private/Spotify Developer App`

---

## Setup

### 1. Clone and prepare

```bash
git clone https://github.com/sometimeskind/music-pipeline
cd music-pipeline
```

### 2. Export YouTube Premium cookies

spotdl requires YouTube Premium cookies for M4A 256 kbps quality.

1. Install the browser extension **"Get cookies.txt LOCALLY"**
2. Sign in to [music.youtube.com](https://music.youtube.com) with your YouTube Premium account
3. Export cookies in Netscape format
4. Save as `cookies.txt` in the repo root (already in `.gitignore`)

Cookies expire periodically. Re-export when downloads start failing at quality.

### 3. Set up Spotify credentials

Store your Spotify Developer app credentials in 1Password:

- **Item:** `Private/Spotify Developer App`
- **Fields:** `client_id`, `client_secret`

Create an `.env.tpl` for `op run`:

```bash
SPOTIFY_CLIENT_ID=op://Private/Spotify Developer App/client_id
SPOTIFY_CLIENT_SECRET=op://Private/Spotify Developer App/client_secret
```

### 4. Add your first playlist

```bash
op run --env-file=.env.tpl -- docker compose run --rm -it pipeline music-setup
```

### 5. Start the service

```bash
op run --env-file=.env.tpl -- docker compose up -d
```

The container runs `music-ingest` daily at 03:00 (UTC). Override with `CRON_SCHEDULE` env var.

---

## Host-side `just` recipes

These live in the dotfiles repo (`sometimeskind/dotfiles`) at `just/.justfile` → `~/.justfile`.

| Recipe | What it does |
|---|---|
| `just sync` | Run full ingest now |
| `just setup` | Add a new playlist (interactive) |
| `just remove <name>` | Remove a playlist |
| `just import` | Import files dropped into inbox |
| `just rescan` | POST to Navidrome API to trigger library refresh |
| `just logs` | Tail container logs |
| `just up` | Start the container |
| `just down` | Stop the container |
| `just backup` | Dump beets DB + export JSON |

---

## Directory structure (inside container)

```
/root/Music/
  inbox/
    spotdl/
      <name>.spotdl      ← spotdl sync state (do not delete)
      <name>/            ← spotdl downloads (cleared after beet import)
  library/               ← beets-managed: $albumartist/$album/$track - $title.m4a
  quarantine/            ← low-confidence MusicBrainz matches, review manually
  playlists/             ← generated .m3u files (relative paths for Navidrome)
/root/.config/beets/
  library.db             ← SQLite database — back this up
  import.log             ← log of every skipped import
  config.yaml            ← bind-mounted from ./config/beets/config.yaml
```

---

## Navidrome integration

Navidrome lives in a separate stack and reads from the same music volume via NFS.

Required Navidrome settings:
- `ND_MUSICFOLDER` pointing at the music volume root (not just `/library` — so it sees playlists too)
- `ND_AUTOIMPORTPLAYLISTS=true`

After ingest, trigger a rescan via:

```
POST /rest/startScan?u=<user>&p=<pass>&v=1.16.1&c=music-pipeline&f=json
```

The `just rescan` recipe handles this.

---

## Notes and gotchas

- **`source` is single-value.** A track imported by two playlists only carries the source from whichever ran first.
- **`beet update` does not prune deleted files.** Use `beet remove <query>` with a specific query. Never run `beet remove` without a query.
- **Cookies expire.** Re-export from browser when downloads fail at quality.
- **Spotify rate limits.** Always use your own app credentials — the spotdl defaults are shared and hit limits quickly.
- **MusicBrainz threshold.** `strong_rec_thresh: 0.05` is strict. Raise to `0.10` in `config/beets/config.yaml` if too many valid tracks land in quarantine.
