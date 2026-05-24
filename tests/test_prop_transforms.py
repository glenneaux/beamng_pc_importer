from pathlib import Path

from mathutils import Matrix, Vector

from beamng_pc_importer.core import PartDefinition, parse_props


def assert_vector_close(actual, expected, tolerance=0.000001):
    assert len(actual) == len(expected)
    for actual_value, expected_value in zip(actual, expected):
        assert abs(actual_value - expected_value) <= tolerance


def max_matrix_delta(left, right):
    return max(abs(left[row][col] - right[row][col]) for row in range(3) for col in range(3))


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
    assert_vector_close(matrix.to_translation(), (2.0, 4.0, 0.0))
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
    assert_vector_close(specs[0].debug_prop_world_translation_offset, (-0.062224, 0.029367, -0.024573))
    assert_vector_close(specs[0].transform_matrix.to_translation(), (0.723776, 0.209067, 2.047727))


def test_cabover_parking_brake_instancing_uses_raw_prop_axes():
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

    assert_vector_close(instance.matrix_local.to_translation(), (0.723776, -1.730933, 2.047727))
    assert_vector_close(instance.get("beamng_final_local_loc"), (0.723776, -1.730933, 2.047727))


def test_cabover_speedo_needle_raw_prop_axes_match_dae_rest_position():
    part = PartDefinition(
        name="us_semi_cabover_dashboard",
        source_path=Path("vehicles/us_semi/us_semi_cabover_dashboard.jbeam"),
        data={
            "props": [
                ["func", "mesh", "idRef:", "idX:", "idY:", "baseRotation", "rotation", "translation", "min", "max", "offset", "multiplier"],
                ["wheelspeed", "cabover_needle_speedo", "dshl", "dshr", "dsh", {"x": 0, "y": -90, "z": -10}, {"x": 6, "y": 0, "z": 0}, {"x": 0, "y": 0, "z": 0}, 0, 70.7, -22.35, 1, {"baseTranslation": {"x": 0.331, "y": -0.224, "z": 0.209}}],
            ],
        },
    )
    spec = parse_props(
        part,
        Matrix.Identity(4),
        local_node_positions={
            "dshl": Vector((0.786, 0.07443, 1.84654)),
            "dshr": Vector((-0.786, 0.07443, 1.84654)),
            "dsh": Vector((0.0, 0.44, 1.84654)),
        },
    )[0]

    # DAE node matrix location is (0.6581002, -1.960148, 2.055337).
    # With the import parent offset removed, the comparable JBeam-space Y is
    # approximately -0.020148.
    assert_vector_close(spec.transform_matrix.to_translation(), (0.658107, -0.020035, 2.05554), tolerance=0.00025)


def test_cabover_speedo_needle_instancing_preserves_animated_rotation():
    import bpy

    from beamng_pc_importer.visuals import instantiate_flexbody

    part = PartDefinition(
        name="us_semi_cabover_dashboard",
        source_path=Path("vehicles/us_semi/us_semi_cabover_dashboard.jbeam"),
        data={
            "props": [
                ["func", "mesh", "idRef:", "idX:", "idY:", "baseRotation", "rotation", "translation", "min", "max", "offset", "multiplier"],
                ["wheelspeed", "cabover_needle_speedo", "dshl", "dshr", "dsh", {"x": 0, "y": -90, "z": -10}, {"x": 6, "y": 0, "z": 0}, {"x": 0, "y": 0, "z": 0}, 0, 70.7, -22.35, 1, {"baseTranslation": {"x": 0.331, "y": -0.224, "z": 0.209}}],
            ],
        },
    )
    spec = parse_props(
        part,
        Matrix.Identity(4),
        local_node_positions={
            "dshl": Vector((0.786, 0.07443, 1.84654)),
            "dshr": Vector((-0.786, 0.07443, 1.84654)),
            "dsh": Vector((0.0, 0.44, 1.84654)),
        },
    )[0]

    mesh = bpy.data.meshes.new("cabover_needle_speedo_test_mesh")
    template = bpy.data.objects.new("cabover_needle_speedo", mesh)
    template.matrix_world = Matrix(
        (
            (-1.60305e-10, -3.11206e-10, -0.001, 0.6581002),
            (-9.83946e-4, -1.78469e-4, 2.13272e-10, -1.960148),
            (-1.78469e-4, 9.83946e-4, -2.77601e-10, 2.055337),
            (0.0, 0.0, 0.0, 1.0),
        )
    )
    parent = bpy.data.objects.new("us_semi_cabover_dashboard_parent", None)
    parent.matrix_world = Matrix.Translation((0.0, 1.94, 0.0))
    bpy.context.scene.collection.objects.link(template)
    bpy.context.scene.collection.objects.link(parent)

    instance = instantiate_flexbody(template, spec, bpy.context.scene.collection, parent_obj=parent)
    bpy.context.view_layer.update()

    template_rotation = template.matrix_world.to_3x3()
    final_rotation = instance.matrix_world.to_3x3()
    assert max_matrix_delta(final_rotation, template_rotation) > 0.0005


