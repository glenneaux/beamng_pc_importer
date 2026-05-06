bl_info = {
    "name": "BeamNG .pc Importer",
    "author": "Glenn Campigli",
    "version": (1, 0, 1),
    "blender": (3, 6, 0),
    "location": "File > Import > BeamNG Config (.pc)",
    "description": "Import a BeamNG .pc vehicle config with only the selected meshes visible",
    "category": "Import-Export",
}

# Build numbers increment for each build of the current bl_info version.
# Reset ADDON_BUILD to 1 whenever bl_info["version"] changes.
ADDON_BUILD = 20


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


def mark_slot_editor_saved(scene):
    scene["beamng_slot_editor_baseline_snapshot"] = slot_editor_config_snapshot_json(scene.beamng_slot_editor_items)
    scene["beamng_slot_editor_last_snapshot"] = ""
    scene["beamng_slot_editor_dirty"] = False


def set_slot_editor_item(item, slot, part_index, selected_part, parent_part, depth, path, parent_path, expanded=True):
    option_items = slot_option_items_for_storage(slot, selected_part, part_index)
    valid_identifiers = {option["identifier"] for option in option_items}
    choice = selected_part if selected_part in valid_identifiers else "__EMPTY__"
    if slot.get("core_slot") and choice == "__EMPTY__":
        choice = selected_part if selected_part else slot.get("default", "")
        if choice not in valid_identifiers:
            unresolved = choice or slot.get("name", "")
            option_items.insert(
                0,
                {
                    "identifier": "__ERROR__",
                    "name": "<Error>",
                    "description": f"Required slot could not resolve '{unresolved}'",
                },
            )
            choice = "__ERROR__"

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
    self.selected_part = "" if self.choice in {"__EMPTY__", "__ERROR__"} else self.choice
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
    pc_data, source_pc_path = build_slot_editor_pc_data(scene)
    vehicle_name = scene.get("beamng_slot_editor_model", "") or source_pc_path.parent.name
    edited_path = persistent_cache_dir() / "pc_editor" / "vehicles" / str(vehicle_name) / source_pc_path.name
    edited_path.parent.mkdir(parents=True, exist_ok=True)
    edited_path.write_text(json.dumps(pc_data, indent=2), encoding="utf-8")
    return edited_path, source_pc_path


def build_slot_editor_pc_data(scene):
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
        if choice == "__ERROR__":
            raise ValueError(f"Required slot '{item.slot_name}' has no valid resolved part")
        if choice == "__NEW__":
            continue
        if choice == "__EMPTY__":
            parts[item.slot_name] = ""
        else:
            parts[item.slot_name] = choice

    return pc_data, source_pc_path


def skip_jsonc_ws_comments(text, index):
    length = len(text)
    while index < length:
        if text[index].isspace():
            index += 1
            continue
        if text.startswith("//", index):
            newline = text.find("\n", index + 2)
            index = length if newline == -1 else newline + 1
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = length if end == -1 else end + 2
            continue
        break
    return index


def scan_jsonc_string(text, index):
    quote = text[index]
    index += 1
    length = len(text)
    escape = False
    while index < length:
        char = text[index]
        if escape:
            escape = False
        elif char == "\\":
            escape = True
        elif char == quote:
            return index + 1
        index += 1
    return length


def scan_jsonc_identifier(text, index):
    start = index
    length = len(text)
    while index < length and (text[index].isalnum() or text[index] in "_-$"):
        index += 1
    return text[start:index], index


def find_matching_jsonc_brace(text, open_index):
    pairs = {"{": "}", "[": "]"}
    open_char = text[open_index]
    close_char = pairs[open_char]
    depth = 0
    index = open_index
    length = len(text)
    while index < length:
        if text.startswith("//", index):
            newline = text.find("\n", index + 2)
            index = length if newline == -1 else newline + 1
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = length if end == -1 else end + 2
            continue
        char = text[index]
        if char in {'"', "'"}:
            index = scan_jsonc_string(text, index)
            continue
        if char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1


def find_jsonc_object_for_key(text, key):
    index = 0
    length = len(text)
    while index < length:
        if text.startswith("//", index):
            newline = text.find("\n", index + 2)
            index = length if newline == -1 else newline + 1
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = length if end == -1 else end + 2
            continue

        char = text[index]
        if char in {'"', "'"}:
            end = scan_jsonc_string(text, index)
            token = text[index + 1 : end - 1]
            next_index = skip_jsonc_ws_comments(text, end)
        elif char.isalpha() or char in "_$":
            token, end = scan_jsonc_identifier(text, index)
            next_index = skip_jsonc_ws_comments(text, end)
        else:
            index += 1
            continue

        if token == key and next_index < length and text[next_index] == ":":
            object_index = skip_jsonc_ws_comments(text, next_index + 1)
            if object_index < length and text[object_index] == "{":
                object_end = find_matching_jsonc_brace(text, object_index)
                if object_end != -1:
                    return object_index, object_end + 1
        index = end
    return None


