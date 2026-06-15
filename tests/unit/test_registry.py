"""EndpointRegistry: CRUD, persistence, perms, error cases."""

from __future__ import annotations

import stat

import pytest

from postgres_table_copy.endpoints import Endpoint, EndpointRegistry, default_registry_path
from postgres_table_copy.errors import ConfigError, DuplicateEndpointError, UnknownEndpointError


def test_empty_registry_lists_nothing(tmp_registry):
    assert EndpointRegistry(tmp_registry).list() == []


def test_add_get_roundtrip(tmp_registry):
    reg = EndpointRegistry(tmp_registry)
    ep = Endpoint("dev", "svc", schema="app")
    reg.add(ep)
    assert reg.get("dev") == ep


def test_add_persists_across_instances(tmp_registry):
    EndpointRegistry(tmp_registry).add(Endpoint("dev", "svc"))
    # a fresh instance reads the same file
    assert EndpointRegistry(tmp_registry).get("dev").service == "svc"


def test_list_is_sorted_by_name(tmp_registry):
    reg = EndpointRegistry(tmp_registry)
    reg.add(Endpoint("b", "s2"))
    reg.add(Endpoint("a", "s1"))
    assert [e.name for e in reg.list()] == ["a", "b"]


def test_duplicate_add_raises(tmp_registry):
    reg = EndpointRegistry(tmp_registry)
    reg.add(Endpoint("dev", "svc"))
    with pytest.raises(DuplicateEndpointError):
        reg.add(Endpoint("dev", "other"))


def test_get_unknown_raises(tmp_registry):
    with pytest.raises(UnknownEndpointError):
        EndpointRegistry(tmp_registry).get("nope")


def test_remove(tmp_registry):
    reg = EndpointRegistry(tmp_registry)
    reg.add(Endpoint("dev", "svc"))
    reg.remove("dev")
    assert reg.list() == []


def test_remove_unknown_raises(tmp_registry):
    with pytest.raises(UnknownEndpointError):
        EndpointRegistry(tmp_registry).remove("nope")


def test_file_is_chmod_600(tmp_registry):
    EndpointRegistry(tmp_registry).add(Endpoint("dev", "svc"))
    mode = stat.S_IMODE(tmp_registry.stat().st_mode)
    assert mode == 0o600


def test_no_secret_fields_persisted(tmp_registry):
    EndpointRegistry(tmp_registry).add(Endpoint("dev", "svc", schema="app", role="r"))
    text = tmp_registry.read_text().lower()
    assert "password" not in text and "secret" not in text


def test_malformed_yaml_raises_config_error(tmp_registry):
    tmp_registry.write_text("endpoints: [this, is, a, list, not, a, map]\n")
    with pytest.raises(ConfigError):
        EndpointRegistry(tmp_registry).list()


def test_env_var_drives_default_path(tmp_registry):
    # tmp_registry sets $PGCOPY_REGISTRY
    assert default_registry_path() == tmp_registry


def test_explicit_path_overrides_env(tmp_path, tmp_registry):
    other = tmp_path / "other.yaml"
    reg = EndpointRegistry(other)
    reg.add(Endpoint("x", "svc"))
    assert other.exists()
    # the env-pointed file is untouched
    assert EndpointRegistry(tmp_registry).list() == []
