"""Boundary tests for the first topology-native JBeam import slice."""

from __future__ import annotations

from pathlib import Path

import pytest

import beamng_pc_importer as addon


@pytest.fixture
def topology_subset_path() -> Path:
    path = Path(__file__).resolve().parent / "data" / "vehicles" / "topology_subset" / "subset.jbeam"
    assert path.exists(), f"Missing topology subset fixture: {path}"
    return path


def test_topology_subset_imports_one_part(topology_subset_path):
    imported = addon.import_jbeam_topology_subset(topology_subset_path)

    assert imported.source_path == str(topology_subset_path)
    assert len(imported.parts) == 1

    part = imported.parts[0]
    assert part.part_name == "subset_main"
    assert part.slot_type == "main"
    assert part.information["name"] == "Topology Subset Fixture"
    assert part.slots[0]["section"] == "slots2"


def test_topology_subset_imports_nodes_beams_and_triangles(topology_subset_path):
    part = addon.import_jbeam_topology_subset(topology_subset_path).parts[0]

    assert [node.node_id for node in part.nodes] == ["a", "b", "c"]
    assert [node.position for node in part.nodes] == [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)]
    assert part.nodes[0].options == {"nodeWeight": 5.5, "group": "body"}
    assert part.nodes[1].options == {"nodeWeight": 4.25, "group": "body"}

    assert [(beam.id1, beam.id2) for beam in part.beams] == [
        ("a", "b"),
        ("b", "c"),
        ("c", "external_ref"),
    ]
    assert part.beams[0].options == {"beamSpring": 100000, "beamDamp": 120}
    assert part.beams[1].options == {"beamSpring": 90000, "beamDamp": 120}
    assert part.beams[2].missing_nodes == ("external_ref",)

    assert [(triangle.id1, triangle.id2, triangle.id3) for triangle in part.triangles] == [("a", "b", "c")]
    assert part.triangles[0].options == {"dragCoef": 9, "groundModel": "metal"}


def test_topology_subset_preserves_unknown_sections_and_reports_diagnostics(topology_subset_path):
    imported = addon.import_jbeam_topology_subset(topology_subset_path)
    part = imported.parts[0]

    assert sorted(part.unknown_preserved_sections) == ["customUnknown", "hydros"]
    assert part.unknown_preserved_sections["customUnknown"] == {"keep": True}

    diagnostic_codes = [diagnostic.code for diagnostic in imported.diagnostics]
    assert diagnostic_codes.count("unsupported_preserved") == 2
    assert "beam_missing_local_node" in diagnostic_codes
    assert "nodes_not_list" not in diagnostic_codes


def test_topology_subset_distinguishes_blocking_parse_diagnostics():
    imported = addon.import_jbeam_topology_subset('{"bad_part": {"nodes": {"not": "a list"}}}', "memory://bad.jbeam")

    assert len(imported.parts) == 1
    assert imported.parts[0].nodes == []
    assert any(diagnostic.code == "nodes_not_list" and diagnostic.level == "error" for diagnostic in imported.diagnostics)
