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

    print(
        json.dumps(
            {
                "fixture": str(fixture),
                "imported_parts": len(imported.parts),
                "node_move_patch_changes": len(node_changed),
                "full_patch_changes": len(changed),
                "semantic_diff": semantic_lines,
                "round_trip": round_trip,
            },
            indent=2,
        )
    )
    print("topology_import_edit_export_acceptance passed")


if __name__ == "__main__":
    main()
