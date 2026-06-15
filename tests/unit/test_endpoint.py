"""Endpoint: validation, (de)serialisation, conninfo, connect/probe."""

from __future__ import annotations

import pytest

from postgres_table_copy.endpoints import Endpoint
from postgres_table_copy.errors import ConfigError


def test_minimal_endpoint_roundtrips_payload_without_secrets():
    ep = Endpoint(name="dev_uk01", service="uk01")
    assert ep.to_payload() == {"service": "uk01"}
    assert ep.to_dict() == {"name": "dev_uk01", "service": "uk01"}
    # no password/secret field exists anywhere
    assert "password" not in ep.to_dict()


def test_full_endpoint_payload_and_from_dict():
    ep = Endpoint(name="p", service="svc", schema="app", role="copyuser")
    payload = ep.to_payload()
    assert payload == {"service": "svc", "schema": "app", "role": "copyuser"}
    assert Endpoint.from_dict("p", payload) == ep


def test_from_dict_requires_service():
    with pytest.raises(ConfigError):
        Endpoint.from_dict("p", {"schema": "app"})


@pytest.mark.parametrize("name, service", [("", "svc"), ("  ", "svc"), ("p", ""), ("p", "  ")])
def test_rejects_empty_name_or_service(name, service):
    with pytest.raises(ConfigError):
        Endpoint(name=name, service=service)


def test_conninfo_is_service_only():
    assert Endpoint("p", "svc").conninfo() == "service=svc"


def test_connect_uses_injected_connector_with_conninfo(fake_conn):
    captured = {}

    def connector(conninfo):
        captured["conninfo"] = conninfo
        return fake_conn

    conn = Endpoint("p", "svc").connect(connector=connector)
    assert conn is fake_conn
    assert captured["conninfo"] == "service=svc"


def test_probe_reports_version_user_and_create_privilege(fake_conn):
    info = Endpoint("p", "svc", schema="app").probe(connector=lambda _ci: fake_conn)
    assert info["server_version"].startswith("PostgreSQL 16")
    assert info["current_user"] == "copyuser"
    assert info["schema"] == "app"
    assert info["can_create"] is True
    assert fake_conn.closed is True  # connection always closed


def test_probe_defaults_schema_to_public(fake_conn):
    info = Endpoint("p", "svc").probe(connector=lambda _ci: fake_conn)
    assert info["schema"] == "public"


def test_probe_surfaces_can_create_false(make_conn):
    conn = make_conn(responses={"has_schema_privilege": (False,)})
    info = Endpoint("p", "svc").probe(connector=lambda _ci: conn)
    assert info["can_create"] is False
