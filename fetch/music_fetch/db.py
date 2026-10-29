"""The pipeline's Postgres database, ``music`` (homelab#2413).

One database for all of the pipeline's own state: the event store
(:mod:`music_fetch.events`), the album and playlist stores and the other former
JSON dot files (:mod:`music_fetch.state`, homelab#2410), and later the request
and Later tables (homelab#2478).  beets' library stays SQLite: beets has no
Postgres backend.

``MUSIC_DATABASE_URL`` names it (a libpq URL).  Unset, :func:`configured` is
false and the event store records nothing, which is how tests, ``just fetch``
and a pod without the secret run.

The pipeline owns its schema.  :data:`MIGRATIONS` is append-only: migration *n*
is ``MIGRATIONS[n - 1]``, and :func:`connect` applies the ones a database lacks,
under an advisory lock and in one transaction, so processes starting together
(the service, a CLI the MCP queue runs, the beets plugin) migrate it once.
Never edit a shipped migration; add one.  Keep each additive (new tables,
nullable columns, a wider CHECK), so the previous image still runs against the
newer schema: that is the rollback path.
"""

from __future__ import annotations

import logging
import os

import psycopg

logger = logging.getLogger(__name__)

ENV = "MUSIC_DATABASE_URL"
# Seconds a connection attempt may take.  Event writes are best-effort, and an
# import must not stall long on a database that is down.
CONNECT_TIMEOUT = 3
# pg_advisory_xact_lock key that serialises migrations; nothing else takes advisory locks here.
_MIGRATION_LOCK = 2413

# The kinds of event (homelab#2413).  The events table's CHECK lists the same
# values, spelled out in the migrations, and test_events asserts that a migrated
# database agrees: a new kind is a new migration that widens the CHECK.
EVENT_KINDS = (
    # An album-mode import (the [DONE], [WRONG], [INCOMPLETE] and [LOST] lines).
    "import_done",
    "import_wrong",
    "import_incomplete",
    "import_lost",
    # The release check and the duplicate tools: an item retired, kept as a
    # local-only extra, or merged into another; a path clash; a mixed album.
    "retire",
    "local",
    "merge",
    "clash",
    "mixed",
    # The album store: a Usenet grab, an automatic reset, tracks left to the
    # spotdl fallback, an album or download given up on.
    "grab",
    "autoreset",
    "fallback",
    "failure",
    # A download the length guard quarantined.
    "reject",
    # A Music MCP job (homelab#2412) ending: tool, mode, caller, status.
    "mcp_job",
    # Migration 2: a fallback album the library now completes ([FILLED]), and a
    # playlist entry's ID taken off an item that is another version of the song
    # (music-backfill-ids --apply's [WRONG]).
    "filled",
    "wrong_version",
    # Migration 3: an item canon moved from one album to another (a changed
    # spotify_album_id), which the digest checks for a move between unrelated albums.
    "moved",
    # Requests and the Later queue (homelab#2478), nothing emits them yet: an
    # album requested, added to or removed from Later, and a Spotify ID resolved
    # for a release group.
    "album_requested",
    "later_added",
    "later_removed",
    "spotify_id_resolved",
    # Migration 5: a search override, hold or track pin set or cleared (homelab#2484);
    # data.kind says which.
    "override_set",
    "override_cleared",
)