def indent_multiline(text, indent):
    lines = text.splitlines()
    if not lines:
        return text
    return "\n".join(lines[:1] + [indent + line if line else line for line in lines[1:]])


def text_indent_before_index(text, index):
    line_start = text.rfind("\n", 0, index) + 1
    return text[line_start:index].replace(text[line_start:index].lstrip(), "")


def updated_pc_text_with_parts(original_text, pc_data):
    parts = pc_data.get("parts")
    if not isinstance(parts, dict):
        raise ValueError("Edited .pc data does not contain a parts object")

    bounds = find_jsonc_object_for_key(original_text, "parts")
    if bounds is None:
        raise ValueError("Could not locate a parts object in the source .pc file")

    start, end = bounds
    base_indent = text_indent_before_index(original_text, start)
    serialized = json.dumps(parts, indent=2)
    replacement = indent_multiline(serialized, base_indent)
    return original_text[:start] + replacement + original_text[end:]


def write_pc_parts_preserving_file(source_pc_path, pc_data):
    original_text = Path(source_pc_path).read_text(encoding="utf-8-sig")
    updated_text = updated_pc_text_with_parts(original_text, pc_data)
    Path(source_pc_path).write_text(updated_text, encoding="utf-8")


def write_pc_as_preserving_file(source_pc_path, destination_path, pc_data):
    original_text = Path(source_pc_path).read_text(encoding="utf-8-sig")
    updated_text = updated_pc_text_with_parts(original_text, pc_data)
    Path(destination_path).write_text(updated_text, encoding="utf-8")


def user_current_folder_from_preferences(context):
    prefs = get_addon_preferences(context)
    user_folder = prefs.beamng_user_folder if prefs else ""
    if not user_folder:
        return None
    supplied = Path(user_folder)
    return supplied if supplied.name.lower() == "current" else supplied / "current"


def pc_save_virtual_path_for_scene(scene, source_pc_path=None):
    roots = find_beamng_import_collections(scene)
    root = roots[0] if roots else None
    virtual_path = root.get("beamng_pc_virtual_path", "") if root else ""
    if virtual_path:
        return normalize_virtual_path(virtual_path)

    source_pc_path = Path(source_pc_path or "")
    vehicle_name = scene.get("beamng_slot_editor_model", "") or source_pc_path.parent.name
    return normalize_virtual_path(Path("vehicles") / str(vehicle_name) / source_pc_path.name)


def user_pc_path_for_virtual_path(context, virtual_path, filename=None):
    current_folder = user_current_folder_from_preferences(context)
    if current_folder is None:
        raise ValueError("Set the BeamNG user folder in add-on preferences before saving .pc files")

    virtual = Path(normalize_virtual_path(virtual_path))
    if filename:
        safe_name = Path(filename).name
        if not safe_name.lower().endswith(".pc"):
            safe_name += ".pc"
        virtual = virtual.parent / safe_name
    if not normalize_virtual_path(virtual).lower().startswith("vehicles/"):
        virtual = Path("vehicles") / virtual
    return current_folder / virtual


def current_user_pc_source_path(context):
    roots = find_beamng_import_collections(context.scene)
    if not roots:
        return None
    root = roots[0]
    source_pc_path = Path(root.get("beamng_pc_source_path", root.get("beamng_pc_path", "")))
    current_folder = user_current_folder_from_preferences(context)
    if current_folder is None or not source_pc_path.exists():
        return None
    try:
        source_pc_path.relative_to(current_folder / "vehicles")
    except ValueError:
        return None
    return source_pc_path


def slot_editor_can_save_as(context):
    return bool(getattr(context.scene, "beamng_slot_editor_items", None) and context.scene.get("beamng_slot_editor_dirty", False))


def slot_editor_can_save(context):
    return bool(slot_editor_can_save_as(context) and current_user_pc_source_path(context))


def update_slot_editor_saved_source(scene, saved_path, virtual_path):
    for root in find_beamng_import_collections(scene):
        root["beamng_pc_source_path"] = str(saved_path)
        root["beamng_pc_virtual_path"] = normalize_virtual_path(virtual_path)
        root["beamng_pc_source_asset_type"] = "file"
        root["beamng_pc_source_label_prefix"] = "User"
        root["beamng_pc_source_zip_path"] = ""
        root["beamng_pc_source_zip_entry"] = ""
        root.name = f"BeamNG_{Path(saved_path).stem}"
        break
    scene["beamng_slot_editor_source_pc_path"] = str(saved_path)
    scene["beamng_slot_editor_source_virtual_path"] = normalize_virtual_path(virtual_path)
    scene["beamng_slot_editor_source_asset_type"] = "file"
    scene["beamng_slot_editor_source_label_prefix"] = "User"
    scene["beamng_slot_editor_source_zip_path"] = ""
    scene["beamng_slot_editor_source_zip_entry"] = ""


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


AUTHORING_GHOST_MATERIAL_NAME = "BeamNG Authoring Body Ghost"
AUTHORING_ORIGINAL_MESH_PROP = "beamng_authoring_original_mesh_data"
AUTHORING_ORIGINAL_HIDE_SELECT_PROP = "beamng_authoring_original_hide_select"


