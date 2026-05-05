bl_info = {
    "name": "BeamNG .pc Importer",
    "author": "Glenn Campigli",
    "version": (1, 0, 1),
    "blender": (3, 6, 0),
    "location": "File > Import > BeamNG Config (.pc)",
    "description": "Import a BeamNG .pc vehicle config with only the selected meshes visible",
    "category": "Import-Export",
}

ADDON_BUILD = 1


def addon_version_label():
    version = ".".join(str(part) for part in bl_info["version"])
    return f"{version} build {ADDON_BUILD}"

import json
import math
import re
import tempfile
import zipfile
import colorsys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, IntProperty, StringProperty
from bpy.types import AddonPreferences, Operator, Panel, PropertyGroup
from bpy_extras.io_utils import ImportHelper
from mathutils import Euler, Matrix, Vector


try:
    import importlib
    from . import core as _core
    from . import dae_assets as _dae_assets
    from . import visuals as _visuals

    importlib.reload(_core)
    importlib.reload(_dae_assets)
    importlib.reload(_visuals)

    from .core import *
    from .dae_assets import *
    from .visuals import *
except ImportError:
    from core import *
    from dae_assets import *
    from visuals import *


SLOT_EDITOR_PART_INDEX = {}
SLOT_EDITOR_REFRESHING = False


def refresh_slot_child_flags(items):
    parent_paths = {item.parent_path for item in items if item.parent_path}
    for item in items:
        item.has_children = item.path in parent_paths


def slot_editor_snapshot(items, override_path="", override_choice=None, override_selected_part=None):
    snapshot = []
    for item in items:
        choice = item.choice
        selected_part = item.selected_part
        if item.path == override_path:
            if override_choice is not None:
                choice = override_choice
            if override_selected_part is not None:
                selected_part = override_selected_part
        snapshot.append(
            {
                "path": item.path,
                "parent_path": item.parent_path,
                "slot_name": item.slot_name,
                "parent_part": item.parent_part,
                "selected_part": selected_part,
                "options_json": item.options_json,
                "depth": item.depth,
                "is_core": item.is_core,
                "has_children": item.has_children,
                "expanded": item.expanded,
                "choice": choice,
            }
        )
    return snapshot


def slot_editor_snapshot_json(items, **kwargs):
    return json.dumps(slot_editor_snapshot(items, **kwargs), sort_keys=True)


def slot_editor_config_snapshot(items):
    return [
        {
            "path": item.path,
            "slot_name": item.slot_name,
            "selected_part": item.selected_part,
            "choice": item.choice,
        }
        for item in items
    ]


def slot_editor_config_snapshot_json(items):
    return json.dumps(slot_editor_config_snapshot(items), sort_keys=True)


def restore_slot_editor_snapshot(scene, snapshot_json):
    global SLOT_EDITOR_REFRESHING
    items = scene.beamng_slot_editor_items
    snapshot = json.loads(snapshot_json) if snapshot_json else []
    SLOT_EDITOR_REFRESHING = True
    try:
        items.clear()
        for saved in snapshot:
            item = items.add()
            item.path = saved.get("path", "")
            item.parent_path = saved.get("parent_path", "")
            item.slot_name = saved.get("slot_name", "")
            item.parent_part = saved.get("parent_part", "")
            item.selected_part = saved.get("selected_part", "")
            item.options_json = saved.get("options_json", "[]")
            item.depth = int(saved.get("depth", 0))
            item.is_core = bool(saved.get("is_core", False))
            item.has_children = bool(saved.get("has_children", False))
            item.expanded = bool(saved.get("expanded", True))
            item.choice = saved.get("choice", "__EMPTY__")
        refresh_slot_child_flags(items)
    finally:
        SLOT_EDITOR_REFRESHING = False


def update_slot_editor_dirty_state(scene):
    current = slot_editor_config_snapshot_json(scene.beamng_slot_editor_items)
    baseline = scene.get("beamng_slot_editor_baseline_snapshot", "")
    scene["beamng_slot_editor_dirty"] = bool(baseline and current != baseline)


def set_slot_editor_item(item, slot, part_index, selected_part, parent_part, depth, path, parent_path, expanded=True):
    option_items = slot_option_items_for_storage(slot, selected_part, part_index)
    valid_identifiers = {option["identifier"] for option in option_items}
    choice = selected_part if selected_part in valid_identifiers else "__EMPTY__"
    if slot.get("core_slot") and choice == "__EMPTY__":
        choice = selected_part if selected_part else slot.get("default", "")
        if choice not in valid_identifiers:
            choice = option_items[0]["identifier"] if option_items else "__NEW__"

    item.path = path
    item.parent_path = parent_path
    item.slot_name = slot.get("name", "")
    item.parent_part = parent_part
    item.selected_part = selected_part
    item.options_json = json.dumps(option_items)
    item.depth = depth
    item.is_core = bool(slot.get("core_slot"))
    item.has_children = False
    item.expanded = expanded
    item.choice = choice


def add_slot_editor_rows(items, part_index, selected_parts, part_name, depth, parent_path, ancestry, default_children=False):
    added_paths = []
    if not part_name or part_name not in part_index or part_name in ancestry:
        return added_paths
    part_def = part_index[part_name]
    for slot_index, slot in enumerate(parse_slots(part_def.data)):
        slot_name = slot.get("name", "")
        if not slot_name:
            continue
        selected_part = slot.get("default", "") if default_children else selected_parts.get(slot_name, slot.get("default", ""))
        selected_part = selected_part or ""
        path = f"{parent_path}/{slot_index}:{slot_name}" if parent_path else f"{slot_index}:{slot_name}"
        item = items.add()
        set_slot_editor_item(
            item,
            slot,
            part_index,
            selected_part,
            part_name,
            depth,
            path,
            parent_path,
            depth < 2,
        )
        added_paths.append(path)
        if selected_part in part_index:
            added_paths.extend(
                add_slot_editor_rows(
                    items,
                    part_index,
                    selected_parts,
                    selected_part,
                    depth + 1,
                    path,
                    {*ancestry, part_name},
                    default_children,
                )
            )
    return added_paths


def refresh_slot_editor_descendants(scene, item):
    global SLOT_EDITOR_REFRESHING
    if SLOT_EDITOR_REFRESHING or not SLOT_EDITOR_PART_INDEX:
        return
    items = scene.beamng_slot_editor_items
    item_path = item.path
    previous_selected_part = item.selected_part
    previous_choice = "__EMPTY__" if not previous_selected_part else previous_selected_part
    previous_snapshot = slot_editor_snapshot_json(
        items,
        override_path=item_path,
        override_choice=previous_choice,
        override_selected_part=previous_selected_part,
    )
    selected_part = "" if item.choice == "__EMPTY__" else item.choice

    SLOT_EDITOR_REFRESHING = True
    try:
        scene["beamng_slot_editor_last_snapshot"] = previous_snapshot
        item.selected_part = selected_part
        remove_prefix = item_path + "/"
        remove_indices = [
            index
            for index, candidate in enumerate(items)
            if candidate.path.startswith(remove_prefix)
        ]
        for index in reversed(remove_indices):
            items.remove(index)

        if selected_part and selected_part in SLOT_EDITOR_PART_INDEX:
            selected_parts = {}
            before_count = len(items)
            add_slot_editor_rows(
                items,
                SLOT_EDITOR_PART_INDEX,
                selected_parts,
                selected_part,
                item.depth + 1,
                item_path,
                {item.parent_part},
                True,
            )
            added_count = len(items) - before_count
            current_index = next((index for index, candidate in enumerate(items) if candidate.path == item_path), -1)
            if current_index >= 0:
                for offset in range(added_count):
                    items.move(before_count + offset, current_index + 1 + offset)
            item.expanded = True

        refresh_slot_child_flags(items)
        update_slot_editor_dirty_state(scene)
    finally:
        SLOT_EDITOR_REFRESHING = False


def slot_choice_updated(self, _context):
    self.selected_part = "" if self.choice == "__EMPTY__" else self.choice
    if _context is not None and hasattr(_context.scene, "beamng_slot_editor_items"):
        refresh_slot_editor_descendants(_context.scene, self)


