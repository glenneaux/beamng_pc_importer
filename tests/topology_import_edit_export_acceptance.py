"""Blender-run acceptance smoke for the topology import/edit/export prototype.

Run with:
  blender --background --python tests/topology_import_edit_export_acceptance.py
"""

import importlib
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = REPO_ROOT.parent
sys.path.insert(0, str(PACKAGE_PARENT))

for name in list(sys.modules):
    if name == "beamng_pc_importer" or name.startswith("beamng_pc_importer."):
        sys.modules.pop(name, None)

addon = importlib.import_module("beamng_pc_importer")


def main():
    fixture = REPO_ROOT / "tests" / "data" / "vehicles" / "topology_subset" / "subset.jbeam"
    triangles_only_fixture = REPO_ROOT / "tests" / "data" / "vehicles" / "topology_subset" / "triangles_only.jbeam"
    source = fixture.read_text(encoding="utf-8")
    imported = addon.import_jbeam_topology_subset(fixture)
    assert len(imported.parts) == 1
    part = imported.parts[0]
    assert part.nodes and part.beams and part.triangles
    assert "customUnknown" in part.unknown_preserved_sections

    node_move_group = {
        "virtual_path": "vehicles/topology_subset/subset.jbeam",
        "parts": [
            {
                "part": part.part_name,
                "node_updates": [{"node": "a", "new_position": [0.25, 0, 0]}],
                "beam_inserts": [],
                "beam_deletes": [],
            }
        ],
    }
    node_patched, node_changed, node_skipped = addon.apply_jbeam_updates_to_source_text(source, node_move_group)
    assert not node_skipped
    assert len(node_changed) == 1
    assert "customUnknown" in node_patched
    assert "0.25" in node_patched

    full_group = {
        "virtual_path": "vehicles/topology_subset/subset.jbeam",
        "parts": [
            {
                "part": part.part_name,
                "node_updates": [{"node": "a", "new_position": [0.25, 0, 0]}],
                "node_inserts": [],
                "node_deletes": [],
                "beam_inserts": [{"nodes": ["a", "c"]}],
                "beam_deletes": [{"nodes": ["a", "b"]}],
                "triangle_inserts": [],
                "triangle_deletes": [],
            }
        ],
    }
    patched, changed, skipped = addon.apply_jbeam_updates_to_source_text(source, full_group)
    assert not skipped
    assert len(changed) == 3
    assert "customUnknown" in patched
    assert '["a", "c"]' in patched

    semantic_diff = addon.semantic_diff_for_file_group(full_group)
    semantic_lines = addon.semantic_diff_summary_lines(semantic_diff)
    assert semantic_diff["change_count"] == 3
    assert any("Node moved" in line for line in semantic_lines)
    assert any("Beam added" in line for line in semantic_lines)
    assert any("Beam deleted" in line for line in semantic_lines)

    round_trip = addon.round_trip_validate_patched_jbeam_text(patched, full_group)
    assert round_trip["status"] == "pass", round_trip

    triangles_only = addon.import_jbeam_topology_subset(triangles_only_fixture)
    assert len(triangles_only.parts) == 1
    assert len(triangles_only.parts[0].beams) == 0
    assert len(triangles_only.parts[0].triangles) == 1

    root = addon.bpy.data.collections.new("Triangle Only Boundary Regression")
    addon.bpy.context.scene.collection.children.link(root)
    created = addon.create_imported_jbeam_topology_meshes(triangles_only, root)
    assert created == 1
    mesh_objects = []
    for collection in root.children:
        mesh_objects.extend(
            obj for obj in collection.objects
            if obj.get("beamng_imported_topology_subset")
        )
    assert len(mesh_objects) == 1
    obj = mesh_objects[0]
    mesh = obj.data
    assert len(mesh.polygons) == 1
    assert len(mesh.edges) == 3
    assert addon.mesh_json_list(mesh, "beamng_edge_node_ids_json") == []
    assert sorted(addon.mesh_json_list(mesh, "beamng_mesh_edge_node_ids_json")) == [
        ["ta", "tb"],
        ["ta", "tc"],
        ["tb", "tc"],
    ]
    snapshot = addon.semantic_topology_snapshot_for_object(obj, addon.bpy.context.scene, allow_write=True)
    edge_types = {tuple(edge["node_ids"]): edge["semantic_type"] for edge in snapshot["edges"]}
    assert set(edge_types.values()) == {addon.JBEAM_EDGE_SEMANTIC_TRIANGLE_BOUNDARY}
    scan = addon.scan_experimental_jbeam_mesh_edits(addon.bpy.context.scene, active_only=False)
    assert not any(change.get("section") == "beams" for change in scan["changes"]), scan["changes"]

    print(
        json.dumps(
            {
                "fixture": str(fixture),
                "imported_parts": len(imported.parts),
                "node_move_patch_changes": len(node_changed),
                "full_patch_changes": len(changed),
                "semantic_diff": semantic_lines,
                "round_trip": round_trip,
                "triangle_only_boundary_edges": len(mesh.edges),
            },
            indent=2,
        )
    )
    print("topology_import_edit_export_acceptance passed")


if __name__ == "__main__":
    main()