def get_authoring_ghost_material():
    material = bpy.data.materials.get(AUTHORING_GHOST_MATERIAL_NAME)
    if material is None:
        material = bpy.data.materials.new(AUTHORING_GHOST_MATERIAL_NAME)
    material.diffuse_color = (0.55, 0.75, 1.0, 0.22)
    material.use_nodes = True
    material.blend_method = "BLEND"
    material.show_transparent_back = True
    bsdf = material.node_tree.nodes.get("Principled BSDF") if material.node_tree else None
    if bsdf:
        if "Base Color" in bsdf.inputs:
            bsdf.inputs["Base Color"].default_value = material.diffuse_color
        if "Alpha" in bsdf.inputs:
            bsdf.inputs["Alpha"].default_value = material.diffuse_color[3]
    return material


def restore_authoring_reference_object(obj):
    if AUTHORING_ORIGINAL_MESH_PROP in obj:
        original_mesh_name = obj.get(AUTHORING_ORIGINAL_MESH_PROP, "")
        original_mesh = bpy.data.meshes.get(original_mesh_name)
        ghost_mesh = obj.data if obj.type == "MESH" else None
        if original_mesh is not None:
            obj.data = original_mesh
        if ghost_mesh is not None and ghost_mesh != original_mesh and ghost_mesh.users == 0:
            bpy.data.meshes.remove(ghost_mesh)
        del obj[AUTHORING_ORIGINAL_MESH_PROP]

    if AUTHORING_ORIGINAL_HIDE_SELECT_PROP in obj:
        obj.hide_select = bool(obj.get(AUTHORING_ORIGINAL_HIDE_SELECT_PROP))
        del obj[AUTHORING_ORIGINAL_HIDE_SELECT_PROP]


def restore_authoring_reference_mode(root_collection):
    for obj in walk_collection_objects(root_collection):
        restore_authoring_reference_object(obj)


def make_object_authoring_reference(obj, ghost_material):
    if AUTHORING_ORIGINAL_HIDE_SELECT_PROP not in obj:
        obj[AUTHORING_ORIGINAL_HIDE_SELECT_PROP] = bool(obj.hide_select)
    obj.hide_select = True
    obj.hide_set(False)
    obj.hide_render = False
    if hasattr(obj, "show_transparent"):
        obj.show_transparent = True
    obj.color = ghost_material.diffuse_color

    if obj.type != "MESH" or obj.data is None:
        return

    if AUTHORING_ORIGINAL_MESH_PROP not in obj:
        obj[AUTHORING_ORIGINAL_MESH_PROP] = obj.data.name
        obj.data = obj.data.copy()
        obj.data.name = f"{obj.name}_authoring_ghost_mesh"

    obj.data.materials.clear()
    obj.data.materials.append(ghost_material)


def set_jbeam_authoring_selectability(root_collection):
    selectable_types = {
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
    }
    for collection in walk_child_collections(root_collection):
        if collection.get("beamng_layer") == "jbeam":
            collection.hide_viewport = False
            collection.hide_render = False

    for obj in walk_collection_objects(root_collection):
        if obj.get("beamng_layer") != "jbeam":
            continue
        obj.hide_set(False)
        obj.hide_render = obj.get("beamng_visual_type") == "node_label"
        obj.hide_select = obj.get("beamng_visual_type") not in selectable_types


def set_authoring_reference_mode(root_collection):
    ghost_material = get_authoring_ghost_material()
    for obj in walk_collection_objects(root_collection):
        layer = obj.get("beamng_layer")
        if layer == "hierarchy":
            obj.hide_set(False)
            obj.hide_render = True
            obj.hide_select = True
        elif layer in {"mesh", "prop"}:
            make_object_authoring_reference(obj, ghost_material)
    set_jbeam_authoring_selectability(root_collection)


def set_import_layer_visibility(root_collection, show_meshes=True, show_props=True, show_jbeam=True):
    restore_authoring_reference_mode(root_collection)
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

        if self.mode == "AUTHORING":
            for root in roots:
                set_authoring_reference_mode(root)
            self.report({"INFO"}, "JBeam authoring mode enabled")
            return {"FINISHED"}

        show_meshes = self.mode in {"MESHES", "MESHES_PROPS", "ALL"}
        show_props = self.mode in {"PROPS", "MESHES_PROPS", "ALL"}
        show_jbeam = self.mode in {"JBEAM", "ALL"}
        for root in roots:
            set_import_layer_visibility(root, show_meshes, show_props, show_jbeam)

        return {"FINISHED"}


def view3d_window_region(area):
    return next((region for region in area.regions if region.type == "WINDOW"), None)


def view3d_space(area):
    return next((space for space in area.spaces if space.type == "VIEW_3D"), None)


def beamng_objects_by_layer(context, layer_name):
    return [
        obj
        for root in find_beamng_import_collections(context.scene)
        for obj in walk_collection_objects(root)
        if obj.get("beamng_layer") == layer_name
    ]


