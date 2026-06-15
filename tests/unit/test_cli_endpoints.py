"""CLI contract: JSON envelope, exit codes, endpoints commands, error mapping."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from postgres_table_copy.endpoints import Endpoint
from postgres_table_copy.errors import EXIT_CONFIG
from postgres_table_copy import cli as cli_module
from postgres_table_copy.cli import cli

ENVELOPE_KEYS = {"ok", "command", "job_id", "data", "error", "error_category"}


@pytest.fixture
def runner():
    return CliRunner()


def _envelope(result):
    return json.loads(result.output)


def test_add_then_list_roundtrip(runner, tmp_registry):
    add = runner.invoke(cli, ["endpoints", "add", "dev", "--service", "uk01", "--schema", "app"])
    assert add.exit_code == 0
    env = _envelope(add)
    assert set(env) == ENVELOPE_KEYS
    assert env["ok"] is True
    assert env["command"] == "endpoints add"
    assert env["data"] == {"name": "dev", "service": "uk01", "schema": "app"}

    lst = runner.invoke(cli, ["endpoints", "list"])
    assert lst.exit_code == 0
    assert _envelope(lst)["data"]["endpoints"] == [
        {"name": "dev", "service": "uk01", "schema": "app"}
    ]


def test_duplicate_add_is_config_error(runner, tmp_registry):
    runner.invoke(cli, ["endpoints", "add", "dev", "--service", "uk01"])
    dup = runner.invoke(cli, ["endpoints", "add", "dev", "--service", "other"])
    assert dup.exit_code == EXIT_CONFIG
    env = _envelope(dup)
    assert env["ok"] is False
    assert env["error_category"] == "CONFIG"
    assert "already exists" in env["error"]


def test_remove_unknown_is_config_error(runner, tmp_registry):
    res = runner.invoke(cli, ["endpoints", "remove", "ghost"])
    assert res.exit_code == EXIT_CONFIG
    assert _envelope(res)["error_category"] == "CONFIG"


def test_test_command_reports_probe(runner, tmp_registry, monkeypatch):
    runner.invoke(cli, ["endpoints", "add", "dev", "--service", "uk01"])
    monkeypatch.setattr(
        Endpoint,
        "probe",
        lambda self, connector=None: {"server_version": "PostgreSQL 16.2", "can_create": True},
    )
    res = runner.invoke(cli, ["endpoints", "test", "dev"])
    assert res.exit_code == 0
    env = _envelope(res)
    assert env["ok"] is True
    assert env["data"]["can_create"] is True


def test_test_command_maps_sqlstate_to_category(runner, tmp_registry, monkeypatch, db_error):
    runner.invoke(cli, ["endpoints", "add", "dev", "--service", "uk01"])

    def boom(self, connector=None):
        raise db_error("28000")  # invalid_authorization → AUTH → exit 1

    monkeypatch.setattr(Endpoint, "probe", boom)
    res = runner.invoke(cli, ["endpoints", "test", "dev"])
    assert res.exit_code == EXIT_CONFIG  # AUTH maps to exit 1
    assert _envelope(res)["error_category"] == "AUTH"


@pytest.mark.parametrize("command", ["plan", "run", "status", "verify", "cancel", "cleanup", "wait", "copy"])
def test_phased_commands_are_honest_stubs(runner, command):
    res = runner.invoke(cli, [command])
    assert res.exit_code == EXIT_CONFIG
    env = _envelope(res)
    assert set(env) == ENVELOPE_KEYS
    assert env["ok"] is False
    assert env["command"] == command
    assert env["error_category"] == "CONFIG"
    assert "not implemented" in env["error"]


def test_build_envelope_shape_is_stable():
    env = cli_module.build_envelope("x", ok=True, data={"a": 1})
    assert set(env) == ENVELOPE_KEYS
    assert env["error"] is None and env["error_category"] is None and env["job_id"] is None
