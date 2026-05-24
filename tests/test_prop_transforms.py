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


def test_cabover_parking_brake_base_translation_moves_position():
    part = PartDefinition(
        name="us_semi_cabover_dashboard",
        source_path=Path("vehicles/us_semi/us_semi_cabover_dashboard.jbeam"),
        data={
            "props": [
                ["func", "mesh", "idRef:", "idX:", "idY:", "baseRotation", "rotation", "translation", "min", "max", "offset", "multiplier"],
                ["nop", "cabover_parking_brake_stalk", "int_strw", "dshl", "dshr", {"x": 180, "y": -90, "z": -40}, {"x": 0, "y": 0, "z": 40}, {"x": 0, "y": 0, "z": 0}, -1, 1, -1, 1, {"baseTranslation": {"x": -0.0, "y": 0.063, "z": -0.037}}],
            ],
        },
    )
    specs = parse_props(
        part,
        Matrix.Identity(4),
        local_node_positions={
            "int_strw": Vector((0.786, 0.1797, 2.0723)),
            "dshl": Vector((0.786, 0.07443, 1.84654)),
            "dshr": Vector((-0.786, 0.07443, 1.84654)),
        },
    )

    assert len(specs) == 1
    assert_vector_close(specs[0].debug_prop_base_translation, (-0.0, 0.063, -0.037))
    assert_vector_close(specs[0].debug_prop_local_translation, (0.0, 0.063, -0.037))
    assert_vector_close(specs[0].debug_prop_world_translation_offset, (-0.063, -0.033534, 0.015636))
    assert_vector_close(specs[0].transform_matrix.to_translation(), (0.723, 0.146166, 2.087936))


def test_cabover_parking_brake_instancing_applies_base_translation_local_offset():
    import bpy

    from beamng_pc_importer.visuals import instantiate_flexbody

    part = PartDefinition(
        name="us_semi_cabover_dashboard",
        source_path=Path("vehicles/us_semi/us_semi_cabover_dashboard.jbeam"),
        data={
            "props": [
                ["func", "mesh", "idRef:", "idX:", "idY:", "baseRotation", "rotation", "translation", "min", "max", "offset", "multiplier"],
                ["nop", "cabover_parking_brake_stalk", "int_strw", "dshl", "dshr", {"x": 180, "y": -90, "z": -40}, {"x": 0, "y": 0, "z": 40}, {"x": 0, "y": 0, "z": 0}, -1, 1, -1, 1, {"baseTranslation": {"x": -0.0, "y": 0.063, "z": -0.037}}],
            ],
        },
    )
    spec = parse_props(
        part,
        Matrix.Identity(4),
        local_node_positions={
            "int_strw": Vector((0.786, 0.1797, 2.0723)),
            "dshl": Vector((0.786, 0.07443, 1.84654)),
            "dshr": Vector((-0.786, 0.07443, 1.84654)),
        },
    )[0]

    mesh = bpy.data.meshes.new("cabover_parking_brake_stalk_test_mesh")
    template = bpy.data.objects.new("cabover_parking_brake_stalk", mesh)
    parent = bpy.data.objects.new("us_semi_cabover_dashboard_parent", None)
    parent.matrix_world = Matrix.Translation((0.0, 1.94, 0.0))
    bpy.context.scene.collection.objects.link(template)
    bpy.context.scene.collection.objects.link(parent)

    instance = instantiate_flexbody(template, spec, bpy.context.scene.collection, parent_obj=parent)
    bpy.context.view_layer.update()

    assert_vector_close(instance.matrix_local.to_translation(), (0.723, -1.730834, 2.050936))
    assert_vector_close(instance.get("beamng_prop_applied_local_visual_offset"), (-0.0, 0.063, -0.037))
    assert_vector_close(instance.get("beamng_final_local_loc"), (0.723, -1.730834, 2.050936))