def beamng_objects_for_view_mode(context, mode):
    layer_names = {
        "FLEX": {"mesh"},
        "PROPS": {"prop"},
        "JBEAM": {"jbeam"},
        "FLEX_PROPS": {"mesh", "prop"},
        "ALL": {"mesh", "prop", "jbeam"},
    }.get(mode, set())
    if not layer_names:
        return []

    return [
        obj
        for root in find_beamng_import_collections(context.scene)
        for obj in walk_collection_objects(root)
        if obj.get("beamng_layer") in layer_names
    ]


def local_view_is_active(space):
    return bool(getattr(space, "local_view", None))


def restore_selection(context, selected_objects, active_object):
    for obj in context.scene.objects:
        obj.select_set(False)
    for obj in selected_objects:
        if bpy.data.objects.get(obj.name):
            obj.select_set(True)
    if active_object and bpy.data.objects.get(active_object.name):
        context.view_layer.objects.active = active_object


def reveal_collections_for_local_view_objects(objects):
    collections = set()
    for obj in objects:
        collections.update(obj.users_collection)

    pending = list(collections)
    seen = set()
    while pending:
        collection = pending.pop()
        if collection in seen:
            continue
        seen.add(collection)
        collection.hide_viewport = False
        collection.hide_render = False
        parents = [candidate for candidate in bpy.data.collections if collection.name in candidate.children.keys()]
        pending.extend(parents)


def set_view3d_local_view(context, area, objects):
    region = view3d_window_region(area)
    space = view3d_space(area)
    if region is None or space is None:
        return False

    objects = [obj for obj in objects if obj and bpy.data.objects.get(obj.name)]
    if not objects:
        return False

    reveal_collections_for_local_view_objects(objects)
    for obj in objects:
        obj.hide_set(False)
        obj.hide_select = False

    with context.temp_override(
        window=context.window,
        screen=context.screen,
        area=area,
        region=region,
        space_data=space,
    ):
        if local_view_is_active(space):
            bpy.ops.view3d.localview(frame_selected=False)
        bpy.ops.object.select_all(action="DESELECT")
        for obj in objects:
            obj.select_set(True)
        context.view_layer.objects.active = objects[0]
        bpy.ops.view3d.localview(frame_selected=True)
    return True


def exit_view3d_local_view(context, area):
    region = view3d_window_region(area)
    space = view3d_space(area)
    if region is None or space is None or not local_view_is_active(space):
        return False

    with context.temp_override(
        window=context.window,
        screen=context.screen,
        area=area,
        region=region,
        space_data=space,
    ):
        bpy.ops.view3d.localview(frame_selected=False)
    return True


def copy_view3d_region_state(source_region_3d, target_region_3d):
    target_region_3d.view_location = source_region_3d.view_location.copy()
    target_region_3d.view_rotation = source_region_3d.view_rotation.copy()
    target_region_3d.view_distance = source_region_3d.view_distance
    target_region_3d.view_perspective = source_region_3d.view_perspective
    target_region_3d.view_camera_zoom = source_region_3d.view_camera_zoom
    target_region_3d.view_camera_offset = tuple(source_region_3d.view_camera_offset)


def view3d_region_entries(context):
    for area in context.screen.areas:
        if area.type != "VIEW_3D":
            continue
        for space in area.spaces:
            if space.type == "VIEW_3D" and getattr(space, "region_3d", None) is not None:
                yield area, space.region_3d


def view3d_region_state(region_3d):
    return (
        tuple(region_3d.view_location),
        tuple(region_3d.view_rotation),
        region_3d.view_distance,
        region_3d.view_perspective,
        region_3d.view_camera_zoom,
        tuple(region_3d.view_camera_offset),
    )


BEAMNG_VIEW_SYNC_ENABLED = False
BEAMNG_VIEW_SYNC_SOURCE_STATE = None


def sync_view3d_regions_from_source_area(context, source_area):
    if source_area is None or not any(area == source_area for area in context.screen.areas):
        return 0, False, None

    source_space = view3d_space(source_area)
    source_region = getattr(source_space, "region_3d", None) if source_space else None
    if source_region is None:
        return 0, False, None

    synced = 0
    for area, region_3d in view3d_region_entries(context):
        if area == source_area:
            continue
        copy_view3d_region_state(source_region, region_3d)
        area.tag_redraw()
        synced += 1

    return synced, True, view3d_region_state(source_region)


def split_current_view3d_area(context):
    if not context.area or context.area.type != "VIEW_3D":
        return []

    screen = context.screen
    before = set(screen.areas)
    region = view3d_window_region(context.area)
    space = view3d_space(context.area)
    if region is None or space is None:
        return []

    with context.temp_override(
        window=context.window,
        screen=screen,
        area=context.area,
        region=region,
        space_data=space,
    ):
        result = bpy.ops.screen.area_split(direction="VERTICAL", factor=0.5)
    if "FINISHED" not in result:
        return []

    created = [area for area in screen.areas if area not in before and area.type == "VIEW_3D"]
    existing = [area for area in before if area.type == "VIEW_3D"]
    return existing + created


