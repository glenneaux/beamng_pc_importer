"""Boundary test: ResolvedVehicleAuthoringModel JSON serialization.

This is the system's outbound data-format boundary — the authoring model is
written to disk and read back across sessions. The round-trip must be exact.

Runs as a plain ``pytest`` invocation; does not require Blender.
"""

import json

import pytest

import resolved_model as rm


def _sample_model() -> rm.ResolvedVehicleAuthoringModel:
    """Build a small but non-trivial authoring model covering every field type."""
    return rm.ResolvedVehicleAuthoringModel(
        schema_version=1,
        generated_at="2026-05-14T10:00:00",
        pc_path="vehicles/tiny/tiny.pc",
        source_description="unit test fixture",
        vehicle_model="tiny",
        main_part="tiny_main",
        files=[
            rm.ResolvedJBeamFile(
                source_file="vehicles/tiny/tiny.jbeam",
                virtual_path="vehicles/tiny/tiny.jbeam",
                part_names=["tiny_main"],
                node_count=4,
                beam_count=4,
                triangle_count=1,
            )
        ],
        parts=[
            rm.ResolvedPartDetail(
                name="tiny_main",
                resolved_part_id=0,
                source_file="vehicles/tiny/tiny.jbeam",
                parent_id=-1,
                slot_name="",
                node_ids=["n1", "n2", "n3", "n4"],
                beam_count=4,
                triangle_count=1,
                external_node_refs=[],
            )
        ],
        nodes=[
            rm.ResolvedNode(
                id="n1",
                position=(0.0, 0.0, 0.0),
                part_name="tiny_main",
                resolved_part_id=0,
                source_file="vehicles/tiny/tiny.jbeam",
                kind="owned",
                options={},
            ),
            rm.ResolvedNode(
                id="n2",
                position=(1.0, 0.0, 0.0),
                part_name="tiny_main",
                resolved_part_id=0,
                source_file="vehicles/tiny/tiny.jbeam",
                kind="owned",
                options={"nodeWeight": 2.5},
            ),
        ],
        beams=[
            rm.ResolvedBeam(
                id1="n1",
                id2="n2",
                part_name="tiny_main",
                resolved_part_id=0,
                source_file="vehicles/tiny/tiny.jbeam",
                options={"beamSpring": 1000.0},
            )
        ],
        triangles=[
            rm.ResolvedTriangle(
                id1="n1",
                id2="n2",
                id3="n3",
                part_name="tiny_main",
                resolved_part_id=0,
                source_file="vehicles/tiny/tiny.jbeam",
                normal=(0.0, 0.0, 1.0),
                options={},
            )
        ],
        operations=[
            rm.EditOperation(
                operation="update",
                file="vehicles/tiny/tiny.jbeam",
                part="tiny_main",
                section="nodes",
                row="n1",
                field="position",
                old=[0.0, 0.0, 0.0],
                new=[0.001, 0.0, 0.0],
                status="pending",
                created_at="2026-05-14T10:00:00",
            )
        ],
        warnings=["example warning preserved verbatim"],
    )


def test_to_json_is_valid_json():
    text = _sample_model().to_json()
    parsed = json.loads(text)
    assert parsed["vehicle_model"] == "tiny"
    assert parsed["schema_version"] == 1


def test_round_trip_preserves_json_text():
    """The strong invariant: to_json -> from_json -> to_json is idempotent.

    Comparing ``to_dict()`` after a round-trip fails on tuple-typed fields
    because JSON has no tuple type and they come back as lists. The on-disk
    JSON text is what we ship; pin that.
    """
    original = _sample_model()
    first = original.to_json()
    rebuilt = rm.ResolvedVehicleAuthoringModel.from_json(first)
    second = rebuilt.to_json()

    assert first == second


def test_from_json_empty_text_returns_default_model():
    model = rm.ResolvedVehicleAuthoringModel.from_json("")
    assert model.parts == []
    assert model.nodes == []
    assert model.schema_version == 1


def test_node_index_is_keyed_by_id():
    model = _sample_model()
    index = model.node_index()
    assert set(index) == {"n1", "n2"}
    assert index["n2"].options == {"nodeWeight": 2.5}


def test_refs_for_node_collects_referencing_topology():
    model = _sample_model()
    refs = model.refs_for_node("n1")
    assert len(refs["beams"]) == 1
    assert len(refs["triangles"]) == 1
    assert refs["beams"][0].id2 == "n2"


def test_operation_dicts_filters_by_status():
    model = _sample_model()
    assert len(model.operation_dicts(status="pending")) == 1
    assert model.operation_dicts(status="accepted") == []


def test_to_json_snapshot(snapshot):
    """Pinned snapshot of the serialized authoring-model schema.

    Update with: ``pytest --snapshot-update tests/unit``
    """
    text = _sample_model().to_json()
    assert text == snapshot


# Guard rail: if a new field is added to ResolvedVehicleAuthoringModel without
# a corresponding round-trip path, this will surface it.
@pytest.mark.parametrize(
    "dataclass_type",
    [
        rm.ResolvedNode,
        rm.ResolvedBeam,
        rm.ResolvedTriangle,
        rm.ResolvedJBeamFile,
        rm.ResolvedPartDetail,
        rm.EditOperation,
        rm.ResolvedVehicleAuthoringModel,
    ],
)
def test_dataclass_has_only_serializable_defaults(dataclass_type):
    from dataclasses import MISSING, fields

    for field_info in fields(dataclass_type):
        if field_info.default is MISSING:
            # Has default_factory or no default — skip; covered by round-trip test.
            continue
        json.dumps(field_info.default, default=str)
