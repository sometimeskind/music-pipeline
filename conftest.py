"""Every unit test runs on the music database, the pipeline's only state (homelab#2502).

Loaded for ``pytest fetch/tests scan/tests service/tests scripts/tests`` from the
repo root (CI; the root ``pytest.ini`` makes it pytest's rootdir, else it is
``fetch/`` and this file is never loaded) and as ``/app/conftest.py`` in the
dev image.  The Docker
integration tests (``tests/``, its own ``pytest.ini``) and the MCP adapter's
(``mcp/``, its own venv) have their own roots, so neither loads it.

With ``MUSIC_TEST_DATABASE_URL`` set (CI's postgres service; locally, a
throwaway ``postgres`` container), one database is created and migrated per
session and every test starts on it empty: :func:`_state_db` truncates the
tables the last test wrote and sets ``MUSIC_DATABASE_URL``.  A test that
wants a database of its own (a migration test) takes ``pg_url``
(``fetch/tests/pg_fixture.py``), which overrides it.

Without it, a test that reaches the database is skipped, not failed: the
state is the database's, so such a test has nothing to run against.
"""

from __future__ import annotations

import os
import uuid

import pytest

TEST_URL = os.environ.get("MUSIC_TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def _state_db_conn():
    """A migrated database on the test server for this session, as (its URL, an
    autocommit connection to it, the query that names its dirty tables and
    sequences), or None."""
    if not TEST_URL:
        yield None
        return
    import psycopg

    from music_fetch import db

    name = f"music_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(TEST_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    url = psycopg.conninfo.make_conninfo(TEST_URL, dbname=name)
    conn = db.connect(url)
    yield url, conn, _dirty_query(conn)
    conn.close()
    with psycopg.connect(TEST_URL, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def _dirty_query(conn) -> str:
    """One query naming the tables with rows (``t``) and the sequences past their
    start (``s``): most tests touch neither, so their reset is one round trip."""
    tables = [t for (t,) in conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'schema_migrations'")]
    sequences = [s for (s,) in conn.execute("SELECT sequencename FROM pg_sequences WHERE schemaname = 'public'")]
    checks = [f"SELECT 't', '{t}' WHERE EXISTS (SELECT 1 FROM \"{t}\")" for t in tables]
    checks += [f"SELECT 's', '{s}' FROM \"{s}\" WHERE is_called" for s in sequences]
    return " UNION ALL ".join(checks)


@pytest.fixture(autouse=True)
def _state_db(_state_db_conn, monkeypatch):
    """An empty, migrated state database for every test."""
    try:
        from music_fetch import db, events, state
    except ImportError:  # a test tree without the fetch package
        yield
        return
    if _state_db_conn is None:
        def skip(url=None):
            pytest.skip("the state is the music database's: set MUSIC_TEST_DATABASE_URL")

        monkeypatch.delenv(db.ENV, raising=False)
        monkeypatch.setattr(state, "connect", skip)
        yield
        return
    url, conn, dirty = _state_db_conn
    rows = conn.execute(dirty).fetchall()
    tables = ", ".join(f'"{name}"' for kind, name in rows if kind == "t")
    if tables:
        conn.execute(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")
    for name in (name for kind, name in rows if kind == "s"):
        conn.execute(f'ALTER SEQUENCE "{name}" RESTART')
    monkeypatch.setenv(db.ENV, url)
    events._reset()
    yield
    events._reset()
