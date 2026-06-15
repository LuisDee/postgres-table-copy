"""Shared test fixtures and the psycopg-boundary fakes.

We mock at the connection/cursor boundary (DB-API-ish) so unit tests never need
a real Postgres or even psycopg installed. Build connections through the
`make_conn` factory / `fake_conn` fixture rather than re-rolling mocks per test
(CLAUDE.md, Testing & TDD).
"""

from __future__ import annotations

from typing import Any

import pytest


class FakeDBError(Exception):
    """Stand-in for a psycopg error: exposes `.sqlstate` like the real thing."""

    def __init__(self, sqlstate: str, message: str | None = None):
        super().__init__(message or f"db error {sqlstate}")
        self.sqlstate = sqlstate


class FakeCursor:
    def __init__(self, conn: "FakeConnection"):
        self._conn = conn
        self._result: Any = None

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> "FakeCursor":
        self._conn.executed.append((sql, params))
        if self._conn.error_sqlstate is not None:
            raise FakeDBError(self._conn.error_sqlstate)
        self._result = self._conn.resolve(sql)
        return self

    def fetchone(self) -> Any:
        return self._result

    def fetchall(self) -> list[Any]:
        return list(self._result) if self._result is not None else []

    def close(self) -> None:  # pragma: no cover - trivial
        pass


class FakeConnection:
    """Resolves a fetched row by matching a substring of the SQL against
    `responses`. Set `error_sqlstate` to make every execute() raise."""

    def __init__(self, responses: dict[str, Any] | None = None, error_sqlstate: str | None = None):
        self.responses = responses or {}
        self.error_sqlstate = error_sqlstate
        self.executed: list[tuple[str, Any]] = []
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def resolve(self, sql: str) -> Any:
        low = sql.lower()
        for key, value in self.responses.items():
            if key.lower() in low:
                return value
        return None

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *exc: object) -> bool:
        self.close()
        return False


_DEFAULT_RESPONSES = {
    "version()": ("PostgreSQL 16.2 on x86_64-pc-linux-gnu, compiled by gcc",),
    "current_user": ("copyuser",),
    "has_schema_privilege": (True,),
}


@pytest.fixture
def make_conn():
    """Factory: make_conn(responses=..., error_sqlstate=...) -> FakeConnection."""

    def _make(responses: dict[str, Any] | None = None, error_sqlstate: str | None = None) -> FakeConnection:
        return FakeConnection(
            responses=dict(_DEFAULT_RESPONSES, **(responses or {})),
            error_sqlstate=error_sqlstate,
        )

    return _make


@pytest.fixture
def fake_conn(make_conn) -> FakeConnection:
    return make_conn()


@pytest.fixture
def db_error():
    """Factory for FakeDBError so tests can raise psycopg-shaped errors."""
    return FakeDBError


@pytest.fixture
def tmp_registry(tmp_path, monkeypatch):
    """Point the registry at a temp file via $PGCOPY_REGISTRY and return its path."""
    path = tmp_path / "endpoints.yaml"
    monkeypatch.setenv("PGCOPY_REGISTRY", str(path))
    return path