class BeamNGSlotEditorItem(PropertyGroup):
    path: StringProperty(default="")
    parent_path: StringProperty(default="")
    slot_name: StringProperty(default="")
    parent_part: StringProperty(default="")
    selected_part: StringProperty(default="")
    options_json: StringProperty(default="[]")
    depth: IntProperty(default=0)
    is_core: BoolProperty(default=False)
    has_children: BoolProperty(default=False)
    expanded: BoolProperty(default=True)
    choice: EnumProperty(name="Part", items=slot_choice_items, update=slot_choice_updated)


def populate_vehicle_slot_editor(scene, pc_data, part_index):
    global SLOT_EDITOR_PART_INDEX, SLOT_EDITOR_REFRESHING
    if not hasattr(scene, "beamng_slot_editor_items"):
        return
    SLOT_EDITOR_PART_INDEX = dict(part_index)
    items = scene.beamng_slot_editor_items
    SLOT_EDITOR_REFRESHING = True
    items.clear()

    try:
        selected_parts = pc_data.get("parts", {})
        if not isinstance(selected_parts, dict):
            selected_parts = {}
        main_part, _main_part_source = infer_main_part_name(pc_data, part_index)
        scene["beamng_slot_editor_main_part"] = main_part or ""
        scene["beamng_slot_editor_model"] = str(pc_data.get("model", ""))

        add_slot_editor_rows(items, part_index, selected_parts, main_part, 0, "", set(), False)
        refresh_slot_child_flags(items)
        scene["beamng_slot_editor_baseline_snapshot"] = slot_editor_config_snapshot_json(items)
        scene["beamng_slot_editor_last_snapshot"] = ""
        scene["beamng_slot_editor_dirty"] = False
    finally:
        SLOT_EDITOR_REFRESHING = False


def slot_editor_pc_data_from_scene(scene):
    roots = find_beamng_import_collections(scene)
    if not roots:
        raise ValueError("No BeamNG import collection is available to reload")
    root = roots[0]
    source_pc_path = Path(root.get("beamng_pc_source_path", root.get("beamng_pc_path", "")))
    if not source_pc_path.exists():
        raise ValueError(f"Original .pc file is no longer available: {source_pc_path}")

    pc_data = load_jsonc(source_pc_path)
    parts = pc_data.get("parts")
    if not isinstance(parts, dict):
        parts = {}
        pc_data["parts"] = parts

    for item in scene.beamng_slot_editor_items:
        choice = item.choice
        if choice == "__NEW__":
            raise ValueError(f"Slot '{item.slot_name}' is set to New...., which is not implemented yet")
        if choice == "__EMPTY__":
            parts[item.slot_name] = ""
        else:
            parts[item.slot_name] = choice

    vehicle_name = scene.get("beamng_slot_editor_model", "") or source_pc_path.parent.name
    edited_path = persistent_cache_dir() / "pc_editor" / "vehicles" / str(vehicle_name) / source_pc_path.name
    edited_path.parent.mkdir(parents=True, exist_ok=True)
    edited_path.write_text(json.dumps(pc_data, indent=2), encoding="utf-8")
    return edited_path, source_pc_path


def resolve_selected_parts(pc_data, part_index, include_props=True):
    main_part, _main_part_source = infer_main_part_name(pc_data, part_index)
    if not main_part:
        raise ValueError("The .pc file is missing mainPartName and no main slot part could be inferred")

    selected_parts = pc_data.get("parts", {})
    resolved_parts = []
    visited = set()

    def walk(part_name: str, accumulated_transform: Matrix, local_transform: Matrix, parent_id: int, slot_name: str, component_context):
        if not part_name or part_name not in part_index:
            return

        translation = accumulated_transform.to_translation()
        visit_key = (part_name, parent_id, tuple(round(v, 6) for v in translation))
        if visit_key in visited:
            return
        visited.add(visit_key)

        part_def = part_index[part_name]
        current_components = merge_component_context(component_context, extract_component_context(part_def.data))
        part_id = len(resolved_parts)
        resolved_parts.append(
            ResolvedPart(
                id=part_id,
                part_def=part_def,
                transform_matrix=accumulated_transform,
                local_transform_matrix=local_transform,
                parent_id=parent_id,
                slot_name=slot_name,
                component_context=current_components,
            )
        )

        for slot in parse_slots(part_def.data):
            selected_name = selected_parts.get(slot["name"], slot["default"])
            if not selected_name:
                continue
            slot_transform = get_option_transform(slot["options"], current_components)
            walk(
                selected_name,
                accumulated_transform @ slot_transform,
                slot_transform,
                part_id,
                slot["name"],
                current_components,
            )

    walk(main_part, Matrix.Identity(4), Matrix.Identity(4), -1, "", {})
    global_components = {}
    for resolved_part in resolved_parts:
        global_components = merge_component_context(global_components, extract_component_context(resolved_part.part_def.data))

    resolved_node_positions = []
    global_node_positions = {}
    resolved_component_contexts = []
    for resolved_part in resolved_parts:
        component_context = merge_component_context(
            global_components,
            resolved_part.component_context,
        )
        resolved_component_contexts.append(component_context)
        local_node_positions = parse_nodes(
            resolved_part.part_def,
            resolved_part.transform_matrix,
            component_context,
        )
        resolved_node_positions.append(local_node_positions)
        for name, position in local_node_positions.items():
            global_node_positions.setdefault(name, position)

    flexbodies = []
    visual_nodes = []
    visual_beams = []
    visual_triangles = []
    visual_hydros = []
    visual_rails = []
    visual_slidenodes = []
    for index, resolved_part in enumerate(resolved_parts):
        component_context = resolved_component_contexts[index]
        for node_name, position in resolved_node_positions[index].items():
            visual_nodes.append(
                JBeamNodeSpec(
                    name=node_name,
                    position=position,
                    part_name=resolved_part.part_def.name,
                    resolved_part_id=resolved_part.id,
                )
            )
        visual_beams.extend(
            parse_beams(
                resolved_part.part_def,
                resolved_node_positions[index],
                global_node_positions,
                component_context,
                resolved_part.id,
            )
        )
        visual_triangles.extend(
            parse_triangles(
                resolved_part.part_def,
                resolved_node_positions[index],
                global_node_positions,
                component_context,
                resolved_part.id,
            )
        )
        visual_hydros.extend(
            parse_hydros(
                resolved_part.part_def,
                resolved_node_positions[index],
                global_node_positions,
                component_context,
                resolved_part.id,
            )
        )
        part_rails, part_slidenodes = parse_rails_and_slidenodes(
            resolved_part.part_def,
            resolved_node_positions[index],
            global_node_positions,
            component_context,
            resolved_part.id,
        )
        visual_rails.extend(part_rails)
        visual_slidenodes.extend(part_slidenodes)
        flexbodies.extend(
            parse_flexbodies(
                resolved_part.part_def,
                resolved_part.transform_matrix,
                component_context,
                resolved_part.id,
            )
        )
        if include_props:
            flexbodies.extend(
                parse_props(
                    resolved_part.part_def,
                    resolved_part.transform_matrix,
                    component_context,
                    global_node_positions,
                    resolved_node_positions[index],
                    resolved_part.id,
                )
            )

    return (
        resolved_parts,
        flexbodies,
        visual_nodes,
        visual_beams,
        visual_triangles,
        visual_hydros,
        visual_rails,
        visual_slidenodes,
    )






def update_import_progress(context, percent, message):
    wm = context.window_manager
    wm.progress_update(percent)
    if context.workspace:
        context.workspace.status_text_set(f"BeamNG import: {message} ({percent}%)")
    for window in wm.windows:
        for area in window.screen.areas:
            area.tag_redraw()
    last_percent = getattr(update_import_progress, "last_percent", None)
    if percent != last_percent or percent in {1, 5, 10, 20, 28, 35, 98, 100}:
        print(f"[BeamNG Importer] {percent:3d}% {message}")
        update_import_progress.last_percent = percent


def walk_collection_objects(collection):
    for obj in collection.objects:
        yield obj
    for child in collection.children:
        yield from walk_collection_objects(child)


def walk_child_collections(collection):
    for child in collection.children:
        yield child
        yield from walk_child_collections(child)


def find_beamng_import_collections(scene):
    return [
        collection
        for collection in walk_child_collections(scene.collection)
        if collection.get("beamng_pc_import_root")
    ]


