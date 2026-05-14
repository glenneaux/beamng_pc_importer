"""Boundary tests: PC import operator -> Blender scene state.

These drive the registered ``bpy.ops.import_scene.beamng_pc`` operator and
assert observable scene state. They are the system's top-level integration
boundary.

The fixture vehicle deliberately has no flexbodies (and no DAE assets), so
the operator is expected to refuse the import with a clear error. The tests
pin that contract --- "tiny PC without flexbodies is rejected" is a real
guarantee callers depend on.

A richer happy-path test would need a fixture that includes at least one
flexbody plus a DAE the asset catalog can resolve. That work is intentionally
deferred; when it lands, the no-flexbody tests below should remain (with a
counterpart happy-path test added alongside).
"""

from __future__ import annotations

import pytest


def test_register_and_unregister_are_clean(addon_registered):
    """Registering then unregistering must not raise."""
    import bpy

    assert hasattr(bpy.ops.import_scene, "beamng_pc")


def test_import_without_flexbodies_is_rejected(addon_registered, tiny_pc_path):
    """The importer must refuse a config that resolves to zero flexbodies.

    Pins the user-visible failure contract --- without this guard the import
    would silently produce an empty Blender scene.
    """
    import bpy

    with pytest.raises(RuntimeError, match="No flexbodies were resolved"):
        bpy.ops.import_scene.beamng_pc(
            filepath=str(tiny_pc_path),
            clear_existing=True,
            include_jbeam_visuals=True,
            create_experimental_jbeam_meshes=False,
            include_user_overrides=False,
            vanilla_data_only=True,
        )


def test_failed_import_still_populates_pc_path_metadata(addon_registered, tiny_pc_path):
    """Scene metadata about the source PC is written before the resolver runs.

    Even when the import is cancelled (no flexbodies), the editor needs the
    source-path metadata so the slot editor and reports can refer back to it.
    """
    import bpy

    with pytest.raises(RuntimeError):
        bpy.ops.import_scene.beamng_pc(
            filepath=str(tiny_pc_path),
            clear_existing=True,
            include_jbeam_visuals=False,
            create_experimental_jbeam_meshes=False,
            include_user_overrides=False,
            vanilla_data_only=True,
        )

    scene = bpy.context.scene
    assert scene["beamng_slot_editor_source_pc_path"] == str(tiny_pc_path)
    # Vanilla-only import: overrides must be off.
    assert scene["beamng_import_include_user_overrides"] is False