def two_view3d_areas_for_split(context):
    view_areas = [area for area in context.screen.areas if area.type == "VIEW_3D"]
    if len(view_areas) < 2:
        view_areas = split_current_view3d_area(context)
    view_areas = [area for area in context.screen.areas if area.type == "VIEW_3D"] if len(view_areas) < 2 else view_areas
    if len(view_areas) < 2:
        return None, None

    view_areas = sorted(view_areas, key=lambda area: (area.x, area.y))
    if context.area in view_areas:
        current_index = view_areas.index(context.area)
        if current_index == 0:
            return view_areas[0], view_areas[1]
        return view_areas[current_index - 1], view_areas[current_index]
    return view_areas[0], view_areas[1]


class BEAMNG_OT_setup_split_prop_flexbody_views(Operator):
    bl_idname = "beamng_pc_importer.setup_split_prop_flexbody_views"
    bl_label = "Split Props/Flexbody Views"
    bl_description = "Create two 3D Viewports: one isolated to flexbody meshes and one isolated to props"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(context.area and context.area.type == "VIEW_3D")

    def execute(self, context):
        flexbody_objects = beamng_objects_by_layer(context, "mesh")
        prop_objects = beamng_objects_by_layer(context, "prop")
        if not flexbody_objects:
            self.report({"WARNING"}, "No imported BeamNG flexbody mesh objects found")
            return {"CANCELLED"}
        if not prop_objects:
            self.report({"WARNING"}, "No imported BeamNG prop objects found")
            return {"CANCELLED"}

        selected_objects = list(context.selected_objects)
        active_object = context.view_layer.objects.active
        flexbody_area, prop_area = two_view3d_areas_for_split(context)
        if flexbody_area is None or prop_area is None:
            self.report({"ERROR"}, "Could not create or find two 3D Viewports")
            return {"CANCELLED"}

        try:
            flexbody_ok = set_view3d_local_view(context, flexbody_area, flexbody_objects)
            prop_ok = set_view3d_local_view(context, prop_area, prop_objects)
        finally:
            restore_selection(context, selected_objects, active_object)

        if not flexbody_ok or not prop_ok:
            self.report({"ERROR"}, "Could not isolate one or both 3D Viewports")
            return {"CANCELLED"}

        self.report({"INFO"}, "Split view set up: flexbodies in one Viewport, props in the other")
        return {"FINISHED"}


class BEAMNG_OT_set_active_view_filter(Operator):
    bl_idname = "beamng_pc_importer.set_active_view_filter"
    bl_label = "Set This View Filter"
    bl_description = "Switch only the active 3D Viewport between BeamNG flexbodies, props, JBeam, or combinations"
    bl_options = {"REGISTER", "UNDO"}

    mode: StringProperty(default="ALL")

    @classmethod
    def poll(cls, context):
        return bool(context.area and context.area.type == "VIEW_3D")

    def execute(self, context):
        objects = beamng_objects_for_view_mode(context, self.mode)
        if not objects:
            self.report({"WARNING"}, "No BeamNG objects were found for this view filter")
            return {"CANCELLED"}

        selected_objects = list(context.selected_objects)
        active_object = context.view_layer.objects.active
        try:
            if not set_view3d_local_view(context, context.area, objects):
                self.report({"ERROR"}, "Could not set Local View for this 3D Viewport")
                return {"CANCELLED"}
        finally:
            restore_selection(context, selected_objects, active_object)

        labels = {
            "FLEX": "Flexbodies",
            "PROPS": "Props",
            "JBEAM": "JBeam",
            "FLEX_PROPS": "Flexbodies + Props",
            "ALL": "All BeamNG Layers",
        }
        self.report({"INFO"}, f"This View: {labels.get(self.mode, self.mode)}")
        return {"FINISHED"}


class BEAMNG_OT_exit_split_local_views(Operator):
    bl_idname = "beamng_pc_importer.exit_split_local_views"
    bl_label = "Exit Split Local Views"
    bl_description = "Exit Local View in every 3D Viewport on the current screen"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        exited = 0
        for area in context.screen.areas:
            if area.type == "VIEW_3D" and exit_view3d_local_view(context, area):
                exited += 1

        if exited == 0:
            self.report({"INFO"}, "No 3D Viewport Local Views were active")
        else:
            self.report({"INFO"}, f"Exited Local View in {exited} 3D Viewport(s)")
        return {"FINISHED"}


