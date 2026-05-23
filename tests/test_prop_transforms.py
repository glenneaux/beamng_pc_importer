from pathlib import Path

from mathutils import Matrix, Vector

from beamng_pc_importer.core import PartDefinition, parse_props


def assert_vector_close(actual, expected, tolerance=0.000001):
    assert len(actual) == len(expected)
    for actual_value, expected_value in zip(actual, expected):
        assert abs(actual_value - expected_value) <= tolerance


def test_prop_anchor_basis_and_local_translation_follow_beamng_docs():
    part = PartDefinition(
        name="test_part",
        source_path=Path("vehicles/test/test.jbeam"),
        data={
            "props": [
                ["func", "mesh", "idRef:", "idX:", "idY:", "baseRotation", "rotation", "translation", "min", "max", "offset", "multiplier"],
                ["nop", "test_prop", "ref", "xref", "yref", {"x": 0, "y": 0, "z": 0}, {"x": 0, "y": 0, "z": 0}, {"x": 0, "y": 0, "z": 0}, 0, 100, 0, 1, {"baseTranslation": {"x": 1, "y": 2, "z": 3}}],
            ],
        },
    )
    specs = parse_props(
        part,
        Matrix.Identity(4),
        local_node_positions={
            "ref": Vector((1.0, 2.0, 3.0)),
            "xref": Vector((2.0, 2.0, 3.0)),
            "yref": Vector((1.0, 3.0, 3.0)),
        },
    )

    assert len(specs) == 1
    matrix = specs[0].transform_matrix
    assert_vector_close(matrix.to_translation(), (2.0, 4.0, 6.0))
    assert_vector_close(matrix.col[0].to_3d(), (1.0, 0.0, 0.0))
    assert_vector_close(matrix.col[1].to_3d(), (0.0, 1.0, 0.0))
    assert_vector_close(matrix.col[2].to_3d(), (0.0, 0.0, 1.0))


def test_prop_inherited_options_and_static_function_factor_are_applied():
    part = PartDefinition(
        name="test_part",
        source_path=Path("vehicles/test/test.jbeam"),
        data={
            "props": [
                ["func", "mesh", "idRef:", "idX:", "idY:", "baseRotation", "rotation", "translation", "min", "max", "offset", "multiplier"],
                {"baseTranslation": {"x": 0, "y": 0, "z": 1}},
                ["foo", "test_prop", "ref", "xref", "yref", {"x": 0, "y": 0, "z": 0}, {"x": 0, "y": 0, "z": 10}, {"x": 1, "y": 0, "z": 0}, 5, 100, 2, 7],
            ],
        },
    )
    specs = parse_props(
        part,
        Matrix.Identity(4),
        local_node_positions={
            "ref": Vector((0.0, 0.0, 0.0)),
            "xref": Vector((1.0, 0.0, 0.0)),
            "yref": Vector((0.0, 1.0, 0.0)),
        },
    )

    assert len(specs) == 1
    assert specs[0].debug_prop_anim_factor == 7.0
    assert_vector_close(specs[0].debug_prop_local_translation, (7.0, 0.0, 1.0))


def test_prop_global_translation_overrides_base_translation_position():
    part = PartDefinition(
        name="test_part",
        source_path=Path("vehicles/test/test.jbeam"),
        data={
            "props": [
                ["func", "mesh", "idRef:", "idX:", "idY:", "baseRotation", "rotation", "translation", "min", "max", "offset", "multiplier"],
                ["nop", "test_prop", "ref", "xref", "yref", {"x": 0, "y": 0, "z": 0}, {"x": 0, "y": 0, "z": 0}, {"x": 0, "y": 0, "z": 0}, 0, 100, 0, 1, {"baseTranslation": {"x": 99, "y": 99, "z": 99}, "baseTranslationGlobal": {"x": 10, "y": 20, "z": 30}}],
            ],
        },
    )
    specs = parse_props(
        part,
        Matrix.Identity(4),
        local_node_positions={
            "ref": Vector((1.0, 2.0, 3.0)),
            "xref": Vector((2.0, 2.0, 3.0)),
            "yref": Vector((1.0, 3.0, 3.0)),
        },
    )

    assert len(specs) == 1
    assert_vector_close(specs[0].transform_matrix.to_translation(), (10.0, 20.0, 30.0))
