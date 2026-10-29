"""A fresh Postgres database per test, for the tests that need one (homelab#2413, #2410).

Imported by the tests that use it (``from pg_fixture import pg_url``): a
conftest.py here would clash with scan/tests', both packages being ``tests``.

They run when ``MUSIC_TEST_DATABASE_URL`` names a server whose role may create
databases (CI's postgres service); see test_events for running one locally.
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest

from music_fetch import db, events

TEST_URL = os.environ.get("MUSIC_TEST_DATABASE_URL")


@pytest.fixture
def pg_url(monkeypatch):
    """A fresh, empty database on the test server, set as MUSIC_DATABASE_URL."""
    name = f"music_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(TEST_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    url = psycopg.conninfo.make_conninfo(TEST_URL, dbname=name)
    monkeypatch.setenv(db.ENV, url)
    yield url
    events._reset()
    with psycopg.connect(TEST_URL, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
