"""`pgcopy` entry point (skeleton).

This is a placeholder so the package and console-script wire up cleanly. The
real subcommands (`endpoints`, `plan`, `run`, `status`, `verify`, `cancel`,
`cleanup`, `wait`, `copy`) land per the cuts roadmap in docs/design.md §6,
starting with Cut 0 (endpoint registry). No data-plane code exists yet.
"""

from __future__ import annotations

import json
import sys

from . import __version__

# Exit codes — see docs/design.md §4.2. Kept here so they're stable from day 0.
EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_TRANSIENT = 2
EXIT_DATA = 3
EXIT_INTERNAL = 4


def main(argv: list[str] | None = None) -> int:
    """Minimal entry point. Real Click command tree arrives in Cut 0."""
    argv = sys.argv[1:] if argv is None else argv
    envelope = {
        "ok": False,
        "command": argv[0] if argv else None,
        "job_id": None,
        "data": {"version": __version__, "status": "pre-implementation"},
        "error": "not implemented yet — see docs/design.md cuts roadmap (§6)",
        "error_category": "CONFIG",
    }
    print(json.dumps(envelope, indent=2))
    return EXIT_CONFIG


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
