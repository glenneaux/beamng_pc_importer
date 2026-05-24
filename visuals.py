import colorsys
import json
import tempfile
import zipfile
from pathlib import Path

import bpy
from mathutils import Matrix, Vector

try:
    from .core import *
except ImportError:
    from core import *


JBEAM_POSITION_PRECISION = 3


def link_collection(parent, name):
    collection = bpy.data.collections.new(name)
    parent.children.link(collection)
    return collection


def create_empty(name, collection, matrix_world=None, parent=None, local_matrix=None):
    empty = bpy.data.objects.new(name, None)
    empty.empty_display_type = "CUBE"
    empty.empty_display_size = 0.18
    collection.objects.link(empty)
    if parent is not None:
        empty.parent = parent
        empty.matrix_local = local_matrix if local_matrix is not None else Matrix.Identity(4)
    else:
        empty.matrix_world = matrix_world if matrix_world is not None else Matrix.Identity(4)
    return empty


def build_part_hierarchy(resolved_parts, collection):
    part_objects = {}
    for resolved_part in resolved_parts:
        part_def = resolved_part.part_def
        label = f"{resolved_part.id:03d}_{part_def.name}"
        if resolved_part.slot_name:
            label = f"{resolved_part.id:03d}_{resolved_part.slot_name}__{part_def.name}"

        parent = part_objects.get(resolved_part.parent_id)
        empty = create_empty(
            label,
            collection,
            matrix_world=resolved_part.transform_matrix,
            parent=parent,
            local_matrix=resolved_part.local_transform_matrix,
        )
        empty["beamng_part_name"] = part_def.name
        empty["beamng_jbeam_path"] = str(part_def.source_path)
        empty["beamng_slot_name"] = resolved_part.slot_name
        empty["beamng_resolved_part_id"] = resolved_part.id
        empty["beamng_layer"] = "hierarchy"
        part_objects[resolved_part.id] = empty
    return part_objects


def get_or_create_material(name, color):
    material = bpy.data.materials.get(name)
    if material is None:
        material = bpy.data.materials.new(name)
    material.diffuse_color = color
    return material


def get_or_create_translucent_material(name, color, alpha=0.24):
    material = get_or_create_material(name, (color[0], color[1], color[2], alpha))
    material.use_nodes = True
    material.blend_method = "BLEND"
    material.use_screen_refraction = True
    material.show_transparent_back = True
    material.diffuse_color = (color[0], color[1], color[2], alpha)
    bsdf = material.node_tree.nodes.get("Principled BSDF") if material.node_tree else None
    if bsdf:
        if "Alpha" in bsdf.inputs:
            bsdf.inputs["Alpha"].default_value = alpha
        if "Base Color" in bsdf.inputs:
            bsdf.inputs["Base Color"].default_value = (color[0], color[1], color[2], alpha)
    return material


def get_or_create_jbeam_mesh_material(name, color):
    material = get_or_create_translucent_material(name, color, 0.46)
    material.use_nodes = True
    material.blend_method = "BLEND"
    material.show_transparent_back = True
    material.diffuse_color = (color[0], color[1], color[2], 0.46)
    return material


def get_or_create_jbeam_edge_material(name, color):
    wire_color = (
        min(color[0] * 1.18 + 0.08, 1.0),
        min(color[1] * 1.18 + 0.08, 1.0),
        min(color[2] * 1.18 + 0.08, 1.0),
        1.0,
    )
    material = get_or_create_material(name, wire_color)
    material.diffuse_color = wire_color
    return material


JBEAM_COLOR_GOLDEN_RATIO = 0.618033988749895
JBEAM_COLOR_SEED_HUE = 0.02
JBEAM_COLOR_SATURATIONS = (0.78, 0.64, 0.86)
JBEAM_COLOR_VALUES = (0.95, 0.82, 0.70)