def set_import_layer_visibility(root_collection, show_meshes=True, show_props=True, show_jbeam=True):
    show_hierarchy = show_meshes or show_props
    for obj in walk_collection_objects(root_collection):
        layer = obj.get("beamng_layer")
        if layer == "hierarchy":
            obj.hide_set(not show_hierarchy)
            obj.hide_render = True
        elif layer == "mesh":
            obj.hide_set(not show_meshes)
            obj.hide_render = not show_meshes
        elif layer == "prop":
            obj.hide_set(not show_props)
            obj.hide_render = not show_props
        elif layer == "jbeam":
            obj.hide_set(not show_jbeam)
            obj.hide_render = not show_jbeam
            if obj.get("beamng_visual_type") == "selectable_triangle":
                obj.hide_select = not show_jbeam

    for child in root_collection.children:
        if child.get("beamng_layer") == "jbeam":
            child.hide_viewport = not show_jbeam
            child.hide_render = not show_jbeam


def selected_jbeam_part_id(context):
    active = context.active_object
    if not active or active.get("beamng_layer") != "jbeam":
        return None
    part_id = active.get("beamng_resolved_part_id")
    if part_id is None:
        return None
    try:
        return int(part_id)
    except (TypeError, ValueError):
        return None


def find_jbeam_root_for_object(context, obj):
    if obj is None:
        return None
    collections = list(obj.users_collection)
    seen = set()
    while collections:
        collection = collections.pop(0)
        if collection in seen:
            continue
        seen.add(collection)
        current = collection
        while current is not None:
            if current.get("beamng_pc_import_root"):
                return current
            parents = [candidate for candidate in bpy.data.collections if current.name in candidate.children.keys()]
            current = parents[0] if parents else None
    roots = find_beamng_import_collections(context.scene)
    return roots[0] if roots else None


def jbeam_part_parent_map(root_collection):
    parents = {}
    for collection in walk_child_collections(root_collection):
        if collection.get("beamng_layer") != "jbeam":
            continue
        part_id = collection.get("beamng_resolved_part_id")
        if part_id is None:
            continue
        try:
            part_id = int(part_id)
            parent_id = int(collection.get("beamng_parent_resolved_part_id", -1))
        except (TypeError, ValueError):
            continue
        parents.setdefault(part_id, parent_id)
    return parents


def related_jbeam_part_ids(root_collection, part_id, relation):
    parents = jbeam_part_parent_map(root_collection)
    if part_id not in parents:
        return set()

    if relation == "self":
        return {part_id}

    parent_id = parents.get(part_id, -1)
    if relation == "siblings":
        return {candidate for candidate, candidate_parent in parents.items() if candidate_parent == parent_id}

    if relation == "parents":
        result = set()
        current = parent_id
        while current in parents and current not in result:
            result.add(current)
            current = parents.get(current, -1)
        return result

    if relation == "children":
        result = set()
        pending = [part_id]
        while pending:
            current = pending.pop()
            for candidate, candidate_parent in parents.items():
                if candidate_parent == current and candidate not in result:
                    result.add(candidate)
                    pending.append(candidate)
        return result

    return set()


def jbeam_objects_for_part_ids(root_collection, part_ids, visual_types=None):
    if visual_types is not None:
        visual_types = set(visual_types)
    objects = []
    for obj in walk_collection_objects(root_collection):
        if obj.get("beamng_layer") != "jbeam":
            continue
        allowed_visual_types = {
            "nodes",
            "beams",
            "triangles",
            "hydros",
            "rails",
            "selectable_node",
            "selectable_beam",
            "selectable_triangle",
            "selectable_hydro",
            "selectable_rail",
            "selectable_slidenode",
            "node_label",
        }
        visual_type = obj.get("beamng_visual_type")
        if visual_type not in allowed_visual_types:
            continue
        if visual_types is not None and visual_type not in visual_types:
            continue
        try:
            part_id = int(obj.get("beamng_resolved_part_id", -999999))
        except (TypeError, ValueError):
            continue
        if part_id in part_ids:
            objects.append(obj)
    return objects


def set_jbeam_collections_visibility(root_collection, part_ids, visible):
    for collection in walk_child_collections(root_collection):
        if collection.get("beamng_layer") != "jbeam":
            continue
        try:
            part_id = int(collection.get("beamng_resolved_part_id", -999999))
        except (TypeError, ValueError):
            continue
        if part_id in part_ids:
            collection.hide_viewport = not visible
            collection.hide_render = not visible


def set_all_jbeam_visibility(root_collection, visible):
    for collection in walk_child_collections(root_collection):
        if collection.get("beamng_layer") == "jbeam":
            collection.hide_viewport = not visible
            collection.hide_render = not visible

    for obj in walk_collection_objects(root_collection):
        if obj.get("beamng_layer") != "jbeam":
            continue
        obj.hide_set(not visible)
        obj.hide_render = obj.get("beamng_visual_type") == "node_label" or not visible
        if obj.get("beamng_visual_type") == "selectable_triangle":
            obj.hide_select = not visible


def set_jbeam_visual_type_visibility(root_collection, visual_types, visible):
    visual_types = set(visual_types)
    for obj in walk_collection_objects(root_collection):
        if obj.get("beamng_layer") != "jbeam":
            continue
        if obj.get("beamng_visual_type") not in visual_types:
            continue
        obj.hide_set(not visible)
        obj.hide_render = not visible
        if obj.get("beamng_visual_type") == "selectable_triangle":
            obj.hide_select = not visible


class BEAMNG_OT_jbeam_relationship(Operator):
    bl_idname = "beamng_pc_importer.jbeam_relationship"
    bl_label = "JBeam Relationship Action"
    bl_options = {"REGISTER", "UNDO"}

    relation: StringProperty(default="siblings")
    action: StringProperty(default="select")

    @classmethod
    def poll(cls, context):
        return selected_jbeam_part_id(context) is not None

    def execute(self, context):
        part_id = selected_jbeam_part_id(context)
        root = find_jbeam_root_for_object(context, context.active_object)
        if root is None or part_id is None:
            self.report({"WARNING"}, "No BeamNG JBeam selection found")
            return {"CANCELLED"}

        part_ids = related_jbeam_part_ids(root, part_id, self.relation)
        if not part_ids:
            self.report({"WARNING"}, f"No {self.relation} JBeam parts found")
            return {"CANCELLED"}

        if self.action == "select":
            for obj in context.scene.objects:
                obj.select_set(False)
            objects = jbeam_objects_for_part_ids(root, part_ids)
            for obj in objects:
                obj.hide_set(False)
                obj.select_set(True)
            if objects:
                context.view_layer.objects.active = objects[0]
            self.report({"INFO"}, f"Selected {len(objects)} JBeam objects")
            return {"FINISHED"}

        if self.action in {"hide", "show"}:
            visible = self.action == "show"
            set_jbeam_collections_visibility(root, part_ids, visible)
            for obj in jbeam_objects_for_part_ids(root, part_ids):
                obj.hide_set(not visible)
                obj.hide_render = not visible
            self.report({"INFO"}, f"{'Showed' if visible else 'Hid'} {len(part_ids)} JBeam part groups")
            return {"FINISHED"}

        return {"CANCELLED"}


class BEAMNG_OT_select_jbeam_body_structure(Operator):
    bl_idname = "beamng_pc_importer.select_jbeam_body_structure"
    bl_label = "Select Same Body Nodes/Beams"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return selected_jbeam_part_id(context) is not None

    def execute(self, context):
        part_id = selected_jbeam_part_id(context)
        root = find_jbeam_root_for_object(context, context.active_object)
        if root is None or part_id is None:
            self.report({"WARNING"}, "No BeamNG JBeam selection found")
            return {"CANCELLED"}

        set_jbeam_collections_visibility(root, {part_id}, True)
        objects = jbeam_objects_for_part_ids(
            root,
            {part_id},
            {"selectable_node", "selectable_beam"},
        )
        if not objects:
            self.report({"WARNING"}, "No selectable nodes or beams found for this body")
            return {"CANCELLED"}

        for obj in context.scene.objects:
            obj.select_set(False)
        for obj in objects:
            obj.hide_set(False)
            obj.hide_select = False
            obj.select_set(True)
        context.view_layer.objects.active = objects[0]
        self.report({"INFO"}, f"Selected {len(objects)} node/beam objects from this body")
        return {"FINISHED"}


