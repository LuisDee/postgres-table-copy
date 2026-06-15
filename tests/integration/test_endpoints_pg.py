"""Integration: probe a real PostgreSQL.

Gated behind `-m integration` (deselected by default). Runs against
`$PGCOPY_TEST_DSN` if set; otherwise skips cleanly. A testcontainers-based
fixture (postgres:16) lands with the data-plane cuts that actually need a DB.
"""

from __future__ import annotations

import os

import pytest

from postgres_table_copy.endpoints import Endpoint

pytestmark = pytest.mark.integration

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("PGCOPY_TEST_DSN")


@pytest.fixture
def connector():
    if not DSN:
        pytest.skip("set $PGCOPY_TEST_DSN to run integration tests")
    return lambda _conninfo: psycopg.connect(DSN)


def test_probe_reports_server_version_and_privilege(connector):
    info = Endpoint("it", "ignored-uses-dsn").probe(connector=connector)
    assert "PostgreSQL" in info["server_version"]
    assert isinstance(info["can_create"], bool)
    assert info["current_user"]