def color_for_resolved_part(resolved_part_id: int):
    if resolved_part_id < 0:
        return (0.9, 0.9, 0.9, 1.0)
    hue = (JBEAM_COLOR_SEED_HUE + resolved_part_id * JBEAM_COLOR_GOLDEN_RATIO) % 1.0
    saturation = JBEAM_COLOR_SATURATIONS[resolved_part_id % len(JBEAM_COLOR_SATURATIONS)]
    value = JBEAM_COLOR_VALUES[(resolved_part_id // len(JBEAM_COLOR_SATURATIONS)) % len(JBEAM_COLOR_VALUES)]
    red, green, blue = colorsys.hsv_to_rgb(hue, saturation, value)
    return (red, green, blue, 1.0)


def hydro_color_for_resolved_part(_resolved_part_id: int):
    return (0.0, 0.92, 1.0, 1.0)


def slider_color_for_resolved_part(_resolved_part_id: int):
    return (1.0, 0.55, 0.05, 1.0)


def safe_collection_name(value: str):
    return re.sub(r"[\\/:*?\"<>|]+", "_", value).strip() or "unnamed"


def rounded_position_tuple(position, precision=JBEAM_POSITION_PRECISION):
    return tuple(round(float(value), precision) for value in position)


def preserved_position_tuple(position):
    return tuple(float(value) for value in position)


def create_jbeam_nodes_object(nodes, collection, part_name="", resolved_part_id=-1, color=None):
    if not nodes:
        return None
    color = color or color_for_resolved_part(resolved_part_id)

    radius = 0.035
    vertices = []
    faces = []
    offsets = (
        Vector((radius, 0.0, 0.0)),
        Vector((-radius, 0.0, 0.0)),
        Vector((0.0, radius, 0.0)),
        Vector((0.0, -radius, 0.0)),
        Vector((0.0, 0.0, radius)),
        Vector((0.0, 0.0, -radius)),
    )
    local_faces = (
        (0, 2, 4),
        (2, 1, 4),
        (1, 3, 4),
        (3, 0, 4),
        (2, 0, 5),
        (1, 2, 5),
        (3, 1, 5),
        (0, 3, 5),
    )

    for node in nodes:
        base = len(vertices)
        vertices.extend(tuple(node.position + offset) for offset in offsets)
        faces.extend(tuple(base + index for index in face) for face in local_faces)

    mesh = bpy.data.meshes.new(f"BeamNG_JBeam_Nodes_{safe_collection_name(part_name)}_Mesh")
    mesh.from_pydata(vertices, [], faces)
    mesh.update()

    obj = bpy.data.objects.new("JBeam Nodes", mesh)
    obj["beamng_layer"] = "jbeam"
    obj["beamng_visual_type"] = "nodes"
    obj["beamng_node_count"] = len(nodes)
    obj["beamng_part_name"] = part_name
    obj["beamng_resolved_part_id"] = resolved_part_id
    obj["beamng_parent_resolved_part_id"] = collection.get("beamng_parent_resolved_part_id", -1)
    obj.show_in_front = True
    obj.hide_select = True
    obj.color = color
    mesh.materials.append(get_or_create_material(f"BeamNG JBeam Part {resolved_part_id:03d} Nodes", obj.color))
    collection.objects.link(obj)
    return obj


def create_jbeam_beams_object(beams, collection, part_name="", resolved_part_id=-1, color=None):
    if not beams:
        return None
    color = color or color_for_resolved_part(resolved_part_id)

    curve = bpy.data.curves.new(f"BeamNG_JBeam_Beams_{safe_collection_name(part_name)}_Curve", "CURVE")
    curve.dimensions = "3D"
    curve.resolution_u = 1
    curve.bevel_depth = 0.004
    curve.bevel_resolution = 2

    for beam in beams:
        spline = curve.splines.new("POLY")
        spline.points.add(1)
        spline.points[0].co = (beam.start.x, beam.start.y, beam.start.z, 1.0)
        spline.points[1].co = (beam.end.x, beam.end.y, beam.end.z, 1.0)

    obj = bpy.data.objects.new("JBeam Beams", curve)
    obj["beamng_layer"] = "jbeam"
    obj["beamng_visual_type"] = "beams"
    obj["beamng_beam_count"] = len(beams)
    obj["beamng_part_name"] = part_name
    obj["beamng_resolved_part_id"] = resolved_part_id
    obj["beamng_parent_resolved_part_id"] = collection.get("beamng_parent_resolved_part_id", -1)
    obj.show_in_front = True
    obj.hide_select = True
    obj.color = color
    curve.materials.append(get_or_create_material(f"BeamNG JBeam Part {resolved_part_id:03d} Beams", obj.color))
    collection.objects.link(obj)
    return obj


def create_selectable_jbeam_node(node, collection, color):
    empty = bpy.data.objects.new(f"Node {node.name}", None)
    empty.empty_display_type = "SPHERE"
    empty.empty_display_size = 0.08
    empty.location = node.position
    empty.show_name = False
    empty.show_in_front = True
    empty.color = color
    empty["beamng_layer"] = "jbeam"
    empty["beamng_visual_type"] = "selectable_node"
    empty["beamng_node_id"] = node.name
    empty["beamng_part_name"] = node.part_name
    empty["beamng_resolved_part_id"] = node.resolved_part_id
    empty["beamng_parent_resolved_part_id"] = collection.get("beamng_parent_resolved_part_id", -1)
    collection.objects.link(empty)
    return empty


def create_jbeam_node_label(node, collection, color):
    font_curve = bpy.data.curves.new(f"JBeam_Node_Label_{safe_collection_name(node.name)}", "FONT")
    font_curve.body = node.name
    font_curve.align_x = "CENTER"
    font_curve.align_y = "CENTER"
    font_curve.size = 0.08
    obj = bpy.data.objects.new(f"Label {node.name}", font_curve)
    obj.location = node.position + Vector((0.0, 0.0, 0.08))
    obj.show_in_front = True
    obj.hide_select = True
    obj.hide_render = True
    obj.color = color
    obj["beamng_layer"] = "jbeam"
    obj["beamng_visual_type"] = "node_label"
    obj["beamng_node_id"] = node.name
    obj["beamng_part_name"] = node.part_name
    obj["beamng_resolved_part_id"] = node.resolved_part_id
    obj["beamng_parent_resolved_part_id"] = collection.get("beamng_parent_resolved_part_id", -1)
    font_curve.materials.append(get_or_create_material(f"BeamNG JBeam Part {node.resolved_part_id:03d} Labels", color))
    collection.objects.link(obj)
    return obj


def create_selectable_jbeam_beam(beam, collection, color, index):
    curve = bpy.data.curves.new(f"JBeam_Selectable_Beam_{safe_collection_name(beam.id1)}_{safe_collection_name(beam.id2)}", "CURVE")
    curve.dimensions = "3D"
    curve.resolution_u = 1
    curve.bevel_depth = 0.009
    curve.bevel_resolution = 3

    spline = curve.splines.new("POLY")
    spline.points.add(1)
    spline.points[0].co = (beam.start.x, beam.start.y, beam.start.z, 1.0)
    spline.points[1].co = (beam.end.x, beam.end.y, beam.end.z, 1.0)

    obj = bpy.data.objects.new(f"Beam {beam.id1}-{beam.id2}", curve)
    obj.show_in_front = True
    obj.color = color
    obj["beamng_layer"] = "jbeam"
    obj["beamng_visual_type"] = "selectable_beam"
    obj["beamng_beam_name"] = f"{beam.id1}-{beam.id2}"
    obj["beamng_beam_id1"] = beam.id1
    obj["beamng_beam_id2"] = beam.id2
    obj["beamng_beam_index"] = index
    obj["beamng_part_name"] = beam.part_name
    obj["beamng_resolved_part_id"] = beam.resolved_part_id
    obj["beamng_parent_resolved_part_id"] = collection.get("beamng_parent_resolved_part_id", -1)
    curve.materials.append(get_or_create_material(f"BeamNG JBeam Part {beam.resolved_part_id:03d} Selectable Beams", color))
    collection.objects.link(obj)
    return obj


def create_jbeam_triangles_object(triangles, collection, part_name="", resolved_part_id=-1, color=None):
    if not triangles:
        return None
    color = color or color_for_resolved_part(resolved_part_id)

    vertices = []
    faces = []
    for triangle in triangles:
        base = len(vertices)
        vertices.extend((tuple(triangle.p1), tuple(triangle.p2), tuple(triangle.p3)))
        faces.append((base, base + 1, base + 2))

    mesh = bpy.data.meshes.new(f"BeamNG_JBeam_Triangles_{safe_collection_name(part_name)}_Mesh")
    mesh.from_pydata(vertices, [], faces)
    mesh.update()

    obj = bpy.data.objects.new("JBeam Triangles", mesh)
    obj["beamng_layer"] = "jbeam"
    obj["beamng_visual_type"] = "triangles"
    obj["beamng_triangle_count"] = len(triangles)
    obj["beamng_part_name"] = part_name
    obj["beamng_resolved_part_id"] = resolved_part_id
    obj["beamng_parent_resolved_part_id"] = collection.get("beamng_parent_resolved_part_id", -1)
    obj.show_in_front = True
    obj.hide_select = True
    obj.color = (color[0], color[1], color[2], 0.24)
    mesh.materials.append(get_or_create_translucent_material(f"BeamNG JBeam Part {resolved_part_id:03d} Triangles", color, 0.24))
    collection.objects.link(obj)
    return obj


def create_selectable_jbeam_triangle(triangle, collection, color, index):
    mesh = bpy.data.meshes.new(
        f"JBeam_Selectable_Triangle_{safe_collection_name(triangle.id1)}_{safe_collection_name(triangle.id2)}_{safe_collection_name(triangle.id3)}"
    )
    mesh.from_pydata((tuple(triangle.p1), tuple(triangle.p2), tuple(triangle.p3)), [], ((0, 1, 2),))
    mesh.update()

    obj = bpy.data.objects.new(f"Triangle {triangle.id1}-{triangle.id2}-{triangle.id3}", mesh)
    obj.show_in_front = True
    obj.hide_select = False
    obj.color = (color[0], color[1], color[2], 0.38)
    obj["beamng_layer"] = "jbeam"
    obj["beamng_visual_type"] = "selectable_triangle"
    obj["beamng_triangle_name"] = f"{triangle.id1}-{triangle.id2}-{triangle.id3}"
    obj["beamng_triangle_id1"] = triangle.id1
    obj["beamng_triangle_id2"] = triangle.id2
    obj["beamng_triangle_id3"] = triangle.id3
    obj["beamng_triangle_index"] = index
    obj["beamng_part_name"] = triangle.part_name
    obj["beamng_resolved_part_id"] = triangle.resolved_part_id
    obj["beamng_parent_resolved_part_id"] = collection.get("beamng_parent_resolved_part_id", -1)
    mesh.materials.append(
        get_or_create_translucent_material(
            f"BeamNG JBeam Part {triangle.resolved_part_id:03d} Selectable Triangles",
            color,
            0.38,
        )
    )
    collection.objects.link(obj)
    return obj


def create_jbeam_hydros_object(hydros, collection, part_name="", resolved_part_id=-1, color=None):
    if not hydros:
        return None
    color = color or hydro_color_for_resolved_part(resolved_part_id)

    curve = bpy.data.curves.new(f"BeamNG_JBeam_Hydros_{safe_collection_name(part_name)}_Curve", "CURVE")
    curve.dimensions = "3D"
    curve.resolution_u = 1
    curve.bevel_depth = 0.006
    curve.bevel_resolution = 2

    for hydro in hydros:
        spline = curve.splines.new("POLY")
        spline.points.add(1)
        spline.points[0].co = (hydro.start.x, hydro.start.y, hydro.start.z, 1.0)
        spline.points[1].co = (hydro.end.x, hydro.end.y, hydro.end.z, 1.0)

    obj = bpy.data.objects.new("JBeam Hydros", curve)
    obj["beamng_layer"] = "jbeam"
    obj["beamng_visual_type"] = "hydros"
    obj["beamng_hydro_count"] = len(hydros)
    obj["beamng_part_name"] = part_name
    obj["beamng_resolved_part_id"] = resolved_part_id
    obj["beamng_parent_resolved_part_id"] = collection.get("beamng_parent_resolved_part_id", -1)
    obj.show_in_front = True
    obj.hide_select = True
    obj.color = color
    curve.materials.append(get_or_create_material(f"BeamNG JBeam Part {resolved_part_id:03d} Hydros", color))
    collection.objects.link(obj)
    return obj


def create_selectable_jbeam_hydro(hydro, collection, color, index):
    curve = bpy.data.curves.new(f"JBeam_Selectable_Hydro_{safe_collection_name(hydro.id1)}_{safe_collection_name(hydro.id2)}", "CURVE")
    curve.dimensions = "3D"
    curve.resolution_u = 1
    curve.bevel_depth = 0.014
    curve.bevel_resolution = 3

    spline = curve.splines.new("POLY")
    spline.points.add(1)
    spline.points[0].co = (hydro.start.x, hydro.start.y, hydro.start.z, 1.0)
    spline.points[1].co = (hydro.end.x, hydro.end.y, hydro.end.z, 1.0)

    obj = bpy.data.objects.new(f"Hydro {hydro.id1}-{hydro.id2}", curve)
    obj.show_in_front = True
    obj.color = color
    obj["beamng_layer"] = "jbeam"
    obj["beamng_visual_type"] = "selectable_hydro"
    obj["beamng_hydro_name"] = f"{hydro.id1}-{hydro.id2}"
    obj["beamng_hydro_id1"] = hydro.id1
    obj["beamng_hydro_id2"] = hydro.id2
    obj["beamng_hydro_index"] = index
    obj["beamng_hydro_input_source"] = hydro.input_source
    obj["beamng_hydro_factor"] = hydro.factor
    obj["beamng_part_name"] = hydro.part_name
    obj["beamng_resolved_part_id"] = hydro.resolved_part_id
    obj["beamng_parent_resolved_part_id"] = collection.get("beamng_parent_resolved_part_id", -1)
    curve.materials.append(get_or_create_material(f"BeamNG JBeam Part {hydro.resolved_part_id:03d} Selectable Hydros", color))
    collection.objects.link(obj)
    return obj


def create_jbeam_rails_object(rails, collection, part_name="", resolved_part_id=-1, color=None):
    if not rails:
        return None
    color = color or slider_color_for_resolved_part(resolved_part_id)

    curve = bpy.data.curves.new(f"BeamNG_JBeam_Rails_{safe_collection_name(part_name)}_Curve", "CURVE")
    curve.dimensions = "3D"
    curve.resolution_u = 1
    curve.bevel_depth = 0.007
    curve.bevel_resolution = 2

    for rail in rails:
        if len(rail.points) < 2:
            continue
        spline = curve.splines.new("POLY")
        spline.points.add(len(rail.points) - 1)
        for index, point in enumerate(rail.points):
            spline.points[index].co = (point.x, point.y, point.z, 1.0)

    obj = bpy.data.objects.new("JBeam Rails", curve)
    obj["beamng_layer"] = "jbeam"
    obj["beamng_visual_type"] = "rails"
    obj["beamng_rail_count"] = len(rails)
    obj["beamng_part_name"] = part_name
    obj["beamng_resolved_part_id"] = resolved_part_id
    obj["beamng_parent_resolved_part_id"] = collection.get("beamng_parent_resolved_part_id", -1)
    obj.show_in_front = True
    obj.hide_select = True
    obj.color = color
    curve.materials.append(get_or_create_material(f"BeamNG JBeam Part {resolved_part_id:03d} Rails", color))
    collection.objects.link(obj)
    return obj


def create_selectable_jbeam_rail(rail, collection, color, index):
    if len(rail.points) < 2:
        return None
    curve = bpy.data.curves.new(f"JBeam_Selectable_Rail_{safe_collection_name(rail.name)}", "CURVE")
    curve.dimensions = "3D"
    curve.resolution_u = 1
    curve.bevel_depth = 0.015
    curve.bevel_resolution = 3
    spline = curve.splines.new("POLY")
    spline.points.add(len(rail.points) - 1)
    for point_index, point in enumerate(rail.points):
        spline.points[point_index].co = (point.x, point.y, point.z, 1.0)

    obj = bpy.data.objects.new(f"Rail {rail.name}", curve)
    obj.show_in_front = True
    obj.color = color
    obj["beamng_layer"] = "jbeam"
    obj["beamng_visual_type"] = "selectable_rail"
    obj["beamng_rail_name"] = rail.name
    obj["beamng_rail_nodes"] = ", ".join(rail.node_ids)
    obj["beamng_rail_index"] = index
    obj["beamng_rail_capped"] = rail.capped
    obj["beamng_rail_looped"] = rail.looped
    obj["beamng_part_name"] = rail.part_name
    obj["beamng_resolved_part_id"] = rail.resolved_part_id
    obj["beamng_parent_resolved_part_id"] = collection.get("beamng_parent_resolved_part_id", -1)
    curve.materials.append(get_or_create_material(f"BeamNG JBeam Part {rail.resolved_part_id:03d} Selectable Rails", color))
    collection.objects.link(obj)
    return obj


def create_selectable_jbeam_slidenode(slidenode, collection, color, index):
    empty = bpy.data.objects.new(f"Slidenode {slidenode.node_id} on {slidenode.rail_name}", None)
    empty.empty_display_type = "ARROWS"
    empty.empty_display_size = 0.12
    empty.location = slidenode.position
    empty.show_in_front = True
    empty.color = color
    empty["beamng_layer"] = "jbeam"
    empty["beamng_visual_type"] = "selectable_slidenode"
    empty["beamng_slidenode_id"] = slidenode.node_id
    empty["beamng_slidenode_rail"] = slidenode.rail_name
    empty["beamng_slidenode_index"] = index
    empty["beamng_slidenode_attached"] = slidenode.attached
    empty["beamng_slidenode_fix_to_rail"] = slidenode.fix_to_rail
    empty["beamng_part_name"] = slidenode.part_name
    empty["beamng_resolved_part_id"] = slidenode.resolved_part_id
    empty["beamng_parent_resolved_part_id"] = collection.get("beamng_parent_resolved_part_id", -1)
    collection.objects.link(empty)
    return empty


def create_selectable_jbeam_debug_objects(
    nodes,
    beams,
    triangles,
    hydros,
    rails,
    slidenodes,
    collection,
    color,
    show_node_labels=False,
):
    debug_collection = link_collection(collection, "Selectable IDs")
    debug_collection["beamng_layer"] = "jbeam"
    debug_collection["beamng_visual_type"] = "selectable_debug"
    debug_collection["beamng_part_name"] = collection.get("beamng_part_name", "")
    debug_collection["beamng_resolved_part_id"] = collection.get("beamng_resolved_part_id", -1)
    debug_collection["beamng_parent_resolved_part_id"] = collection.get("beamng_parent_resolved_part_id", -1)

    for node in nodes:
        create_selectable_jbeam_node(node, debug_collection, color)
        if show_node_labels:
            create_jbeam_node_label(node, debug_collection, color)
    for index, beam in enumerate(beams, 1):
        create_selectable_jbeam_beam(beam, debug_collection, color, index)
    for index, triangle in enumerate(triangles, 1):
        create_selectable_jbeam_triangle(triangle, debug_collection, color, index)
    hydro_color = hydro_color_for_resolved_part(collection.get("beamng_resolved_part_id", -1))
    slider_color = slider_color_for_resolved_part(collection.get("beamng_resolved_part_id", -1))
    for index, hydro in enumerate(hydros, 1):
        create_selectable_jbeam_hydro(hydro, debug_collection, hydro_color, index)
    for index, rail in enumerate(rails, 1):
        create_selectable_jbeam_rail(rail, debug_collection, slider_color, index)
    for index, slidenode in enumerate(slidenodes, 1):
        create_selectable_jbeam_slidenode(slidenode, debug_collection, slider_color, index)
    return debug_collection


def create_jbeam_visuals(
    nodes,
    beams,
    triangles,
    hydros,
    rails,
    slidenodes,
    parent_collection,
    resolved_parts=None,
    selectable_debug=False,
    show_node_labels=False,
):
    visual_collection = link_collection(parent_collection, "JBeam Structure")
    visual_collection["beamng_layer"] = "jbeam"

    part_labels = {}
    parent_part_ids = {}
    for resolved_part in resolved_parts or []:
        part_labels[resolved_part.id] = (
            f"{resolved_part.id:03d}_{resolved_part.slot_name}__{resolved_part.part_def.name}"
            if resolved_part.slot_name
            else f"{resolved_part.id:03d}_{resolved_part.part_def.name}"
        )
        parent_part_ids[resolved_part.id] = resolved_part.parent_id

    nodes_by_part = defaultdict(list)
    beams_by_part = defaultdict(list)
    triangles_by_part = defaultdict(list)
    hydros_by_part = defaultdict(list)
    rails_by_part = defaultdict(list)
    slidenodes_by_part = defaultdict(list)
    part_names = {}
    for node in nodes:
        nodes_by_part[node.resolved_part_id].append(node)
        part_names[node.resolved_part_id] = node.part_name
    for beam in beams:
        beams_by_part[beam.resolved_part_id].append(beam)
        part_names[beam.resolved_part_id] = beam.part_name
    for triangle in triangles:
        triangles_by_part[triangle.resolved_part_id].append(triangle)
        part_names[triangle.resolved_part_id] = triangle.part_name
    for hydro in hydros:
        hydros_by_part[hydro.resolved_part_id].append(hydro)
        part_names[hydro.resolved_part_id] = hydro.part_name
    for rail in rails:
        rails_by_part[rail.resolved_part_id].append(rail)
        part_names[rail.resolved_part_id] = rail.part_name
    for slidenode in slidenodes:
        slidenodes_by_part[slidenode.resolved_part_id].append(slidenode)
        part_names[slidenode.resolved_part_id] = slidenode.part_name

    for resolved_part_id in sorted(
        set(nodes_by_part)
        | set(beams_by_part)
        | set(triangles_by_part)
        | set(hydros_by_part)
        | set(rails_by_part)
        | set(slidenodes_by_part)
    ):
        part_name = part_names.get(resolved_part_id, "")
        label = part_labels.get(resolved_part_id, f"{resolved_part_id:03d}_{part_name}")
        part_collection = link_collection(visual_collection, safe_collection_name(label))
        part_collection["beamng_layer"] = "jbeam"
        part_collection["beamng_part_name"] = part_name
        part_collection["beamng_resolved_part_id"] = resolved_part_id
        part_collection["beamng_parent_resolved_part_id"] = parent_part_ids.get(resolved_part_id, -1)
        color = color_for_resolved_part(resolved_part_id)
        part_nodes = nodes_by_part.get(resolved_part_id, [])
        part_beams = beams_by_part.get(resolved_part_id, [])
        part_triangles = triangles_by_part.get(resolved_part_id, [])
        part_hydros = hydros_by_part.get(resolved_part_id, [])
        part_rails = rails_by_part.get(resolved_part_id, [])
        part_slidenodes = slidenodes_by_part.get(resolved_part_id, [])
        create_jbeam_nodes_object(part_nodes, part_collection, part_name, resolved_part_id, color)
        create_jbeam_beams_object(part_beams, part_collection, part_name, resolved_part_id, color)
        create_jbeam_triangles_object(part_triangles, part_collection, part_name, resolved_part_id, color)
        create_jbeam_hydros_object(part_hydros, part_collection, part_name, resolved_part_id)
        create_jbeam_rails_object(part_rails, part_collection, part_name, resolved_part_id)
        if selectable_debug:
            create_selectable_jbeam_debug_objects(
                part_nodes,
                part_beams,
                part_triangles,
                part_hydros,
                part_rails,
                part_slidenodes,
                part_collection,
                color,
                show_node_labels,
            )

    visual_collection.hide_viewport = True
    return visual_collection


def create_experimental_jbeam_meshes(nodes, beams, triangles, parent_collection, resolved_parts=None):
    mesh_collection = link_collection(parent_collection, "Experimental JBeam Meshes")
    mesh_collection["beamng_layer"] = "jbeam_mesh"
    mesh_collection["beamng_visual_type"] = "experimental_jbeam_meshes"

    part_labels = {}
    parent_part_ids = {}
    part_sources = {}
    for resolved_part in resolved_parts or []:
        part_labels[resolved_part.id] = (
            f"{resolved_part.id:03d}_{resolved_part.slot_name}__{resolved_part.part_def.name}"
            if resolved_part.slot_name
            else f"{resolved_part.id:03d}_{resolved_part.part_def.name}"
        )
        parent_part_ids[resolved_part.id] = resolved_part.parent_id
        part_sources[resolved_part.id] = str(resolved_part.part_def.source_path)

    nodes_by_part = defaultdict(list)
    beams_by_part = defaultdict(list)
    triangles_by_part = defaultdict(list)
    part_names = {}
    node_owner_part_ids = defaultdict(set)
    for node in nodes:
        nodes_by_part[node.resolved_part_id].append(node)
        part_names[node.resolved_part_id] = node.part_name
        node_owner_part_ids[str(node.name)].add(node.resolved_part_id)
    for beam in beams:
        beams_by_part[beam.resolved_part_id].append(beam)
        part_names[beam.resolved_part_id] = beam.part_name
    for triangle in triangles:
        triangles_by_part[triangle.resolved_part_id].append(triangle)
        part_names[triangle.resolved_part_id] = triangle.part_name

    created = 0
    for resolved_part_id in sorted(set(nodes_by_part) | set(beams_by_part) | set(triangles_by_part)):
        part_name = part_names.get(resolved_part_id, "")
        label = part_labels.get(resolved_part_id, f"{resolved_part_id:03d}_{part_name}")
        color = color_for_resolved_part(resolved_part_id)
        local_node_ids = {node.name for node in nodes_by_part.get(resolved_part_id, [])}
        vertex_positions = []
        vertex_node_ids = []
        vertex_kinds = []
        vertex_owner_part_ids = []
        vertex_options = []
        vertex_index_by_node_id = {}
        local_node_options = {node.name: dict(getattr(node, "options", {}) or {}) for node in nodes_by_part.get(resolved_part_id, [])}

        def ensure_vertex(node_id, position):
            node_id = str(node_id)
            if node_id in vertex_index_by_node_id:
                return vertex_index_by_node_id[node_id]
            vertex_index = len(vertex_positions)
            vertex_index_by_node_id[node_id] = vertex_index
            vertex_positions.append(preserved_position_tuple(position))
            vertex_node_ids.append(node_id)
            vertex_options.append(dict(local_node_options.get(node_id, {})))
            if node_id in local_node_ids:
                owner_part_id = resolved_part_id
                vertex_kind = "owned"
            else:
                owner_ids = sorted(part_id for part_id in node_owner_part_ids.get(node_id, set()) if part_id != resolved_part_id)
                owner_part_id = owner_ids[0] if owner_ids else -1
                vertex_kind = "proxy"
            vertex_kinds.append(vertex_kind)
            vertex_owner_part_ids.append(owner_part_id)
            return vertex_index

        for node in nodes_by_part.get(resolved_part_id, []):
            ensure_vertex(node.name, node.position)

        edges = []
        edge_ids = []
        edge_options = []
        seen_edges = set()
        for beam in beams_by_part.get(resolved_part_id, []):
            v1 = ensure_vertex(beam.id1, beam.start)
            v2 = ensure_vertex(beam.id2, beam.end)
            if v1 == v2:
                continue
            edge_key = tuple(sorted((v1, v2)))
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            edges.append((v1, v2))
            edge_ids.append((beam.id1, beam.id2))
            edge_options.append(dict(getattr(beam, "options", {}) or {}))

        faces = []
        face_ids = []
        face_options = []
        for triangle in triangles_by_part.get(resolved_part_id, []):
            face = (
                ensure_vertex(triangle.id1, triangle.p1),
                ensure_vertex(triangle.id2, triangle.p2),
                ensure_vertex(triangle.id3, triangle.p3),
            )
            if len(set(face)) < 3:
                continue
            faces.append(face)
            face_ids.append((triangle.id1, triangle.id2, triangle.id3))
            face_options.append(dict(getattr(triangle, "options", {}) or {}))

        if not vertex_positions:
            continue

        mesh = bpy.data.meshes.new(f"Experimental_JBeam_Mesh_{safe_collection_name(label)}")
        mesh.from_pydata(vertex_positions, edges, faces)
        mesh.update()
        node_uids = list(range(1, len(vertex_node_ids) + 1))
        edge_uids = list(range(len(node_uids) + 1, len(node_uids) + len(edge_ids) + 1))
        face_uids = list(
            range(
                len(node_uids) + len(edge_uids) + 1,
                len(node_uids) + len(edge_uids) + len(face_ids) + 1,
            )
        )
        mesh["beamng_next_topology_uid"] = len(node_uids) + len(edge_uids) + len(face_uids) + 1
        mesh["beamng_node_ids_json"] = json.dumps(vertex_node_ids)
        mesh["beamng_node_kinds_json"] = json.dumps(vertex_kinds)
        mesh["beamng_node_owner_part_ids_json"] = json.dumps(vertex_owner_part_ids)
        mesh["beamng_original_node_positions_json"] = json.dumps(vertex_positions)
        mesh["beamng_node_generated_flags_json"] = json.dumps([False for _node_id in vertex_node_ids])
        mesh["beamng_node_committed_flags_json"] = json.dumps([True for _node_id in vertex_node_ids])
        mesh["beamng_node_params_json"] = json.dumps(vertex_options)
        mesh["beamng_node_committed_params_json"] = json.dumps(vertex_options)
        mesh["beamng_edge_node_ids_json"] = json.dumps(edge_ids)
        mesh["beamng_edge_params_json"] = json.dumps(edge_options)
        mesh["beamng_edge_committed_params_json"] = json.dumps(edge_options)
        mesh["beamng_face_node_ids_json"] = json.dumps(face_ids)
        mesh["beamng_face_params_json"] = json.dumps(face_options)
        mesh["beamng_face_committed_params_json"] = json.dumps(face_options)
        mesh["beamng_node_uid_to_id_json"] = json.dumps({str(uid): node_id for uid, node_id in zip(node_uids, vertex_node_ids)})
        mesh["beamng_node_uid_to_kind_json"] = json.dumps({str(uid): kind for uid, kind in zip(node_uids, vertex_kinds)})
        mesh["beamng_node_uid_to_owner_part_id_json"] = json.dumps({str(uid): owner for uid, owner in zip(node_uids, vertex_owner_part_ids)})
        mesh["beamng_node_uid_to_original_position_json"] = json.dumps({str(uid): position for uid, position in zip(node_uids, vertex_positions)})
        mesh["beamng_node_uid_to_generated_json"] = json.dumps({str(uid): False for uid in node_uids})
        mesh["beamng_node_uid_to_committed_json"] = json.dumps({str(uid): True for uid in node_uids})
        mesh["beamng_node_uid_to_params_json"] = json.dumps({str(uid): params for uid, params in zip(node_uids, vertex_options)})
        mesh["beamng_node_uid_to_committed_params_json"] = json.dumps({str(uid): params for uid, params in zip(node_uids, vertex_options)})
        mesh["beamng_edge_uid_to_params_json"] = json.dumps({str(uid): params for uid, params in zip(edge_uids, edge_options)})
        mesh["beamng_edge_uid_to_committed_params_json"] = json.dumps({str(uid): params for uid, params in zip(edge_uids, edge_options)})
        mesh["beamng_face_uid_to_params_json"] = json.dumps({str(uid): params for uid, params in zip(face_uids, face_options)})
        mesh["beamng_face_uid_to_committed_params_json"] = json.dumps({str(uid): params for uid, params in zip(face_uids, face_options)})
        mesh["beamng_edge_uid_to_semantic_type_json"] = json.dumps({str(uid): "beam" for uid in edge_uids})
        mesh["beamng_edge_uid_to_semantic_state_json"] = json.dumps({str(uid): "valid" for uid in edge_uids})
        mesh["beamng_face_uid_to_semantic_type_json"] = json.dumps({str(uid): "triangle" for uid in face_uids})
        mesh["beamng_face_uid_to_semantic_state_json"] = json.dumps({str(uid): "valid" for uid in face_uids})
        mesh["beamng_topology_revision"] = 0
        mesh["beamng_topology_signature_json"] = json.dumps({})
        mesh["beamng_semantic_topology_json"] = json.dumps({})
        mesh["beamng_previous_semantic_topology_json"] = json.dumps({})
        mesh["beamng_semantic_topology_delta_json"] = json.dumps({})
        mesh["beamng_semantic_topology_delta_count"] = 0
        mesh_edge_ids = []
        for edge in mesh.edges:
            indices = list(edge.vertices)
            if len(indices) == 2 and all(0 <= index < len(vertex_node_ids) for index in indices):
                mesh_edge_ids.append((vertex_node_ids[indices[0]], vertex_node_ids[indices[1]]))
        mesh["beamng_mesh_edge_node_ids_json"] = json.dumps(mesh_edge_ids)
        if hasattr(mesh, "attributes"):
            node_uid_attr = mesh.attributes.new("beamng_node_uid", "INT", "POINT")
            edge_uid_attr = mesh.attributes.new("beamng_edge_uid", "INT", "EDGE")
            face_uid_attr = mesh.attributes.new("beamng_face_uid", "INT", "FACE")
            owner_attr = mesh.attributes.new("beamng_owner_part_id", "INT", "POINT")
            proxy_attr = mesh.attributes.new("beamng_is_proxy_node", "BOOLEAN", "POINT")
            color_attr = mesh.attributes.new("beamng_node_color", "FLOAT_COLOR", "POINT")
            for index, uid in enumerate(node_uids):
                if index < len(node_uid_attr.data):
                    node_uid_attr.data[index].value = uid
            for index, uid in enumerate(edge_uids):
                if index < len(edge_uid_attr.data):
                    edge_uid_attr.data[index].value = uid
            for index, uid in enumerate(face_uids):
                if index < len(face_uid_attr.data):
                    face_uid_attr.data[index].value = uid
            for index, owner_part_id in enumerate(vertex_owner_part_ids):
                if index < len(owner_attr.data):
                    owner_attr.data[index].value = owner_part_id
                if index < len(proxy_attr.data):
                    proxy_attr.data[index].value = vertex_kinds[index] == "proxy"
                if index < len(color_attr.data):
                    owner_color = color_for_resolved_part(owner_part_id)
                    color_attr.data[index].color = owner_color

        obj = bpy.data.objects.new(f"JBeam Mesh {label}", mesh)
        obj["beamng_layer"] = "jbeam_mesh"
        obj["beamng_visual_type"] = "experimental_jbeam_mesh"
        obj["beamng_part_name"] = part_name
        obj["beamng_resolved_part_id"] = resolved_part_id
        obj["beamng_parent_resolved_part_id"] = parent_part_ids.get(resolved_part_id, -1)
        obj["beamng_jbeam_path"] = part_sources.get(resolved_part_id, "")
        obj["beamng_owned_node_count"] = sum(1 for kind in vertex_kinds if kind == "owned")
        obj["beamng_proxy_node_count"] = sum(1 for kind in vertex_kinds if kind == "proxy")
        obj["beamng_beam_edge_count"] = len(edges)
        obj["beamng_triangle_face_count"] = len(faces)
        obj["beamng_object_transform_locked"] = True
        obj.display_type = "TEXTURED"
        obj.show_in_front = False
        obj.show_wire = True
        obj.lock_location = (True, True, True)
        obj.lock_rotation = (True, True, True)
        obj.lock_scale = (True, True, True)
        obj.color = color
        mesh.materials.append(get_or_create_jbeam_mesh_material(f"Experimental JBeam Mesh Part {resolved_part_id:03d}", color))
        mesh.materials.append(get_or_create_jbeam_edge_material(f"Experimental JBeam Mesh Part {resolved_part_id:03d} Edges", color))
        mesh_collection.objects.link(obj)
        owned_group = obj.vertex_groups.new(name="Owned Nodes")
        proxy_group = obj.vertex_groups.new(name="Proxy Nodes")
        proxy_groups_by_owner = {}
        for vertex_index, (kind, owner_part_id) in enumerate(zip(vertex_kinds, vertex_owner_part_ids)):
            if kind == "owned":
                owned_group.add([vertex_index], 1.0, "ADD")
            else:
                proxy_group.add([vertex_index], 1.0, "ADD")
                owner_group = proxy_groups_by_owner.get(owner_part_id)
                if owner_group is None:
                    owner_group = obj.vertex_groups.new(name=f"Proxy Nodes From Part {owner_part_id:03d}")
                    proxy_groups_by_owner[owner_part_id] = owner_group
                owner_group.add([vertex_index], 1.0, "ADD")

        created += 1

    return created


def create_imported_jbeam_topology_meshes(imported_jbeam, parent_collection):
    """Materialize the first topology-subset import as editable Blender meshes."""
    mesh_collection = link_collection(parent_collection, "Imported JBeam Topology")
    mesh_collection["beamng_layer"] = "jbeam_mesh"
    mesh_collection["beamng_visual_type"] = "imported_jbeam_topology_meshes"
    mesh_collection["beamng_source_path"] = imported_jbeam.source_path
    mesh_collection["beamng_source_sha256"] = imported_jbeam.cached_source.get("sha256", "")

    created = 0
    for part_index, part in enumerate(imported_jbeam.parts):
        if not part.nodes:
            continue
        color = color_for_resolved_part(part_index)
        obj = create_imported_jbeam_part_mesh(part, imported_jbeam, mesh_collection, part_index, color)
        if obj is not None:
            created += 1
    return created


def create_imported_jbeam_part_mesh(part, imported_jbeam, mesh_collection, part_index, color):
    vertex_positions = [preserved_position_tuple(node.position) for node in part.nodes]
    vertex_node_ids = [node.node_id for node in part.nodes]
    vertex_index_by_node_id = {node_id: index for index, node_id in enumerate(vertex_node_ids)}

    beam_edges = []
    beam_edge_keys = {}
    beam_options_by_key = {}
    beam_guid_by_key = {}
    for beam in part.beams:
        if beam.missing_nodes:
            continue
        if beam.id1 not in vertex_index_by_node_id or beam.id2 not in vertex_index_by_node_id:
            continue
        edge = (vertex_index_by_node_id[beam.id1], vertex_index_by_node_id[beam.id2])
        if edge[0] == edge[1]:
            continue
        edge_key = tuple(sorted(edge))
        if edge_key in beam_edge_keys:
            continue
        beam_edges.append(edge)
        beam_edge_keys[edge_key] = beam
        beam_options_by_key[edge_key] = dict(beam.options or {})
        beam_guid_by_key[tuple(sorted((str(beam.id1), str(beam.id2))))] = beam.topology_guid

    faces = []
    face_records = []
    for triangle in part.triangles:
        if triangle.missing_nodes:
            continue
        if any(node_id not in vertex_index_by_node_id for node_id in (triangle.id1, triangle.id2, triangle.id3)):
            continue
        face = (
            vertex_index_by_node_id[triangle.id1],
            vertex_index_by_node_id[triangle.id2],
            vertex_index_by_node_id[triangle.id3],
        )
        if len(set(face)) < 3:
            continue
        faces.append(face)
        face_records.append(triangle)

    mesh = bpy.data.meshes.new(f"Imported_JBeam_Topology_{safe_collection_name(part.part_name)}")
    mesh.from_pydata(vertex_positions, beam_edges, faces)
    mesh.update()

    node_uids = list(range(1, len(vertex_node_ids) + 1))
    edge_uids = list(range(len(node_uids) + 1, len(node_uids) + len(mesh.edges) + 1))
    face_uids = list(
        range(
            len(node_uids) + len(edge_uids) + 1,
            len(node_uids) + len(edge_uids) + len(mesh.polygons) + 1,
        )
    )
    mesh["beamng_next_topology_uid"] = len(node_uids) + len(edge_uids) + len(face_uids) + 1

    node_uid_to_id = {str(uid): node.node_id for uid, node in zip(node_uids, part.nodes)}
    node_uid_to_guid = {str(uid): node.topology_guid for uid, node in zip(node_uids, part.nodes)}
    node_uid_to_params = {str(uid): dict(node.options or {}) for uid, node in zip(node_uids, part.nodes)}
    edge_uid_to_type = {}
    edge_uid_to_state = {}
    edge_uid_to_params = {}
    edge_uid_to_guid = {}
    non_exportable_topology_uids = []
    edge_node_ids = []
    mesh_edge_params = []
    for edge, uid in zip(mesh.edges, edge_uids):
        indices = tuple(edge.vertices)
        edge_key = tuple(sorted(indices))
        node_pair = [vertex_node_ids[index] for index in indices]
        edge_node_ids.append(node_pair)
        beam = beam_edge_keys.get(edge_key)
        if beam is not None:
            edge_uid_to_type[str(uid)] = "beam"
            edge_params = beam_options_by_key.get(edge_key, {})
            edge_uid_to_params[str(uid)] = edge_params
            edge_uid_to_guid[str(uid)] = beam.topology_guid
        else:
            edge_uid_to_type[str(uid)] = "triangle_boundary"
            edge_params = {}
            edge_uid_to_params[str(uid)] = edge_params
            non_exportable_topology_uids.append(uid)
        mesh_edge_params.append(edge_params)
        edge_uid_to_state[str(uid)] = "valid"

    face_uid_to_type = {str(uid): "triangle" for uid in face_uids}
    face_uid_to_state = {str(uid): "valid" for uid in face_uids}
    face_uid_to_params = {str(uid): dict(triangle.options or {}) for uid, triangle in zip(face_uids, face_records)}
    face_uid_to_guid = {str(uid): triangle.topology_guid for uid, triangle in zip(face_uids, face_records)}
    face_node_ids = [[triangle.id1, triangle.id2, triangle.id3] for triangle in face_records]

    mesh["beamng_node_ids_json"] = json.dumps(vertex_node_ids)
    mesh["beamng_node_kinds_json"] = json.dumps(["owned" for _node in part.nodes])
    mesh["beamng_node_owner_part_ids_json"] = json.dumps([part_index for _node in part.nodes])
    mesh["beamng_original_node_positions_json"] = json.dumps(vertex_positions)
    mesh["beamng_node_generated_flags_json"] = json.dumps([False for _node in part.nodes])
    mesh["beamng_node_committed_flags_json"] = json.dumps([True for _node in part.nodes])
    mesh["beamng_node_params_json"] = json.dumps([dict(node.options or {}) for node in part.nodes])
    mesh["beamng_node_committed_params_json"] = json.dumps([dict(node.options or {}) for node in part.nodes])
    mesh["beamng_mesh_edge_node_ids_json"] = json.dumps(edge_node_ids)
    mesh["beamng_edge_node_ids_json"] = json.dumps([[beam.id1, beam.id2] for beam in part.beams if not beam.missing_nodes])
    mesh["beamng_original_beam_key_to_topology_guid_json"] = json.dumps(
        {"|".join(key): value for key, value in beam_guid_by_key.items()}
    )
    mesh["beamng_edge_params_json"] = json.dumps(mesh_edge_params)
    mesh["beamng_edge_committed_params_json"] = json.dumps(mesh_edge_params)
    mesh["beamng_face_node_ids_json"] = json.dumps(face_node_ids)
    mesh["beamng_face_params_json"] = json.dumps([dict(triangle.options or {}) for triangle in face_records])
    mesh["beamng_face_committed_params_json"] = mesh["beamng_face_params_json"]
    mesh["beamng_node_uid_to_id_json"] = json.dumps(node_uid_to_id)
    mesh["beamng_node_uid_to_kind_json"] = json.dumps({str(uid): "owned" for uid in node_uids})
    mesh["beamng_node_uid_to_owner_part_id_json"] = json.dumps({str(uid): part_index for uid in node_uids})
    mesh["beamng_node_uid_to_original_position_json"] = json.dumps(
        {str(uid): pos for uid, pos in zip(node_uids, vertex_positions)}
    )
    mesh["beamng_node_uid_to_generated_json"] = json.dumps({str(uid): False for uid in node_uids})
    mesh["beamng_node_uid_to_committed_json"] = json.dumps({str(uid): True for uid in node_uids})
    mesh["beamng_node_uid_to_params_json"] = json.dumps(node_uid_to_params)
    mesh["beamng_node_uid_to_committed_params_json"] = json.dumps(node_uid_to_params)
    mesh["beamng_node_uid_to_topology_guid_json"] = json.dumps(node_uid_to_guid)
    mesh["beamng_edge_uid_to_params_json"] = json.dumps(edge_uid_to_params)
    mesh["beamng_edge_uid_to_committed_params_json"] = json.dumps(edge_uid_to_params)
    mesh["beamng_edge_uid_to_semantic_type_json"] = json.dumps(edge_uid_to_type)
    mesh["beamng_edge_uid_to_semantic_state_json"] = json.dumps(edge_uid_to_state)
    mesh["beamng_edge_uid_to_topology_guid_json"] = json.dumps(edge_uid_to_guid)
    mesh["beamng_face_uid_to_params_json"] = json.dumps(face_uid_to_params)
    mesh["beamng_face_uid_to_committed_params_json"] = json.dumps(face_uid_to_params)
    mesh["beamng_face_uid_to_semantic_type_json"] = json.dumps(face_uid_to_type)
    mesh["beamng_face_uid_to_semantic_state_json"] = json.dumps(face_uid_to_state)
    mesh["beamng_face_uid_to_topology_guid_json"] = json.dumps(face_uid_to_guid)
    mesh["beamng_helper_topology_uids_json"] = json.dumps([])
    mesh["beamng_non_exportable_topology_uids_json"] = json.dumps(non_exportable_topology_uids)
    mesh["beamng_import_identity_map_json"] = json.dumps(imported_jbeam.import_identity_map)
    mesh["beamng_source_map_json"] = json.dumps(imported_jbeam.source_map)
    mesh["beamng_source_sha256"] = imported_jbeam.cached_source.get("sha256", "")
    mesh["beamng_topology_revision"] = 0
    mesh["beamng_topology_signature_json"] = json.dumps({})
    mesh["beamng_semantic_topology_json"] = json.dumps({})
    mesh["beamng_previous_semantic_topology_json"] = json.dumps({})
    mesh["beamng_semantic_topology_delta_json"] = json.dumps({})
    mesh["beamng_semantic_topology_delta_count"] = 0

    if hasattr(mesh, "attributes"):
        _write_imported_jbeam_mesh_attributes(mesh, node_uids, edge_uids, face_uids, part_index)

    obj = bpy.data.objects.new(f"Imported JBeam {part.part_name}", mesh)
    obj["beamng_layer"] = "jbeam_mesh"
    obj["beamng_visual_type"] = "experimental_jbeam_mesh"
    obj["beamng_imported_topology_subset"] = True
    obj["beamng_part_name"] = part.part_name
    obj["beamng_part_guid"] = part.part_guid
    obj["beamng_resolved_part_id"] = part_index
    obj["beamng_jbeam_path"] = part.source_path
    obj["beamng_owned_node_count"] = len(part.nodes)
    obj["beamng_proxy_node_count"] = 0
    obj["beamng_beam_edge_count"] = len([beam for beam in part.beams if not beam.missing_nodes])
    obj["beamng_triangle_face_count"] = len(face_records)
    obj["beamng_object_transform_locked"] = True
    obj.display_type = "TEXTURED"
    obj.show_wire = True
    obj.color = color
    mesh.materials.append(get_or_create_jbeam_mesh_material(f"Imported JBeam Mesh Part {part_index:03d}", color))
    mesh.materials.append(
        get_or_create_jbeam_edge_material(f"Imported JBeam Mesh Part {part_index:03d} Edges", color)
    )
    mesh_collection.objects.link(obj)
    owned_group = obj.vertex_groups.new(name="Owned Nodes")
    owned_group.add(list(range(len(part.nodes))), 1.0, "ADD")
    return obj


def _write_imported_jbeam_mesh_attributes(mesh, node_uids, edge_uids, face_uids, part_index):
    node_uid_attr = mesh.attributes.new("beamng_node_uid", "INT", "POINT")
    edge_uid_attr = mesh.attributes.new("beamng_edge_uid", "INT", "EDGE")
    face_uid_attr = mesh.attributes.new("beamng_face_uid", "INT", "FACE")
    owner_attr = mesh.attributes.new("beamng_owner_part_id", "INT", "POINT")
    proxy_attr = mesh.attributes.new("beamng_is_proxy_node", "BOOLEAN", "POINT")
    for index, uid in enumerate(node_uids):
        if index < len(node_uid_attr.data):
            node_uid_attr.data[index].value = uid
        if index < len(owner_attr.data):
            owner_attr.data[index].value = part_index
        if index < len(proxy_attr.data):
            proxy_attr.data[index].value = False
    for index, uid in enumerate(edge_uids):
        if index < len(edge_uid_attr.data):
            edge_uid_attr.data[index].value = uid
    for index, uid in enumerate(face_uids):
        if index < len(face_uid_attr.data):
            face_uid_attr.data[index].value = uid


def tag_mesh_data(mesh_data, part_name: str, jbeam_path: Path, vehicle_model: str, editing_enabled: bool):
    for legacy_key in ("mesh_editing_enabled", "vehicle_model", "jbeam_part", "jbeam_file_path"):
        if legacy_key in mesh_data:
            del mesh_data[legacy_key]
    mesh_data[MESH_JBEAM_PART] = part_name
    mesh_data[MESH_JBEAM_FILE_PATH] = str(jbeam_path)
    mesh_data[MESH_VEHICLE_MODEL] = vehicle_model
    mesh_data[MESH_EDITING_ENABLED] = editing_enabled


def import_dae_templates(context, dae_path: Path, template_collection):
    before = {obj.name_full for obj in bpy.data.objects}
    bpy.ops.wm.collada_import(filepath=str(dae_path))
    imported = [obj for obj in bpy.data.objects if obj.name_full not in before]

    imported_meshes = []
    for obj in imported:
        for collection in list(obj.users_collection):
            collection.objects.unlink(obj)
        template_collection.objects.link(obj)
        obj.hide_set(True)
        obj.hide_render = True
        if obj.type == "MESH":
            tag_mesh_data(obj.data, "__template__", dae_path, "", False)
            imported_meshes.append(obj)
    return imported_meshes


def materialize_dae_asset(asset: DaeAssetSource, extracted_zip_assets: dict):
    if asset.asset_type == "file":
        return Path(asset.path)

    cached = extracted_zip_assets.get(asset)
    if cached is not None:
        return cached

    zip_label = safe_collection_name(Path(asset.zip_path).stem or "zip")
    entry_label = safe_collection_name(asset.zip_entry)
    temp_dir = Path(tempfile.gettempdir()) / "beamng_pc_importer_common" / zip_label
    temp_dir.mkdir(parents=True, exist_ok=True)
    out_path = temp_dir / entry_label

    with zipfile.ZipFile(asset.zip_path, "r") as archive:
        with archive.open(asset.zip_entry, "r") as src, out_path.open("wb") as dst:
            dst.write(src.read())

    extracted_zip_assets[asset] = out_path
    return out_path


def build_template_lookup(mesh_objects):
    lookup = defaultdict(list)
    for obj in mesh_objects:
        keys = {
            normalized_name(obj.name),
            normalized_name(obj.data.name) if obj.data else "",
        }
        for key in keys:
            if key:
                lookup[key].append(obj)
    return lookup


def find_matching_template(mesh_name, lookup):
    key = normalized_name(mesh_name)
    if key in lookup:
        return lookup[key][0]

    for candidate_key, objs in lookup.items():
        if candidate_key.startswith(key) or key.startswith(candidate_key):
            return objs[0]
    return None


def matrix_without_translation(matrix):
    result = matrix.copy()
    result.translation = Vector((0.0, 0.0, 0.0))
    return result


def matrix_without_translation_positive_scale(matrix):
    _location, rotation, scale = matrix.decompose()
    positive_scale = Vector((abs(scale.x), abs(scale.y), abs(scale.z)))
    return Matrix.LocRotScale(Vector((0.0, 0.0, 0.0)), rotation, positive_scale)


def matrix_scale_only(matrix):
    _location, _rotation, scale = matrix.decompose()
    return Matrix.Diagonal((abs(scale.x), abs(scale.y), abs(scale.z), 1.0))


def prop_template_basis_from_dae(spec: FlexbodySpec, template_obj):
    # BeamNG uses JBeam to place props, while the DAE still defines the mesh's
    # authored local basis. Derive the Blender preview basis from those two
    # source transforms instead of classifying prop names or functions.
    _spec_loc, spec_rot, _spec_scale = spec.transform_matrix.decompose()
    _template_loc, template_rot, template_scale = template_obj.matrix_world.decompose()
    positive_scale = Vector((abs(template_scale.x), abs(template_scale.y), abs(template_scale.z)))
    basis_rot = spec_rot.inverted() @ template_rot
    return Matrix.LocRotScale(Vector((0.0, 0.0, 0.0)), basis_rot, positive_scale)


def bake_negative_handedness_into_mesh(instance, target_matrix):
    if target_matrix.to_3x3().determinant() >= 0.0:
        return target_matrix, False

    # Keep the same visual result, but move the reflection from object scale into
    # this prop's mesh data so Blender reports a positive object transform.
    reflection = Matrix.Diagonal((-1.0, -1.0, -1.0, 1.0))
    instance.data = instance.data.copy()
    instance.data.transform(reflection)
    instance.data.update()
    return target_matrix @ reflection, True


def instantiate_flexbody(template_obj, spec: FlexbodySpec, destination_collection, parent_obj=None):
    instance = template_obj.copy()
    instance.data = template_obj.data
    destination_collection.objects.link(instance)
    instance.hide_set(False)
    instance.hide_render = False

    instance["beamng_part_name"] = spec.part_name
    instance["beamng_jbeam_path"] = str(spec.jbeam_path)
    instance["beamng_flexbody_mesh"] = spec.mesh
    instance["beamng_source_type"] = spec.source_type
    instance["beamng_resolved_part_id"] = spec.resolved_part_id
    instance["beamng_layer"] = "prop" if spec.source_type == "prop" else "mesh"
    instance["beamng_spec_pos"] = tuple(round(value, 6) for value in spec.pos)
    instance["beamng_spec_rot_deg"] = tuple(round(value, 6) for value in spec.rot)
    instance["beamng_spec_scale"] = tuple(round(value, 6) for value in spec.scale)
    template_location, template_rotation, template_scale = template_obj.matrix_world.decompose()
    instance["beamng_template_scale"] = tuple(round(value, 6) for value in template_scale)
    instance["beamng_template_rotation_deg"] = tuple(
        round(math.degrees(value), 6)
        for value in template_rotation.to_euler("XYZ")
    )
    if spec.debug_anchor_nodes:
        instance["beamng_prop_anchor_nodes"] = spec.debug_anchor_nodes
    if spec.debug_anchor_origin:
        instance["beamng_prop_anchor_origin"] = spec.debug_anchor_origin
    if spec.debug_anchor_x:
        instance["beamng_prop_anchor_x_ref"] = spec.debug_anchor_x
    if spec.debug_anchor_y:
        instance["beamng_prop_anchor_y_ref"] = spec.debug_anchor_y
    if spec.debug_missing_anchor_nodes:
        instance["beamng_prop_missing_anchor_nodes"] = spec.debug_missing_anchor_nodes
    if spec.debug_prop_base_translation:
        instance["beamng_prop_base_translation"] = spec.debug_prop_base_translation
    if spec.debug_prop_anim_translation:
        instance["beamng_prop_anim_translation"] = spec.debug_prop_anim_translation
    if spec.debug_prop_local_translation:
        instance["beamng_prop_local_translation"] = spec.debug_prop_local_translation
    if spec.debug_prop_world_translation_offset:
        instance["beamng_prop_world_translation_offset"] = spec.debug_prop_world_translation_offset
    if spec.debug_prop_global_translation:
        instance["beamng_prop_global_translation"] = spec.debug_prop_global_translation
    if spec.debug_prop_base_rotation:
        instance["beamng_prop_base_rotation_deg"] = spec.debug_prop_base_rotation
    if spec.debug_prop_row_rotation:
        instance["beamng_prop_row_rotation_deg"] = spec.debug_prop_row_rotation
    instance["beamng_prop_anim_factor"] = spec.debug_prop_anim_factor
    if spec.debug_prop_anchor_x_axis:
        instance["beamng_prop_anchor_x_axis"] = spec.debug_prop_anchor_x_axis
    if spec.debug_prop_anchor_y_axis:
        instance["beamng_prop_anchor_y_axis"] = spec.debug_prop_anchor_y_axis
    if spec.debug_prop_anchor_z_axis:
        instance["beamng_prop_anchor_z_axis"] = spec.debug_prop_anchor_z_axis
    instance["beamng_prop_anchor_determinant"] = spec.debug_prop_anchor_determinant

    normalized_negative_scale = False
    if spec.use_template_transform:
        target_matrix = spec.transform_matrix @ template_obj.matrix_world
    elif spec.keep_template_translation:
        target_matrix = spec.transform_matrix @ template_obj.matrix_world
    else:
        template_transform = matrix_without_translation(template_obj.matrix_world)
        if spec.source_type == "prop":
            template_transform = prop_template_basis_from_dae(spec, template_obj)
            instance["beamng_prop_template_orientation_mode"] = "dae_derived_basis"
        target_matrix = spec.transform_matrix @ template_transform
    if spec.source_type == "prop":
        target_matrix, normalized_negative_scale = bake_negative_handedness_into_mesh(instance, target_matrix)
    instance["beamng_normalized_negative_scale"] = normalized_negative_scale
    instance["beamng_target_world_loc"] = tuple(round(value, 6) for value in target_matrix.to_translation())

    if parent_obj is not None:
        instance.parent = parent_obj
        instance.matrix_parent_inverse = Matrix.Identity(4)
        local_matrix = parent_obj.matrix_world.inverted() @ target_matrix
        if spec.source_type == "prop" and spec.debug_prop_base_translation:
            # BeamNG applies baseTranslation as prop-local visual offset after
            # the anchor-derived placement. Keep this field-driven; no prop
            # names or families belong in the transform path.
            local_matrix.translation += Vector(spec.debug_prop_base_translation)
            instance["beamng_prop_applied_local_visual_offset"] = spec.debug_prop_base_translation
        instance.matrix_local = local_matrix
        final_world_matrix = parent_obj.matrix_world @ local_matrix
    else:
        instance.matrix_world = target_matrix
        local_matrix = target_matrix
        final_world_matrix = target_matrix
    instance["beamng_final_world_loc"] = tuple(round(value, 6) for value in final_world_matrix.to_translation())
    instance["beamng_final_local_loc"] = tuple(round(value, 6) for value in local_matrix.to_translation())
    return instance