class BEAMNG_OT_toggle_view_sync(Operator):
    bl_idname = "beamng_pc_importer.toggle_view_sync"
    bl_label = "Sync 3D Views"
    bl_description = "Toggle synchronized 3D View navigation across all 3D Views on the current screen"
    bl_options = {"REGISTER"}

    _timer = None
    _source_area = None

    @classmethod
    def poll(cls, context):
        return bool(context.area and context.area.type == "VIEW_3D")

    def execute(self, context):
        global BEAMNG_VIEW_SYNC_ENABLED, BEAMNG_VIEW_SYNC_SOURCE_STATE

        wm = context.window_manager
        if BEAMNG_VIEW_SYNC_ENABLED:
            BEAMNG_VIEW_SYNC_ENABLED = False
            BEAMNG_VIEW_SYNC_SOURCE_STATE = None
            wm["beamng_view_sync_active"] = False
            self.report({"INFO"}, "BeamNG viewport sync stopped")
            return {"FINISHED"}

        view_count = sum(1 for area in context.screen.areas if area.type == "VIEW_3D")
        if view_count < 2:
            self.report({"WARNING"}, "Open or split a second 3D Viewport before enabling sync")
            return {"CANCELLED"}

        BEAMNG_VIEW_SYNC_ENABLED = True
        self._source_area = context.area
        source_space = view3d_space(self._source_area)
        source_region = getattr(source_space, "region_3d", None) if source_space else None
        BEAMNG_VIEW_SYNC_SOURCE_STATE = view3d_region_state(source_region) if source_region else None
        wm["beamng_view_sync_active"] = True
        self._timer = wm.event_timer_add(0.05, window=context.window)
        wm.modal_handler_add(self)
        self.report({"INFO"}, "BeamNG viewport sync started from this 3D View")
        return {"RUNNING_MODAL"}

    def invoke(self, context, _event):
        return self.execute(context)

    def modal(self, context, event):
        global BEAMNG_VIEW_SYNC_ENABLED, BEAMNG_VIEW_SYNC_SOURCE_STATE

        wm = context.window_manager
        if not BEAMNG_VIEW_SYNC_ENABLED:
            wm["beamng_view_sync_active"] = False
            self.cancel(context)
            return {"CANCELLED"}

        if event.type == "TIMER":
            source_space = view3d_space(self._source_area) if self._source_area else None
            source_region = getattr(source_space, "region_3d", None) if source_space else None
            if source_region is None:
                source_ok = False
                source_state = None
            else:
                source_ok = True
                source_state = view3d_region_state(source_region)

            if not source_ok:
                BEAMNG_VIEW_SYNC_ENABLED = False
                BEAMNG_VIEW_SYNC_SOURCE_STATE = None
                wm["beamng_view_sync_active"] = False
                self.cancel(context)
                return {"CANCELLED"}
            if source_state != BEAMNG_VIEW_SYNC_SOURCE_STATE:
                _synced, source_ok, source_state = sync_view3d_regions_from_source_area(context, self._source_area)
                if not source_ok:
                    BEAMNG_VIEW_SYNC_ENABLED = False
                    BEAMNG_VIEW_SYNC_SOURCE_STATE = None
                    wm["beamng_view_sync_active"] = False
                    self.cancel(context)
                    return {"CANCELLED"}
                BEAMNG_VIEW_SYNC_SOURCE_STATE = source_state
        return {"PASS_THROUGH"}

    def cancel(self, context):
        global BEAMNG_VIEW_SYNC_SOURCE_STATE

        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        self._source_area = None
        BEAMNG_VIEW_SYNC_SOURCE_STATE = None


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
            result = import_beamng_pc_path(
                context,
                self,
                edited_pc_path,
                True,
                True,
                False,
                False,
                f"Edited slot configuration from {source_pc_path}",
            )
            if "FINISHED" in result:
                context.scene["beamng_slot_editor_dirty"] = True
            return result
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


class BEAMNG_OT_save_slot_configuration(Operator):
    bl_idname = "beamng_pc_importer.save_slot_configuration"
    bl_label = "Save PC"
    bl_description = "Save changes back to the loaded user .pc file by replacing only its parts block"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return slot_editor_can_save(context)

    def invoke(self, context, _event):
        source_pc_path = current_user_pc_source_path(context)
        if source_pc_path is None:
            self.report({"WARNING"}, "Save is only available for .pc files loaded from user data")
            return {"CANCELLED"}
        return context.window_manager.invoke_confirm(self, _event)

    def execute(self, context):
        source_pc_path = current_user_pc_source_path(context)
        if source_pc_path is None:
            self.report({"WARNING"}, "Save is only available for .pc files loaded from user data")
            return {"CANCELLED"}

        try:
            pc_data, _source = build_slot_editor_pc_data(context.scene)
            write_pc_parts_preserving_file(source_pc_path, pc_data)
            mark_slot_editor_saved(context.scene)
            self.report({"INFO"}, f"Saved .pc parts to {source_pc_path}")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, f"Failed to save .pc: {exc}")
            return {"CANCELLED"}