class BEAMNG_OT_show_all_jbeams(Operator):
    bl_idname = "beamng_pc_importer.show_all_jbeams"
    bl_label = "Show All JBeams"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        roots = find_beamng_import_collections(context.scene)
        if not roots:
            self.report({"WARNING"}, "No BeamNG import collections found")
            return {"CANCELLED"}

        shown_roots = 0
        for root in roots:
            set_all_jbeam_visibility(root, True)
            shown_roots += 1

        self.report({"INFO"}, f"Showed all JBeams in {shown_roots} import collection(s)")
        return {"FINISHED"}


class BEAMNG_OT_hide_selected_jbeam_items(Operator):
    bl_idname = "beamng_pc_importer.hide_selected_jbeam_items"
    bl_label = "Hide Selected JBeam Items"
    bl_description = "Hide selected selectable JBeam nodes, beams, triangles, hydros, rails, and slidenodes"
    bl_options = {"REGISTER", "UNDO"}

    selectable_visual_types = {
        "selectable_node",
        "node_label",
        "selectable_beam",
        "selectable_triangle",
        "selectable_hydro",
        "selectable_rail",
        "selectable_slidenode",
    }

    @classmethod
    def poll(cls, context):
        return any(
            obj.get("beamng_layer") == "jbeam" and obj.get("beamng_visual_type") in cls.selectable_visual_types
            for obj in context.selected_objects
        )

    def execute(self, context):
        objects = [
            obj
            for obj in context.selected_objects
            if obj.get("beamng_layer") == "jbeam" and obj.get("beamng_visual_type") in self.selectable_visual_types
        ]
        if not objects:
            self.report({"WARNING"}, "No selected selectable JBeam items to hide")
            return {"CANCELLED"}

        for obj in objects:
            obj.hide_set(True)
            obj.select_set(False)

        self.report({"INFO"}, f"Hid {len(objects)} selected JBeam item(s)")
        return {"FINISHED"}


class BEAMNG_OT_set_jbeam_visual_visibility(Operator):
    bl_idname = "beamng_pc_importer.set_jbeam_visual_visibility"
    bl_label = "Set JBeam Visual Visibility"
    bl_options = {"REGISTER", "UNDO"}

    visual_group: StringProperty(default="triangles")
    action: StringProperty(default="show")

    def execute(self, context):
        roots = find_beamng_import_collections(context.scene)
        if not roots:
            self.report({"WARNING"}, "No BeamNG import collections found")
            return {"CANCELLED"}

        visual_groups = {
            "nodes": {"nodes", "selectable_node", "node_label"},
            "beams": {"beams", "selectable_beam"},
            "triangles": {"triangles", "selectable_triangle"},
            "hydros": {"hydros", "selectable_hydro"},
            "sliders": {"rails", "selectable_rail", "selectable_slidenode"},
        }
        visual_types = visual_groups.get(self.visual_group)
        if not visual_types:
            self.report({"WARNING"}, f"Unknown JBeam visual group: {self.visual_group}")
            return {"CANCELLED"}

        visible = self.action == "show"
        for root in roots:
            set_jbeam_visual_type_visibility(root, visual_types, visible)

        label = self.visual_group.capitalize()
        self.report({"INFO"}, f"{'Showed' if visible else 'Hid'} JBeam {label}")
        return {"FINISHED"}


def format_vector(values):
    return ", ".join(f"{value:.6f}" for value in values)


def format_object_transform(obj):
    location, rotation, scale = obj.matrix_world.decompose()
    rotation_deg = [math.degrees(value) for value in rotation.to_euler("XYZ")]
    local_location, local_rotation, local_scale = obj.matrix_local.decompose()
    local_rotation_deg = [math.degrees(value) for value in local_rotation.to_euler("XYZ")]
    return {
        "world_location": location,
        "world_rotation_deg": rotation_deg,
        "world_scale": scale,
        "local_location": local_location,
        "local_rotation_deg": local_rotation_deg,
        "local_scale": local_scale,
    }


class BEAMNG_OT_set_visibility(Operator):
    bl_idname = "beamng_pc_importer.set_visibility"
    bl_label = "Set BeamNG Import Visibility"
    bl_options = {"REGISTER", "UNDO"}

    mode: StringProperty(default="BOTH")

    def execute(self, context):
        roots = find_beamng_import_collections(context.scene)
        if not roots:
            self.report({"WARNING"}, "No BeamNG import collections found")
            return {"CANCELLED"}

        show_meshes = self.mode in {"MESHES", "MESHES_PROPS", "ALL"}
        show_props = self.mode in {"PROPS", "MESHES_PROPS", "ALL"}
        show_jbeam = self.mode in {"JBEAM", "ALL"}
        for root in roots:
            set_import_layer_visibility(root, show_meshes, show_props, show_jbeam)

        return {"FINISHED"}


class BEAMNG_OT_print_prop_transforms(Operator):
    bl_idname = "beamng_pc_importer.print_prop_transforms"
    bl_label = "Write Prop Transform Debug File"
    bl_options = {"REGISTER"}

    def execute(self, context):
        roots = find_beamng_import_collections(context.scene)
        props = [
            obj
            for root in roots
            for obj in walk_collection_objects(root)
            if obj.get("beamng_layer") == "prop"
        ]

        if not props:
            self.report({"WARNING"}, "No imported BeamNG props found")
            return {"CANCELLED"}

        first_root = roots[0]
        pc_path = Path(first_root.get("beamng_pc_path", ""))
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if pc_path.name:
            report_name = f"{pc_path.stem}_prop_debug_{timestamp}.txt"
            report_path = pc_path.parent / report_name
        else:
            report_name = f"beamng_prop_debug_{timestamp}.txt"
            report_path = Path(tempfile.gettempdir()) / report_name

        lines = [
            "[BeamNG Importer] Prop transform diagnostics",
            f"Generated: {datetime.now().isoformat(timespec='seconds')}",
            f"Scene: {context.scene.name}",
            f"Imports: {len(roots)}",
            f"Props found: {len(props)}",
            "",
        ]
        for obj in sorted(props, key=lambda item: item.name_full):
            transform = format_object_transform(obj)
            parent_name = obj.parent.name_full if obj.parent else "<none>"
            lines.extend(
                [
                    f"[BeamNG Prop] {obj.name_full}",
                    f"  mesh={obj.get('beamng_flexbody_mesh', '')} part={obj.get('beamng_part_name', '')} parent={parent_name}",
                    f"  jbeam={obj.get('beamng_jbeam_path', '')}",
                    f"  anchor_nodes={obj.get('beamng_prop_anchor_nodes', ())}",
                    f"  anchor_origin=({format_vector(obj.get('beamng_prop_anchor_origin', (0.0, 0.0, 0.0)))})",
                    f"  anchor_x_ref=({format_vector(obj.get('beamng_prop_anchor_x_ref', (0.0, 0.0, 0.0)))})",
                    f"  anchor_y_ref=({format_vector(obj.get('beamng_prop_anchor_y_ref', (0.0, 0.0, 0.0)))})",
                    f"  missing_anchor_nodes={obj.get('beamng_prop_missing_anchor_nodes', ())}",
                    f"  local_translation=({format_vector(obj.get('beamng_prop_local_translation', (0.0, 0.0, 0.0)))})",
                    f"  global_translation=({format_vector(obj.get('beamng_prop_global_translation', (0.0, 0.0, 0.0)))})",
                    f"  base_rot_deg=({format_vector(obj.get('beamng_prop_base_rotation_deg', (0.0, 0.0, 0.0)))})",
                    f"  row_rot_deg=({format_vector(obj.get('beamng_prop_row_rotation_deg', (0.0, 0.0, 0.0)))})",
                    f"  row_anim_factor={obj.get('beamng_prop_anim_factor', 0.0):.6f}",
                    f"  spec_pos=({format_vector(obj.get('beamng_spec_pos', (0.0, 0.0, 0.0)))})",
                    f"  spec_rot_deg=({format_vector(obj.get('beamng_spec_rot_deg', (0.0, 0.0, 0.0)))})",
                    f"  template_scale=({format_vector(obj.get('beamng_template_scale', (0.0, 0.0, 0.0)))})",
                    f"  template_orientation_mode={obj.get('beamng_prop_template_orientation_mode', '')}",
                    f"  normalized_negative_scale={obj.get('beamng_normalized_negative_scale', False)}",
                    f"  world_loc=({format_vector(transform['world_location'])})",
                    f"  world_rot_deg=({format_vector(transform['world_rotation_deg'])})",
                    f"  world_scale=({format_vector(transform['world_scale'])})",
                    f"  local_loc=({format_vector(transform['local_location'])})",
                    f"  local_rot_deg=({format_vector(transform['local_rotation_deg'])})",
                    f"  local_scale=({format_vector(transform['local_scale'])})",
                    "",
                ]
            )

        try:
            report_path.write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            report_path = Path(tempfile.gettempdir()) / report_name
            report_path.write_text("\n".join(lines), encoding="utf-8")

        print(f"[BeamNG Importer] Wrote prop diagnostics to: {report_path}")
        self.report({"INFO"}, f"Wrote prop diagnostics: {report_path}")
        return {"FINISHED"}


