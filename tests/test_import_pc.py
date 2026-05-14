"""Boundary tests: PC import operator -> Blender scene state.

These drive the registered ``bpy.ops.import_scene.beamng_pc`` operator on the
fixture vehicle and assert observable scene state. They are the system's
top-level integration boundary.
"""

from __future__ import annotations


def test_register_and_unregister_are_clean(addon_registered):
    """Registering then unregistering must not raise."""
    import bpy

    assert hasattr(bpy.ops.import_scene, "beamng_pc")


def test_import_tiny_vehicle_creates_collections(addon_registered, tiny_pc_path):
    import bpy

    result = bpy.ops.import_scene.beamng_pc(
        filepath=str(tiny_pc_path),
        clear_existing=True,
        include_jbeam_visuals=True,
        selectable_jbeam_debug=False,
        show_jbeam_node_labels=False,
        create_experimental_jbeam_meshes=False,
        include_user_overrides=False,
        vanilla_data_only=True,
    )
    assert result == {"FINISHED"}

    collection_names = [c.name for c in bpy.data.collections]
    # The importer creates at least one collection whose name contains the
    # PC stem ("tiny"). Exact naming is an internal detail; this check pins
    # the boundary contract: a successful import produces a discoverable
    # collection rooted on the .pc filename.
    assert any("tiny" in name.lower() for name in collection_names), collection_names


def test_import_populates_scene_metadata(addon_registered, tiny_pc_path):
    import bpy

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


def test_import_creates_experimental_jbeam_mesh_when_requested(addon_registered, tiny_pc_path):
    """Experimental JBeam mesh path: nodes -> vertices, beams -> edges, triangles -> faces."""
    import bpy

    bpy.ops.import_scene.beamng_pc(
        filepath=str(tiny_pc_path),
        clear_existing=True,
        include_jbeam_visuals=False,
        create_experimental_jbeam_meshes=True,
        include_user_overrides=False,
        vanilla_data_only=True,
    )

    jbeam_meshes = [obj for obj in bpy.data.objects if obj.type == "MESH" and obj.get("beamng_importer_mesh_editing_enabled")]
    assert jbeam_meshes, "No experimental JBeam mesh object was created"

    mesh = jbeam_meshes[0].data
    # 4 nodes -> 4 vertices, 4 beams -> at least 4 edges (triangles add 3 more),
    # 1 triangle -> 1 face. Exact edge count depends on how beams + triangle
    # edges overlap, so check inclusively.
    assert len(mesh.vertices) == 4
    assert len(mesh.polygons) == 1
    assert len(mesh.edges) >= 4
