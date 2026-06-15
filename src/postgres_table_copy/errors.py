"""Error model: categories, exit codes, exceptions, and scoped SQLSTATE handling.

The contract (docs/design.md §4.2 and §3.13):
  - a small, fixed exit-code table, stable from day 0;
  - every failure carries an `error_category` for the JSON envelope;
  - tolerated SQLSTATEs are matched **per operation**, never as a global union.
"""

from __future__ import annotations

from contextlib import contextmanager
from enum import Enum

# --- exit codes (docs/design.md §4.2) -------------------------------------
EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_TRANSIENT = 2
EXIT_DATA = 3
EXIT_INTERNAL = 4


class ErrorCategory(str, Enum):
    CONFIG = "CONFIG"        # bad user input / config
    AUTH = "AUTH"            # credentials / privileges
    TRANSIENT = "TRANSIENT"  # network, timeout, lock wait — retry might work
    DATA = "DATA"            # constraint violation, type mismatch, drift
    INTERNAL = "INTERNAL"    # bug in this tool


_CATEGORY_EXIT: dict[ErrorCategory, int] = {
    ErrorCategory.CONFIG: EXIT_CONFIG,
    ErrorCategory.AUTH: EXIT_CONFIG,       # auth is a user-fixable (exit 1) failure
    ErrorCategory.TRANSIENT: EXIT_TRANSIENT,
    ErrorCategory.DATA: EXIT_DATA,
    ErrorCategory.INTERNAL: EXIT_INTERNAL,
}


def exit_code_for(category: ErrorCategory) -> int:
    return _CATEGORY_EXIT[category]


# --- SQLSTATE → category --------------------------------------------------
# Mapped by 2-char SQLSTATE class (PostgreSQL Appendix A), with the one
# privilege override that doesn't follow its class.
_CLASS_CATEGORY: dict[str, ErrorCategory] = {
    "08": ErrorCategory.TRANSIENT,  # connection exception
    "40": ErrorCategory.TRANSIENT,  # transaction rollback (serialization, deadlock)
    "53": ErrorCategory.TRANSIENT,  # insufficient resources
    "57": ErrorCategory.TRANSIENT,  # operator intervention (cancel, shutdown)
    "58": ErrorCategory.TRANSIENT,  # system error
    "22": ErrorCategory.DATA,       # data exception
    "23": ErrorCategory.DATA,       # integrity constraint violation
    "28": ErrorCategory.AUTH,       # invalid authorization specification
    "42": ErrorCategory.CONFIG,     # syntax error or access rule violation
    "3D": ErrorCategory.CONFIG,     # invalid catalog name
    "3F": ErrorCategory.CONFIG,     # invalid schema name
}


def sqlstate_to_category(sqlstate: str | None) -> ErrorCategory:
    if not sqlstate:
        return ErrorCategory.INTERNAL
    if sqlstate == "42501":  # insufficient_privilege is an AUTH problem
        return ErrorCategory.AUTH
    return _CLASS_CATEGORY.get(sqlstate[:2], ErrorCategory.INTERNAL)


# --- exception hierarchy --------------------------------------------------
class PgcopyError(Exception):
    """Base for all expected, categorised failures."""

    category: ErrorCategory = ErrorCategory.INTERNAL

    @property
    def exit_code(self) -> int:
        return exit_code_for(self.category)


class ConfigError(PgcopyError):
    category = ErrorCategory.CONFIG


class AuthError(PgcopyError):
    category = ErrorCategory.AUTH


class TransientError(PgcopyError):
    category = ErrorCategory.TRANSIENT


class DataError(PgcopyError):
    category = ErrorCategory.DATA


class UnknownEndpointError(ConfigError):
    def __init__(self, name: str):
        super().__init__(f"unknown endpoint: {name!r}")
        self.name = name


class DuplicateEndpointError(ConfigError):
    def __init__(self, name: str):
        super().__init__(f"endpoint already exists: {name!r}")
        self.name = name


# --- scoped SQLSTATE tolerance (rule #4) ----------------------------------
@contextmanager
def tolerate(*sqlstates: str):
    """Swallow an exception only if its ``.sqlstate`` is in ``sqlstates``.

    Scope this to the single statement that legitimately produces the code
    (e.g. ``with tolerate("42P07"):`` around a CREATE). Anything else
    propagates. Works on any exception exposing ``.sqlstate`` (psycopg errors
    do), so we don't import psycopg here.
    """
    allowed = set(sqlstates)
    try:
        yield
    except Exception as exc:  # noqa: BLE001 — intentional, narrowed below
        state = getattr(exc, "sqlstate", None)
        if state in allowed:
            return
        raise