class BEAMNG_OT_toggle_relationship_lines(Operator):
    bl_idname = "beamng_pc_importer.toggle_relationship_lines"
    bl_label = "Toggle Relationship Lines"
    bl_options = {"REGISTER"}

    def execute(self, context):
        space = context.space_data
        if not space or space.type != "VIEW_3D":
            self.report({"WARNING"}, "Open this from a 3D Viewport")
            return {"CANCELLED"}

        overlay = space.overlay
        overlay.show_relationship_lines = not overlay.show_relationship_lines
        state = "shown" if overlay.show_relationship_lines else "hidden"
        self.report({"INFO"}, f"Relationship lines {state}")
        return {"FINISHED"}


class BEAMNG_OT_apply_slot_configuration(Operator):
    bl_idname = "beamng_pc_importer.apply_slot_configuration"
    bl_label = "Apply / Reload Vehicle"
    bl_description = "Rebuild the imported vehicle from the current slot dropdown selections"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(
            getattr(context.scene, "beamng_slot_editor_items", None)
            and context.scene.get("beamng_slot_editor_dirty", False)
        )

    def execute(self, context):
        if not getattr(context.scene, "beamng_slot_editor_items", None):
            self.report({"WARNING"}, "No vehicle slot tree is available")
            return {"CANCELLED"}

        try:
            edited_pc_path, source_pc_path = slot_editor_pc_data_from_scene(context.scene)
            context.scene["beamng_slot_editor_source_pc_path"] = str(source_pc_path)
            return import_beamng_pc_path(
                context,
                self,
                edited_pc_path,
                True,
                True,
                False,
                False,
                f"Edited slot configuration from {source_pc_path}",
            )
        except Exception as exc:
            self.report({"ERROR"}, f"Failed to apply slot configuration: {exc}")
            return {"CANCELLED"}


class BEAMNG_OT_revert_slot_change(Operator):
    bl_idname = "beamng_pc_importer.revert_slot_change"
    bl_label = "Revert Slot Change"
    bl_description = "Undo the most recent vehicle slot dropdown change"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(
            getattr(context.scene, "beamng_slot_editor_items", None)
            and context.scene.get("beamng_slot_editor_dirty", False)
            and context.scene.get("beamng_slot_editor_last_snapshot", "")
        )

    def execute(self, context):
        snapshot = context.scene.get("beamng_slot_editor_last_snapshot", "")
        if not snapshot:
            self.report({"WARNING"}, "No slot change is available to revert")
            return {"CANCELLED"}

        try:
            restore_slot_editor_snapshot(context.scene, snapshot)
            context.scene["beamng_slot_editor_last_snapshot"] = ""
            update_slot_editor_dirty_state(context.scene)
            self.report({"INFO"}, "Reverted the last slot change")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, f"Failed to revert slot change: {exc}")
            return {"CANCELLED"}


class VIEW3D_PT_beamng_pc_importer(Panel):
    bl_label = "BeamNG PC Importer"
    bl_idname = "VIEW3D_PT_beamng_pc_importer"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BeamNG"

    def draw(self, context):
        layout = self.layout
        roots = find_beamng_import_collections(context.scene)
        layout.label(text=f"Imports: {len(roots)}")
        layout.label(text=f"Version: {addon_version_label()}")

        row = layout.row(align=True)
        op = row.operator(BEAMNG_OT_set_visibility.bl_idname, text="Body Only")
        op.mode = "MESHES"
        op = row.operator(BEAMNG_OT_set_visibility.bl_idname, text="Props Only")
        op.mode = "PROPS"

        row = layout.row(align=True)
        op = row.operator(BEAMNG_OT_set_visibility.bl_idname, text="JBeam Only")
        op.mode = "JBEAM"
        op = row.operator(BEAMNG_OT_set_visibility.bl_idname, text="Body + Props")
        op.mode = "MESHES_PROPS"

        op = layout.operator(BEAMNG_OT_set_visibility.bl_idname, text="Show All")
        op.mode = "ALL"
        layout.operator(BEAMNG_OT_show_all_jbeams.bl_idname, text="Show All JBeams")
        layout.operator(BEAMNG_OT_hide_selected_jbeam_items.bl_idname, text="Hide Selected JBeam Items")
        row = layout.row(align=True)
        op = row.operator(BEAMNG_OT_set_jbeam_visual_visibility.bl_idname, text="Show Triangles")
        op.visual_group = "triangles"
        op.action = "show"
        op = row.operator(BEAMNG_OT_set_jbeam_visual_visibility.bl_idname, text="Hide Triangles")
        op.visual_group = "triangles"
        op.action = "hide"
        row = layout.row(align=True)
        op = row.operator(BEAMNG_OT_set_jbeam_visual_visibility.bl_idname, text="Show Hydros")
        op.visual_group = "hydros"
        op.action = "show"
        op = row.operator(BEAMNG_OT_set_jbeam_visual_visibility.bl_idname, text="Hide Hydros")
        op.visual_group = "hydros"
        op.action = "hide"
        row = layout.row(align=True)
        op = row.operator(BEAMNG_OT_set_jbeam_visual_visibility.bl_idname, text="Show Sliders")
        op.visual_group = "sliders"
        op.action = "show"
        op = row.operator(BEAMNG_OT_set_jbeam_visual_visibility.bl_idname, text="Hide Sliders")
        op.visual_group = "sliders"
        op.action = "hide"

        layout.separator()
        slot_items = getattr(context.scene, "beamng_slot_editor_items", [])
        if slot_items:
            box = layout.box()
            box.label(text="Vehicle Slots")
            main_part = context.scene.get("beamng_slot_editor_main_part", "")
            if main_part:
                box.label(text=f"Root: {main_part}")
            row = box.row(align=True)
            row.operator(BEAMNG_OT_apply_slot_configuration.bl_idname, text="Apply / Reload")
            row.operator(BEAMNG_OT_revert_slot_change.bl_idname, text="Revert")
            expanded_by_path = {item.path: item.expanded for item in slot_items}
            for item in slot_items:
                parent_path = item.parent_path
                visible = True
                while parent_path:
                    if not expanded_by_path.get(parent_path, True):
                        visible = False
                        break
                    parent_item = next((candidate for candidate in slot_items if candidate.path == parent_path), None)
                    parent_path = parent_item.parent_path if parent_item else ""
                if not visible:
                    continue

                row = box.row(align=True)
                for _indent in range(min(item.depth, 8)):
                    row.label(text="", icon="BLANK1")
                if item.has_children:
                    icon = "TRIA_DOWN" if item.expanded else "TRIA_RIGHT"
                    row.prop(item, "expanded", text="", icon=icon, emboss=False)
                else:
                    row.label(text="", icon="BLANK1")
                label = item.slot_name + (" *" if item.is_core else "")
                row.label(text=label)
                row.prop(item, "choice", text="")
        else:
            box = layout.box()
            box.label(text="Vehicle Slots")
            box.label(text="Import a .pc to populate the slot tree")

        layout.separator()
        active = context.active_object
        if active and active.get("beamng_layer") == "jbeam":
            visual_type = active.get("beamng_visual_type", "")
            box = layout.box()
            box.label(text="Selected JBeam")
            if visual_type in {"selectable_node", "node_label"}:
                box.label(text=f"Node: {active.get('beamng_node_id', '')}")
            elif visual_type == "selectable_beam":
                box.label(text=f"Beam: {active.get('beamng_beam_name', '')}")
                box.label(text=f"From: {active.get('beamng_beam_id1', '')}")
                box.label(text=f"To: {active.get('beamng_beam_id2', '')}")
            elif visual_type == "selectable_triangle":
                box.label(text=f"Triangle: {active.get('beamng_triangle_name', '')}")
                box.label(text=f"Node 1: {active.get('beamng_triangle_id1', '')}")
                box.label(text=f"Node 2: {active.get('beamng_triangle_id2', '')}")
                box.label(text=f"Node 3: {active.get('beamng_triangle_id3', '')}")
            elif visual_type == "selectable_hydro":
                box.label(text=f"Hydro: {active.get('beamng_hydro_name', '')}")
                box.label(text=f"From: {active.get('beamng_hydro_id1', '')}")
                box.label(text=f"To: {active.get('beamng_hydro_id2', '')}")
                box.label(text=f"Input: {active.get('beamng_hydro_input_source', '')}")
                box.label(text=f"Factor: {active.get('beamng_hydro_factor', '')}")
            elif visual_type == "selectable_rail":
                box.label(text=f"Rail: {active.get('beamng_rail_name', '')}")
                box.label(text=f"Nodes: {str(active.get('beamng_rail_nodes', ''))[:80]}")
                box.label(text=f"Capped: {active.get('beamng_rail_capped', '')}")
                box.label(text=f"Looped: {active.get('beamng_rail_looped', '')}")
            elif visual_type == "selectable_slidenode":
                box.label(text=f"Slidenode: {active.get('beamng_slidenode_id', '')}")
                box.label(text=f"Rail: {active.get('beamng_slidenode_rail', '')}")
                box.label(text=f"Attached: {active.get('beamng_slidenode_attached', '')}")
                box.label(text=f"Fix to Rail: {active.get('beamng_slidenode_fix_to_rail', '')}")
            else:
                box.label(text=f"Type: {visual_type}")
            box.label(text=f"Part: {active.get('beamng_part_name', '')}")
            box.label(text=f"Resolved ID: {active.get('beamng_resolved_part_id', '')}")
            box.operator(BEAMNG_OT_select_jbeam_body_structure.bl_idname, text="Select Same Body Nodes/Beams")

        layout.separator()
        layout.operator(BEAMNG_OT_toggle_relationship_lines.bl_idname, text="Toggle Parent Lines")
        layout.operator(BEAMNG_OT_print_prop_transforms.bl_idname, text="Write Prop Debug File")


