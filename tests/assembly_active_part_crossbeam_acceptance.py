"""Blender-run acceptance smoke for assembly Active Part and crossbeam workflow.

Run with:
  blender --background --factory-startup --python tests/assembly_active_part_crossbeam_acceptance.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import bmesh
import bpy


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT.parent))

import beamng_pc_importer as addon  # noqa: E402


def make_part(name, part_name, part_id, node_ids, x):
    mesh = bpy.data.meshes.new(name + "Mesh")
    mesh.from_pydata([(x + i, 0, 0) for i in range(len(node_ids))], [], [])
    mesh.update()
    mesh["beamng_node_ids_json"] = json.dumps(node_ids)
    mesh["beamng_node_kinds_json"] = json.dumps(["owned"] * len(node_ids))
    mesh["beamng_node_owner_part_ids_json"] = json.dumps([part_id] * len(node_ids))
    mesh["beamng_original_node_positions_json"] = json.dumps([[x + i, 0, 0] for i in range(len(node_ids))])
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    obj["beamng_visual_type"] = "experimental_jbeam_mesh"
    obj["beamng_part_name"] = part_name
    obj["beamng_part_guid"] = part_name + "_guid"
    obj["beamng_jbeam_path"] = f"vehicles/test/{part_name}.jbeam"
    obj["beamng_resolved_part_id"] = part_id
    obj["beamng_owned_node_count"] = len(node_ids)
    obj["beamng_proxy_node_count"] = 0
    return obj


def select_one_edit_vertex(obj, index):
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    addon.sync_active_jbeam_part_from_selection(bpy.context)
    bpy.ops.object.mode_set(mode="EDIT")
    bm = bmesh.from_edit_mesh(obj.data)
    bm.verts.ensure_lookup_table()
    for vert in bm.verts:
        vert.select = False
    bm.verts[index].select = True
    bmesh.update_edit_mesh(obj.data)


def main():
    try:
        addon.register()
    except ValueError:
        pass

    target = make_part("Target", "target_part", 1, ["ta", "tb"], 0)
    source = make_part("Source", "source_part", 2, ["sx"], 10)

    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    source.select_set(True)
    bpy.context.view_layer.objects.active = source
    addon.sync_active_jbeam_part_from_selection(bpy.context)
    addon.store_proxy_clipboard_nodes(
        bpy.context.scene,
        [{
            "node_id": "sx",
            "world_position": [10, 0, 0],
            "owner_part_id": 2,
            "source_object": "Source",
        }],
    )

    select_one_edit_vertex(target, 0)
    result = bpy.ops.beamng_pc_importer.create_crossbeam_to_marked_node()
    assert result == {"FINISHED"}

    identity = addon.ensure_experimental_mesh_identity(target, bpy.context.scene, allow_write=False)
    assert "sx" in [str(value) for value in identity["node_ids"]]
    assert "sx" in str(target.data.get("beamng_explicit_beam_edge_keys_json", ""))

    bpy.ops.object.mode_set(mode="OBJECT")
    result = bpy.ops.beamng_pc_importer.validate_jbeam_assembly()
    assert result == {"FINISHED"}
    assert bpy.context.scene.get("beamng_jbeam_last_assembly_validation_status") in {"ok", "blocked"}
    print("assembly_active_part_crossbeam_acceptance passed")


if __name__ == "__main__":
    main()
