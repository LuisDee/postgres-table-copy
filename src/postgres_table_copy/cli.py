"""`pgcopy` CLI entry point.

Cut 0 implements the endpoint registry (`endpoints add/list/remove/test`) and
the day-0 contract: a stable JSON envelope on every command and the fixed
exit-code table (docs/design.md §4). The phased data-plane commands
(`plan`/`run`/…) are registered as honest "not implemented" stubs so the
envelope/exit-code contract is visible and testable from the start; they gain
behaviour in their cuts (§6).
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterator

import click

from . import __version__
from .endpoints import Endpoint, EndpointRegistry
from .errors import (
    EXIT_INTERNAL,
    ErrorCategory,
    PgcopyError,
    exit_code_for,
    sqlstate_to_category,
)


def build_envelope(
    command: str,
    *,
    ok: bool,
    data: dict[str, Any] | None = None,
    error: str | None = None,
    error_category: ErrorCategory | str | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    """The single JSON shape every command emits (docs/design.md §4.1)."""
    category = error_category.value if isinstance(error_category, ErrorCategory) else error_category
    return {
        "ok": ok,
        "command": command,
        "job_id": job_id,
        "data": data if data is not None else {},
        "error": error,
        "error_category": category,
    }


def _emit(envelope: dict[str, Any]) -> None:
    click.echo(json.dumps(envelope, indent=2, default=str))


@contextmanager
def _run(command: str, job_id: str | None = None) -> Iterator[None]:
    """Run a command body, turning failures into a categorised envelope + the
    matching exit code. PgcopyError carries its own category; anything with a
    `.sqlstate` (psycopg errors) is mapped by SQLSTATE; everything else is an
    INTERNAL bug."""
    try:
        yield
    except PgcopyError as exc:
        _emit(build_envelope(command, ok=False, error=str(exc), error_category=exc.category, job_id=job_id))
        raise SystemExit(exc.exit_code)
    except Exception as exc:  # noqa: BLE001 — top-level boundary
        state = getattr(exc, "sqlstate", None)
        if state:
            category = sqlstate_to_category(state)
            _emit(build_envelope(command, ok=False, error=str(exc), error_category=category, job_id=job_id))
            raise SystemExit(exit_code_for(category))
        _emit(build_envelope(command, ok=False, error=repr(exc), error_category=ErrorCategory.INTERNAL, job_id=job_id))
        raise SystemExit(EXIT_INTERNAL)


_registry_option = click.option(
    "--registry",
    "registry_path",
    envvar="PGCOPY_REGISTRY",
    default=None,
    help="Path to endpoints.yaml (default ~/.pgcopy/endpoints.yaml or $PGCOPY_REGISTRY).",
)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="pgcopy")
def cli() -> None:
    """pgcopy — client-side PostgreSQL table copy across a fleet."""


@cli.group()
def endpoints() -> None:
    """Manage the endpoint registry (topology only; secrets live in .pgpass)."""


@endpoints.command("add")
@click.argument("name")
@click.option("--service", required=True, help="pg_service.conf service name.")
@click.option("--schema", default=None, help="Default schema for this endpoint.")
@click.option("--role", default=None, help="Role to assume, optional.")
@_registry_option
def endpoints_add(name: str, service: str, schema: str | None, role: str | None, registry_path: str | None) -> None:
    """Register endpoint NAME backed by a pg_service service."""
    with _run("endpoints add"):
        endpoint = Endpoint(name=name, service=service, schema=schema, role=role)
        EndpointRegistry(registry_path).add(endpoint)
        _emit(build_envelope("endpoints add", ok=True, data=endpoint.to_dict()))


@endpoints.command("list")
@_registry_option
def endpoints_list(registry_path: str | None) -> None:
    """List registered endpoints."""
    with _run("endpoints list"):
        items = [e.to_dict() for e in EndpointRegistry(registry_path).list()]
        _emit(build_envelope("endpoints list", ok=True, data={"endpoints": items}))


@endpoints.command("remove")
@click.argument("name")
@_registry_option
def endpoints_remove(name: str, registry_path: str | None) -> None:
    """Remove endpoint NAME."""
    with _run("endpoints remove"):
        EndpointRegistry(registry_path).remove(name)
        _emit(build_envelope("endpoints remove", ok=True, data={"removed": name}))


@endpoints.command("test")
@click.argument("name")
@_registry_option
def endpoints_test(name: str, registry_path: str | None) -> None:
    """Connect to endpoint NAME; report version and CREATE privilege."""
    with _run("endpoints test"):
        endpoint = EndpointRegistry(registry_path).get(name)
        _emit(build_envelope("endpoints test", ok=True, data=endpoint.probe()))


# --- phased commands: honest stubs until their cut (docs/design.md §6) ------
_PHASED = ("plan", "run", "status", "verify", "cancel", "cleanup", "wait", "copy")


# A tiny CONFIG-categorised error for the stubs, kept local to avoid leaking a
# "NotImplemented" type into the public error hierarchy.
class _NotImplementedYet(PgcopyError):
    category = ErrorCategory.CONFIG


def _make_stub(name: str):
    @click.argument("args", nargs=-1)
    def _stub(args: tuple[str, ...]) -> None:
        with _run(name):
            raise _NotImplementedYet(
                f"'{name}' is not implemented yet — see docs/design.md cuts roadmap (§6)"
            )

    _stub.__name__ = f"{name}_stub"
    return cli.command(name)(_stub)


for _name in _PHASED:
    _make_stub(_name)


def main() -> None:  # console-script entry point (pyproject [project.scripts])
    cli()


if __name__ == "__main__":  # pragma: no cover
    main()