def test_cabover_steering_wheel_preserves_source_handedness():
    import bpy

    from beamng_pc_importer.visuals import instantiate_flexbody

    part = PartDefinition(
        name="us_semi_cabover_steer",
        source_path=Path("vehicles/us_semi/us_semi_steeringwheels.jbeam"),
        data={
            "props": [
                ["func", "mesh", "idRef:", "idX:", "idY:", "baseRotation", "rotation", "translation", "min", "max", "offset", "multiplier"],
                ["steering", "cabover_steering_wheel", "int_strw", "dshl", "dshr", {"x": 0, "y": 90, "z": 180}, {"x": 0, "y": 0, "z": 1}, {"x": 0, "y": 0, "z": 0}, -1000, 1000, 0, 1, {"baseTranslation": {"x": 0, "y": 0, "z": 0}}],
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

    mesh = bpy.data.meshes.new("cabover_steering_wheel_test_mesh")
    mesh.from_pydata([(1.0, 0.0, 0.0)], [], [])
    template = bpy.data.objects.new("cabover_steering_wheel", mesh)
    template.matrix_world = Matrix(
        (
            (0.001, 0.0, 0.0, 0.7860397),
            (0.0, 9.06308e-4, 4.22618e-4, -1.760317),
            (0.0, -4.22618e-4, 9.06308e-4, 2.072255),
            (0.0, 0.0, 0.0, 1.0),
        )
    )
    parent = bpy.data.objects.new("us_semi_cabover_steer_parent", None)
    parent.matrix_world = Matrix.Translation((0.0, 1.94, 0.0))
    bpy.context.scene.collection.objects.link(template)
    bpy.context.scene.collection.objects.link(parent)

    instance = instantiate_flexbody(template, spec, bpy.context.scene.collection, parent_obj=parent)
    bpy.context.view_layer.update()

    assert instance.matrix_world.to_3x3().determinant() > 0.0
    assert instance.get("beamng_normalized_negative_scale") is False
    assert_vector_close(instance.data.vertices[0].co, (1.0, 0.0, 0.0))


def test_cabover_steering_wheel_uses_orthonormal_rotation_basis():
    part = PartDefinition(
        name="us_semi_cabover_steer",
        source_path=Path("vehicles/us_semi/us_semi_steeringwheels.jbeam"),
        data={
            "props": [
                ["func", "mesh", "idRef:", "idX:", "idY:", "baseRotation", "rotation", "translation", "min", "max", "offset", "multiplier"],
                ["steering", "cabover_steering_wheel", "int_strw", "dshl", "dshr", {"x": 0, "y": 90, "z": 180}, {"x": 0, "y": 0, "z": 1}, {"x": 0, "y": 0, "z": 0}, -1000, 1000, 0, 1, {"baseTranslation": {"x": 0, "y": 0, "z": 0}}],
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

    assert_vector_close(spec.transform_matrix.to_translation(), (0.786, 0.1797, 2.0723))
    assert abs(spec.transform_matrix.to_3x3().determinant() - 1.0) < 0.000001