class BEAMNG_OT_save_as_slot_configuration(Operator):
    bl_idname = "beamng_pc_importer.save_as_slot_configuration"
    bl_label = "Save PC As..."
    bl_description = "Save the edited configuration as a new user .pc file"
    bl_options = {"REGISTER", "UNDO"}

    config_name: StringProperty(
        name="Configuration Name",
        description="New .pc filename to create in the BeamNG user current/vehicles folder",
        default="",
    )

    @classmethod
    def poll(cls, context):
        return slot_editor_can_save_as(context)

    def invoke(self, context, _event):
        roots = find_beamng_import_collections(context.scene)
        root = roots[0] if roots else None
        source_path = Path(root.get("beamng_pc_source_path", root.get("beamng_pc_path", ""))) if root else Path("config.pc")
        self.config_name = source_path.stem
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, _context):
        self.layout.prop(self, "config_name")

    def execute(self, context):
        if not self.config_name.strip():
            self.report({"ERROR"}, "Enter a configuration name")
            return {"CANCELLED"}

        try:
            pc_data, source_pc_path = build_slot_editor_pc_data(context.scene)
            source_virtual_path = pc_save_virtual_path_for_scene(context.scene, source_pc_path)
            destination_path = user_pc_path_for_virtual_path(context, source_virtual_path, self.config_name.strip())
            if destination_path.exists():
                self.report({"ERROR"}, f"Refusing to overwrite existing .pc: {destination_path}")
                return {"CANCELLED"}

            destination_path.parent.mkdir(parents=True, exist_ok=True)
            write_pc_as_preserving_file(source_pc_path, destination_path, pc_data)
            relative_virtual = normalize_virtual_path(destination_path.relative_to(user_current_folder_from_preferences(context)))
            update_slot_editor_saved_source(context.scene, destination_path, relative_virtual)
            mark_slot_editor_saved(context.scene)
            self.report({"INFO"}, f"Saved new .pc: {destination_path}")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, f"Failed to save .pc as: {exc}")
            return {"CANCELLED"}


def draw_vehicle_slot_editor(layout, context):
    slot_items = getattr(context.scene, "beamng_slot_editor_items", [])
    box = layout.box()
    box.label(text="Configuration Editor")
    if not slot_items:
        box.label(text="Import a .pc to populate the slot tree")
        return

    main_part = context.scene.get("beamng_slot_editor_main_part", "")
    if main_part:
        box.label(text=f"Root: {main_part}")
    row = box.row(align=True)
    row.operator(BEAMNG_OT_apply_slot_configuration.bl_idname, text="Apply / Reload")
    row.operator(BEAMNG_OT_revert_slot_change.bl_idname, text="Revert")
    row = box.row(align=True)
    row.operator(BEAMNG_OT_save_slot_configuration.bl_idname, text="Save PC")
    row.operator(BEAMNG_OT_save_as_slot_configuration.bl_idname, text="Save PC As...")

    source_label = context.scene.get("beamng_slot_editor_source_pc_path", "")
    if source_label:
        box.label(text=f"Source: {Path(source_label).name}")

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
        op = layout.operator(BEAMNG_OT_set_visibility.bl_idname, text="JBeam Authoring Mode")
        op.mode = "AUTHORING"

        box = layout.box()
        box.label(text="This View")
        row = box.row(align=True)
        op = row.operator(BEAMNG_OT_set_active_view_filter.bl_idname, text="Flex")
        op.mode = "FLEX"
        op = row.operator(BEAMNG_OT_set_active_view_filter.bl_idname, text="Props")
        op.mode = "PROPS"
        op = row.operator(BEAMNG_OT_set_active_view_filter.bl_idname, text="JBeam")
        op.mode = "JBEAM"
        row = box.row(align=True)
        op = row.operator(BEAMNG_OT_set_active_view_filter.bl_idname, text="Flex + Props")
        op.mode = "FLEX_PROPS"
        op = row.operator(BEAMNG_OT_set_active_view_filter.bl_idname, text="All")
        op.mode = "ALL"
        row = box.row(align=True)
        row.operator(BEAMNG_OT_toggle_view_sync.bl_idname, text="Sync Views")
        row.operator(BEAMNG_OT_exit_split_local_views.bl_idname, text="Exit Local Views")

        row = layout.row(align=True)
        row.operator(BEAMNG_OT_setup_split_prop_flexbody_views.bl_idname, text="Split Props/Flex")
        row.operator(BEAMNG_OT_exit_split_local_views.bl_idname, text="Exit Split")
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
        draw_vehicle_slot_editor(layout, context)

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