MIGRATIONS: list[str] = [
    # 1: the event store.  Album-scoped events carry both keys: the Spotify album
    # ID the album store is keyed by today, and the MusicBrainz release group it
    # is rekeyed to in homelab#2410 (null until an import resolves it), so an
    # album's history survives the rekey without a migration of its own.
    """
    CREATE TABLE events (
        id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        at               timestamptz NOT NULL DEFAULT now(),
        kind             text NOT NULL CONSTRAINT events_kind_check CHECK (kind IN (
                             'import_done', 'import_wrong', 'import_incomplete', 'import_lost',
                             'retire', 'local', 'merge', 'clash', 'mixed',
                             'grab', 'autoreset', 'fallback', 'failure', 'reject', 'mcp_job',
                             'album_requested', 'later_added', 'later_removed', 'spotify_id_resolved')),
        source           text NOT NULL,
        spotify_album_id text,
        release_group    uuid,
        item_id          integer,
        caller           text,
        job_id           text,
        message          text NOT NULL,
        data             jsonb NOT NULL DEFAULT '{}'::jsonb
    );
    CREATE INDEX events_at ON events (at);
    CREATE INDEX events_kind_at ON events (kind, at);
    CREATE INDEX events_spotify_album_at ON events (spotify_album_id, at) WHERE spotify_album_id IS NOT NULL;
    CREATE INDEX events_release_group_at ON events (release_group, at) WHERE release_group IS NOT NULL;
    """,
    # 2: two more kinds, filled and wrong_version (a wider CHECK, so the previous image still writes).
    """
    ALTER TABLE events DROP CONSTRAINT events_kind_check;
    ALTER TABLE events ADD CONSTRAINT events_kind_check CHECK (kind IN (
        'import_done', 'import_wrong', 'import_incomplete', 'import_lost',
        'retire', 'local', 'merge', 'clash', 'mixed',
        'grab', 'autoreset', 'fallback', 'failure', 'reject', 'mcp_job',
        'album_requested', 'later_added', 'later_removed', 'spotify_id_resolved',
        'filled', 'wrong_version'));
    """,
    # 3: the moved kind (a wider CHECK again).
    """
    ALTER TABLE events DROP CONSTRAINT events_kind_check;
    ALTER TABLE events ADD CONSTRAINT events_kind_check CHECK (kind IN (
        'import_done', 'import_wrong', 'import_incomplete', 'import_lost',
        'retire', 'local', 'merge', 'clash', 'mixed',
        'grab', 'autoreset', 'fallback', 'failure', 'reject', 'mcp_job',
        'album_requested', 'later_added', 'later_removed', 'spotify_id_resolved',
        'filled', 'wrong_version', 'moved'));
    """,
    # 4: the pipeline's own state, moved off the JSON dot files (homelab#2410,
    # read and written through music_fetch.state).  An album row is keyed by its
    # MusicBrainz release group; a row whose release group is not known yet has
    # none, and ``unresolved`` says why (a flag, not a provisional key).
    # album_refs maps every Spotify album ID (and a local album's
    # ``artist/name``) to its row: two Spotify editions of one release group are
    # one row with two refs.
    """
    CREATE TABLE albums (
        id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        release_group    uuid UNIQUE,
        unresolved       text CONSTRAINT albums_unresolved_check CHECK (unresolved IN (
                             'pending', 'no-match', 'conflict', 'in-flight')),
        spotify_album_id text,
        status           text NOT NULL CONSTRAINT albums_status_check CHECK (status IN (
                             'wanted', 'dry-run', 'grabbed', 'missing', 'have', 'imported',
                             'failed', 'fallback', 'filled')),
        origin           text NOT NULL DEFAULT 'album' CONSTRAINT albums_origin_check CHECK (origin IN (
                             'album', 'wants', 'library')),
        mixed            boolean NOT NULL DEFAULT false,
        data             jsonb NOT NULL DEFAULT '{}'::jsonb,
        updated_at       timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT albums_resolved_check CHECK ((release_group IS NULL) = (unresolved IS NOT NULL))
    );
    CREATE INDEX albums_status ON albums (status);
    CREATE INDEX albums_unresolved ON albums (unresolved) WHERE unresolved IS NOT NULL;
    CREATE TABLE album_refs (
        ref      text PRIMARY KEY,
        album_id bigint NOT NULL REFERENCES albums (id) ON DELETE CASCADE,
        kind     text NOT NULL CONSTRAINT album_refs_kind_check CHECK (kind IN ('spotify', 'name'))
    );
    CREATE INDEX album_refs_album ON album_refs (album_id);
    CREATE TABLE album_playlists (
        playlist text PRIMARY KEY,
        data     jsonb NOT NULL DEFAULT '{}'::jsonb
    );
    CREATE TABLE indexer_use (
        at   timestamptz NOT NULL,
        kind text NOT NULL CONSTRAINT indexer_use_kind_check CHECK (kind IN ('hit', 'grab', 'reimport'))
    );
    CREATE INDEX indexer_use_at ON indexer_use (at);
    CREATE TABLE playlist_stores (
        playlist   text PRIMARY KEY,
        source     text NOT NULL DEFAULT '',
        updated_at timestamptz NOT NULL DEFAULT now(),
        entries    jsonb NOT NULL
    );
    CREATE TABLE spotdl_failures (
        url   text PRIMARY KEY,
        entry jsonb NOT NULL
    );
    CREATE TABLE kv (
        name       text NOT NULL,
        key        text NOT NULL DEFAULT '',
        value      jsonb NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (name, key)
    );
    """,
    # 5: search overrides, holds and track pins, moved off the ConfigMap
    # (homelab#2484, read and written through music_fetch.overrides).  An
    # override or hold names its album's row, not a release group: most rows
    # have none until their import, and those are the albums that need an
    # override.  ``refs`` keeps the row's refs (Spotify album IDs, names, its
    # release group) as of the last save, so an override whose row is deleted
    # (a merge's loser, an album that left its playlist) re-attaches to the row
    # that next carries one of them.  A pin names a track by Spotify track ID
    # (what spotdl matches), with its ISRC when known.
    """
    ALTER TABLE events DROP CONSTRAINT events_kind_check;
    ALTER TABLE events ADD CONSTRAINT events_kind_check CHECK (kind IN (
        'import_done', 'import_wrong', 'import_incomplete', 'import_lost',
        'retire', 'local', 'merge', 'clash', 'mixed',
        'grab', 'autoreset', 'fallback', 'failure', 'reject', 'mcp_job',
        'album_requested', 'later_added', 'later_removed', 'spotify_id_resolved',
        'filled', 'wrong_version', 'moved', 'override_set', 'override_cleared'));
    CREATE TABLE album_overrides (
        id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        kind     text NOT NULL CONSTRAINT album_overrides_kind_check CHECK (kind IN ('search', 'hold')),
        album_id bigint REFERENCES albums (id) ON DELETE SET NULL,
        refs     text[] NOT NULL,
        words    text,
        reason   text NOT NULL DEFAULT '',
        set_by   text NOT NULL,
        set_at   timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT album_overrides_words_check CHECK ((kind = 'search') = (words IS NOT NULL))
    );
    CREATE UNIQUE INDEX album_overrides_album_kind ON album_overrides (album_id, kind) WHERE album_id IS NOT NULL;
    CREATE TABLE track_pins (
        id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        spotify_track_id text UNIQUE,
        isrc             text,
        url              text NOT NULL,
        label            text NOT NULL DEFAULT '',
        reason           text NOT NULL DEFAULT '',
        set_by           text NOT NULL,
        set_at           timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT track_pins_track_check CHECK (spotify_track_id IS NOT NULL OR isrc IS NOT NULL)
    );
    """,
]


def configured() -> bool:
    return bool(os.environ.get(ENV))


def connect(url: str | None = None) -> psycopg.Connection:
    """An autocommit connection to the pipeline's database, migrated to the latest schema."""
    conn = psycopg.connect(url or os.environ[ENV], autocommit=True, connect_timeout=CONNECT_TIMEOUT,
                           application_name="music-pipeline")
    try:
        migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def migrate(conn: psycopg.Connection) -> int:
    """Apply the migrations *conn*'s database lacks; returns its schema version."""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK,))
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                     "version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        row = conn.execute("SELECT coalesce(max(version), 0) FROM schema_migrations").fetchone()
        current = row[0] if row else 0
        if current > len(MIGRATIONS):
            # A newer image migrated it; this one (a rollback) runs on the tables it knows.
            logger.warning("music database is at schema %d, this image knows %d", current, len(MIGRATIONS))
            return current
        for version, sql in enumerate(MIGRATIONS[current:], start=current + 1):
            conn.execute(sql)
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
            logger.info("music database migrated to schema %d", version)
        return max(current, len(MIGRATIONS))
