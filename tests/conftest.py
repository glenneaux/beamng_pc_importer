"""Shared test fixtures.

These tests run inside Blender's bundled Python (see ``run_pytest.py``).
``bpy`` is expected to be importable; if it is not, every test is skipped so
running ``pytest`` outside Blender produces a clean message rather than a
hard import error.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_ROOT = REPO_ROOT / "tests" / "data"
TINY_VEHICLE_DIR = FIXTURES_ROOT / "vehicles" / "tiny"
TINY_PC = TINY_VEHICLE_DIR / "tiny.pc"

try:
    import bpy  # noqa: F401

    BLENDER_AVAILABLE = True
except ImportError:
    BLENDER_AVAILABLE = False


def pytest_collection_modifyitems(config, items):
    if BLENDER_AVAILABLE:
        return
    skip = pytest.mark.skip(reason="Blender not available; launch via tests/run_pytest.py")
    for item in items:
        item.add_marker(skip)


@pytest.fixture
def tiny_pc_path() -> Path:
    assert TINY_PC.exists(), f"Missing fixture vehicle at {TINY_PC}"
    return TINY_PC


@pytest.fixture
def tiny_vehicle_root() -> Path:
    return TINY_VEHICLE_DIR


@pytest.fixture
def reset_blender():
    """Fresh, empty scene for each test."""
    import bpy

    bpy.ops.wm.read_factory_settings(use_empty=True)
    yield
    bpy.ops.wm.read_factory_settings(use_empty=True)


@pytest.fixture
def addon_registered(reset_blender):
    """Register the add-on for one test, then unregister.

    Asserts that ``register()``/``unregister()`` are clean and the import
    operator is exposed at ``bpy.ops.import_scene.beamng_pc``.
    """
    import bpy

    addon = importlib.import_module("beamng_pc_importer")
    addon.register()
    try:
        assert hasattr(bpy.ops.import_scene, "beamng_pc")
        yield addon
    finally:
        addon.unregister()
