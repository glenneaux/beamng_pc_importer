"""Boundary tests: data files in -> resolved authoring model out.

These exercise the JBeam / PC parser pipeline end-to-end without involving
Blender's UI or scene graph. They require ``mathutils`` (Vector/Matrix), which
Blender provides, hence they live under integration/.
"""

from __future__ import annotations

import json

import pytest

import core
import resolved_model as rm


@pytest.fixture
def parsed_tiny(tiny_pc_path, tiny_vehicle_root):
    """Parse the fixture vehicle through the real resolver pipeline."""
    pc_data = core.load_jsonc(tiny_pc_path)
    part_index = core.parse_parts_index(tiny_vehicle_root)
    return pc_data, part_index


def test_pc_file_parses_as_jsonc(parsed_tiny):
    pc_data, _part_index = parsed_tiny
    assert pc_data["mainPartName"] == "tiny_main"
    assert pc_data["model"] == "tiny"


def test_part_index_finds_main_part(parsed_tiny):
    _pc_data, part_index = parsed_tiny
    assert "tiny_main" in part_index
    assert part_index["tiny_main"].data["slotType"] == "main"


def test_resolver_produces_expected_topology(parsed_tiny, tiny_pc_path):
    pc_data, part_index = parsed_tiny

    (
        resolved_parts,
        flexbodies,
        visual_nodes,
        visual_beams,
        visual_triangles,
        visual_hydros,
        visual_rails,
        visual_slidenodes,
    ) = core.resolve_selected_parts(pc_data, part_index)

    assert len(resolved_parts) == 1
    assert resolved_parts[0].part_def.name == "tiny_main"

    assert {node.name for node in visual_nodes} == {"n1", "n2", "n3", "n4"}
    assert len(visual_beams) == 4
    assert {(b.id1, b.id2) for b in visual_beams} == {
        ("n1", "n2"),
        ("n2", "n3"),
        ("n3", "n4"),
        ("n4", "n1"),
    }
    assert len(visual_triangles) == 1
    assert flexbodies == []
    assert visual_hydros == []
    assert visual_rails == []
    assert visual_slidenodes == []


def test_resolver_preserves_node_positions(parsed_tiny):
    pc_data, part_index = parsed_tiny
    (_parts, _flex, nodes, *_rest) = core.resolve_selected_parts(pc_data, part_index)

    positions_by_id = {n.name: tuple(round(v, 6) for v in n.position) for n in nodes}
    assert positions_by_id == {
        "n1": (0.0, 0.0, 0.0),
        "n2": (1.0, 0.0, 0.0),
        "n3": (0.0, 1.0, 0.0),
        "n4": (0.0, 0.0, 1.0),
    }


def test_authoring_model_snapshot(parsed_tiny, tiny_pc_path, snapshot):
    """Pin the serialized authoring model for the fixture vehicle.

    This is the highest-leverage boundary test: any unintended change to the
    parser, resolver, or schema will surface here. Update with
    ``--snapshot-update`` only when the change is deliberate and reviewed.
    """
    pc_data, part_index = parsed_tiny
    (
        resolved_parts,
        _flexbodies,
        visual_nodes,
        visual_beams,
        visual_triangles,
        *_rest,
    ) = core.resolve_selected_parts(pc_data, part_index)

    model = rm.build_authoring_model_from_import(
        pc_path=tiny_pc_path,
        pc_data=pc_data,
        source_description="snapshot test",
        main_part="tiny_main",
        resolved_parts=resolved_parts,
        visual_nodes=visual_nodes,
        visual_beams=visual_beams,
        visual_triangles=visual_triangles,
    )

    # Normalize volatile fields before snapshotting.
    payload = model.to_dict()
    payload["generated_at"] = "<pinned>"
    payload["pc_path"] = "<fixture>/tiny.pc"
    for entry in payload.get("files", []):
        entry["source_file"] = "<fixture>/tiny.jbeam"
        entry["virtual_path"] = ""
    for entry in payload.get("parts", []):
        entry["source_file"] = "<fixture>/tiny.jbeam"
    for entry in payload.get("nodes", []):
        entry["source_file"] = "<fixture>/tiny.jbeam"
    for entry in payload.get("beams", []):
        entry["source_file"] = "<fixture>/tiny.jbeam"
    for entry in payload.get("triangles", []):
        entry["source_file"] = "<fixture>/tiny.jbeam"

    normalized = json.dumps(payload, indent=2, sort_keys=True)
    assert normalized == snapshot


def test_jsonc_handles_trailing_comma(tmp_path):
    """The .pc / .jbeam loader must tolerate trailing commas (BeamNG convention)."""
    text = '{ "a": [1, 2, 3,], "b": 4, }'
    parsed = core.load_jsonc_text(text)
    assert parsed == {"a": [1, 2, 3], "b": 4}


def test_jsonc_strips_line_and_block_comments():
    text = """
    {
        // a line comment
        "a": 1, /* an inline block */
        "b": /* multi
                 line */ 2
    }
    """
    parsed = core.load_jsonc_text(text)
    assert parsed == {"a": 1, "b": 2}