class SCENE_PT_beamng_configuration_editor(Panel):
    bl_label = "BeamNG Configuration Editor"
    bl_idname = "SCENE_PT_beamng_configuration_editor"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "scene"

    def draw(self, context):
        layout = self.layout
        layout.label(text=f"Version: {addon_version_label()}")
        draw_vehicle_slot_editor(layout, context)


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
    source_asset=None,
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
        if source_asset is not None:
            source_pc_path = source_asset.path if source_asset.asset_type == "file" else str(pc_path)
            source_virtual_path = source_asset.virtual_path
            root_collection["beamng_pc_source_asset_type"] = source_asset.asset_type
            root_collection["beamng_pc_source_label_prefix"] = source_asset.label_prefix
            root_collection["beamng_pc_source_zip_path"] = source_asset.zip_path
            root_collection["beamng_pc_source_zip_entry"] = source_asset.zip_entry
        else:
            source_pc_path = context.scene.get("beamng_slot_editor_source_pc_path", str(pc_path))
            source_virtual_path = context.scene.get(
                "beamng_slot_editor_source_virtual_path",
                normalize_virtual_path(Path("vehicles") / pc_path.parent.name / pc_path.name),
            )
            root_collection["beamng_pc_source_asset_type"] = context.scene.get("beamng_slot_editor_source_asset_type", "file")
            root_collection["beamng_pc_source_label_prefix"] = context.scene.get("beamng_slot_editor_source_label_prefix", "")
            root_collection["beamng_pc_source_zip_path"] = context.scene.get("beamng_slot_editor_source_zip_path", "")
            root_collection["beamng_pc_source_zip_entry"] = context.scene.get("beamng_slot_editor_source_zip_entry", "")
        root_collection["beamng_pc_source_path"] = source_pc_path
        root_collection["beamng_pc_virtual_path"] = normalize_virtual_path(source_virtual_path)
        context.scene["beamng_slot_editor_source_pc_path"] = root_collection["beamng_pc_source_path"]
        context.scene["beamng_slot_editor_source_virtual_path"] = root_collection["beamng_pc_virtual_path"]
        context.scene["beamng_slot_editor_source_asset_type"] = root_collection["beamng_pc_source_asset_type"]
        context.scene["beamng_slot_editor_source_label_prefix"] = root_collection["beamng_pc_source_label_prefix"]
        context.scene["beamng_slot_editor_source_zip_path"] = root_collection["beamng_pc_source_zip_path"]
        context.scene["beamng_slot_editor_source_zip_entry"] = root_collection["beamng_pc_source_zip_entry"]
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
        filepath = Path(self.filepath)
        context.scene["beamng_slot_editor_source_pc_path"] = str(filepath)
        context.scene["beamng_slot_editor_source_virtual_path"] = normalize_virtual_path(Path("vehicles") / filepath.parent.name / filepath.name)
        context.scene["beamng_slot_editor_source_asset_type"] = "file"
        context.scene["beamng_slot_editor_source_label_prefix"] = ""
        context.scene["beamng_slot_editor_source_zip_path"] = ""
        context.scene["beamng_slot_editor_source_zip_entry"] = ""
        return import_beamng_pc_path(
            context,
            self,
            filepath,
            self.clear_existing,
            self.include_jbeam_visuals,
            self.selectable_jbeam_debug,
            self.show_jbeam_node_labels,
            str(filepath),
            None,
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
        context.scene["beamng_slot_editor_source_pc_path"] = source.path if source.asset_type == "file" else str(pc_path)
        context.scene["beamng_slot_editor_source_virtual_path"] = normalize_virtual_path(source.virtual_path)
        context.scene["beamng_slot_editor_source_asset_type"] = source.asset_type
        context.scene["beamng_slot_editor_source_label_prefix"] = source.label_prefix
        context.scene["beamng_slot_editor_source_zip_path"] = source.zip_path
        context.scene["beamng_slot_editor_source_zip_entry"] = source.zip_entry
        return import_beamng_pc_path(
            context,
            self,
            pc_path,
            self.clear_existing,
            self.include_jbeam_visuals,
            self.selectable_jbeam_debug,
            self.show_jbeam_node_labels,
            source_description,
            source,
        )


def menu_func_import(self, _context):
    self.layout.operator(IMPORT_OT_beamng_pc.bl_idname, text="BeamNG Config (.pc File)")
    self.layout.operator(IMPORT_OT_beamng_pc_from_assets.bl_idname, text="BeamNG Config From BeamNG Assets")


def menu_func_view_sync(self, _context):
    self.layout.separator()
    self.layout.operator(BEAMNG_OT_toggle_view_sync.bl_idname, text="Sync BeamNG 3D Views")


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
    BEAMNG_OT_setup_split_prop_flexbody_views,
    BEAMNG_OT_set_active_view_filter,
    BEAMNG_OT_exit_split_local_views,
    BEAMNG_OT_toggle_view_sync,
    BEAMNG_OT_print_prop_transforms,
    BEAMNG_OT_toggle_relationship_lines,
    BEAMNG_OT_apply_slot_configuration,
    BEAMNG_OT_revert_slot_change,
    BEAMNG_OT_save_slot_configuration,
    BEAMNG_OT_save_as_slot_configuration,
    VIEW3D_PT_beamng_pc_importer,
    SCENE_PT_beamng_configuration_editor,
    IMPORT_OT_beamng_pc,
    IMPORT_OT_beamng_pc_from_assets,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.beamng_slot_editor_items = CollectionProperty(type=BeamNGSlotEditorItem)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    bpy.types.VIEW3D_MT_view.append(menu_func_view_sync)
    bpy.types.VIEW3D_MT_object_context_menu.append(menu_func_jbeam_context)


def unregister():
    bpy.types.VIEW3D_MT_object_context_menu.remove(menu_func_jbeam_context)
    bpy.types.VIEW3D_MT_view.remove(menu_func_view_sync)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    if hasattr(bpy.types.Scene, "beamng_slot_editor_items"):
        del bpy.types.Scene.beamng_slot_editor_items
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