class BeamNGPCImporterPreferences(AddonPreferences):
    bl_idname = __name__

    beamng_user_folder: StringProperty(
        name="BeamNG User Folder",
        description="BeamNG user folder containing the current/mods and current/vehicles folders",
        subtype="DIR_PATH",
        default="",
    )
    vanilla_vehicles_folder: StringProperty(
        name="Vanilla Vehicles Folder",
        description="BeamNG install folder, or the game content/vehicles folder",
        subtype="DIR_PATH",
        default="",
    )
    cache_asset_catalogs: BoolProperty(
        name="Cache Asset Catalogs",
        description="Reuse parsed JBeam/DAE catalog data across imports while the add-on remains loaded",
        default=True,
    )

    def draw(self, _context):
        layout = self.layout
        layout.label(text="BeamNG Asset Roots")
        layout.prop(self, "beamng_user_folder")
        layout.prop(self, "vanilla_vehicles_folder")
        layout.prop(self, "cache_asset_catalogs")


def get_addon_preferences(context):
    addon = context.preferences.addons.get(__name__)
    return addon.preferences if addon else None


def write_import_report(lines):
    text = bpy.data.texts.get("BeamNG Import Report") or bpy.data.texts.new("BeamNG Import Report")
    text.clear()
    text.write("\n".join(str(line) for line in lines))
    text.write("\n")
    return text


