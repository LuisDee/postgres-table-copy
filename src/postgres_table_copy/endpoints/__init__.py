"""Endpoint abstraction + registry (Cut 0)."""

from .endpoint import Endpoint
from .registry import EndpointRegistry, default_registry_path

__all__ = ["Endpoint", "EndpointRegistry", "default_registry_path"]
