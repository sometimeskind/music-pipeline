"""The pipeline's Postgres database, ``music`` (homelab#2413).

One database for all of the pipeline's own state: the event store
(:mod:`music_fetch.events`) now, and later the album and playlist stores
(homelab#2410) and the request and Later tables (homelab#2478).  beets' library
stays SQLite: beets has no Postgres backend.

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