def import_beamng_pc_path(
    context,
    operator,
    pc_path: Path,
    clear_existing=True,
    include_jbeam_visuals=True,
    selectable_jbeam_debug=False,
    show_jbeam_node_labels=False,
    source_description="",
):
    wm = context.window_manager
    update_import_progress.last_percent = None
    wm.progress_begin(0, 100)
    if context.window:
        context.window.cursor_set("WAIT")

    report_lines = [
        "[BeamNG Importer] Import report",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"PC path: {pc_path}",
    ]
    if source_description:
        report_lines.append(f"Selected source: {source_description}")

    try:
        update_import_progress(context, 1, "checking selected file")
        if not pc_path.exists():
            operator.report({"ERROR"}, f"Missing file: {pc_path}")
            return {"CANCELLED"}

        update_import_progress(context, 5, "parsing .pc config")
        pc_data = load_jsonc(pc_path)

        vehicle_root = pc_path.parent
        prefs = get_addon_preferences(context)
        beamng_user_folder = prefs.beamng_user_folder if prefs else ""
        vanilla_vehicles_folder = prefs.vanilla_vehicles_folder if prefs else ""
        cache_asset_catalogs = prefs.cache_asset_catalogs if prefs else True
        jbeam_sources, dae_sources, virtual_vehicle_root = collect_beamng_asset_sources(
            pc_path,
            beamng_user_folder,
            vanilla_vehicles_folder,
            cache_asset_catalogs,
        )
        report_lines.extend(
            [
                f"Vehicle folder inferred as: {pc_path.parent.name}",
                f"User folder preference: {beamng_user_folder or '(not set)'}",
                f"Vanilla vehicles preference: {vanilla_vehicles_folder or '(not set)'}",
                f"JBeam sources found: {len(jbeam_sources)}",
                f"DAE sources found: {len(dae_sources)}",
            ]
        )
        update_import_progress(context, 10, "scanning JBeam parts")
        if jbeam_sources:
            part_index = parse_parts_index(vehicle_root, jbeam_sources, cache_asset_catalogs)
        else:
            part_index = parse_parts_index(vehicle_root)
        report_lines.append(f"JBeam parts indexed: {len(part_index)}")
        if not part_index:
            write_import_report(report_lines)
            operator.report({"ERROR"}, "No JBeam parts were found next to the selected .pc")
            return {"CANCELLED"}
        main_part_name, main_part_source = infer_main_part_name(pc_data, part_index)
        report_lines.append(
            f"Main part: {main_part_name or '(not found)'}"
            + (f" ({main_part_source})" if main_part_source else "")
        )
        populate_vehicle_slot_editor(context.scene, pc_data, part_index)

        update_import_progress(context, 20, "resolving selected part tree")
        (
            resolved_parts,
            flexbodies,
            visual_nodes,
            visual_beams,
            visual_triangles,
            visual_hydros,
            visual_rails,
            visual_slidenodes,
        ) = resolve_selected_parts(
            pc_data,
            part_index,
            True,
        )
        report_lines.extend(
            [
                f"Resolved parts: {len(resolved_parts)}",
                f"Resolved flexbodies/props: {len(flexbodies)}",
                f"Visual nodes: {len(visual_nodes)}",
                f"Visual beams: {len(visual_beams)}",
                f"Visual triangles: {len(visual_triangles)}",
                f"Visual hydros: {len(visual_hydros)}",
                f"Visual rails/slidenodes: {len(visual_rails) + len(visual_slidenodes)}",
            ]
        )
        if not flexbodies:
            write_import_report(report_lines)
            operator.report({"ERROR"}, "No flexbodies were resolved from the selected config")
            return {"CANCELLED"}

        root_collection_name = f"BeamNG_{pc_path.stem}"
        template_collection_name = f"{root_collection_name}_templates"

        update_import_progress(context, 28, "creating Blender collections")
        if clear_existing:
            for collection_name in (root_collection_name, template_collection_name):
                existing = bpy.data.collections.get(collection_name)
                if existing:
                    bpy.data.collections.remove(existing)

        scene_root = context.scene.collection
        root_collection = link_collection(scene_root, root_collection_name)
        root_collection["beamng_pc_import_root"] = True
        root_collection["beamng_pc_path"] = str(pc_path)
        root_collection["beamng_pc_source_path"] = context.scene.get("beamng_slot_editor_source_pc_path", str(pc_path))
        context.scene["beamng_slot_editor_source_pc_path"] = root_collection["beamng_pc_source_path"]
        template_collection = link_collection(scene_root, template_collection_name)
        template_collection.hide_viewport = True
        part_objects = build_part_hierarchy(resolved_parts, root_collection)
        if include_jbeam_visuals:
            create_jbeam_visuals(
                visual_nodes,
                visual_beams,
                visual_triangles,
                visual_hydros,
                visual_rails,
                visual_slidenodes,
                root_collection,
                resolved_parts,
                selectable_jbeam_debug,
                show_jbeam_node_labels,
            )
        vehicle_model = str(pc_data.get("model", ""))

        update_import_progress(context, 35, "cataloging DAE assets")
        if dae_sources:
            required_mesh_names = {spec.mesh for spec in flexbodies if spec.mesh}
            dae_name_cache, dae_paths_by_dir, mesh_to_dae_paths = build_dae_catalog(
                virtual_vehicle_root,
                dae_sources,
                cache_asset_catalogs,
                required_mesh_names,
            )
            dae_search_root = virtual_vehicle_root
        else:
            dae_name_cache, dae_paths_by_dir, mesh_to_dae_paths = build_dae_catalog(vehicle_root)
            dae_search_root = vehicle_root
        report_lines.extend(
            [
                f"DAE files cataloged: {len(dae_name_cache)}",
                f"Mesh names with DAE candidates: {len(mesh_to_dae_paths)}",
            ]
        )
        imported_dae_cache = {}
        extracted_zip_assets = {}
        warnings = []
        imported_count = 0
        total_specs = max(len(flexbodies), 1)

        for index, spec in enumerate(flexbodies):
            update_import_progress(
                context,
                40 + int((index / total_specs) * 55),
                f"importing {index + 1}/{len(flexbodies)}: {spec.mesh}",
            )
            dae_asset = choose_dae_for_mesh(
                spec.mesh,
                spec.jbeam_path,
                dae_search_root,
                dae_paths_by_dir,
                mesh_to_dae_paths,
            )
            if dae_asset is None:
                warnings.append(f"No DAE found for mesh '{spec.mesh}' from part '{spec.part_name}'")
                continue

            dae_path = materialize_dae_asset(dae_asset, extracted_zip_assets)

            if dae_asset not in imported_dae_cache:
                mesh_objects = import_dae_templates(context, dae_path, template_collection)
                imported_dae_cache[dae_asset] = build_template_lookup(mesh_objects)

            template_lookup = imported_dae_cache[dae_asset]
            template_obj = find_matching_template(spec.mesh, template_lookup)
            if template_obj is None:
                warnings.append(f"No imported object matched mesh '{spec.mesh}' in {dae_path.name}")
                continue

            parent_obj = part_objects.get(spec.resolved_part_id)
            instantiate_flexbody(template_obj, spec, root_collection, parent_obj)
            imported_count += 1

        update_import_progress(context, 98, "finishing import")
        summary = f"Imported {imported_count} meshes/props for {pc_path.name}"
        if warnings:
            summary += f" with {len(warnings)} warnings"
            report_lines.append("")
            report_lines.append("Warnings:")
            report_lines.extend(f"- {warning}" for warning in warnings)
            for warning in warnings[:10]:
                operator.report({"WARNING"}, warning)
        report_lines.extend(
            [
                "",
                f"Imported meshes/props: {imported_count}",
                f"Warnings: {len(warnings)}",
                f"Result: {summary}",
            ]
        )
        write_import_report(report_lines)
        if imported_count == 0:
            operator.report({"WARNING"}, "No meshes were imported. Open the 'BeamNG Import Report' text block for details.")
        operator.report({"INFO"}, summary)
        update_import_progress(context, 100, "complete")
        return {"FINISHED"}
    except Exception as exc:
        report_lines.extend(["", f"Exception: {exc}"])
        write_import_report(report_lines)
        operator.report({"ERROR"}, f"Failed to import BeamNG config: {exc}")
        return {"CANCELLED"}
    finally:
        save_dirty_disk_caches()
        wm.progress_end()
        if context.workspace:
            context.workspace.status_text_set(None)
        if context.window:
            context.window.cursor_set("DEFAULT")


class IMPORT_OT_beamng_pc(Operator, ImportHelper):
    bl_idname = "import_scene.beamng_pc"
    bl_label = "Import BeamNG Config"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".pc"
    filter_glob: StringProperty(default="*.pc", options={"HIDDEN"})
    clear_existing: BoolProperty(
        name="Clear Existing Imported Collection",
        description="Remove a previous BeamNG import collection with the same config name",
        default=True,
    )
    include_jbeam_visuals: BoolProperty(
        name="Create JBeam Visuals",
        description="Create a hidden visualization layer for resolved JBeam nodes, beams, and collision triangles",
        default=True,
    )
    selectable_jbeam_debug: BoolProperty(
        name="Selectable JBeam IDs",
        description="Create individual selectable node and beam debug objects with exact ID metadata",
        default=False,
    )
    show_jbeam_node_labels: BoolProperty(
        name="Show JBeam Node Labels",
        description="Create viewport text labels for selectable JBeam nodes",
        default=False,
    )

    def execute(self, context):
        return import_beamng_pc_path(
            context,
            self,
            Path(self.filepath),
            self.clear_existing,
            self.include_jbeam_visuals,
            self.selectable_jbeam_debug,
            self.show_jbeam_node_labels,
            str(Path(self.filepath)),
        )


PC_VEHICLE_ENUM_ITEMS = []
PC_CONFIG_ENUM_ITEMS_BY_VEHICLE = {}
PC_SOURCE_BY_KEY = {}
PC_SOURCE_KEYS_BY_VEHICLE = {}


def refresh_pc_source_options(context):
    global PC_VEHICLE_ENUM_ITEMS, PC_CONFIG_ENUM_ITEMS_BY_VEHICLE, PC_SOURCE_BY_KEY, PC_SOURCE_KEYS_BY_VEHICLE
    prefs = get_addon_preferences(context)
    beamng_user_folder = prefs.beamng_user_folder if prefs else ""
    vanilla_vehicles_folder = prefs.vanilla_vehicles_folder if prefs else ""
    cache_asset_catalogs = prefs.cache_asset_catalogs if prefs else True
    sources = collect_beamng_pc_sources(
        beamng_user_folder,
        vanilla_vehicles_folder,
        cache_asset_catalogs,
    )

    vehicle_labels = {}
    config_items_by_vehicle = defaultdict(list)
    by_key = {}
    source_keys_by_vehicle = defaultdict(list)
    for index, source in enumerate(sources):
        vehicle_name = pc_vehicle_from_virtual_path(source.virtual_path) or "<Unknown>"
        config_name = Path(source.virtual_path).name
        label_prefix = getattr(source, "label_prefix", "") or ("Zip" if source.asset_type == "zip" else "File")
        key = str(index)
        if source.asset_type == "zip":
            description = f"{source.zip_path} :: {source.zip_entry}"
        else:
            description = source.path
        label = f"{config_name} | {label_prefix}"
        config_items_by_vehicle[vehicle_name].append((key, label, description))
        source_keys_by_vehicle[vehicle_name].append(key)
        vehicle_labels[vehicle_name] = vehicle_name
        by_key[key] = source

    vehicle_items = []
    for vehicle_name in sorted(vehicle_labels, key=str.lower):
        configs = sorted(config_items_by_vehicle[vehicle_name], key=lambda item: item[1].lower())
        config_items_by_vehicle[vehicle_name] = configs
        source_keys_by_vehicle[vehicle_name] = [item[0] for item in configs]
        count = len(configs)
        vehicle_items.append((vehicle_name, vehicle_name, f"{count} discovered .pc configuration{'s' if count != 1 else ''}"))

    PC_VEHICLE_ENUM_ITEMS = vehicle_items
    PC_CONFIG_ENUM_ITEMS_BY_VEHICLE = dict(config_items_by_vehicle)
    PC_SOURCE_KEYS_BY_VEHICLE = dict(source_keys_by_vehicle)
    PC_SOURCE_BY_KEY = by_key
    return vehicle_items


