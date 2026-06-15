"""Error model: exit codes, SQLSTATE→category, scoped tolerance."""

from __future__ import annotations

import pytest

from postgres_table_copy.errors import (
    EXIT_CONFIG,
    EXIT_DATA,
    EXIT_INTERNAL,
    EXIT_TRANSIENT,
    AuthError,
    ConfigError,
    DataError,
    DuplicateEndpointError,
    ErrorCategory,
    PgcopyError,
    TransientError,
    UnknownEndpointError,
    exit_code_for,
    sqlstate_to_category,
    tolerate,
)


@pytest.mark.parametrize(
    "exc_cls, category, exit_code",
    [
        (ConfigError, ErrorCategory.CONFIG, EXIT_CONFIG),
        (AuthError, ErrorCategory.AUTH, EXIT_CONFIG),  # auth → user-fixable (1)
        (TransientError, ErrorCategory.TRANSIENT, EXIT_TRANSIENT),
        (DataError, ErrorCategory.DATA, EXIT_DATA),
        (PgcopyError, ErrorCategory.INTERNAL, EXIT_INTERNAL),
    ],
)
def test_exception_category_and_exit_code(exc_cls, category, exit_code):
    exc = exc_cls("boom")
    assert exc.category is category
    assert exc.exit_code == exit_code
    assert exit_code_for(category) == exit_code


def test_registry_errors_are_config_with_name():
    assert UnknownEndpointError("dev").category is ErrorCategory.CONFIG
    assert DuplicateEndpointError("dev").name == "dev"
    assert "dev" in str(UnknownEndpointError("dev"))


@pytest.mark.parametrize(
    "sqlstate, expected",
    [
        ("23505", ErrorCategory.DATA),       # unique_violation
        ("23503", ErrorCategory.DATA),       # foreign_key_violation
        ("28000", ErrorCategory.AUTH),       # invalid_authorization
        ("42501", ErrorCategory.AUTH),       # insufficient_privilege (override)
        ("42P01", ErrorCategory.CONFIG),     # undefined_table
        ("42P07", ErrorCategory.CONFIG),     # duplicate_table
        ("40001", ErrorCategory.TRANSIENT),  # serialization_failure
        ("40P01", ErrorCategory.TRANSIENT),  # deadlock_detected
        ("08006", ErrorCategory.TRANSIENT),  # connection_failure
        ("57014", ErrorCategory.TRANSIENT),  # query_canceled
        ("XX000", ErrorCategory.INTERNAL),   # internal_error
        (None, ErrorCategory.INTERNAL),
        ("", ErrorCategory.INTERNAL),
    ],
)
def test_sqlstate_to_category(sqlstate, expected):
    assert sqlstate_to_category(sqlstate) is expected


def test_tolerate_swallows_only_listed_sqlstate(db_error):
    # tolerated → swallowed
    with tolerate("42P07"):
        raise db_error("42P07")

    # not tolerated → propagates
    with pytest.raises(Exception) as ei:
        with tolerate("42P07"):
            raise db_error("23505")
    assert ei.value.sqlstate == "23505"


def test_tolerate_reraises_when_no_sqlstate():
    with pytest.raises(ValueError):
        with tolerate("42P07"):
            raise ValueError("not a db error")
