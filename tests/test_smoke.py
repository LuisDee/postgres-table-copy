"""Smoke tests — prove the package imports and the CLI envelope is well-formed.

Real behaviour tests arrive with each cut (see docs/design.md §8). Test-first
is the rule: new behaviour starts with a failing test here (or in tests/unit/).
"""

from __future__ import annotations

import json

from postgres_table_copy import __version__
from postgres_table_copy.cli import EXIT_CONFIG, main


def test_version_present():
    assert isinstance(__version__, str)


def test_cli_emits_json_envelope(capsys):
    rc = main(["plan"])
    assert rc == EXIT_CONFIG  # not implemented yet
    out = json.loads(capsys.readouterr().out)
    # The JSON envelope shape is part of the contract from day 0 (design §4.1).
    assert set(out) == {"ok", "command", "job_id", "data", "error", "error_category"}
    assert out["command"] == "plan"
    assert out["ok"] is False
