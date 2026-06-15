"""YAML-backed endpoint registry (topology only — no secrets).

Default location ``~/.pgcopy/endpoints.yaml``; override with ``$PGCOPY_REGISTRY``
or by passing ``path``. The file is written ``0o600``. Secrets never enter this
file (rule #9): it maps endpoint names to libpq *service* names; passwords live
in ``~/.pgpass``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from ..errors import ConfigError, DuplicateEndpointError, UnknownEndpointError
from .endpoint import Endpoint

DEFAULT_DIR = ".pgcopy"
DEFAULT_FILE = "endpoints.yaml"
SCHEMA_VERSION = 1


def default_registry_path() -> Path:
    env = os.environ.get("PGCOPY_REGISTRY")
    if env:
        return Path(env)
    return Path.home() / DEFAULT_DIR / DEFAULT_FILE


class EndpointRegistry:
    def __init__(self, path: str | os.PathLike[str] | None = None):
        self.path = Path(path) if path is not None else default_registry_path()

    # --- io ---------------------------------------------------------------
    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": SCHEMA_VERSION, "endpoints": {}}
        try:
            data = yaml.safe_load(self.path.read_text()) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"registry {self.path} is not valid YAML: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("endpoints", {}), dict):
            raise ConfigError(f"registry {self.path} is malformed (expected an 'endpoints' map)")
        data.setdefault("version", SCHEMA_VERSION)
        data.setdefault("endpoints", {})
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write atomically-ish, then lock down perms (even though no secrets).
        self.path.write_text(yaml.safe_dump(data, sort_keys=True, default_flow_style=False))
        self.path.chmod(0o600)

    # --- api --------------------------------------------------------------
    def list(self) -> list[Endpoint]:
        endpoints = self._read()["endpoints"]
        return [Endpoint.from_dict(name, endpoints[name]) for name in sorted(endpoints)]

    def get(self, name: str) -> Endpoint:
        endpoints = self._read()["endpoints"]
        if name not in endpoints:
            raise UnknownEndpointError(name)
        return Endpoint.from_dict(name, endpoints[name])

    def add(self, endpoint: Endpoint) -> Endpoint:
        data = self._read()
        if endpoint.name in data["endpoints"]:
            raise DuplicateEndpointError(endpoint.name)
        data["endpoints"][endpoint.name] = endpoint.to_payload()
        self._write(data)
        return endpoint

    def remove(self, name: str) -> None:
        data = self._read()
        if name not in data["endpoints"]:
            raise UnknownEndpointError(name)
        del data["endpoints"][name]
        self._write(data)
