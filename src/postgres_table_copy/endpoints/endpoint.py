"""`Endpoint` — one named, reusable connection target.

An endpoint stores only non-secret topology: a name, the libpq *service* it
resolves through (`~/.pg_service.conf`), and optional default schema / role.
Secrets live in `~/.pgpass` (rule #9) — never here. Connections are made by
libpq via `service=…`, so the password is resolved from `.pgpass`/the service
file and never passes through our process as plaintext.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..errors import ConfigError

# A connector takes a libpq conninfo string and returns a DB-API connection.
Connector = Callable[[str], Any]


@dataclass(frozen=True)
class Endpoint:
    name: str
    service: str
    schema: str | None = None
    role: str | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ConfigError("endpoint name must be non-empty")
        if not self.service or not self.service.strip():
            raise ConfigError("endpoint service must be non-empty")

    # --- (de)serialisation: registry payloads carry no secrets -------------
    def to_payload(self) -> dict[str, str]:
        payload: dict[str, str] = {"service": self.service}
        if self.schema:
            payload["schema"] = self.schema
        if self.role:
            payload["role"] = self.role
        return payload

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, **self.to_payload()}

    @classmethod
    def from_dict(cls, name: str, payload: Any) -> "Endpoint":
        if not isinstance(payload, dict) or "service" not in payload:
            raise ConfigError(f"endpoint {name!r} is missing required 'service'")
        return cls(
            name=name,
            service=payload["service"],
            schema=payload.get("schema"),
            role=payload.get("role"),
        )

    # --- connection -------------------------------------------------------
    def conninfo(self) -> str:
        """libpq conninfo. Topology only; secrets come from .pgpass."""
        return f"service={self.service}"

    def connect(self, connector: Connector | None = None) -> Any:
        """Open a connection. `connector` is injectable for tests; defaults to
        a lazily-imported `psycopg.connect` so importing this module never
        requires psycopg."""
        if connector is None:
            import psycopg  # lazy: keeps unit tests psycopg-free

            connector = psycopg.connect
        return connector(self.conninfo())

    def probe(self, connector: Connector | None = None) -> dict[str, Any]:
        """Connect and report server version, current user, and whether the
        role can CREATE in the target schema — the privilege the engine needs.
        Used by `pgcopy endpoints test`."""
        conn = self.connect(connector=connector)
        try:
            schema = self.schema or "public"
            with conn.cursor() as cur:
                cur.execute("SELECT version()")
                version = cur.fetchone()[0]
                cur.execute("SELECT current_user")
                current_user = cur.fetchone()[0]
                cur.execute("SELECT has_schema_privilege(%s, 'CREATE')", (schema,))
                can_create = bool(cur.fetchone()[0])
            return {
                "service": self.service,
                "server_version": version,
                "current_user": current_user,
                "schema": schema,
                "can_create": can_create,
            }
        finally:
            conn.close()