def pc_vehicle_enum_items(self, context):
    if not PC_VEHICLE_ENUM_ITEMS:
        refresh_pc_source_options(context)
    return PC_VEHICLE_ENUM_ITEMS


def pc_config_enum_items(self, context):
    if not PC_VEHICLE_ENUM_ITEMS:
        refresh_pc_source_options(context)
    vehicle_key = getattr(self, "pc_vehicle_key", "")
    if not vehicle_key and PC_VEHICLE_ENUM_ITEMS:
        vehicle_key = PC_VEHICLE_ENUM_ITEMS[0][0]
    return PC_CONFIG_ENUM_ITEMS_BY_VEHICLE.get(vehicle_key, [])


def pc_vehicle_updated(self, context):
    if not PC_VEHICLE_ENUM_ITEMS:
        refresh_pc_source_options(context)
    keys = PC_SOURCE_KEYS_BY_VEHICLE.get(self.pc_vehicle_key, [])
    self.pc_config_key = keys[0] if keys else ""


class IMPORT_OT_beamng_pc_from_assets(Operator):
    bl_idname = "import_scene.beamng_pc_from_assets"
    bl_label = "Import BeamNG Config From Assets"
    bl_options = {"REGISTER", "UNDO"}

    pc_vehicle_key: EnumProperty(
        name="Vehicle",
        description="Vehicle folder discovered in the configured BeamNG user, mod, or vanilla asset folders",
        items=pc_vehicle_enum_items,
        update=pc_vehicle_updated,
    )
    pc_config_key: EnumProperty(
        name="Configuration",
        description="A .pc configuration discovered for the selected vehicle",
        items=pc_config_enum_items,
    )
    clear_existing: BoolProperty(
        name="Clear Existing Imported Collection",
        description="Remove a previous BeamNG import collection with the same config name",
        default=True,
    )
    include_jbeam_visuals: BoolProperty(
        name="Create JBeam Visuals",
        description="Create a hidden visualization layer for resolved JBeam nodes, beams, and collision triangles",
        default=True,
    )
    selectable_jbeam_debug: BoolProperty(
        name="Selectable JBeam IDs",
        description="Create individual selectable node and beam debug objects with exact ID metadata",
        default=False,
    )
    show_jbeam_node_labels: BoolProperty(
        name="Show JBeam Node Labels",
        description="Create viewport text labels for selectable JBeam nodes",
        default=False,
    )

    def invoke(self, context, _event):
        refresh_pc_source_options(context)
        if not PC_VEHICLE_ENUM_ITEMS:
            self.report(
                {"ERROR"},
                "No BeamNG .pc configs found. Set the BeamNG user folder and vanilla vehicles folder in add-on preferences.",
            )
            return {"CANCELLED"}
        self.pc_vehicle_key = PC_VEHICLE_ENUM_ITEMS[0][0]
        keys = PC_SOURCE_KEYS_BY_VEHICLE.get(self.pc_vehicle_key, [])
        self.pc_config_key = keys[0] if keys else ""
        return context.window_manager.invoke_props_dialog(self, width=650)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "pc_vehicle_key")
        layout.prop(self, "pc_config_key")
        layout.separator()
        layout.prop(self, "clear_existing")
        layout.prop(self, "include_jbeam_visuals")
        layout.prop(self, "selectable_jbeam_debug")
        layout.prop(self, "show_jbeam_node_labels")

    def execute(self, context):
        source = PC_SOURCE_BY_KEY.get(self.pc_config_key)
        if source is None:
            refresh_pc_source_options(context)
            source = PC_SOURCE_BY_KEY.get(self.pc_config_key)
        if source is None:
            self.report({"ERROR"}, "Selected BeamNG .pc config is no longer available")
            return {"CANCELLED"}

        pc_path = materialize_pc_asset(source)
        if source.asset_type == "zip":
            source_description = f"{source.zip_path} :: {source.zip_entry}"
        else:
            source_description = source.path
        return import_beamng_pc_path(
            context,
            self,
            pc_path,
            self.clear_existing,
            self.include_jbeam_visuals,
            self.selectable_jbeam_debug,
            self.show_jbeam_node_labels,
            source_description,
        )


def menu_func_import(self, _context):
    self.layout.operator(IMPORT_OT_beamng_pc.bl_idname, text="BeamNG Config (.pc File)")
    self.layout.operator(IMPORT_OT_beamng_pc_from_assets.bl_idname, text="BeamNG Config From BeamNG Assets")


def menu_func_jbeam_context(self, context):
    if selected_jbeam_part_id(context) is None:
        return

    layout = self.layout
    layout.separator()
    box = layout.box()
    box.label(text="BeamNG JBeam Relations")
    box.operator(BEAMNG_OT_select_jbeam_body_structure.bl_idname, text="Select Same Body Nodes/Beams")
    box.operator(BEAMNG_OT_show_all_jbeams.bl_idname, text="Show All JBeams")
    box.operator(BEAMNG_OT_hide_selected_jbeam_items.bl_idname, text="Hide Selected JBeam Items")
    row = box.row(align=True)
    op = row.operator(BEAMNG_OT_set_jbeam_visual_visibility.bl_idname, text="Show Triangles")
    op.visual_group = "triangles"
    op.action = "show"
    op = row.operator(BEAMNG_OT_set_jbeam_visual_visibility.bl_idname, text="Hide Triangles")
    op.visual_group = "triangles"
    op.action = "hide"
    row = box.row(align=True)
    op = row.operator(BEAMNG_OT_set_jbeam_visual_visibility.bl_idname, text="Show Hydros")
    op.visual_group = "hydros"
    op.action = "show"
    op = row.operator(BEAMNG_OT_set_jbeam_visual_visibility.bl_idname, text="Hide Hydros")
    op.visual_group = "hydros"
    op.action = "hide"
    row = box.row(align=True)
    op = row.operator(BEAMNG_OT_set_jbeam_visual_visibility.bl_idname, text="Show Sliders")
    op.visual_group = "sliders"
    op.action = "show"
    op = row.operator(BEAMNG_OT_set_jbeam_visual_visibility.bl_idname, text="Hide Sliders")
    op.visual_group = "sliders"
    op.action = "hide"
    for relation, label in (
        ("siblings", "Siblings"),
        ("children", "Children"),
        ("parents", "Parents"),
    ):
        row = box.row(align=True)
        op = row.operator(BEAMNG_OT_jbeam_relationship.bl_idname, text=f"Select {label}")
        op.relation = relation
        op.action = "select"
        op = row.operator(BEAMNG_OT_jbeam_relationship.bl_idname, text=f"Hide {label}")
        op.relation = relation
        op.action = "hide"
        op = row.operator(BEAMNG_OT_jbeam_relationship.bl_idname, text=f"Show {label}")
        op.relation = relation
        op.action = "show"


classes = (
    BeamNGSlotEditorItem,
    BeamNGPCImporterPreferences,
    BEAMNG_OT_set_visibility,
    BEAMNG_OT_jbeam_relationship,
    BEAMNG_OT_select_jbeam_body_structure,
    BEAMNG_OT_show_all_jbeams,
    BEAMNG_OT_hide_selected_jbeam_items,
    BEAMNG_OT_set_jbeam_visual_visibility,
    BEAMNG_OT_print_prop_transforms,
    BEAMNG_OT_toggle_relationship_lines,
    BEAMNG_OT_apply_slot_configuration,
    BEAMNG_OT_revert_slot_change,
    VIEW3D_PT_beamng_pc_importer,
    IMPORT_OT_beamng_pc,
    IMPORT_OT_beamng_pc_from_assets,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.beamng_slot_editor_items = CollectionProperty(type=BeamNGSlotEditorItem)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    bpy.types.VIEW3D_MT_object_context_menu.append(menu_func_jbeam_context)


def unregister():
    bpy.types.VIEW3D_MT_object_context_menu.remove(menu_func_jbeam_context)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    if hasattr(bpy.types.Scene, "beamng_slot_editor_items"):
        del bpy.types.Scene.beamng_slot_editor_items
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
