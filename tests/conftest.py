"""Shared pytest config for the Xiaomi Vacuum tests."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

if (
    os.name != "nt"
    and importlib.util.find_spec("homeassistant")
    and importlib.util.find_spec("pytest_homeassistant_custom_component")
):
    pytest_plugins = "pytest_homeassistant_custom_component"


def _under(path: Path, *parts: str) -> bool:
    path_parts = path.parts
    wanted = tuple(parts)
    return any(path_parts[i : i + len(wanted)] == wanted for i in range(len(path_parts)))


def pytest_ignore_collect(collection_path, config):  # noqa: ARG001
    """Keep HA harness tests out of native-Windows/pure collection."""
    path = Path(str(collection_path))
    if not _under(path, "tests", "harness"):
        return None
    if os.name == "nt":
        return True
    if importlib.util.find_spec("homeassistant") is None:
        return True
    if importlib.util.find_spec("pytest_homeassistant_custom_component") is None:
        return True
    return None


def pytest_collection_modifyitems(items):
    """Mark tests by runner tier."""
    for item in items:
        path = Path(str(item.path))
        marker = pytest.mark.harness if _under(path, "tests", "harness") else pytest.mark.pure
        item.add_marker(marker)
