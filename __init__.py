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
ADDON_BUILD = 134


def addon_version_label():
    version = ".".join(str(part) for part in bl_info["version"])
    return f"{version} build {ADDON_BUILD}"

import json
import hashlib
import math
import re
import tempfile
import time
import uuid
import zipfile
import colorsys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, IntProperty, StringProperty
from bpy.types import AddonPreferences, Operator, Panel, PropertyGroup
from bpy_extras.io_utils import ImportHelper
from mathutils import Euler, Matrix, Vector


try:
    import importlib
    from . import core as _core
    from . import dae_assets as _dae_assets
    from . import resolved_model as _resolved_model
    from . import slot_authoring as _slot_authoring
    from . import visuals as _visuals

    importlib.reload(_core)
    importlib.reload(_dae_assets)
    importlib.reload(_resolved_model)
    importlib.reload(_slot_authoring)
    importlib.reload(_visuals)

    from .core import *
    from .dae_assets import *
    from .resolved_model import *
    from .slot_authoring import *
    from .visuals import *
except ImportError:
    from core import *
    from dae_assets import *
    from resolved_model import *
    from slot_authoring import *
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


class BeamNGJBeamExportFileItem(PropertyGroup):
    include: BoolProperty(name="Export", default=True)
    virtual_path: StringProperty(default="")
    source_file: StringProperty(default="")
    label: StringProperty(default="")
    parts_label: StringProperty(default="")
    planned_target_path: StringProperty(default="")
    node_update_count: IntProperty(default=0)
    topology_update_count: IntProperty(default=0)


class BeamNGAssemblyPartItem(PropertyGroup):
    part_key: StringProperty(default="")
    part_name: StringProperty(default="")
    object_name: StringProperty(default="")
    source_file: StringProperty(default="")
    part_guid: StringProperty(default="")
    resolved_part_id: IntProperty(default=-1)
    owned_node_count: IntProperty(default=0)
    proxy_node_count: IntProperty(default=0)


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
    resolved_node_options = []
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
        local_node_options = parse_node_options(
            resolved_part.part_def,
            component_context,
        )
        resolved_node_positions.append(local_node_positions)
        resolved_node_options.append(local_node_options)
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
                    options=resolved_node_options[index].get(node_name, {}),
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


def remove_collection_tree(collection):
    for obj in list(walk_collection_objects(collection)):
        bpy.data.objects.remove(obj, do_unlink=True)
    for child in list(collection.children):
        remove_collection_tree(child)
        if child.name in bpy.data.collections:
            bpy.data.collections.remove(child)


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


def selected_active_object(context):
    active = context.active_object
    if not active:
        return None
    if getattr(context, "edit_object", None) is active:
        return active
    try:
        if not active.select_get():
            return None
    except ReferenceError:
        return None
    return active


def active_experimental_jbeam_mesh(context):
    for obj in (
        getattr(context, "edit_object", None),
        getattr(context, "object", None),
        getattr(context, "active_object", None),
    ):
        if obj and obj.type == "MESH" and obj.get("beamng_visual_type") == "experimental_jbeam_mesh":
            return obj
    for obj in getattr(context, "selected_objects", []) or []:
        if obj and obj.type == "MESH" and obj.get("beamng_visual_type") == "experimental_jbeam_mesh":
            return obj
    for obj in getattr(context.scene, "objects", []) or []:
        if (
            obj
            and obj.type == "MESH"
            and obj.mode == "EDIT"
            and obj.get("beamng_visual_type") == "experimental_jbeam_mesh"
        ):
            return obj
    return None


def jbeam_assembly_part_key_for_object(obj):
    if obj is None:
        return ""
    guid = str(obj.get("beamng_part_guid", "") or "").strip()
    if guid:
        return f"guid:{guid}"
    source = normalize_virtual_path(obj.get("beamng_jbeam_path", ""))
    part_name = str(obj.get("beamng_part_name", "") or "").strip()
    resolved_part_id = obj.get("beamng_resolved_part_id", "")
    return f"part:{source}|{part_name}|{resolved_part_id}|{obj.name}"


def experimental_jbeam_part_objects(scene):
    return sorted(
        (
            obj
            for obj in scene.objects
            if obj.type == "MESH" and obj.get("beamng_visual_type") == "experimental_jbeam_mesh"
        ),
        key=lambda obj: (
            normalize_virtual_path(obj.get("beamng_jbeam_path", "")),
            str(obj.get("beamng_part_name", "")),
            obj.name,
        ),
    )


def refresh_jbeam_assembly_parts(scene):
    if not hasattr(scene, "beamng_assembly_part_items"):
        return []
    objects = experimental_jbeam_part_objects(scene)
    items = scene.beamng_assembly_part_items
    items.clear()
    for obj in objects:
        item = items.add()
        item.part_key = jbeam_assembly_part_key_for_object(obj)
        item.part_name = str(obj.get("beamng_part_name", "") or obj.name)
        item.object_name = obj.name
        item.source_file = normalize_virtual_path(obj.get("beamng_jbeam_path", ""))
        item.part_guid = str(obj.get("beamng_part_guid", "") or "")
        item.resolved_part_id = int(obj.get("beamng_resolved_part_id", -1) or -1)
        item.owned_node_count = int(obj.get("beamng_owned_node_count", 0) or 0)
        item.proxy_node_count = int(obj.get("beamng_proxy_node_count", 0) or 0)
    valid_keys = {item.part_key for item in items}
    active_key = str(scene.get("beamng_active_jbeam_part_key", "") or "")
    if active_key and active_key not in valid_keys:
        scene["beamng_active_jbeam_part_key"] = ""
        scene["beamng_active_jbeam_part_name"] = ""
        scene["beamng_active_jbeam_part_object"] = ""
    scene["beamng_assembly_part_count"] = len(items)
    return objects


def active_jbeam_assembly_part_object(scene):
    active_key = str(scene.get("beamng_active_jbeam_part_key", "") or "")
    if not active_key:
        return None
    for obj in experimental_jbeam_part_objects(scene):
        if jbeam_assembly_part_key_for_object(obj) == active_key:
            return obj
    return None


def set_active_jbeam_assembly_part(scene, obj):
    if obj is None:
        scene["beamng_active_jbeam_part_key"] = ""
        scene["beamng_active_jbeam_part_name"] = ""
        scene["beamng_active_jbeam_part_object"] = ""
        scene["beamng_active_jbeam_part_source"] = ""
        return
    scene["beamng_active_jbeam_part_key"] = jbeam_assembly_part_key_for_object(obj)
    scene["beamng_active_jbeam_part_name"] = str(obj.get("beamng_part_name", "") or obj.name)
    scene["beamng_active_jbeam_part_object"] = obj.name
    scene["beamng_active_jbeam_part_source"] = normalize_virtual_path(obj.get("beamng_jbeam_path", ""))


def sync_active_jbeam_part_from_selection(context):
    obj = active_experimental_jbeam_mesh(context)
    if obj is None:
        return None
    key = jbeam_assembly_part_key_for_object(obj)
    if str(context.scene.get("beamng_active_jbeam_part_key", "") or "") != key:
        set_active_jbeam_assembly_part(context.scene, obj)
    apply_jbeam_active_part_reference_display(context.scene)
    return obj


def apply_jbeam_active_part_reference_display(scene):
    active_key = str(scene.get("beamng_active_jbeam_part_key", "") or "")
    for obj in experimental_jbeam_part_objects(scene):
        is_active = bool(active_key) and jbeam_assembly_part_key_for_object(obj) == active_key
        obj["beamng_active_part_state"] = "active" if is_active else "reference"
        if is_active or not active_key:
            obj.display_type = "TEXTURED"
            obj.show_wire = True
            obj.hide_select = False
            obj.color = tuple(obj.get("beamng_original_view_color", obj.color))
        else:
            if "beamng_original_view_color" not in obj:
                obj["beamng_original_view_color"] = tuple(obj.color)
            base = tuple(obj.get("beamng_original_view_color", obj.color))
            obj.display_type = "WIRE"
            obj.show_wire = True
            obj.hide_select = False
            obj.color = (base[0] * 0.45, base[1] * 0.45, base[2] * 0.45, 0.35)


def active_part_allows_topology_edit(context, obj):
    active_key = str(context.scene.get("beamng_active_jbeam_part_key", "") or "")
    if not active_key:
        return True
    return jbeam_assembly_part_key_for_object(obj) == active_key


def require_active_part_for_topology_edit(operator, context, obj):
    if selected_active_object(context) is obj:
        set_active_jbeam_assembly_part(context.scene, obj)
        apply_jbeam_active_part_reference_display(context.scene)
    if active_part_allows_topology_edit(context, obj):
        return True
    active_name = context.scene.get("beamng_active_jbeam_part_name", "") or "(none)"
    operator.report({"WARNING"}, f"Active Part is '{active_name}'. Switch Active Part before editing this mesh")
    return False


def active_object_debug_label(context):
    obj = getattr(context, "edit_object", None) or getattr(context, "object", None) or getattr(context, "active_object", None)
    if obj is None:
        return "Active object: none"
    return (
        f"Active object: {obj.name} / {obj.type} / "
        f"{obj.get('beamng_visual_type', '(no BeamNG visual type)')}"
    )


def experimental_jbeam_panel_redraw_timer():
    try:
        has_experimental_mesh = any(
            obj.type == "MESH"
            and obj.get("beamng_visual_type") == "experimental_jbeam_mesh"
            for obj in bpy.data.objects
        )
        has_edit_mesh = any(
            obj.type == "MESH"
            and obj.mode == "EDIT"
            and obj.get("beamng_visual_type") == "experimental_jbeam_mesh"
            for obj in bpy.data.objects
        )
        if has_experimental_mesh:
            sync_active_jbeam_part_from_selection(bpy.context)
            prefs = get_addon_preferences(bpy.context)
            if has_edit_mesh and (prefs is None or bool(getattr(prefs, "auto_sync_proxy_nodes", True))):
                poll_experimental_jbeam_edit_mesh_proxy_sync(bpy.context.scene)
            if prefs is None or bool(getattr(prefs, "auto_scan_jbeam_edits", True)):
                poll_experimental_jbeam_mesh_auto_scan(bpy.context.scene)
            for window in bpy.context.window_manager.windows:
                screen = window.screen
                for area in screen.areas:
                    if area.type == "VIEW_3D":
                        area.tag_redraw()
            return 0.25 if has_edit_mesh else 0.5
    except Exception:
        return 1.0
    return 1.0


JBEAM_PROXY_SYNC_DEBOUNCE_SECONDS = 0.35
JBEAM_AUTO_SCAN_DEBOUNCE_SECONDS = 0.75
_jbeam_proxy_sync_due_time = 0.0
_jbeam_proxy_sync_timer_pending = False
_jbeam_proxy_sync_last_signature = None
_jbeam_proxy_sync_poll_due_time = 0.0
_jbeam_auto_scan_due_time = 0.0
_jbeam_auto_scan_timer_pending = False
_jbeam_auto_scan_running = False
_jbeam_auto_scan_last_signature = None
_jbeam_auto_scan_poll_due_time = 0.0


def redraw_experimental_jbeam_viewports():
    try:
        for window in bpy.context.window_manager.windows:
            screen = window.screen
            for area in screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
    except Exception:
        pass


def experimental_jbeam_proxy_sync_timer():
    global _jbeam_proxy_sync_timer_pending
    now = time.monotonic()
    remaining = _jbeam_proxy_sync_due_time - now
    if remaining > 0:
        return min(max(remaining, 0.05), 0.5)

    _jbeam_proxy_sync_timer_pending = False
    try:
        scene = bpy.context.scene
        if scene is not None:
            sync_experimental_jbeam_proxy_nodes(scene)
            redraw_experimental_jbeam_viewports()
    except Exception:
        pass
    return None


def experimental_jbeam_auto_scan_timer():
    global _jbeam_auto_scan_timer_pending, _jbeam_auto_scan_running
    now = time.monotonic()
    remaining = _jbeam_auto_scan_due_time - now
    if remaining > 0:
        return min(max(remaining, 0.05), 0.75)

    _jbeam_auto_scan_timer_pending = False
    prefs = get_addon_preferences(bpy.context)
    if prefs is not None and not bool(getattr(prefs, "auto_scan_jbeam_edits", True)):
        return None

    scene = bpy.context.scene
    if scene is None:
        return None

    _jbeam_auto_scan_running = True
    try:
        result = scan_experimental_jbeam_mesh_edits(scene, active_only=False)
        scene["beamng_jbeam_auto_scan_count"] = int(scene.get("beamng_jbeam_auto_scan_count", 0)) + 1
        scene["beamng_jbeam_last_auto_scan_message"] = (
            f"Auto scanned {int(result.get('scanned_mesh_count', 0))} mesh(es), "
            f"pending {int(scene.get('beamng_jbeam_pending_node_move_count', 0))}"
        )
        redraw_experimental_jbeam_viewports()
    except Exception as exc:
        scene["beamng_jbeam_last_auto_scan_message"] = f"Auto scan failed: {exc}"
    finally:
        _jbeam_auto_scan_running = False
    return None


def experimental_jbeam_mesh_position_signature(scene):
    signature = []
    for obj in experimental_jbeam_mesh_objects(scene, active_only=False):
        if obj.mode != "EDIT":
            continue
        try:
            _edit_mesh, positions = read_experimental_mesh_vertices(obj)
        except Exception:
            continue
        for index, position in enumerate(positions):
            signature.append(
                (
                    obj.name,
                    index,
                    tuple(rounded_position_list(position)),
                )
            )
    return tuple(signature)


def experimental_jbeam_mesh_scan_signature(scene):
    signature = []
    for obj in experimental_jbeam_mesh_objects(scene, active_only=False):
        try:
            if obj.mode == "EDIT":
                import bmesh

                edit_mesh = bmesh.from_edit_mesh(obj.data)
                edit_mesh.verts.ensure_lookup_table()
                edit_mesh.verts.index_update()
                edit_mesh.edges.ensure_lookup_table()
                edit_mesh.edges.index_update()
                edit_mesh.faces.ensure_lookup_table()
                edit_mesh.faces.index_update()
                vertex_values = tuple(tuple(rounded_position_list(vertex.co)) for vertex in edit_mesh.verts)
                edge_values = tuple(sorted(tuple(sorted(vertex.index for vertex in edge.verts)) for edge in edit_mesh.edges))
                face_values = tuple(tuple(vertex.index for vertex in face.verts) for face in edit_mesh.faces)
            else:
                mesh = obj.data
                vertex_values = tuple(tuple(rounded_position_list(vertex.co)) for vertex in mesh.vertices)
                edge_values = tuple(sorted(tuple(sorted(edge.vertices)) for edge in mesh.edges))
                face_values = tuple(tuple(polygon.vertices) for polygon in mesh.polygons)
            signature.append((obj.name, obj.mode, vertex_values, edge_values, face_values))
        except Exception:
            continue
    return tuple(signature)


def poll_experimental_jbeam_edit_mesh_proxy_sync(scene):
    global _jbeam_proxy_sync_last_signature, _jbeam_proxy_sync_poll_due_time
    try:
        signature = experimental_jbeam_mesh_position_signature(scene)
    except Exception:
        return
    now = time.monotonic()
    if signature != _jbeam_proxy_sync_last_signature:
        _jbeam_proxy_sync_last_signature = signature
        _jbeam_proxy_sync_poll_due_time = now + JBEAM_PROXY_SYNC_DEBOUNCE_SECONDS
        return
    if _jbeam_proxy_sync_poll_due_time and now >= _jbeam_proxy_sync_poll_due_time:
        _jbeam_proxy_sync_poll_due_time = 0.0
        result = sync_experimental_jbeam_proxy_nodes(scene)
        if result.get("restored") or result.get("synced"):
            redraw_experimental_jbeam_viewports()


def poll_experimental_jbeam_mesh_auto_scan(scene):
    global _jbeam_auto_scan_last_signature, _jbeam_auto_scan_poll_due_time
    if _jbeam_auto_scan_running:
        return
    try:
        signature = experimental_jbeam_mesh_scan_signature(scene)
    except Exception:
        return
    now = time.monotonic()
    if signature != _jbeam_auto_scan_last_signature:
        _jbeam_auto_scan_last_signature = signature
        _jbeam_auto_scan_poll_due_time = now + JBEAM_AUTO_SCAN_DEBOUNCE_SECONDS
        return
    if _jbeam_auto_scan_poll_due_time and now >= _jbeam_auto_scan_poll_due_time:
        _jbeam_auto_scan_poll_due_time = 0.0
        tag_experimental_jbeam_auto_scan()


def tag_experimental_jbeam_proxy_sync():
    prefs = get_addon_preferences(bpy.context)
    if prefs is not None and not bool(getattr(prefs, "auto_sync_proxy_nodes", True)):
        return
    global _jbeam_proxy_sync_due_time, _jbeam_proxy_sync_timer_pending
    _jbeam_proxy_sync_due_time = time.monotonic() + JBEAM_PROXY_SYNC_DEBOUNCE_SECONDS
    if not _jbeam_proxy_sync_timer_pending:
        _jbeam_proxy_sync_timer_pending = True
        bpy.app.timers.register(experimental_jbeam_proxy_sync_timer, first_interval=JBEAM_PROXY_SYNC_DEBOUNCE_SECONDS)


def tag_experimental_jbeam_auto_scan():
    if _jbeam_auto_scan_running:
        return
    prefs = get_addon_preferences(bpy.context)
    if prefs is not None and not bool(getattr(prefs, "auto_scan_jbeam_edits", True)):
        return
    global _jbeam_auto_scan_due_time, _jbeam_auto_scan_timer_pending
    _jbeam_auto_scan_due_time = time.monotonic() + JBEAM_AUTO_SCAN_DEBOUNCE_SECONDS
    if not _jbeam_auto_scan_timer_pending:
        _jbeam_auto_scan_timer_pending = True
        bpy.app.timers.register(experimental_jbeam_auto_scan_timer, first_interval=JBEAM_AUTO_SCAN_DEBOUNCE_SECONDS)


@persistent
def experimental_jbeam_mesh_depsgraph_update_post(scene, depsgraph):
    try:
        updated_ids = {update.id for update in depsgraph.updates}
        for obj in experimental_jbeam_mesh_objects(scene, active_only=False):
            if obj in updated_ids or obj.data in updated_ids:
                tag_experimental_jbeam_proxy_sync()
                tag_experimental_jbeam_auto_scan()
                return
    except Exception:
        return


@persistent
def experimental_jbeam_mesh_topology_update_post(mesh):
    try:
        attributes = getattr(mesh, "attributes", None)
        if attributes is None or JBEAM_NODE_UID_ATTR not in attributes:
            return
        scene = bpy.context.scene
        if scene is not None:
            sync_experimental_jbeam_proxy_nodes(scene)
        tag_experimental_jbeam_auto_scan()
    except Exception:
        return


def reset_jbeam_edit_session(scene):
    global _jbeam_proxy_sync_last_signature, _jbeam_proxy_sync_poll_due_time
    global _jbeam_auto_scan_last_signature, _jbeam_auto_scan_poll_due_time
    _jbeam_proxy_sync_last_signature = None
    _jbeam_proxy_sync_poll_due_time = 0.0
    _jbeam_auto_scan_last_signature = None
    _jbeam_auto_scan_poll_due_time = 0.0
    scene["beamng_jbeam_operation_history_json"] = json.dumps([])
    scene["beamng_jbeam_operation_history_count"] = 0
    scene["beamng_jbeam_pending_node_moves_json"] = json.dumps([])
    scene["beamng_jbeam_pending_node_move_count"] = 0
    scene["beamng_jbeam_pending_topology_change_count"] = 0
    scene["beamng_jbeam_restored_proxy_move_count"] = 0
    scene["beamng_jbeam_synced_proxy_node_count"] = 0
    scene["beamng_jbeam_removed_proxy_node_count"] = 0
    scene["beamng_jbeam_last_proxy_sync_message"] = ""
    scene["beamng_jbeam_auto_scan_count"] = 0
    scene["beamng_jbeam_last_auto_scan_message"] = ""
    scene["beamng_jbeam_dirty"] = False
    model_json = scene.get("beamng_authoring_model_json", "")
    if model_json:
        try:
            model = ResolvedVehicleAuthoringModel.from_json(model_json)
            model.operations = []
            store_authoring_model_snapshot(scene, model)
        except Exception:
            pass
    for obj in scene.objects:
        if obj.type == "MESH" and obj.get("beamng_visual_type") == "experimental_jbeam_mesh":
            obj.data["beamng_node_move_changes_json"] = json.dumps([])
            obj["beamng_dirty_node_move_count"] = 0
            obj["beamng_dirty_topology_change_count"] = 0
            obj["beamng_proxy_sync_identity_ready"] = False


@persistent
def clear_jbeam_edit_sessions_on_load(_dummy):
    for scene in bpy.data.scenes:
        reset_jbeam_edit_session(scene)


def selected_jbeam_part_id(context):
    active = selected_active_object(context)
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


def ensure_jbeam_container_visible(root_collection):
    for collection in walk_child_collections(root_collection):
        if collection.get("beamng_layer") != "jbeam":
            continue
        if collection.get("beamng_resolved_part_id") is None:
            collection.hide_viewport = False
            collection.hide_render = False


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


def jbeam_node_owner_part_ids(root_collection):
    stored = root_collection.get("beamng_resolved_node_owner_part_ids", "")
    if stored:
        try:
            decoded = json.loads(stored)
            owners = defaultdict(set)
            for node_id, part_ids in decoded.items():
                owners[str(node_id)].update(int(part_id) for part_id in part_ids)
            return owners
        except (TypeError, ValueError, json.JSONDecodeError):
            pass

    owners = defaultdict(set)
    for obj in walk_collection_objects(root_collection):
        if obj.get("beamng_layer") != "jbeam":
            continue
        node_id = obj.get("beamng_node_id")
        if not node_id:
            continue
        try:
            part_id = int(obj.get("beamng_resolved_part_id", -999999))
        except (TypeError, ValueError):
            continue
        owners[str(node_id)].add(part_id)
    return owners


def jbeam_external_node_refs_for_part(root_collection, part_id):
    stored = root_collection.get("beamng_resolved_part_external_node_refs", "")
    if stored:
        try:
            decoded = json.loads(stored)
            return {str(node_id) for node_id in decoded.get(str(part_id), [])}
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return set()


def jbeam_node_ids_for_object(obj):
    visual_type = obj.get("beamng_visual_type") if obj else ""
    if visual_type in {"selectable_node", "node_label"}:
        node_id = obj.get("beamng_node_id")
        return {str(node_id)} if node_id else set()
    if visual_type in {"selectable_beam", "selectable_hydro"}:
        return {str(value) for value in (obj.get("beamng_beam_id1") or obj.get("beamng_hydro_id1"), obj.get("beamng_beam_id2") or obj.get("beamng_hydro_id2")) if value}
    if visual_type == "selectable_triangle":
        return {str(value) for value in (obj.get("beamng_triangle_id1"), obj.get("beamng_triangle_id2"), obj.get("beamng_triangle_id3")) if value}
    if visual_type == "selectable_slidenode":
        node_id = obj.get("beamng_slidenode_id")
        return {str(node_id)} if node_id else set()
    if visual_type == "selectable_rail":
        rail_nodes = obj.get("beamng_rail_nodes", "")
        return {node.strip() for node in str(rail_nodes).split(",") if node.strip()}
    return set()


def jbeam_reference_objects_for_part(root_collection, part_id):
    reference_types = {
        "selectable_beam",
        "selectable_triangle",
        "selectable_hydro",
        "selectable_rail",
        "selectable_slidenode",
    }
    return jbeam_objects_for_part_ids(root_collection, {part_id}, reference_types)


def jbeam_referenced_part_ids_for_node_ids(root_collection, source_part_id, node_ids):
    node_owners = jbeam_node_owner_part_ids(root_collection)
    referenced_node_ids = set()
    referenced_part_ids = set()
    for node_id in node_ids:
        owner_part_ids = node_owners.get(str(node_id), set())
        external_owner_ids = {part_id for part_id in owner_part_ids if part_id != source_part_id}
        if external_owner_ids:
            referenced_node_ids.add(str(node_id))
            referenced_part_ids.update(external_owner_ids)
    return referenced_part_ids, referenced_node_ids


def jbeam_referenced_part_ids_for_objects(root_collection, source_part_id, objects):
    node_ids = set()
    for obj in objects:
        node_ids.update(jbeam_node_ids_for_object(obj))
    return jbeam_referenced_part_ids_for_node_ids(root_collection, source_part_id, node_ids)


def experimental_jbeam_mesh_objects(scene, active_only=False):
    active = selected_active_object(bpy.context)
    if active_only and active and active.get("beamng_visual_type") == "experimental_jbeam_mesh":
        return [active]
    return [
        obj
        for obj in scene.objects
        if obj.type == "MESH" and obj.get("beamng_visual_type") == "experimental_jbeam_mesh"
    ]


def read_experimental_mesh_vertices(obj):
    mesh = obj.data
    if obj.mode == "EDIT":
        import bmesh

        edit_mesh = bmesh.from_edit_mesh(mesh)
        edit_mesh.verts.ensure_lookup_table()
        edit_mesh.verts.index_update()
        return edit_mesh, [vertex.co.copy() for vertex in edit_mesh.verts]
    return None, [vertex.co.copy() for vertex in mesh.vertices]


def proxy_node_world_positions_for_overlay(obj):
    mesh = obj.data
    positions = []
    uid_to_kind = mesh_json_dict(mesh, "beamng_node_uid_to_kind_json")
    legacy_kinds = [str(kind or "owned") for kind in mesh_json_list(mesh, "beamng_node_kinds_json")]
    if obj.mode == "EDIT":
        import bmesh

        edit_mesh = bmesh.from_edit_mesh(mesh)
        edit_mesh.verts.ensure_lookup_table()
        layer = edit_mesh.verts.layers.int.get(JBEAM_NODE_IS_PROXY_ATTR)
        topology_uids = ensure_experimental_topology_uids(obj, allow_write=False).get("nodes", []) if uid_to_kind else []
        for vertex in edit_mesh.verts:
            is_proxy = False
            if layer is not None and int(vertex[layer]) != 0:
                is_proxy = True
            elif vertex.index < len(topology_uids):
                is_proxy = str(uid_to_kind.get(topology_uid_key(topology_uids[vertex.index]), "")) == "proxy"
            elif vertex.index < len(legacy_kinds):
                is_proxy = legacy_kinds[vertex.index] == "proxy"
            if is_proxy:
                positions.append(obj.matrix_world @ vertex.co)
        return positions

    attr = mesh.attributes.get(JBEAM_NODE_IS_PROXY_ATTR) if hasattr(mesh, "attributes") else None
    topology_uids = ensure_experimental_topology_uids(obj, allow_write=False).get("nodes", []) if uid_to_kind else []
    if attr is None:
        attr_data = []
    else:
        attr_data = attr.data
    for index, vertex in enumerate(mesh.vertices):
        is_proxy = False
        if index < len(attr_data) and int(attr_data[index].value) != 0:
            is_proxy = True
        elif index < len(topology_uids):
            is_proxy = str(uid_to_kind.get(topology_uid_key(topology_uids[index]), "")) == "proxy"
        elif index < len(legacy_kinds):
            is_proxy = legacy_kinds[index] == "proxy"
        if is_proxy:
            positions.append(obj.matrix_world @ vertex.co)
    return positions


def experimental_jbeam_topology_overlay_geometry(obj):
    mesh = obj.data
    identity = ensure_experimental_mesh_identity(obj, bpy.context.scene, allow_write=False)
    node_ids = [str(node_id) for node_id in identity.get("node_ids", [])]
    node_kinds = [str(kind or "owned") for kind in identity.get("node_kinds", [])]
    semantic_snapshot = semantic_topology_snapshot_for_object(obj, bpy.context.scene, allow_write=False)
    semantic_by_key = semantic_edge_types_by_key(semantic_snapshot)
    owned_points = []
    proxy_points = []
    beam_lines = []
    boundary_lines = []
    relation_lines = []

    if obj.mode == "EDIT":
        import bmesh

        edit_mesh = bmesh.from_edit_mesh(mesh)
        edit_mesh.verts.ensure_lookup_table()
        edit_mesh.verts.index_update()
        edit_mesh.edges.ensure_lookup_table()
        edit_mesh.edges.index_update()
        world_positions = [obj.matrix_world @ vertex.co for vertex in edit_mesh.verts]
        raw_edges = [(edge.index, [vertex.index for vertex in edge.verts]) for edge in edit_mesh.edges]
    else:
        world_positions = [obj.matrix_world @ vertex.co for vertex in mesh.vertices]
        raw_edges = [(edge.index, list(edge.vertices)) for edge in mesh.edges]

    for index, position in enumerate(world_positions):
        if index < len(node_kinds) and node_kinds[index] == "proxy":
            proxy_points.append(position)
        else:
            owned_points.append(position)

    for _edge_index, indices in raw_edges:
        if len(indices) != 2 or not all(0 <= index < len(world_positions) for index in indices):
            continue
        if not all(index < len(node_ids) for index in indices):
            relation_lines.extend([world_positions[indices[0]], world_positions[indices[1]]])
            continue
        semantic_type = semantic_by_key.get(edge_key((node_ids[indices[0]], node_ids[indices[1]])), JBEAM_EDGE_SEMANTIC_RELATIONSHIP)
        target = (
            beam_lines
            if semantic_type == JBEAM_EDGE_SEMANTIC_BEAM
            else boundary_lines
            if semantic_type == JBEAM_EDGE_SEMANTIC_TRIANGLE_BOUNDARY
            else relation_lines
        )
        target.extend([world_positions[indices[0]], world_positions[indices[1]]])

    return {
        "owned_points": owned_points,
        "proxy_points": proxy_points,
        "beam_lines": beam_lines,
        "boundary_lines": boundary_lines,
        "relation_lines": relation_lines,
    }


def draw_gpu_lines(shader, line_positions, color):
    if not line_positions:
        return
    from gpu_extras.batch import batch_for_shader

    batch = batch_for_shader(shader, "LINES", {"pos": line_positions})
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)


def draw_gpu_points(shader, point_positions, color, size=6.0):
    if not point_positions:
        return
    import gpu
    from gpu_extras.batch import batch_for_shader

    previous_size = 1.0
    try:
        gpu.state.point_size_set(size)
    except Exception:
        pass
    batch = batch_for_shader(shader, "POINTS", {"pos": point_positions})
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)
    try:
        gpu.state.point_size_set(previous_size)
    except Exception:
        pass


def draw_experimental_jbeam_proxy_node_overlay():
    context = bpy.context
    scene = context.scene
    region_data = getattr(context, "region_data", None)
    if scene is None or region_data is None:
        return
    prefs = get_addon_preferences(context)
    show_proxy_overlay = prefs is None or bool(getattr(prefs, "show_proxy_node_overlay", True))
    show_topology_overlay = prefs is None or bool(getattr(prefs, "show_jbeam_semantic_overlay", True))
    if not show_proxy_overlay and not show_topology_overlay:
        return

    if show_topology_overlay:
        try:
            import gpu

            shader = gpu.shader.from_builtin("UNIFORM_COLOR")
            for obj in experimental_jbeam_mesh_objects(scene, active_only=False):
                geometry = experimental_jbeam_topology_overlay_geometry(obj)
                draw_gpu_lines(shader, geometry["relation_lines"], (0.55, 0.55, 0.55, 0.85))
                draw_gpu_lines(shader, geometry["boundary_lines"], (1.0, 0.82, 0.12, 0.95))
                draw_gpu_lines(shader, geometry["beam_lines"], (0.1, 0.95, 0.32, 1.0))
                draw_gpu_points(shader, geometry["owned_points"], (0.15, 0.55, 1.0, 1.0), 5.5)
                draw_gpu_points(shader, geometry["proxy_points"], (1.0, 0.62, 0.05, 1.0), 7.0)
        except Exception:
            pass

    if not show_proxy_overlay:
        return

    line_positions = []
    view_right = region_data.view_rotation @ Vector((1.0, 0.0, 0.0))
    view_up = region_data.view_rotation @ Vector((0.0, 1.0, 0.0))
    for obj in experimental_jbeam_mesh_objects(scene, active_only=False):
        for position in proxy_node_world_positions_for_overlay(obj):
            size = max(0.025, (position - region_data.view_location).length * 0.004)
            line_positions.extend(
                [
                    position - view_right * size,
                    position + view_right * size,
                    position - view_up * size,
                    position + view_up * size,
                ]
            )
    if not line_positions:
        return

    try:
        import gpu
        from gpu_extras.batch import batch_for_shader

        shader = gpu.shader.from_builtin("UNIFORM_COLOR")
        batch = batch_for_shader(shader, "LINES", {"pos": line_positions})
        shader.bind()
        shader.uniform_float("color", (1.0, 0.62, 0.05, 1.0))
        batch.draw(shader)
    except Exception:
        return


def register_experimental_jbeam_proxy_overlay():
    global _jbeam_proxy_overlay_draw_handle
    if _jbeam_proxy_overlay_draw_handle is None:
        _jbeam_proxy_overlay_draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            draw_experimental_jbeam_proxy_node_overlay, (), "WINDOW", "POST_VIEW"
        )


def unregister_experimental_jbeam_proxy_overlay():
    global _jbeam_proxy_overlay_draw_handle
    if _jbeam_proxy_overlay_draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_jbeam_proxy_overlay_draw_handle, "WINDOW")
        _jbeam_proxy_overlay_draw_handle = None


def set_experimental_mesh_vertex(edit_mesh, mesh, index, position):
    if edit_mesh is not None:
        edit_mesh.verts[index].co = position
        import bmesh

        bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
    else:
        mesh.vertices[index].co = position
        mesh.update()


def update_experimental_proxy_node_baseline(obj, index, position):
    mesh = obj.data
    target = rounded_position_list(position)
    baselines = mesh_json_list(mesh, "beamng_original_node_positions_json")
    if index >= len(baselines):
        baselines.extend([[] for _ in range(index + 1 - len(baselines))])
    baselines[index] = target
    mesh["beamng_original_node_positions_json"] = json.dumps(baselines)

    node_uids = ensure_experimental_topology_uids(obj, allow_write=True).get("nodes", [])
    uid_key = topology_uid_key(node_uids[index]) if index < len(node_uids) else ""
    if uid_key:
        uid_to_baseline = mesh_json_dict(mesh, "beamng_node_uid_to_original_position_json")
        uid_to_baseline[uid_key] = target
        mesh["beamng_node_uid_to_original_position_json"] = json.dumps(uid_to_baseline)


def sync_experimental_jbeam_proxy_nodes(scene, tolerance=0.0005):
    owner_positions = {}
    proxy_targets = []
    restored_proxy_count = 0
    synced_proxy_count = 0

    for obj in experimental_jbeam_mesh_objects(scene, active_only=False):
        materialized = bool(obj.get("beamng_proxy_sync_identity_ready", False))
        identity = ensure_experimental_mesh_identity(obj, scene, allow_write=not materialized)
        if not materialized:
            obj["beamng_proxy_sync_identity_ready"] = True
        node_ids = identity.get("node_ids", [])
        node_kinds = identity.get("node_kinds", [])
        baselines = identity.get("original_positions", [])
        _edit_mesh, positions = read_experimental_mesh_vertices(obj)
        for index, node_id in enumerate(node_ids):
            if index >= len(positions) or index >= len(node_kinds):
                continue
            kind = str(node_kinds[index])
            node_id = str(node_id)
            current = rounded_position_list(positions[index])
            baseline = baselines[index] if index < len(baselines) else []
            baseline = rounded_position_list(baseline) if isinstance(baseline, (list, tuple)) and len(baseline) == 3 else current
            if kind == "proxy":
                proxy_targets.append((obj, index, node_id, current, baseline))
            elif current != baseline and (Vector(current) - Vector(baseline)).length > tolerance:
                owner_positions[node_id] = current
            elif node_id not in owner_positions:
                owner_positions[node_id] = current

    for obj, index, node_id, current, baseline in proxy_targets:
        target = owner_positions.get(node_id, baseline)
        if not target:
            continue
        if current == target or (Vector(current) - Vector(target)).length <= tolerance:
            continue
        edit_mesh, _positions = read_experimental_mesh_vertices(obj)
        set_experimental_mesh_vertex(edit_mesh, obj.data, index, Vector(target))
        update_experimental_proxy_node_baseline(obj, index, target)
        if current != baseline:
            restored_proxy_count += 1
        else:
            synced_proxy_count += 1

    if restored_proxy_count or synced_proxy_count:
        scene["beamng_jbeam_restored_proxy_move_count"] = (
            int(scene.get("beamng_jbeam_restored_proxy_move_count", 0)) + restored_proxy_count
        )
        scene["beamng_jbeam_synced_proxy_node_count"] = (
            int(scene.get("beamng_jbeam_synced_proxy_node_count", 0)) + synced_proxy_count
        )
        scene["beamng_jbeam_last_proxy_sync_message"] = (
            f"Proxy nodes restored {restored_proxy_count}, synced {synced_proxy_count}"
        )
    return {"restored": restored_proxy_count, "synced": synced_proxy_count}


def selected_experimental_jbeam_vertex_indices(obj):
    if obj is None or obj.type != "MESH" or obj.get("beamng_visual_type") != "experimental_jbeam_mesh":
        return []
    mesh = obj.data
    if obj.mode == "EDIT":
        import bmesh

        edit_mesh = bmesh.from_edit_mesh(mesh)
        edit_mesh.verts.ensure_lookup_table()
        edit_mesh.verts.index_update()
        return [vertex.index for vertex in edit_mesh.verts if vertex.select]
    return [vertex.index for vertex in mesh.vertices if vertex.select]


def selected_experimental_jbeam_edge_indices(obj):
    if obj is None or obj.type != "MESH" or obj.get("beamng_visual_type") != "experimental_jbeam_mesh":
        return []
    mesh = obj.data
    if obj.mode == "EDIT":
        import bmesh

        edit_mesh = bmesh.from_edit_mesh(mesh)
        edit_mesh.edges.ensure_lookup_table()
        edit_mesh.edges.index_update()
        return [edge.index for edge in edit_mesh.edges if edge.select]
    return [edge.index for edge in mesh.edges if edge.select]


def selected_experimental_jbeam_face_indices(obj):
    if obj is None or obj.type != "MESH" or obj.get("beamng_visual_type") != "experimental_jbeam_mesh":
        return []
    mesh = obj.data
    if obj.mode == "EDIT":
        import bmesh

        edit_mesh = bmesh.from_edit_mesh(mesh)
        edit_mesh.faces.ensure_lookup_table()
        edit_mesh.faces.index_update()
        return [face.index for face in edit_mesh.faces if face.select]
    return [polygon.index for polygon in mesh.polygons if polygon.select]


def selected_proxy_node_sources(context, target_obj):
    sources = {}
    target_name = target_obj.name if target_obj else ""
    for obj in getattr(context, "selected_objects", []) or []:
        if obj is None or obj.name == target_name:
            continue
        visual_type = obj.get("beamng_visual_type", "")
        if visual_type in {"selectable_node", "node_label"}:
            node_id = str(obj.get("beamng_node_id", "") or "")
            if node_id:
                sources[node_id] = {
                    "node_id": node_id,
                    "world_position": obj.matrix_world.translation.copy(),
                    "owner_part_id": int(obj.get("beamng_resolved_part_id", -1) or -1),
                    "source_object": obj.name,
                }
        elif obj.type == "MESH" and visual_type == "experimental_jbeam_mesh":
            identity = ensure_experimental_mesh_identity(obj, context.scene, allow_write=False)
            selected_indices = selected_experimental_jbeam_vertex_indices(obj)
            _edit_mesh, positions = read_experimental_mesh_vertices(obj)
            node_ids = identity.get("node_ids", [])
            node_kinds = identity.get("node_kinds", [])
            owner_part_ids = identity.get("owner_part_ids", [])
            for index in selected_indices:
                if index >= len(node_ids) or index >= len(positions):
                    continue
                node_id = str(node_ids[index])
                owner_part_id = owner_part_ids[index] if index < len(owner_part_ids) else obj.get("beamng_resolved_part_id", -1)
                if index < len(node_kinds) and str(node_kinds[index]) != "proxy":
                    owner_part_id = int(obj.get("beamng_resolved_part_id", owner_part_id) or owner_part_id)
                sources[node_id] = {
                    "node_id": node_id,
                    "world_position": obj.matrix_world @ Vector(positions[index]),
                    "owner_part_id": int(owner_part_id) if str(owner_part_id).lstrip("-").isdigit() else -1,
                    "source_object": obj.name,
                }
    return list(sources.values())


def selected_nodes_for_proxy_clipboard(context):
    sources = {}
    candidate_objects = list(getattr(context, "selected_objects", []) or [])
    for obj in (getattr(context, "edit_object", None), getattr(context, "active_object", None)):
        if obj is not None and obj not in candidate_objects:
            candidate_objects.append(obj)
    for obj in candidate_objects:
        if obj is None:
            continue
        visual_type = obj.get("beamng_visual_type", "")
        if visual_type in {"selectable_node", "node_label"}:
            node_id = str(obj.get("beamng_node_id", "") or "")
            if node_id:
                sources[node_id] = {
                    "node_id": node_id,
                    "world_position": rounded_position_list(obj.matrix_world.translation),
                    "owner_part_id": int(obj.get("beamng_resolved_part_id", -1) or -1),
                    "source_object": obj.name,
                }
        elif obj.type == "MESH" and visual_type == "experimental_jbeam_mesh":
            identity = ensure_experimental_mesh_identity(obj, context.scene, allow_write=True)
            selected_indices = selected_experimental_jbeam_vertex_indices(obj)
            _edit_mesh, positions = read_experimental_mesh_vertices(obj)
            node_ids = identity.get("node_ids", [])
            node_kinds = identity.get("node_kinds", [])
            owner_part_ids = identity.get("owner_part_ids", [])
            for index in selected_indices:
                if index >= len(node_ids) or index >= len(positions):
                    continue
                node_id = str(node_ids[index])
                owner_part_id = owner_part_ids[index] if index < len(owner_part_ids) else obj.get("beamng_resolved_part_id", -1)
                if index < len(node_kinds) and str(node_kinds[index]) != "proxy":
                    owner_part_id = int(obj.get("beamng_resolved_part_id", owner_part_id) or owner_part_id)
                sources[node_id] = {
                    "node_id": node_id,
                    "world_position": rounded_position_list(obj.matrix_world @ Vector(positions[index])),
                    "owner_part_id": int(owner_part_id) if str(owner_part_id).lstrip("-").isdigit() else -1,
                    "source_object": obj.name,
                }
    return list(sources.values())


def proxy_clipboard_nodes(scene):
    try:
        nodes = json.loads(scene.get("beamng_proxy_import_clipboard_json", "[]"))
    except (TypeError, json.JSONDecodeError):
        return []
    return nodes if isinstance(nodes, list) else []


def store_proxy_clipboard_nodes(scene, nodes):
    scene["beamng_proxy_import_clipboard_json"] = json.dumps(nodes)
    scene["beamng_proxy_import_clipboard_count"] = len(nodes)


def add_proxy_nodes_to_experimental_mesh(context, target_obj, sources):
    if target_obj is None or target_obj.type != "MESH":
        return {"added": 0, "skipped": 0}
    identity = ensure_experimental_mesh_identity(target_obj, context.scene, allow_write=True)
    existing_node_ids = {str(node_id) for node_id in identity.get("node_ids", [])}
    sources_to_add = [source for source in sources if str(source.get("node_id", "")) not in existing_node_ids]
    if not sources_to_add:
        return {"added": 0, "skipped": len(sources)}

    import bmesh

    mesh = target_obj.data
    was_edit = target_obj.mode == "EDIT"
    local_positions = [target_obj.matrix_world.inverted() @ source["world_position"] for source in sources_to_add]
    added_indices = []
    if was_edit:
        edit_mesh = bmesh.from_edit_mesh(mesh)
        edit_mesh.verts.ensure_lookup_table()
        for vertex in edit_mesh.verts:
            vertex.select = False
        for local_position in local_positions:
            vertex = edit_mesh.verts.new(local_position)
            vertex.select = True
            added_indices.append(vertex)
        edit_mesh.verts.ensure_lookup_table()
        edit_mesh.verts.index_update()
        added_indices = [vertex.index for vertex in added_indices]
        bmesh.update_edit_mesh(mesh, loop_triangles=True, destructive=True)
    else:
        edit_mesh = bmesh.new()
        edit_mesh.from_mesh(mesh)
        edit_mesh.verts.ensure_lookup_table()
        for vertex in edit_mesh.verts:
            vertex.select = False
        for local_position in local_positions:
            vertex = edit_mesh.verts.new(local_position)
            vertex.select = True
            added_indices.append(vertex)
        edit_mesh.verts.ensure_lookup_table()
        edit_mesh.verts.index_update()
        added_indices = [vertex.index for vertex in added_indices]
        edit_mesh.to_mesh(mesh)
        edit_mesh.free()
        mesh.update()

    topology_uids = ensure_experimental_topology_uids(target_obj, allow_write=True)
    node_uids = topology_uids.get("nodes", [])
    uid_to_node_id = mesh_json_dict(mesh, "beamng_node_uid_to_id_json")
    uid_to_kind = mesh_json_dict(mesh, "beamng_node_uid_to_kind_json")
    uid_to_owner = mesh_json_dict(mesh, "beamng_node_uid_to_owner_part_id_json")
    uid_to_baseline = mesh_json_dict(mesh, "beamng_node_uid_to_original_position_json")
    uid_to_generated = mesh_json_dict(mesh, "beamng_node_uid_to_generated_json")
    uid_to_committed = mesh_json_dict(mesh, "beamng_node_uid_to_committed_json")
    uid_to_params = mesh_json_dict(mesh, "beamng_node_uid_to_params_json")
    uid_to_committed_params = mesh_json_dict(mesh, "beamng_node_uid_to_committed_params_json")
    for offset, (index, source) in enumerate(zip(added_indices, sources_to_add)):
        uid_key = topology_uid_key(node_uids[index]) if index < len(node_uids) else ""
        if not uid_key:
            continue
        uid_to_node_id[uid_key] = str(source.get("node_id", ""))
        uid_to_kind[uid_key] = "proxy"
        uid_to_owner[uid_key] = int(source.get("owner_part_id", -1) or -1)
        uid_to_baseline[uid_key] = rounded_position_list(local_positions[offset])
        uid_to_generated[uid_key] = False
        uid_to_committed[uid_key] = True
        uid_to_params[uid_key] = {}
        uid_to_committed_params[uid_key] = {}
    mesh["beamng_node_uid_to_id_json"] = json.dumps(uid_to_node_id)
    mesh["beamng_node_uid_to_kind_json"] = json.dumps(uid_to_kind)
    mesh["beamng_node_uid_to_owner_part_id_json"] = json.dumps(uid_to_owner)
    mesh["beamng_node_uid_to_original_position_json"] = json.dumps(uid_to_baseline)
    mesh["beamng_node_uid_to_generated_json"] = json.dumps(uid_to_generated)
    mesh["beamng_node_uid_to_committed_json"] = json.dumps(uid_to_committed)
    mesh["beamng_node_uid_to_params_json"] = json.dumps(uid_to_params)
    mesh["beamng_node_uid_to_committed_params_json"] = json.dumps(uid_to_committed_params)
    ensure_experimental_mesh_identity(target_obj, context.scene, allow_write=True)
    return {"added": len(sources_to_add), "skipped": len(sources) - len(sources_to_add)}


def experimental_node_index_by_id(obj, node_id, allowed_kinds=None):
    identity = ensure_experimental_mesh_identity(obj, bpy.context.scene, allow_write=False)
    node_id = str(node_id)
    node_ids = [str(value) for value in identity.get("node_ids", [])]
    node_kinds = [str(value or "owned") for value in identity.get("node_kinds", [])]
    for index, candidate in enumerate(node_ids):
        if candidate != node_id:
            continue
        if allowed_kinds and (index >= len(node_kinds) or node_kinds[index] not in allowed_kinds):
            continue
        return index
    return -1


def create_or_mark_jbeam_beam_between_indices(obj, scene, index_a, index_b):
    if index_a < 0 or index_b < 0 or index_a == index_b:
        return {"created_edge": False, "marked": False, "key": ()}
    identity = ensure_experimental_mesh_identity(obj, scene, allow_write=True)
    node_ids = identity.get("node_ids", [])
    if index_a >= len(node_ids) or index_b >= len(node_ids):
        return {"created_edge": False, "marked": False, "key": ()}
    import bmesh

    mesh = obj.data
    created_edge = False
    if obj.mode == "EDIT":
        edit_mesh = bmesh.from_edit_mesh(mesh)
        edit_mesh.verts.ensure_lookup_table()
        edit_mesh.edges.ensure_lookup_table()
        verts = [edit_mesh.verts[index_a], edit_mesh.verts[index_b]]
        try:
            edge = edit_mesh.edges.new(verts)
            created_edge = True
        except ValueError:
            edge = None
        for vertex in edit_mesh.verts:
            vertex.select = False
        for vertex in verts:
            vertex.select = True
        if edge is not None:
            edge.select = True
        bmesh.update_edit_mesh(mesh, loop_triangles=True, destructive=True)
    else:
        edit_mesh = bmesh.new()
        edit_mesh.from_mesh(mesh)
        edit_mesh.verts.ensure_lookup_table()
        edit_mesh.edges.ensure_lookup_table()
        verts = [edit_mesh.verts[index_a], edit_mesh.verts[index_b]]
        try:
            edit_mesh.edges.new(verts)
            created_edge = True
        except ValueError:
            pass
        edit_mesh.to_mesh(mesh)
        edit_mesh.free()
        mesh.update()

    key = edge_key((node_ids[index_a], node_ids[index_b]))
    existing = {
        tuple(str(item) for item in item_key)
        for item_key in mesh_json_list(mesh, "beamng_explicit_beam_edge_keys_json")
        if isinstance(item_key, (list, tuple)) and len(item_key) >= 2
    }
    before_count = len(existing)
    existing.add(key)
    mesh["beamng_explicit_beam_edge_keys_json"] = json.dumps([list(item_key) for item_key in sorted(existing)])
    ensure_experimental_mesh_identity(obj, scene, allow_write=True)
    return {"created_edge": created_edge, "marked": len(existing) != before_count, "key": key}


def remove_proxy_vertices_by_node_ids(obj, scene, node_ids):
    remove_ids = {str(node_id) for node_id in node_ids if str(node_id)}
    if not remove_ids:
        return 0
    identity = ensure_experimental_mesh_identity(obj, scene, allow_write=True)
    current_node_ids = [str(node_id) for node_id in identity.get("node_ids", [])]
    current_node_kinds = [str(kind or "owned") for kind in identity.get("node_kinds", [])]
    indices = {
        index
        for index, (node_id, kind) in enumerate(zip(current_node_ids, current_node_kinds))
        if node_id in remove_ids and kind == "proxy"
    }
    if not indices:
        return 0
    import bmesh

    mesh = obj.data
    if obj.mode == "EDIT":
        bm = bmesh.from_edit_mesh(mesh)
        bm.verts.ensure_lookup_table()
        bm.verts.index_update()
        verts = [vertex for vertex in bm.verts if vertex.index in indices]
        bmesh.ops.delete(bm, geom=verts, context="VERTS")
        bmesh.update_edit_mesh(mesh, loop_triangles=True, destructive=True)
    else:
        bm = bmesh.new()
        bm.from_mesh(mesh)
        bm.verts.ensure_lookup_table()
        bm.verts.index_update()
        verts = [vertex for vertex in bm.verts if vertex.index in indices]
        bmesh.ops.delete(bm, geom=verts, context="VERTS")
        bm.to_mesh(mesh)
        bm.free()
        mesh.update()
    ensure_experimental_mesh_identity(obj, scene, allow_write=True)
    semantic_topology_snapshot_for_object(obj, scene, allow_write=True)
    obj["beamng_proxy_node_count"] = sum(
        1
        for kind in ensure_experimental_mesh_identity(obj, scene, allow_write=False).get("node_kinds", [])
        if str(kind) == "proxy"
    )
    return len(indices)


def mesh_json_list(mesh, key, fallback=None):
    fallback = [] if fallback is None else fallback
    try:
        value = json.loads(mesh.get(key, "[]"))
    except (TypeError, json.JSONDecodeError):
        return list(fallback)
    return value if isinstance(value, list) else list(fallback)


def mesh_json_dict(mesh, key, fallback=None):
    fallback = {} if fallback is None else fallback
    try:
        value = json.loads(mesh.get(key, "{}"))
    except (TypeError, json.JSONDecodeError):
        return dict(fallback)
    return value if isinstance(value, dict) else dict(fallback)


JBEAM_NODE_UID_ATTR = "beamng_node_uid"
JBEAM_EDGE_UID_ATTR = "beamng_edge_uid"
JBEAM_FACE_UID_ATTR = "beamng_face_uid"
JBEAM_NODE_OWNER_PART_ATTR = "beamng_node_owner_part_id"
JBEAM_NODE_IS_PROXY_ATTR = "beamng_node_is_proxy"
JBEAM_EDGE_SEMANTIC_ATTR = "beamng_edge_semantic_type"
JBEAM_FACE_SEMANTIC_ATTR = "beamng_face_semantic_type"
JBEAM_EDGE_SEMANTIC_BEAM = "beam"
JBEAM_EDGE_SEMANTIC_TRIANGLE_BOUNDARY = "triangle_boundary"
JBEAM_EDGE_SEMANTIC_RELATIONSHIP = "relationship"
JBEAM_FACE_SEMANTIC_TRIANGLE = "triangle"
JBEAM_FACE_SEMANTIC_INVALID = "invalid_face"
JBEAM_EDGE_SEMANTIC_CODES = {
    JBEAM_EDGE_SEMANTIC_BEAM: 1,
    JBEAM_EDGE_SEMANTIC_TRIANGLE_BOUNDARY: 2,
    JBEAM_EDGE_SEMANTIC_RELATIONSHIP: 3,
}
JBEAM_FACE_SEMANTIC_CODES = {
    JBEAM_FACE_SEMANTIC_TRIANGLE: 1,
    JBEAM_FACE_SEMANTIC_INVALID: 2,
}
_jbeam_proxy_overlay_draw_handle = None


def new_mesh_topology_guid(used_uids):
    uid = str(uuid.uuid4())
    while uid in used_uids:
        uid = str(uuid.uuid4())
    used_uids.add(uid)
    return uid


def new_mesh_legacy_topology_uid(mesh, used_uids):
    next_uid = int(mesh.get("beamng_next_topology_uid", 1) or 1)
    while str(next_uid) in used_uids or next_uid <= 0:
        next_uid += 1
    mesh["beamng_next_topology_uid"] = next_uid + 1
    uid = str(next_uid)
    used_uids.add(uid)
    return uid


def topology_uid_key(uid):
    value = str(uid or "").strip()
    if value in {"", "0", "0.0", "None", "none", "null"}:
        return ""
    return value


def topology_uid_is_valid(uid):
    return bool(topology_uid_key(uid))


def topology_uid_sort_key(uid):
    value = topology_uid_key(uid)
    return (0, int(value)) if value.isdigit() else (1, value)


def bmesh_string_value(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore").strip("\x00")
    return str(value or "").strip("\x00")


def set_bmesh_string_value(element, layer, value):
    text = str(value or "")
    try:
        element[layer] = text.encode("utf-8")
    except TypeError:
        element[layer] = text


def object_mode_string_attribute_values(mesh, attr_name, domain, count, allow_write=True):
    if not hasattr(mesh, "attributes"):
        return ["" for _index in range(count)]
    attr = mesh.attributes.get(attr_name)
    if attr is not None and getattr(attr, "data_type", "") != "STRING" and allow_write:
        try:
            mesh.attributes.remove(attr)
            attr = None
        except Exception:
            pass
    if attr is None:
        if not allow_write:
            return ["" for _index in range(count)]
        try:
            attr = mesh.attributes.new(attr_name, "STRING", domain)
        except Exception:
            return [str(value) if value else "" for value in object_mode_int_attribute_values(mesh, attr_name, domain, count, allow_write)]
    values = []
    for index in range(count):
        if index >= len(attr.data):
            values.append("")
            continue
        values.append(topology_uid_key(getattr(attr.data[index], "value", "")))
    return values


def set_object_mode_string_attribute_values(mesh, attr_name, domain, values):
    if not hasattr(mesh, "attributes"):
        return
    attr = mesh.attributes.get(attr_name)
    if attr is not None and getattr(attr, "data_type", "") != "STRING":
        try:
            mesh.attributes.remove(attr)
            attr = None
        except Exception:
            return
    if attr is None:
        try:
            attr = mesh.attributes.new(attr_name, "STRING", domain)
        except Exception:
            return
    for index, value in enumerate(values):
        if index < len(attr.data):
            attr.data[index].value = str(value or "")


def object_mode_int_attribute_values(mesh, attr_name, domain, count, allow_write=True):
    attr = mesh.attributes.get(attr_name) if hasattr(mesh, "attributes") else None
    if attr is None:
        if not allow_write or not hasattr(mesh, "attributes"):
            return [0 for _index in range(count)]
        attr = mesh.attributes.new(attr_name, "INT", domain)
    values = []
    for index in range(count):
        values.append(int(attr.data[index].value) if index < len(attr.data) else 0)
    return values


def set_object_mode_int_attribute_values(mesh, attr_name, domain, values):
    if not hasattr(mesh, "attributes"):
        return
    attr = mesh.attributes.get(attr_name)
    if attr is None:
        attr = mesh.attributes.new(attr_name, "INT", domain)
    for index, value in enumerate(values):
        if index < len(attr.data):
            attr.data[index].value = int(value)


def set_point_int_attribute_values(obj, attr_name, values):
    set_element_int_attribute_values(obj, attr_name, "POINT", values)


def set_element_int_attribute_values(obj, attr_name, domain, values):
    mesh = obj.data
    if obj.mode == "EDIT":
        import bmesh

        edit_mesh = bmesh.from_edit_mesh(mesh)
        collection = {
            "POINT": edit_mesh.verts,
            "EDGE": edit_mesh.edges,
            "FACE": edit_mesh.faces,
        }.get(domain, edit_mesh.verts)
        collection.ensure_lookup_table()
        layer = collection.layers.int.get(attr_name)
        if layer is None:
            layer = collection.layers.int.new(attr_name)
        for index, value in enumerate(values):
            if index < len(collection):
                collection[index][layer] = int(value)
        bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
        return
    set_object_mode_int_attribute_values(mesh, attr_name, domain, values)


def ensure_experimental_topology_uids(obj, allow_write=True):
    mesh = obj.data
    if obj.mode == "EDIT":
        import bmesh

        edit_mesh = bmesh.from_edit_mesh(mesh)
        edit_mesh.verts.ensure_lookup_table()
        edit_mesh.verts.index_update()
        edit_mesh.edges.ensure_lookup_table()
        edit_mesh.edges.index_update()
        edit_mesh.faces.ensure_lookup_table()
        edit_mesh.faces.index_update()

        layers = {}
        layer_kinds = {}
        for domain, collection, attr_name in (
            ("verts", edit_mesh.verts, JBEAM_NODE_UID_ATTR),
            ("edges", edit_mesh.edges, JBEAM_EDGE_UID_ATTR),
            ("faces", edit_mesh.faces, JBEAM_FACE_UID_ATTR),
        ):
            string_layers = getattr(collection.layers, "string", None)
            int_layers = getattr(collection.layers, "int", None)
            layer = string_layers.get(attr_name) if string_layers is not None else None
            kind = "string" if layer is not None else ""
            if layer is None and string_layers is not None and allow_write:
                layer = string_layers.new(attr_name)
                kind = "string"
            if layer is None and int_layers is not None:
                layer = int_layers.get(attr_name)
                kind = "int" if layer is not None else ""
            layers[domain] = layer
            layer_kinds[domain] = kind

        results = {}
        global_used = set()
        changed = False
        for domain, elements in (("verts", edit_mesh.verts), ("edges", edit_mesh.edges), ("faces", edit_mesh.faces)):
            layer = layers[domain]
            layer_kind = layer_kinds.get(domain, "")
            values = []
            for element in elements:
                if layer is None:
                    uid = ""
                elif layer_kind == "string":
                    uid = topology_uid_key(bmesh_string_value(element[layer]))
                else:
                    uid = topology_uid_key(element[layer])
                if allow_write and (not uid or uid in global_used):
                    uid = new_mesh_topology_guid(global_used) if layer_kind == "string" else new_mesh_legacy_topology_uid(mesh, global_used)
                    if layer is not None:
                        if layer_kind == "string":
                            set_bmesh_string_value(element, layer, uid)
                        else:
                            element[layer] = int(uid)
                    changed = True
                elif uid:
                    global_used.add(uid)
                values.append(uid)
            results[domain] = values
        if changed:
            bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
        return {
            "nodes": results["verts"],
            "edges": results["edges"],
            "faces": results["faces"],
        }

    counts = {
        "nodes": len(mesh.vertices),
        "edges": len(mesh.edges),
        "faces": len(mesh.polygons),
    }
    values_by_domain = {
        "nodes": object_mode_string_attribute_values(mesh, JBEAM_NODE_UID_ATTR, "POINT", counts["nodes"], allow_write),
        "edges": object_mode_string_attribute_values(mesh, JBEAM_EDGE_UID_ATTR, "EDGE", counts["edges"], allow_write),
        "faces": object_mode_string_attribute_values(mesh, JBEAM_FACE_UID_ATTR, "FACE", counts["faces"], allow_write),
    }
    if allow_write:
        global_used = set()
        for key, attr_name, domain in (
            ("nodes", JBEAM_NODE_UID_ATTR, "POINT"),
            ("edges", JBEAM_EDGE_UID_ATTR, "EDGE"),
            ("faces", JBEAM_FACE_UID_ATTR, "FACE"),
        ):
            changed = False
            values = list(values_by_domain[key])
            for index, uid in enumerate(values):
                uid = topology_uid_key(uid)
                if not uid or uid in global_used:
                    values[index] = new_mesh_topology_guid(global_used)
                    changed = True
                else:
                    global_used.add(uid)
                    values[index] = uid
            if changed:
                set_object_mode_string_attribute_values(mesh, attr_name, domain, values)
            values_by_domain[key] = values
    return values_by_domain


def bootstrap_uid_map_from_array(mesh, map_key, uid_values, array_values):
    uid_map = mesh_json_dict(mesh, map_key)
    changed = False
    current_uid_keys = {topology_uid_key(uid) for uid in uid_values if topology_uid_key(uid)}
    if (
        uid_map
        and current_uid_keys
        and not (set(uid_map) & current_uid_keys)
        and len(array_values) == len(uid_values)
    ):
        # Blender can lose or defer custom attribute data during import/reload. If all
        # current UIDs are freshly rebuilt, old UID-map entries are stale, not deletes.
        uid_map = {}
        changed = True
    for index, uid in enumerate(uid_values):
        key = topology_uid_key(uid)
        if not key or index >= len(array_values):
            continue
        if key not in uid_map:
            uid_map[key] = array_values[index]
            changed = True
    if changed:
        mesh[map_key] = json.dumps(uid_map)
    return uid_map


def topology_params_for_current_elements(mesh, uid_values, array_key, map_key, allow_write=True):
    array_values = mesh_json_list(mesh, array_key)
    uid_map = bootstrap_uid_map_from_array(mesh, map_key, uid_values, array_values) if allow_write else mesh_json_dict(mesh, map_key)
    params = []
    changed = False
    for index, uid in enumerate(uid_values):
        uid_key = topology_uid_key(uid)
        if not uid_key:
            params.append({})
            continue
        value = uid_map.get(uid_key)
        if value is None:
            value = array_values[index] if index < len(array_values) and isinstance(array_values[index], dict) else {}
            uid_map[uid_key] = value
            changed = True
        params.append(value if isinstance(value, dict) else {})
    if allow_write:
        if changed:
            mesh[map_key] = json.dumps(uid_map)
        mesh[array_key] = json.dumps(params)
    return params


def prune_uid_keyed_map(mesh, key, live_uids):
    data = mesh_json_dict(mesh, key)
    live = {topology_uid_key(uid) for uid in live_uids if topology_uid_key(uid)}
    pruned = {str(uid): value for uid, value in data.items() if str(uid) in live}
    if pruned != data:
        mesh[key] = json.dumps(pruned)
    return len(data) - len(pruned)


def repair_experimental_jbeam_semantic_topology(scene, active_only=False):
    repaired_mesh_count = 0
    pruned_entry_count = 0
    revision_count = 0
    for obj in experimental_jbeam_mesh_objects(scene, active_only=active_only):
        mesh = obj.data
        ensure_experimental_mesh_identity(obj, scene, allow_write=True)
        topology_uids = ensure_experimental_topology_uids(obj, allow_write=True)
        for key, live_uids in (
            ("beamng_node_uid_to_id_json", topology_uids.get("nodes", [])),
            ("beamng_node_uid_to_kind_json", topology_uids.get("nodes", [])),
            ("beamng_node_uid_to_owner_part_id_json", topology_uids.get("nodes", [])),
            ("beamng_node_uid_to_original_position_json", topology_uids.get("nodes", [])),
            ("beamng_node_uid_to_generated_json", topology_uids.get("nodes", [])),
            ("beamng_node_uid_to_committed_json", topology_uids.get("nodes", [])),
            ("beamng_node_uid_to_params_json", topology_uids.get("nodes", [])),
            ("beamng_node_uid_to_committed_params_json", topology_uids.get("nodes", [])),
            ("beamng_edge_uid_to_params_json", topology_uids.get("edges", [])),
            ("beamng_edge_uid_to_committed_params_json", topology_uids.get("edges", [])),
            ("beamng_edge_uid_to_semantic_type_json", topology_uids.get("edges", [])),
            ("beamng_edge_uid_to_semantic_state_json", topology_uids.get("edges", [])),
            ("beamng_face_uid_to_params_json", topology_uids.get("faces", [])),
            ("beamng_face_uid_to_committed_params_json", topology_uids.get("faces", [])),
            ("beamng_face_uid_to_semantic_type_json", topology_uids.get("faces", [])),
            ("beamng_face_uid_to_semantic_state_json", topology_uids.get("faces", [])),
        ):
            pruned_entry_count += prune_uid_keyed_map(mesh, key, live_uids)
        before_revision = int(mesh.get("beamng_topology_revision", 0) or 0)
        semantic_topology_snapshot_for_object(obj, scene, allow_write=True)
        after_revision = int(mesh.get("beamng_topology_revision", 0) or 0)
        if after_revision != before_revision:
            revision_count += 1
        repaired_mesh_count += 1
    return {
        "repaired_mesh_count": repaired_mesh_count,
        "pruned_entry_count": pruned_entry_count,
        "revision_count": revision_count,
    }


def default_new_jbeam_node_params(scene):
    params = {}
    node_weight = str(getattr(scene, "beamng_jbeam_node_weight", "") or "").strip()
    node_material = str(getattr(scene, "beamng_jbeam_node_material", "") or "").strip()
    node_group = str(getattr(scene, "beamng_jbeam_node_group", "") or "").strip()
    friction = str(getattr(scene, "beamng_jbeam_node_friction", "") or "").strip()
    if node_weight:
        params["nodeWeight"] = node_weight
    if node_material:
        params["nodeMaterial"] = node_material
    if node_group:
        params["group"] = node_group
    if friction:
        params["frictionCoef"] = friction
    if getattr(scene, "beamng_jbeam_node_collision_override", False):
        params["collision"] = bool(getattr(scene, "beamng_jbeam_node_collision", True))
    if getattr(scene, "beamng_jbeam_node_self_collision_override", False):
        params["selfCollision"] = bool(getattr(scene, "beamng_jbeam_node_self_collision", False))
    return params


def ensure_experimental_mesh_identity(obj, scene=None, allow_write=True):
    if obj is None or obj.type != "MESH" or obj.get("beamng_visual_type") != "experimental_jbeam_mesh":
        return {}

    mesh = obj.data
    _edit_mesh, current_positions = read_experimental_mesh_vertices(obj)
    vertex_count = len(current_positions)
    topology_uids = ensure_experimental_topology_uids(obj, allow_write=allow_write)
    node_uids = topology_uids.get("nodes", [])
    old_node_ids = [str(node_id) for node_id in mesh_json_list(mesh, "beamng_node_ids_json")]
    old_node_kinds = [str(kind or "owned") for kind in mesh_json_list(mesh, "beamng_node_kinds_json")]
    old_owner_part_ids = mesh_json_list(mesh, "beamng_node_owner_part_ids_json")
    old_original_positions = mesh_json_list(mesh, "beamng_original_node_positions_json")
    old_generated_flags = [bool(value) for value in mesh_json_list(mesh, "beamng_node_generated_flags_json")]
    old_committed_flags = [bool(value) for value in mesh_json_list(mesh, "beamng_node_committed_flags_json")]
    old_node_params = mesh_json_list(mesh, "beamng_node_params_json")
    old_committed_node_params = mesh_json_list(mesh, "beamng_node_committed_params_json")

    uid_to_node_id = bootstrap_uid_map_from_array(mesh, "beamng_node_uid_to_id_json", node_uids, old_node_ids) if allow_write else mesh_json_dict(mesh, "beamng_node_uid_to_id_json")
    uid_to_kind = bootstrap_uid_map_from_array(mesh, "beamng_node_uid_to_kind_json", node_uids, old_node_kinds) if allow_write else mesh_json_dict(mesh, "beamng_node_uid_to_kind_json")
    uid_to_owner = bootstrap_uid_map_from_array(mesh, "beamng_node_uid_to_owner_part_id_json", node_uids, old_owner_part_ids) if allow_write else mesh_json_dict(mesh, "beamng_node_uid_to_owner_part_id_json")
    uid_to_baseline = bootstrap_uid_map_from_array(mesh, "beamng_node_uid_to_original_position_json", node_uids, old_original_positions) if allow_write else mesh_json_dict(mesh, "beamng_node_uid_to_original_position_json")
    uid_to_generated = bootstrap_uid_map_from_array(mesh, "beamng_node_uid_to_generated_json", node_uids, old_generated_flags) if allow_write else mesh_json_dict(mesh, "beamng_node_uid_to_generated_json")
    uid_to_committed = bootstrap_uid_map_from_array(mesh, "beamng_node_uid_to_committed_json", node_uids, old_committed_flags) if allow_write else mesh_json_dict(mesh, "beamng_node_uid_to_committed_json")
    uid_to_params = bootstrap_uid_map_from_array(mesh, "beamng_node_uid_to_params_json", node_uids, old_node_params) if allow_write else mesh_json_dict(mesh, "beamng_node_uid_to_params_json")
    uid_to_committed_params = bootstrap_uid_map_from_array(mesh, "beamng_node_uid_to_committed_params_json", node_uids, old_committed_node_params) if allow_write else mesh_json_dict(mesh, "beamng_node_uid_to_committed_params_json")

    node_ids = []
    node_kinds = []
    owner_part_ids = []
    original_positions = []
    generated_flags = []
    committed_flags = []
    node_params = []
    committed_node_params = []

    existing_ids = {str(value) for value in uid_to_node_id.values()}
    resolved_part_id = int(obj.get("beamng_resolved_part_id", -1))
    default_params = default_new_jbeam_node_params(scene) if scene is not None else {}
    uid_maps_changed = False
    for index in range(vertex_count):
        uid = node_uids[index] if index < len(node_uids) else 0
        uid_key = topology_uid_key(uid)
        if not uid_key:
            continue
        if uid_key not in uid_to_node_id:
            node_id = generated_jbeam_node_id(obj.get("beamng_part_name", ""), index, existing_ids)
            existing_ids.add(node_id)
            uid_to_node_id[uid_key] = node_id
            uid_to_kind[uid_key] = "owned"
            uid_to_owner[uid_key] = resolved_part_id
            uid_to_baseline[uid_key] = rounded_position_list(current_positions[index])
            uid_to_generated[uid_key] = True
            uid_to_committed[uid_key] = False
            uid_to_params[uid_key] = dict(default_params)
            uid_to_committed_params[uid_key] = {}
            uid_maps_changed = True

        node_ids.append(str(uid_to_node_id.get(uid_key, "")))
        node_kinds.append(str(uid_to_kind.get(uid_key, "owned") or "owned"))
        owner_part_ids.append(uid_to_owner.get(uid_key, resolved_part_id))
        original_positions.append(uid_to_baseline.get(uid_key, rounded_position_list(current_positions[index])))
        generated_flags.append(bool(uid_to_generated.get(uid_key, False)))
        committed_flags.append(bool(uid_to_committed.get(uid_key, True)))
        params = uid_to_params.get(uid_key, {})
        node_params.append(params if isinstance(params, dict) else {})
        committed_params = uid_to_committed_params.get(uid_key, {})
        committed_node_params.append(committed_params if isinstance(committed_params, dict) else {})

    if allow_write:
        if uid_maps_changed:
            mesh["beamng_node_uid_to_id_json"] = json.dumps(uid_to_node_id)
            mesh["beamng_node_uid_to_kind_json"] = json.dumps(uid_to_kind)
            mesh["beamng_node_uid_to_owner_part_id_json"] = json.dumps(uid_to_owner)
            mesh["beamng_node_uid_to_original_position_json"] = json.dumps(uid_to_baseline)
            mesh["beamng_node_uid_to_generated_json"] = json.dumps(uid_to_generated)
            mesh["beamng_node_uid_to_committed_json"] = json.dumps(uid_to_committed)
            mesh["beamng_node_uid_to_params_json"] = json.dumps(uid_to_params)
            mesh["beamng_node_uid_to_committed_params_json"] = json.dumps(uid_to_committed_params)
        mesh["beamng_node_ids_json"] = json.dumps(node_ids)
        mesh["beamng_node_kinds_json"] = json.dumps(node_kinds)
        mesh["beamng_node_owner_part_ids_json"] = json.dumps(owner_part_ids)
        mesh["beamng_original_node_positions_json"] = json.dumps(original_positions)
        mesh["beamng_node_generated_flags_json"] = json.dumps(generated_flags)
        mesh["beamng_node_committed_flags_json"] = json.dumps(committed_flags)
        mesh["beamng_node_params_json"] = json.dumps(node_params)
        mesh["beamng_node_committed_params_json"] = json.dumps(committed_node_params)
        set_point_int_attribute_values(
            obj,
            JBEAM_NODE_OWNER_PART_ATTR,
            [int(value) if str(value).lstrip("-").isdigit() else -1 for value in owner_part_ids],
        )
        set_point_int_attribute_values(
            obj,
            JBEAM_NODE_IS_PROXY_ATTR,
            [1 if str(kind) == "proxy" else 0 for kind in node_kinds],
        )
    return {
        "node_ids": node_ids,
        "node_kinds": node_kinds,
        "owner_part_ids": owner_part_ids,
        "original_positions": original_positions,
        "generated_flags": generated_flags,
        "committed_flags": committed_flags,
        "node_params": node_params,
        "committed_node_params": committed_node_params,
        "current_positions": current_positions,
    }


def selected_experimental_jbeam_edge_node_keys(obj):
    identity = ensure_experimental_mesh_identity(obj)
    node_ids = identity.get("node_ids", [])
    keys = []
    if not node_ids:
        return keys
    mesh = obj.data
    if obj.mode == "EDIT":
        import bmesh

        edit_mesh = bmesh.from_edit_mesh(mesh)
        edit_mesh.verts.ensure_lookup_table()
        edit_mesh.verts.index_update()
        edit_mesh.edges.ensure_lookup_table()
        for edge in edit_mesh.edges:
            if not edge.select:
                continue
            indices = [vertex.index for vertex in edge.verts]
            if len(indices) == 2 and all(0 <= index < len(node_ids) for index in indices):
                keys.append(edge_key((node_ids[indices[0]], node_ids[indices[1]])))
    else:
        for edge in mesh.edges:
            if not edge.select:
                continue
            indices = list(edge.vertices)
            if len(indices) == 2 and all(0 <= index < len(node_ids) for index in indices):
                keys.append(edge_key((node_ids[indices[0]], node_ids[indices[1]])))
    return keys


def authoring_model_for_context(context):
    try:
        return current_authoring_model(context.scene)
    except Exception:
        return None


def model_node_for_id(context, node_id):
    model = authoring_model_for_context(context)
    return model.node_index().get(str(node_id)) if model is not None else None


def model_beam_for_ids(context, id1, id2):
    model = authoring_model_for_context(context)
    return model.beam_index().get(edge_key((id1, id2))) if model is not None else None


def model_triangle_for_ids(context, id1, id2, id3):
    model = authoring_model_for_context(context)
    return model.triangle_index().get(face_key((id1, id2, id3))) if model is not None else None


def experimental_jbeam_node_info_for_selection(context, limit=6):
    obj = active_experimental_jbeam_mesh(context)
    if obj is None or obj.type != "MESH" or obj.get("beamng_visual_type") != "experimental_jbeam_mesh":
        return []
    mesh = obj.data
    identity = ensure_experimental_mesh_identity(obj, context.scene, allow_write=False)
    if not identity:
        return []
    node_ids = identity["node_ids"]
    node_kinds = identity["node_kinds"]
    owner_part_ids = identity["owner_part_ids"]
    baseline_positions = identity["original_positions"]
    generated_flags = identity["generated_flags"]
    committed_flags = identity["committed_flags"]
    node_params = identity["node_params"]
    committed_node_params = identity["committed_node_params"]
    topology_uids = ensure_experimental_topology_uids(obj, allow_write=False)
    node_uids = topology_uids.get("nodes", [])
    pending_changes = mesh_json_list(mesh, "beamng_node_move_changes_json")

    selected_indices = selected_experimental_jbeam_vertex_indices(obj)
    if not selected_indices:
        return []

    _edit_mesh, current_positions = read_experimental_mesh_vertices(obj)
    pending_by_index = {
        int(change.get("vertex_index", -1)): change
        for change in pending_changes
        if str(change.get("vertex_index", "")).lstrip("-").isdigit()
    }
    accepted_by_node = {}
    for operation in jbeam_operation_history(context.scene):
        if operation.get("source_object") == obj.name and operation.get("field") == "position":
            accepted_by_node[str(operation.get("row", ""))] = operation

    infos = []
    for vertex_index in selected_indices[:limit]:
        if vertex_index >= len(node_ids) or vertex_index >= len(current_positions):
            continue
        node_id = str(node_ids[vertex_index])
        kind = node_kinds[vertex_index] if vertex_index < len(node_kinds) else ""
        owner_part_id = owner_part_ids[vertex_index] if vertex_index < len(owner_part_ids) else -1
        baseline = baseline_positions[vertex_index] if vertex_index < len(baseline_positions) else []
        current = rounded_position_list(current_positions[vertex_index])
        pending = pending_by_index.get(vertex_index)
        accepted = accepted_by_node.get(node_id)
        model = authoring_model_for_context(context)
        model_node = model.node_index().get(node_id) if model is not None else None
        model_refs = model.refs_for_node(node_id) if model is not None else {"beams": [], "triangles": []}
        model_params = dict(getattr(model_node, "options", {}) or {}) if model_node is not None else {}
        infos.append(
            {
                "node_id": node_id,
                "topology_uid": topology_uid_key(node_uids[vertex_index]) if vertex_index < len(node_uids) else "",
                "kind": kind,
                "owner_part_id": owner_part_id,
                "vertex_index": vertex_index,
                "current_position": current,
                "baseline_position": rounded_position_list(baseline) if len(baseline) == 3 else [],
                "pending_position": pending.get("new") if pending else [],
                "accepted_position": accepted.get("new") if accepted else [],
                "generated": bool(generated_flags[vertex_index]) if vertex_index < len(generated_flags) else False,
                "committed": bool(committed_flags[vertex_index]) if vertex_index < len(committed_flags) else True,
                "params": model_params or (node_params[vertex_index] if vertex_index < len(node_params) and isinstance(node_params[vertex_index], dict) else {}),
                "committed_params": committed_node_params[vertex_index] if vertex_index < len(committed_node_params) and isinstance(committed_node_params[vertex_index], dict) else {},
                "model_backed": model_node is not None,
                "model_reference_beam_count": len(model_refs.get("beams", [])),
                "model_reference_triangle_count": len(model_refs.get("triangles", [])),
                "source_file": obj.get("beamng_jbeam_path", ""),
                "part": obj.get("beamng_part_name", ""),
            }
        )
    return infos


def experimental_jbeam_edge_info_for_selection(context, limit=6):
    obj = active_experimental_jbeam_mesh(context)
    if obj is None or obj.type != "MESH" or obj.get("beamng_visual_type") != "experimental_jbeam_mesh":
        return []
    edge_node_ids, _faces = read_experimental_mesh_topology(obj, allow_identity_write=False)
    topology_uids = ensure_experimental_topology_uids(obj, allow_write=False)
    edge_uids = topology_uids.get("edges", [])
    semantic_snapshot = semantic_topology_snapshot_for_object(obj, context.scene, allow_write=False)
    semantic_by_key = semantic_edge_types_by_key(semantic_snapshot)
    edge_params = topology_params_for_current_elements(obj.data, edge_uids, "beamng_edge_params_json", "beamng_edge_uid_to_params_json", allow_write=False)
    committed_edge_params = topology_params_for_current_elements(obj.data, edge_uids, "beamng_edge_committed_params_json", "beamng_edge_uid_to_committed_params_json", allow_write=False)
    infos = []
    for edge_index in selected_experimental_jbeam_edge_indices(obj)[:limit]:
        if edge_index >= len(edge_node_ids):
            continue
        ids = edge_node_ids[edge_index]
        if not isinstance(ids, (list, tuple)) or len(ids) < 2:
            continue
        model_beam = model_beam_for_ids(context, ids[0], ids[1])
        model_params = dict(getattr(model_beam, "options", {}) or {}) if model_beam is not None else {}
        infos.append(
            {
                "edge_index": edge_index,
                "topology_uid": topology_uid_key(edge_uids[edge_index]) if edge_index < len(edge_uids) else "",
                "id1": str(ids[0]),
                "id2": str(ids[1]),
                "semantic_type": semantic_by_key.get(edge_key(ids), JBEAM_EDGE_SEMANTIC_RELATIONSHIP),
                "params": model_params or (edge_params[edge_index] if edge_index < len(edge_params) and isinstance(edge_params[edge_index], dict) else {}),
                "committed_params": committed_edge_params[edge_index] if edge_index < len(committed_edge_params) and isinstance(committed_edge_params[edge_index], dict) else {},
                "model_backed": model_beam is not None,
                "part": obj.get("beamng_part_name", ""),
                "source_file": obj.get("beamng_jbeam_path", ""),
            }
        )
    return infos


def experimental_jbeam_face_info_for_selection(context, limit=6):
    obj = active_experimental_jbeam_mesh(context)
    if obj is None or obj.type != "MESH" or obj.get("beamng_visual_type") != "experimental_jbeam_mesh":
        return []
    _edges, face_node_ids = read_experimental_mesh_topology(obj, allow_identity_write=False)
    topology_uids = ensure_experimental_topology_uids(obj, allow_write=False)
    face_uids = topology_uids.get("faces", [])
    semantic_snapshot = semantic_topology_snapshot_for_object(obj, context.scene, allow_write=False)
    face_semantics_by_index = {
        int(item.get("index", -1)): item
        for item in semantic_snapshot.get("faces", [])
        if isinstance(item, dict)
    } if isinstance(semantic_snapshot, dict) else {}
    face_params = topology_params_for_current_elements(obj.data, face_uids, "beamng_face_params_json", "beamng_face_uid_to_params_json", allow_write=False)
    committed_face_params = topology_params_for_current_elements(obj.data, face_uids, "beamng_face_committed_params_json", "beamng_face_uid_to_committed_params_json", allow_write=False)
    face_normals = []
    if obj.mode == "EDIT":
        import bmesh

        edit_mesh = bmesh.from_edit_mesh(obj.data)
        edit_mesh.faces.ensure_lookup_table()
        face_normals = [tuple(round(float(value), 3) for value in face.normal[:]) for face in edit_mesh.faces]
    else:
        face_normals = [tuple(round(float(value), 3) for value in polygon.normal[:]) for polygon in obj.data.polygons]
    infos = []
    for face_index in selected_experimental_jbeam_face_indices(obj)[:limit]:
        if face_index >= len(face_node_ids):
            continue
        ids = face_node_ids[face_index]
        if not isinstance(ids, (list, tuple)) or len(ids) < 3:
            continue
        model_triangle = model_triangle_for_ids(context, ids[0], ids[1], ids[2])
        model_params = dict(getattr(model_triangle, "options", {}) or {}) if model_triangle is not None else {}
        infos.append(
            {
                "face_index": face_index,
                "topology_uid": topology_uid_key(face_uids[face_index]) if face_index < len(face_uids) else "",
                "id1": str(ids[0]),
                "id2": str(ids[1]),
                "id3": str(ids[2]),
                "semantic_type": str(face_semantics_by_index.get(face_index, {}).get("semantic_type", JBEAM_FACE_SEMANTIC_TRIANGLE)),
                "semantic_state": str(face_semantics_by_index.get(face_index, {}).get("semantic_state", "valid")),
                "normal": face_normals[face_index] if face_index < len(face_normals) else (),
                "params": model_params or (face_params[face_index] if face_index < len(face_params) and isinstance(face_params[face_index], dict) else {}),
                "committed_params": committed_face_params[face_index] if face_index < len(committed_face_params) and isinstance(committed_face_params[face_index], dict) else {},
                "model_backed": model_triangle is not None,
                "part": obj.get("beamng_part_name", ""),
                "source_file": obj.get("beamng_jbeam_path", ""),
            }
        )
    return infos


def read_experimental_mesh_topology(obj, allow_identity_write=True):
    mesh = obj.data
    identity = ensure_experimental_mesh_identity(obj, allow_write=allow_identity_write)
    node_ids = identity.get("node_ids", [])
    if not node_ids:
        return [], []

    edges = []
    faces = []
    if obj.mode == "EDIT":
        import bmesh

        edit_mesh = bmesh.from_edit_mesh(mesh)
        edit_mesh.verts.ensure_lookup_table()
        edit_mesh.verts.index_update()
        edit_mesh.edges.ensure_lookup_table()
        edit_mesh.edges.index_update()
        edit_mesh.faces.ensure_lookup_table()
        edit_mesh.faces.index_update()
        for edge in edit_mesh.edges:
            indices = [vertex.index for vertex in edge.verts]
            if len(indices) == 2 and all(0 <= index < len(node_ids) for index in indices):
                edges.append((node_ids[indices[0]], node_ids[indices[1]]))
        for face in edit_mesh.faces:
            indices = [vertex.index for vertex in face.verts]
            if len(indices) == 3 and all(0 <= index < len(node_ids) for index in indices):
                faces.append(tuple(node_ids[index] for index in indices))
    else:
        for edge in mesh.edges:
            indices = list(edge.vertices)
            if len(indices) == 2 and all(0 <= index < len(node_ids) for index in indices):
                edges.append((node_ids[indices[0]], node_ids[indices[1]]))
        for polygon in mesh.polygons:
            indices = list(polygon.vertices)
            if len(indices) == 3 and all(0 <= index < len(node_ids) for index in indices):
                faces.append(tuple(node_ids[index] for index in indices))
    return edges, faces


def experimental_mesh_non_triangle_face_count(obj):
    if obj is None or obj.type != "MESH":
        return 0
    mesh = obj.data
    if obj.mode == "EDIT":
        import bmesh

        edit_mesh = bmesh.from_edit_mesh(mesh)
        edit_mesh.faces.ensure_lookup_table()
        return sum(1 for face in edit_mesh.faces if len(face.verts) != 3)
    return sum(1 for polygon in mesh.polygons if len(polygon.vertices) != 3)


def active_experimental_mesh_validation_summary(context, limit=6):
    obj = active_experimental_jbeam_mesh(context)
    if obj is None or obj.type != "MESH" or obj.get("beamng_visual_type") != "experimental_jbeam_mesh":
        return []
    identity = ensure_experimental_mesh_identity(obj, context.scene, allow_write=False)
    node_ids = [str(node_id) for node_id in identity.get("node_ids", [])]
    edges, faces = read_experimental_mesh_topology(obj, allow_identity_write=False)
    semantic_snapshot = semantic_topology_snapshot_for_object(obj, context.scene, allow_write=False)
    semantic_by_key = semantic_edge_types_by_key(semantic_snapshot)
    beam_edges = [
        edge for edge in edges
        if semantic_by_key.get(edge_key(edge), JBEAM_EDGE_SEMANTIC_RELATIONSHIP) == JBEAM_EDGE_SEMANTIC_BEAM
    ]
    messages = []
    duplicate_nodes = duplicate_items(node_ids)
    duplicate_edges = duplicate_items(edge_key(edge) for edge in beam_edges)
    duplicate_faces = duplicate_items(face_key(face) for face in faces)
    non_triangles = experimental_mesh_non_triangle_face_count(obj)
    orphan_provisional = orphan_provisional_node_indices(obj)
    if duplicate_nodes:
        messages.append(("ERROR", f"Duplicate node IDs: {duplicate_nodes[:limit]}"))
    if duplicate_faces:
        messages.append(("ERROR", f"Duplicate triangle keys: {duplicate_faces[:limit]}"))
    if duplicate_edges:
        messages.append(("WARN", f"Duplicate beam edge keys: {duplicate_edges[:limit]}"))
    if non_triangles:
        messages.append(("WARN", f"Non-triangle faces ignored: {non_triangles}"))
    if orphan_provisional:
        messages.append(("WARN", f"Orphan provisional node(s): {len(orphan_provisional)}"))
    model = authoring_model_for_context(context)
    if model is not None:
        model_nodes = model.node_index()
        missing_model_nodes = [
            node_id
            for node_id in node_ids
            if node_id not in model_nodes and not node_id.startswith(str(obj.get("beamng_part_name", "")) + "_new_")
        ]
        if missing_model_nodes:
            messages.append(("WARN", f"Mesh nodes not in model snapshot: {missing_model_nodes[:limit]}"))
        if len(model.operations) != int(context.scene.get("beamng_jbeam_operation_history_count", 0)):
            messages.append(("WARN", "Model operation count differs from legacy history mirror"))
    if not messages:
        messages.append(("OK", "Topology validation: no active-mesh issues"))
    return messages


def edge_key(edge):
    return tuple(sorted(str(node_id) for node_id in edge[:2]))


def face_key(face):
    return tuple(str(node_id) for node_id in face[:3])


def face_identity_key(face):
    return tuple(sorted(str(node_id) for node_id in face[:3]))


def topology_signature_for_snapshot(snapshot):
    return {
        "vertices": [
            [topology_uid_key(item.get("uid", "")), str(item.get("node_id", ""))]
            for item in snapshot.get("vertices", [])
        ],
        "edges": [
            [
                topology_uid_key(item.get("uid", "")),
                [topology_uid_key(uid) for uid in item.get("vertex_uids", [])],
                [str(node_id) for node_id in item.get("node_ids", [])],
                str(item.get("semantic_type", "")),
            ]
            for item in snapshot.get("edges", [])
        ],
        "faces": [
            [
                topology_uid_key(item.get("uid", "")),
                [topology_uid_key(uid) for uid in item.get("vertex_uids", [])],
                [str(node_id) for node_id in item.get("node_ids", [])],
                str(item.get("semantic_type", "")),
                [str(node_id) for node_id in item.get("winding", [])],
            ]
            for item in snapshot.get("faces", [])
        ],
    }


def semantic_topology_items_by_uid(snapshot, key):
    result = {}
    if not isinstance(snapshot, dict):
        return result
    for item in snapshot.get(key, []):
        if not isinstance(item, dict):
            continue
        uid = topology_uid_key(item.get("uid", ""))
        if uid:
            result[uid] = item
    return result


def semantic_topology_item_summary(item):
    return {
        "uid": topology_uid_key(item.get("uid", "")),
        "index": int(item.get("index", -1) or -1),
        "node_id": str(item.get("node_id", "")),
        "node_ids": list(item.get("node_ids", [])) if isinstance(item.get("node_ids", []), (list, tuple)) else [],
        "vertex_uids": list(item.get("vertex_uids", [])) if isinstance(item.get("vertex_uids", []), (list, tuple)) else [],
        "semantic_type": str(item.get("semantic_type", "")),
        "semantic_state": str(item.get("semantic_state", "")),
        "ownership_mode": str(item.get("ownership_mode", "")),
        "owner_part_id": item.get("owner_part_id", -1),
        "winding": list(item.get("winding", [])) if isinstance(item.get("winding", []), (list, tuple)) else [],
    }


def semantic_topology_delta(previous_snapshot, current_snapshot):
    delta = {
        "schema_version": 1,
        "object_name": str(current_snapshot.get("object_name", "")) if isinstance(current_snapshot, dict) else "",
        "from_revision": int(previous_snapshot.get("topology_revision", 0) or 0) if isinstance(previous_snapshot, dict) else 0,
        "to_revision": int(current_snapshot.get("topology_revision", 0) or 0) if isinstance(current_snapshot, dict) else 0,
        "created_vertices": [],
        "deleted_vertices": [],
        "changed_vertices": [],
        "created_edges": [],
        "deleted_edges": [],
        "changed_edges": [],
        "created_faces": [],
        "deleted_faces": [],
        "changed_faces": [],
    }
    if not isinstance(previous_snapshot, dict) or not previous_snapshot:
        delta["initialized"] = True
        return delta

    for section, created_key, deleted_key, changed_key in (
        ("vertices", "created_vertices", "deleted_vertices", "changed_vertices"),
        ("edges", "created_edges", "deleted_edges", "changed_edges"),
        ("faces", "created_faces", "deleted_faces", "changed_faces"),
    ):
        previous = semantic_topology_items_by_uid(previous_snapshot, section)
        current = semantic_topology_items_by_uid(current_snapshot, section)
        for uid in sorted(set(current) - set(previous), key=topology_uid_sort_key):
            delta[created_key].append(semantic_topology_item_summary(current[uid]))
        for uid in sorted(set(previous) - set(current), key=topology_uid_sort_key):
            delta[deleted_key].append(semantic_topology_item_summary(previous[uid]))
        for uid in sorted(set(previous) & set(current), key=topology_uid_sort_key):
            old = semantic_topology_item_summary(previous[uid])
            new = semantic_topology_item_summary(current[uid])
            changes = {}
            for field in ("node_id", "node_ids", "vertex_uids", "semantic_type", "semantic_state", "ownership_mode", "owner_part_id", "winding"):
                if old.get(field) != new.get(field):
                    changes[field] = {"old": old.get(field), "new": new.get(field)}
            if changes:
                delta[changed_key].append({"uid": uid, "changes": changes, "current": new})
    delta["created_count"] = sum(len(delta[key]) for key in ("created_vertices", "created_edges", "created_faces"))
    delta["deleted_count"] = sum(len(delta[key]) for key in ("deleted_vertices", "deleted_edges", "deleted_faces"))
    delta["changed_count"] = sum(len(delta[key]) for key in ("changed_vertices", "changed_edges", "changed_faces"))
    delta["change_count"] = delta["created_count"] + delta["deleted_count"] + delta["changed_count"]
    return delta


def semantic_edge_type_for_key(key, original_beam_keys, explicit_beam_keys, triangle_boundary_keys):
    if key in original_beam_keys or key in explicit_beam_keys:
        return JBEAM_EDGE_SEMANTIC_BEAM
    if key in triangle_boundary_keys:
        return JBEAM_EDGE_SEMANTIC_TRIANGLE_BOUNDARY
    return JBEAM_EDGE_SEMANTIC_RELATIONSHIP


def semantic_topology_snapshot_for_object(obj, scene=None, allow_write=True):
    if obj is None or obj.type != "MESH" or obj.get("beamng_visual_type") != "experimental_jbeam_mesh":
        return {}
    mesh = obj.data
    identity = ensure_experimental_mesh_identity(obj, scene, allow_write=allow_write)
    topology_uids = ensure_experimental_topology_uids(obj, allow_write=allow_write)
    node_ids = [str(node_id) for node_id in identity.get("node_ids", [])]
    node_kinds = [str(kind or "owned") for kind in identity.get("node_kinds", [])]
    owner_part_ids = identity.get("owner_part_ids", [])
    _edit_mesh, positions = read_experimental_mesh_vertices(obj)
    node_uids = topology_uids.get("nodes", [])
    edge_uids = topology_uids.get("edges", [])
    face_uids = topology_uids.get("faces", [])

    original_beam_keys = {
        edge_key(edge)
        for edge in mesh_json_list(mesh, "beamng_edge_node_ids_json")
        if isinstance(edge, (list, tuple)) and len(edge) >= 2
    }
    explicit_beam_keys = {
        edge_key(edge)
        for edge in mesh_json_list(mesh, "beamng_explicit_beam_edge_keys_json")
        if isinstance(edge, (list, tuple)) and len(edge) >= 2
    }

    raw_edges = []
    raw_faces = []
    if obj.mode == "EDIT":
        import bmesh

        bm = bmesh.from_edit_mesh(mesh)
        bm.verts.ensure_lookup_table()
        bm.verts.index_update()
        bm.edges.ensure_lookup_table()
        bm.edges.index_update()
        bm.faces.ensure_lookup_table()
        bm.faces.index_update()
        for edge in bm.edges:
            indices = [vertex.index for vertex in edge.verts]
            raw_edges.append((edge.index, indices))
        for face in bm.faces:
            indices = [vertex.index for vertex in face.verts]
            raw_faces.append((face.index, indices))
    else:
        raw_edges = [(edge.index, list(edge.vertices)) for edge in mesh.edges]
        raw_faces = [(polygon.index, list(polygon.vertices)) for polygon in mesh.polygons]

    triangle_boundary_keys = set()
    for _face_index, indices in raw_faces:
        if len(indices) != 3 or not all(0 <= index < len(node_ids) for index in indices):
            continue
        ids = [node_ids[index] for index in indices]
        triangle_boundary_keys.add(edge_key((ids[0], ids[1])))
        triangle_boundary_keys.add(edge_key((ids[1], ids[2])))
        triangle_boundary_keys.add(edge_key((ids[2], ids[0])))

    edge_type_map = mesh_json_dict(mesh, "beamng_edge_uid_to_semantic_type_json")
    face_type_map = mesh_json_dict(mesh, "beamng_face_uid_to_semantic_type_json")
    edge_state_map = mesh_json_dict(mesh, "beamng_edge_uid_to_semantic_state_json")
    face_state_map = mesh_json_dict(mesh, "beamng_face_uid_to_semantic_state_json")
    edge_semantic_codes = [JBEAM_EDGE_SEMANTIC_CODES[JBEAM_EDGE_SEMANTIC_RELATIONSHIP] for _edge in raw_edges]
    face_semantic_codes = [JBEAM_FACE_SEMANTIC_CODES[JBEAM_FACE_SEMANTIC_INVALID] for _face in raw_faces]
    edge_maps_changed = False
    face_maps_changed = False

    vertices = []
    for index, uid in enumerate(node_uids):
        uid_key = topology_uid_key(uid)
        if not uid_key or index >= len(node_ids):
            continue
        kind = node_kinds[index] if index < len(node_kinds) else "owned"
        vertices.append(
            {
                "uid": uid_key,
                "index": index,
                "node_id": node_ids[index],
                "position": rounded_position_list(positions[index]) if index < len(positions) else [],
                "semantic_type": "node",
                "semantic_state": "proxy" if kind == "proxy" else "valid",
                "ownership_mode": "proxy" if kind == "proxy" else "owned",
                "owner_part_id": owner_part_ids[index] if index < len(owner_part_ids) else obj.get("beamng_resolved_part_id", -1),
            }
        )

    edges = []
    for edge_index, indices in raw_edges:
        if edge_index >= len(edge_uids) or len(indices) != 2:
            continue
        if not all(0 <= index < len(node_ids) and index < len(node_uids) for index in indices):
            continue
        uid_key = topology_uid_key(edge_uids[edge_index])
        if not uid_key:
            continue
        ids = [node_ids[indices[0]], node_ids[indices[1]]]
        key = edge_key(ids)
        semantic_type = edge_type_map.get(uid_key)
        inferred_type = semantic_edge_type_for_key(key, original_beam_keys, explicit_beam_keys, triangle_boundary_keys)
        if semantic_type in {"", None, JBEAM_EDGE_SEMANTIC_RELATIONSHIP}:
            semantic_type = inferred_type
            edge_type_map[uid_key] = semantic_type
            edge_maps_changed = True
        semantic_state = edge_state_map.get(uid_key) or ("valid" if semantic_type == JBEAM_EDGE_SEMANTIC_BEAM else "reference_only")
        if uid_key not in edge_state_map:
            edge_state_map[uid_key] = semantic_state
            edge_maps_changed = True
        if 0 <= edge_index < len(edge_semantic_codes):
            edge_semantic_codes[edge_index] = JBEAM_EDGE_SEMANTIC_CODES.get(
                semantic_type, JBEAM_EDGE_SEMANTIC_CODES[JBEAM_EDGE_SEMANTIC_RELATIONSHIP]
            )
        edges.append(
            {
                "uid": uid_key,
                "index": edge_index,
                "vertex_uids": [topology_uid_key(node_uids[indices[0]]), topology_uid_key(node_uids[indices[1]])],
                "node_ids": ids,
                "semantic_type": semantic_type,
                "semantic_state": semantic_state,
                "ownership_mode": "owned",
            }
        )

    faces = []
    warnings = []
    for face_index, indices in raw_faces:
        if face_index >= len(face_uids):
            continue
        uid_key = topology_uid_key(face_uids[face_index])
        if not uid_key:
            continue
        valid_triangle = len(indices) == 3 and all(0 <= index < len(node_ids) and index < len(node_uids) for index in indices)
        semantic_type = JBEAM_FACE_SEMANTIC_TRIANGLE if valid_triangle else JBEAM_FACE_SEMANTIC_INVALID
        semantic_state = "valid" if valid_triangle else "invalid"
        if face_type_map.get(uid_key) != semantic_type:
            face_type_map[uid_key] = semantic_type
            face_maps_changed = True
        if face_state_map.get(uid_key) != semantic_state:
            face_state_map[uid_key] = semantic_state
            face_maps_changed = True
        if not valid_triangle:
            warnings.append(f"Face UID {uid_key} has {len(indices)} vertices; BeamNG triangles require 3.")
        if 0 <= face_index < len(face_semantic_codes):
            face_semantic_codes[face_index] = JBEAM_FACE_SEMANTIC_CODES.get(
                semantic_type, JBEAM_FACE_SEMANTIC_CODES[JBEAM_FACE_SEMANTIC_INVALID]
            )
        faces.append(
            {
                "uid": uid_key,
                "index": face_index,
                "vertex_uids": [topology_uid_key(node_uids[index]) for index in indices if 0 <= index < len(node_uids)],
                "node_ids": [node_ids[index] for index in indices if 0 <= index < len(node_ids)],
                "semantic_type": semantic_type,
                "semantic_state": semantic_state,
                "ownership_mode": "owned",
                "winding": [node_ids[index] for index in indices if 0 <= index < len(node_ids)],
            }
        )

    previous_revision = int(mesh.get("beamng_topology_revision", 0) or 0)
    snapshot = {
        "schema_version": 1,
        "topology_revision": previous_revision,
        "object_name": obj.name,
        "part_name": str(obj.get("beamng_part_name", "")),
        "source_file": str(obj.get("beamng_jbeam_path", "")),
        "vertices": vertices,
        "edges": edges,
        "faces": faces,
        "warnings": warnings,
    }
    signature = topology_signature_for_snapshot(snapshot)
    previous_signature = mesh_json_dict(mesh, "beamng_topology_signature_json")
    previous_snapshot = mesh_json_dict(mesh, "beamng_semantic_topology_json")
    delta = semantic_topology_delta(previous_snapshot, snapshot)
    if allow_write:
        if signature != previous_signature:
            snapshot["topology_revision"] = previous_revision + 1
            mesh["beamng_topology_revision"] = snapshot["topology_revision"]
            mesh["beamng_topology_signature_json"] = json.dumps(signature)
            delta = semantic_topology_delta(previous_snapshot, snapshot)
        if edge_maps_changed:
            mesh["beamng_edge_uid_to_semantic_type_json"] = json.dumps(edge_type_map)
            mesh["beamng_edge_uid_to_semantic_state_json"] = json.dumps(edge_state_map)
        if face_maps_changed:
            mesh["beamng_face_uid_to_semantic_type_json"] = json.dumps(face_type_map)
            mesh["beamng_face_uid_to_semantic_state_json"] = json.dumps(face_state_map)
        set_element_int_attribute_values(obj, JBEAM_EDGE_SEMANTIC_ATTR, "EDGE", edge_semantic_codes)
        set_element_int_attribute_values(obj, JBEAM_FACE_SEMANTIC_ATTR, "FACE", face_semantic_codes)
        mesh["beamng_previous_semantic_topology_json"] = json.dumps(previous_snapshot if isinstance(previous_snapshot, dict) else {})
        mesh["beamng_semantic_topology_delta_json"] = json.dumps(delta)
        mesh["beamng_semantic_topology_delta_count"] = int(delta.get("change_count", 0))
        mesh["beamng_semantic_topology_json"] = json.dumps(snapshot)
    snapshot["delta"] = delta
    return snapshot


def semantic_edge_types_by_key(snapshot):
    result = {}
    for item in snapshot.get("edges", []) if isinstance(snapshot, dict) else []:
        ids = item.get("node_ids", [])
        if isinstance(ids, list) and len(ids) >= 2:
            result[edge_key(ids)] = str(item.get("semantic_type", JBEAM_EDGE_SEMANTIC_RELATIONSHIP))
    return result


def semantic_beam_edges_for_object(obj, scene=None, current_edges=None, allow_write=True):
    if current_edges is None:
        current_edges, _current_faces = read_experimental_mesh_topology(obj, allow_identity_write=allow_write)
    semantic_snapshot = semantic_topology_snapshot_for_object(obj, scene, allow_write=allow_write)
    semantic_by_key = semantic_edge_types_by_key(semantic_snapshot)
    return [
        list(edge)
        for edge in current_edges
        if semantic_by_key.get(edge_key(edge), JBEAM_EDGE_SEMANTIC_RELATIONSHIP) == JBEAM_EDGE_SEMANTIC_BEAM
    ]


def selected_experimental_edge_uid_and_keys(obj):
    identity = ensure_experimental_mesh_identity(obj, allow_write=True)
    node_ids = identity.get("node_ids", [])
    topology_uids = ensure_experimental_topology_uids(obj, allow_write=True)
    edge_uids = topology_uids.get("edges", [])
    results = []
    for edge_index in selected_experimental_jbeam_edge_indices(obj):
        if edge_index < 0 or edge_index >= len(edge_uids):
            continue
        uid = topology_uid_key(edge_uids[edge_index])
        if not uid:
            continue
        ids = None
        if obj.mode == "EDIT":
            import bmesh

            bm = bmesh.from_edit_mesh(obj.data)
            bm.edges.ensure_lookup_table()
            bm.edges.index_update()
            if edge_index < len(bm.edges):
                indices = [vertex.index for vertex in bm.edges[edge_index].verts]
                if len(indices) == 2 and all(0 <= index < len(node_ids) for index in indices):
                    ids = [node_ids[indices[0]], node_ids[indices[1]]]
        else:
            if edge_index < len(obj.data.edges):
                indices = list(obj.data.edges[edge_index].vertices)
                if len(indices) == 2 and all(0 <= index < len(node_ids) for index in indices):
                    ids = [node_ids[indices[0]], node_ids[indices[1]]]
        if ids:
            results.append({"edge_index": edge_index, "uid": uid, "key": edge_key(ids), "nodes": ids})
    return results


def set_selected_edge_semantic_type(obj, scene, semantic_type):
    selected = selected_experimental_edge_uid_and_keys(obj)
    if not selected:
        return {"changed": 0}
    mesh = obj.data
    type_map = mesh_json_dict(mesh, "beamng_edge_uid_to_semantic_type_json")
    state_map = mesh_json_dict(mesh, "beamng_edge_uid_to_semantic_state_json")
    explicit_keys = {
        edge_key(key)
        for key in mesh_json_list(mesh, "beamng_explicit_beam_edge_keys_json")
        if isinstance(key, (list, tuple)) and len(key) >= 2
    }
    for item in selected:
        uid_key = str(item["uid"])
        type_map[uid_key] = str(semantic_type)
        state_map[uid_key] = "valid" if semantic_type == JBEAM_EDGE_SEMANTIC_BEAM else "reference_only"
        if semantic_type == JBEAM_EDGE_SEMANTIC_BEAM:
            explicit_keys.add(item["key"])
        else:
            explicit_keys.discard(item["key"])
    mesh["beamng_edge_uid_to_semantic_type_json"] = json.dumps(type_map)
    mesh["beamng_edge_uid_to_semantic_state_json"] = json.dumps(state_map)
    mesh["beamng_explicit_beam_edge_keys_json"] = json.dumps([list(key) for key in sorted(explicit_keys)])
    semantic_topology_snapshot_for_object(obj, scene, allow_write=True)
    return {"changed": len(selected)}


def duplicate_items(values):
    seen = set()
    duplicates = set()
    for value in values:
        key = tuple(value) if isinstance(value, (list, tuple)) else value
        if key in seen:
            duplicates.add(key)
        seen.add(key)
    return sorted(duplicates, key=str)


def generated_jbeam_node_id(part_name, vertex_index, existing_ids):
    stem = re.sub(r"[^A-Za-z0-9_]+", "_", str(part_name or "node")).strip("_") or "node"
    candidate = f"{stem}_new_{vertex_index}"
    suffix = 1
    existing = {str(node_id) for node_id in existing_ids}
    while candidate in existing:
        suffix += 1
        candidate = f"{stem}_new_{vertex_index}_{suffix}"
    return candidate


JBEAM_POSITION_PRECISION = 3


def rounded_position_list(position, precision=JBEAM_POSITION_PRECISION):
    return [round(float(value), precision) for value in position]


def rounded_position_vector(position, precision=JBEAM_POSITION_PRECISION):
    return Vector(rounded_position_list(position, precision))


def formatted_jbeam_position_number(value, precision=JBEAM_POSITION_PRECISION):
    rounded = round(float(value), precision)
    if rounded == 0:
        rounded = 0.0
    return f"{rounded:.{precision}f}"


def jbeam_decimal_places(value):
    text = str(value).strip()
    if not text or "e" in text.lower() or "." not in text:
        return 0
    return len(text.split(".", 1)[1])


def jbeam_project_position_precision(context=None):
    prefs = get_addon_preferences(context or bpy.context)
    if prefs is None:
        return JBEAM_POSITION_PRECISION
    try:
        return max(0, min(12, int(getattr(prefs, "jbeam_position_precision", JBEAM_POSITION_PRECISION))))
    except (TypeError, ValueError):
        return JBEAM_POSITION_PRECISION


def set_jbeam_project_position_precision(context, precision):
    prefs = get_addon_preferences(context)
    if prefs is None:
        return
    prefs.jbeam_position_precision = max(0, min(12, int(precision)))


def coerce_jbeam_param_value(value):
    if isinstance(value, bool):
        return value
    text = str(value).strip()
    if text.lower() == "true":
        return True
    if text.lower() == "false":
        return False
    try:
        if text and re.fullmatch(r"[-+]?\d+", text):
            return int(text)
        if text and re.fullmatch(r"[-+]?(?:\d+\.\d*|\.\d+)(?:[eE][-+]?\d+)?", text):
            return float(text)
    except ValueError:
        pass
    return text


def selected_node_params_from_scene(scene):
    params = {}
    for attr, key in (
        ("beamng_jbeam_node_weight", "nodeWeight"),
        ("beamng_jbeam_node_material", "nodeMaterial"),
        ("beamng_jbeam_node_group", "group"),
        ("beamng_jbeam_node_friction", "frictionCoef"),
    ):
        value = str(getattr(scene, attr, "") or "").strip()
        if value:
            params[key] = value
    if getattr(scene, "beamng_jbeam_node_collision_override", False):
        params["collision"] = bool(getattr(scene, "beamng_jbeam_node_collision", True))
    if getattr(scene, "beamng_jbeam_node_self_collision_override", False):
        params["selfCollision"] = bool(getattr(scene, "beamng_jbeam_node_self_collision", False))
    return params


def selected_beam_params_from_scene(scene):
    params = {}
    for attr, key in (
        ("beamng_jbeam_beam_spring", "beamSpring"),
        ("beamng_jbeam_beam_damp", "beamDamp"),
        ("beamng_jbeam_beam_deform", "beamDeform"),
        ("beamng_jbeam_beam_strength", "beamStrength"),
        ("beamng_jbeam_beam_precompression", "beamPrecompression"),
        ("beamng_jbeam_beam_type", "beamType"),
        ("beamng_jbeam_beam_break_group", "breakGroup"),
    ):
        value = str(getattr(scene, attr, "") or "").strip()
        if value:
            params[key] = value
    return params


def selected_triangle_params_from_scene(scene):
    params = {}
    for attr, key in (
        ("beamng_jbeam_triangle_group", "group"),
        ("beamng_jbeam_triangle_drag_coef", "dragCoef"),
        ("beamng_jbeam_triangle_ground_model", "groundModel"),
    ):
        value = str(getattr(scene, attr, "") or "").strip()
        if value:
            params[key] = value
    if getattr(scene, "beamng_jbeam_triangle_collision_override", False):
        params["collision"] = bool(getattr(scene, "beamng_jbeam_triangle_collision", True))
    return params


def params_by_topology_key(items, params):
    result = {}
    for item, param in zip(items, params):
        if not isinstance(item, (list, tuple)):
            continue
        key = edge_key(item) if len(item) == 2 else face_identity_key(item)
        result[key] = param if isinstance(param, dict) else {}
    return result


def topology_delta_for_mesh(mesh):
    return mesh_json_dict(mesh, "beamng_semantic_topology_delta_json")


def topology_delta_created_edge_keys(delta):
    result = set()
    for item in delta.get("created_edges", []) if isinstance(delta, dict) else []:
        ids = item.get("node_ids", [])
        if isinstance(ids, list) and len(ids) >= 2:
            result.add(edge_key(ids))
    return result


def topology_delta_created_face_keys(delta):
    result = set()
    for item in delta.get("created_faces", []) if isinstance(delta, dict) else []:
        ids = item.get("node_ids", [])
        if isinstance(ids, list) and len(ids) >= 3:
            result.add(face_identity_key(ids))
    return result


def append_reference_deletes_for_missing_nodes(mesh_changes, obj, missing_node_ids, original_edges, original_faces, reason):
    missing = {str(node_id) for node_id in missing_node_ids if str(node_id)}
    if not missing:
        return 0
    existing_beam_deletes = {
        edge_key(change.get("old", []))
        for change in mesh_changes
        if change.get("section") == "beams" and change.get("operation") == "delete"
    }
    existing_triangle_deletes = {
        face_identity_key(change.get("old", []))
        for change in mesh_changes
        if change.get("section") == "triangles" and change.get("operation") == "delete"
    }
    added = 0
    for ids in original_edges:
        if not isinstance(ids, (list, tuple)) or len(ids) < 2:
            continue
        key = edge_key(ids)
        if key in existing_beam_deletes or not any(str(node_id) in missing for node_id in ids[:2]):
            continue
        mesh_changes.append(
            {
                "file": obj.get("beamng_jbeam_path", ""),
                "part": obj.get("beamng_part_name", ""),
                "resolved_part_id": int(obj.get("beamng_resolved_part_id", -1)),
                "section": "beams",
                "row": "",
                "field": "nodes",
                "old": list(ids[:2]),
                "new": [],
                "operation": "delete",
                "source_object": obj.name,
                "reason": reason,
            }
        )
        existing_beam_deletes.add(key)
        added += 1
    for ids in original_faces:
        if not isinstance(ids, (list, tuple)) or len(ids) < 3:
            continue
        key = face_identity_key(ids)
        if key in existing_triangle_deletes or not any(str(node_id) in missing for node_id in ids[:3]):
            continue
        mesh_changes.append(
            {
                "file": obj.get("beamng_jbeam_path", ""),
                "part": obj.get("beamng_part_name", ""),
                "resolved_part_id": int(obj.get("beamng_resolved_part_id", -1)),
                "section": "triangles",
                "row": "",
                "field": "nodes",
                "old": list(ids[:3]),
                "new": [],
                "operation": "delete",
                "source_object": obj.name,
                "reason": reason,
            }
        )
        existing_triangle_deletes.add(key)
        added += 1
    return added


def proxy_reference_delete_changes_for_owned_deletes(scene, source_object_name, deleted_owned_node_ids):
    deleted = {str(node_id) for node_id in deleted_owned_node_ids if str(node_id)}
    if not deleted:
        return []
    changes = []
    for obj in experimental_jbeam_mesh_objects(scene, active_only=False):
        if obj.name == source_object_name:
            continue
        identity = ensure_experimental_mesh_identity(obj, scene, allow_write=False)
        node_ids = [str(node_id) for node_id in identity.get("node_ids", [])]
        node_kinds = [str(kind or "owned") for kind in identity.get("node_kinds", [])]
        proxy_deleted_here = {
            node_id
            for node_id, kind in zip(node_ids, node_kinds)
            if str(kind) == "proxy" and str(node_id) in deleted
        }
        if not proxy_deleted_here:
            continue
        original_edges = mesh_json_list(obj.data, "beamng_edge_node_ids_json")
        original_faces = mesh_json_list(obj.data, "beamng_face_node_ids_json")
        append_reference_deletes_for_missing_nodes(
            changes,
            obj,
            proxy_deleted_here,
            original_edges,
            original_faces,
            "source_owned_node_deleted",
        )
    return changes


def remove_matching_proxy_vertices_for_owned_deletes(scene, source_object_name, deleted_owned_node_ids):
    deleted = {str(node_id) for node_id in deleted_owned_node_ids if str(node_id)}
    if not deleted:
        return {"removed": 0, "objects": []}
    removed_count = 0
    touched_objects = []
    for obj in experimental_jbeam_mesh_objects(scene, active_only=False):
        if obj.name == source_object_name:
            continue
        identity = ensure_experimental_mesh_identity(obj, scene, allow_write=True)
        node_ids = [str(node_id) for node_id in identity.get("node_ids", [])]
        node_kinds = [str(kind or "owned") for kind in identity.get("node_kinds", [])]
        indices = {
            index
            for index, (node_id, kind) in enumerate(zip(node_ids, node_kinds))
            if str(kind) == "proxy" and str(node_id) in deleted
        }
        if not indices:
            continue
        import bmesh

        mesh = obj.data
        if obj.mode == "EDIT":
            bm = bmesh.from_edit_mesh(mesh)
            bm.verts.ensure_lookup_table()
            bm.verts.index_update()
            verts = [vertex for vertex in bm.verts if vertex.index in indices]
            bmesh.ops.delete(bm, geom=verts, context="VERTS")
            bmesh.update_edit_mesh(mesh, loop_triangles=True, destructive=True)
        else:
            bm = bmesh.new()
            bm.from_mesh(mesh)
            bm.verts.ensure_lookup_table()
            bm.verts.index_update()
            verts = [vertex for vertex in bm.verts if vertex.index in indices]
            bmesh.ops.delete(bm, geom=verts, context="VERTS")
            bm.to_mesh(mesh)
            bm.free()
            mesh.update()
        removed_count += len(indices)
        touched_objects.append(obj.name)
        ensure_experimental_mesh_identity(obj, scene, allow_write=True)
        semantic_topology_snapshot_for_object(obj, scene, allow_write=True)
        obj["beamng_proxy_node_count"] = sum(
            1
            for kind in ensure_experimental_mesh_identity(obj, scene, allow_write=False).get("node_kinds", [])
            if str(kind) == "proxy"
        )
    return {"removed": removed_count, "objects": touched_objects}


def orphan_provisional_node_indices(obj):
    identity = ensure_experimental_mesh_identity(obj, bpy.context.scene, allow_write=False)
    node_ids = [str(node_id) for node_id in identity.get("node_ids", [])]
    node_kinds = [str(kind or "owned") for kind in identity.get("node_kinds", [])]
    committed_flags = [bool(value) for value in identity.get("committed_flags", [])]
    used_indices = set()
    if obj.mode == "EDIT":
        import bmesh

        bm = bmesh.from_edit_mesh(obj.data)
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        for edge in bm.edges:
            used_indices.update(vertex.index for vertex in edge.verts)
        for face in bm.faces:
            used_indices.update(vertex.index for vertex in face.verts)
    else:
        for edge in obj.data.edges:
            used_indices.update(edge.vertices)
        for poly in obj.data.polygons:
            used_indices.update(poly.vertices)
    return [
        index
        for index, _node_id in enumerate(node_ids)
        if index < len(node_kinds)
        and index < len(committed_flags)
        and node_kinds[index] == "owned"
        and not committed_flags[index]
        and index not in used_indices
    ]


def scan_experimental_jbeam_mesh_edits(scene, active_only=False, tolerance=0.0005):
    changes = []
    restored_proxy_count = 0
    topology_change_count = 0
    scanned_mesh_count = 0
    dirty_mesh_count = 0
    non_triangle_face_count = 0
    objects = experimental_jbeam_mesh_objects(scene, active_only=active_only)
    owned_node_deletes_by_object = defaultdict(set)

    for obj in objects:
        mesh = obj.data
        identity = ensure_experimental_mesh_identity(obj, scene)
        if not identity:
            continue
        try:
            node_ids = identity["node_ids"]
            node_kinds = identity["node_kinds"]
            owner_part_ids = identity["owner_part_ids"]
            original_positions = identity["original_positions"]
            committed_flags = identity["committed_flags"]
            node_params = identity["node_params"]
            committed_node_params = identity["committed_node_params"]
            original_edges = json.loads(mesh.get("beamng_edge_node_ids_json", "[]"))
            original_faces = json.loads(mesh.get("beamng_face_node_ids_json", "[]"))
            original_mesh_edges = json.loads(mesh.get("beamng_mesh_edge_node_ids_json", "[]"))
            original_beam_key_to_guid = mesh_json_dict(mesh, "beamng_original_beam_key_to_topology_guid_json")
            topology_uids = ensure_experimental_topology_uids(obj, allow_write=True)
            edge_params = topology_params_for_current_elements(mesh, topology_uids.get("edges", []), "beamng_edge_params_json", "beamng_edge_uid_to_params_json", allow_write=True)
            committed_edge_params = topology_params_for_current_elements(mesh, topology_uids.get("edges", []), "beamng_edge_committed_params_json", "beamng_edge_uid_to_committed_params_json", allow_write=True)
            face_params = topology_params_for_current_elements(mesh, topology_uids.get("faces", []), "beamng_face_params_json", "beamng_face_uid_to_params_json", allow_write=True)
            committed_face_params = topology_params_for_current_elements(mesh, topology_uids.get("faces", []), "beamng_face_committed_params_json", "beamng_face_uid_to_committed_params_json", allow_write=True)
        except (TypeError, json.JSONDecodeError):
            continue
        if not node_ids:
            continue

        scanned_mesh_count += 1
        non_triangle_face_count += experimental_mesh_non_triangle_face_count(obj)
        edit_mesh, current_positions = read_experimental_mesh_vertices(obj)
        mesh_changes = []
        deleted_node_ids = set()
        deleted_proxy_node_ids = set()
        resolved_part_id = int(obj.get("beamng_resolved_part_id", -1))
        current_node_ids = {str(node_id) for node_id in node_ids}
        current_node_kind_by_id = {
            str(node_id): str(node_kinds[index] if index < len(node_kinds) else "owned")
            for index, node_id in enumerate(node_ids)
        }
        current_node_uids = {
            topology_uid_key(uid)
            for uid in ensure_experimental_topology_uids(obj, allow_write=True).get("nodes", [])
            if topology_uid_key(uid)
        }
        uid_to_node_id = mesh_json_dict(mesh, "beamng_node_uid_to_id_json")
        uid_to_kind = mesh_json_dict(mesh, "beamng_node_uid_to_kind_json")
        uid_to_owner = mesh_json_dict(mesh, "beamng_node_uid_to_owner_part_id_json")
        uid_to_baseline = mesh_json_dict(mesh, "beamng_node_uid_to_original_position_json")
        uid_to_committed = mesh_json_dict(mesh, "beamng_node_uid_to_committed_json")
        if current_node_uids and (set(uid_to_node_id) & current_node_uids):
            for uid, node_id in sorted(uid_to_node_id.items(), key=lambda item: str(item[1])):
                if uid in current_node_uids:
                    continue
                if str(node_id) in current_node_ids:
                    continue
                if not bool(uid_to_committed.get(uid, True)):
                    continue
                if str(uid_to_kind.get(uid, "owned")) == "proxy":
                    deleted_proxy_node_ids.add(str(node_id))
                    continue
                old_position = uid_to_baseline.get(uid, [])
                deleted_node_ids.add(str(node_id))
                owned_node_deletes_by_object[obj.name].add(str(node_id))
                mesh_changes.append(
                    {
                        "file": obj.get("beamng_jbeam_path", ""),
                        "part": obj.get("beamng_part_name", ""),
                        "resolved_part_id": resolved_part_id,
                        "section": "nodes",
                        "row": str(node_id),
                        "field": "position",
                        "old": rounded_position_list(old_position) if isinstance(old_position, (list, tuple)) and len(old_position) == 3 else [],
                        "new": [],
                        "operation": "delete",
                        "owner_resolved_part_id": uid_to_owner.get(uid, resolved_part_id),
                        "source_object": obj.name,
                        "vertex_index": -1,
                        "topology_uid": uid,
                    }
                )

        if len(node_ids) != len(original_positions):
            continue
        for index, node_id in enumerate(node_ids):
            if index >= len(current_positions) or index >= len(node_kinds):
                continue
            original_values = rounded_position_list(original_positions[index])
            current_values = rounded_position_list(current_positions[index])
            committed = bool(committed_flags[index]) if index < len(committed_flags) else True
            if not committed:
                params = node_params[index] if index < len(node_params) and isinstance(node_params[index], dict) else {}
                mesh_changes.append(
                    {
                        "file": obj.get("beamng_jbeam_path", ""),
                        "part": obj.get("beamng_part_name", ""),
                        "resolved_part_id": resolved_part_id,
                        "section": "nodes",
                        "row": str(node_id),
                        "field": "position",
                        "old": [],
                        "new": current_values,
                        "params": params,
                        "operation": "insert",
                        "owner_resolved_part_id": resolved_part_id,
                        "source_object": obj.name,
                        "vertex_index": index,
                    }
                )
                set_experimental_mesh_vertex(edit_mesh, mesh, index, Vector(current_values))
                continue
            params = node_params[index] if index < len(node_params) and isinstance(node_params[index], dict) else {}
            committed_params = (
                committed_node_params[index]
                if index < len(committed_node_params) and isinstance(committed_node_params[index], dict)
                else {}
            )
            if params != committed_params:
                mesh_changes.append(
                    {
                        "file": obj.get("beamng_jbeam_path", ""),
                        "part": obj.get("beamng_part_name", ""),
                        "resolved_part_id": resolved_part_id,
                        "section": "nodes",
                        "row": str(node_id),
                        "field": "params",
                        "old": committed_params,
                        "new": params,
                        "operation": "update",
                        "owner_resolved_part_id": resolved_part_id,
                        "source_object": obj.name,
                        "vertex_index": index,
                    }
                )
            original = Vector(original_values)
            current = Vector(current_values)
            if original_values == current_values or (current - original).length <= tolerance:
                continue

            kind = node_kinds[index]
            owner_part_id = owner_part_ids[index] if index < len(owner_part_ids) else -1
            if kind == "proxy":
                set_experimental_mesh_vertex(edit_mesh, mesh, index, original)
                restored_proxy_count += 1
                continue

            change = {
                "file": obj.get("beamng_jbeam_path", ""),
                "part": obj.get("beamng_part_name", ""),
                "resolved_part_id": int(obj.get("beamng_resolved_part_id", -1)),
                "section": "nodes",
                "row": str(node_id),
                "field": "position",
                "old": original_values,
                "new": current_values,
                "operation": "update",
                "owner_resolved_part_id": int(owner_part_id) if str(owner_part_id).lstrip("-").isdigit() else owner_part_id,
                "source_object": obj.name,
                "vertex_index": index,
            }
            mesh_changes.append(change)
            set_experimental_mesh_vertex(edit_mesh, mesh, index, current)

        current_edges, current_faces = read_experimental_mesh_topology(obj)
        semantic_snapshot = semantic_topology_snapshot_for_object(obj, scene, allow_write=True)
        topology_delta = topology_delta_for_mesh(mesh)
        current_edge_semantic_by_key = semantic_edge_types_by_key(semantic_snapshot)
        if not original_mesh_edges:
            original_mesh_edges = [list(edge) for edge in current_edges]
            mesh["beamng_mesh_edge_node_ids_json"] = json.dumps(original_mesh_edges)

        original_edge_by_key = {edge_key(edge): edge for edge in original_edges if len(edge) >= 2}
        original_mesh_edge_by_key = {edge_key(edge): edge for edge in original_mesh_edges if len(edge) >= 2}
        current_edge_by_key = {edge_key(edge): edge for edge in current_edges if len(edge) >= 2}
        original_face_by_key = {face_identity_key(face): face for face in original_faces if len(face) >= 3}
        current_face_by_key = {face_identity_key(face): face for face in current_faces if len(face) >= 3}
        created_edge_keys = topology_delta_created_edge_keys(topology_delta)
        created_face_keys = topology_delta_created_face_keys(topology_delta)
        candidate_new_edge_keys = created_edge_keys or (set(current_edge_by_key) - set(original_mesh_edge_by_key))
        candidate_new_face_keys = created_face_keys or (set(current_face_by_key) - set(original_face_by_key))
        semantic_new_beam_keys = {
            key
            for key in set(current_edge_by_key) - set(original_edge_by_key)
            if current_edge_semantic_by_key.get(key) == JBEAM_EDGE_SEMANTIC_BEAM
        }
        candidate_new_beam_keys = set(candidate_new_edge_keys) | semantic_new_beam_keys
        current_beam_key_set = {
            key
            for key in set(current_edge_by_key)
            if current_edge_semantic_by_key.get(key, JBEAM_EDGE_SEMANTIC_RELATIONSHIP) == JBEAM_EDGE_SEMANTIC_BEAM
        }
        edge_params_by_key = params_by_topology_key(current_edges, edge_params)
        committed_edge_params_by_key = params_by_topology_key(current_edges, committed_edge_params)
        face_params_by_key = params_by_topology_key(current_faces, face_params)
        committed_face_params_by_key = params_by_topology_key(current_faces, committed_face_params)
        def all_nodes_owned_here(ids):
            return all(current_node_kind_by_id.get(str(node_id)) == "owned" for node_id in ids)

        append_reference_deletes_for_missing_nodes(
            mesh_changes,
            obj,
            deleted_proxy_node_ids,
            original_edges,
            original_faces,
            "deleted_proxy_reference",
        )

        for key in sorted(candidate_new_beam_keys & set(current_edge_by_key)):
            if current_edge_semantic_by_key.get(key) != JBEAM_EDGE_SEMANTIC_BEAM:
                continue
            ids = current_edge_by_key[key]
            mesh_changes.append(
                {
                    "file": obj.get("beamng_jbeam_path", ""),
                    "part": obj.get("beamng_part_name", ""),
                    "resolved_part_id": int(obj.get("beamng_resolved_part_id", -1)),
                    "section": "beams",
                    "row": "",
                    "field": "nodes",
                    "old": [],
                    "new": list(ids),
                    "params": edge_params_by_key.get(key, {}),
                    "operation": "insert",
                    "source_object": obj.name,
                }
            )
        for key in sorted((current_beam_key_set & set(original_edge_by_key))):
            params = edge_params_by_key.get(key, {})
            committed_params = committed_edge_params_by_key.get(key, {})
            if params != committed_params:
                ids = current_edge_by_key[key]
                mesh_changes.append(
                    {
                        "file": obj.get("beamng_jbeam_path", ""),
                        "part": obj.get("beamng_part_name", ""),
                        "resolved_part_id": int(obj.get("beamng_resolved_part_id", -1)),
                        "section": "beams",
                        "row": "",
                        "field": "params",
                        "old": committed_params,
                        "new": params,
                        "nodes": list(ids),
                        "operation": "update",
                        "source_object": obj.name,
                    }
                )
        for key in sorted(set(original_edge_by_key) - current_beam_key_set):
            ids = original_edge_by_key[key]
            if not all_nodes_owned_here(ids[:2]):
                continue
            retired_guid = original_beam_key_to_guid.get("|".join(key), "")
            mesh_changes.append(
                {
                    "file": obj.get("beamng_jbeam_path", ""),
                    "part": obj.get("beamng_part_name", ""),
                    "resolved_part_id": int(obj.get("beamng_resolved_part_id", -1)),
                    "section": "beams",
                    "row": "",
                    "field": "nodes",
                    "old": list(ids),
                    "new": [],
                    "operation": "delete",
                    "source_object": obj.name,
                    "retired_topology_guid": retired_guid,
                    "tombstone": {"topology_guid": retired_guid, "kind": "beam"} if retired_guid else {},
                }
            )
        existing_beam_deletes = {
            edge_key(change.get("old", []))
            for change in mesh_changes
            if change.get("section") == "beams" and change.get("operation") == "delete"
        }
        for key, ids in sorted(original_edge_by_key.items()):
            if key in existing_beam_deletes:
                continue
            if not any(str(node_id) in deleted_node_ids for node_id in ids[:2]):
                continue
            mesh_changes.append(
                {
                    "file": obj.get("beamng_jbeam_path", ""),
                    "part": obj.get("beamng_part_name", ""),
                    "resolved_part_id": int(obj.get("beamng_resolved_part_id", -1)),
                    "section": "beams",
                    "row": "",
                    "field": "nodes",
                    "old": list(ids),
                    "new": [],
                    "operation": "delete",
                    "source_object": obj.name,
                    "reason": "deleted_node_reference",
                    "retired_topology_guid": original_beam_key_to_guid.get("|".join(key), ""),
                }
            )
        for key in sorted(candidate_new_face_keys & set(current_face_by_key)):
            ids = current_face_by_key[key]
            mesh_changes.append(
                {
                    "file": obj.get("beamng_jbeam_path", ""),
                    "part": obj.get("beamng_part_name", ""),
                    "resolved_part_id": int(obj.get("beamng_resolved_part_id", -1)),
                    "section": "triangles",
                    "row": "",
                    "field": "nodes",
                    "old": [],
                    "new": list(ids),
                    "params": face_params_by_key.get(key, {}),
                    "operation": "insert",
                    "source_object": obj.name,
                }
            )
        for key in sorted((set(current_face_by_key) & set(original_face_by_key))):
            params = face_params_by_key.get(key, {})
            committed_params = committed_face_params_by_key.get(key, {})
            original_ids = [str(node_id) for node_id in original_face_by_key.get(key, [])[:3]]
            current_ids = [str(node_id) for node_id in current_face_by_key.get(key, [])[:3]]
            if original_ids and current_ids and original_ids != current_ids:
                mesh_changes.append(
                    {
                        "file": obj.get("beamng_jbeam_path", ""),
                        "part": obj.get("beamng_part_name", ""),
                        "resolved_part_id": int(obj.get("beamng_resolved_part_id", -1)),
                        "section": "triangles",
                        "row": "",
                        "field": "nodes",
                        "old": original_ids,
                        "new": current_ids,
                        "operation": "update",
                        "source_object": obj.name,
                        "reason": "triangle_winding_changed",
                    }
                )
            if params != committed_params:
                ids = current_face_by_key[key]
                mesh_changes.append(
                    {
                        "file": obj.get("beamng_jbeam_path", ""),
                        "part": obj.get("beamng_part_name", ""),
                        "resolved_part_id": int(obj.get("beamng_resolved_part_id", -1)),
                        "section": "triangles",
                        "row": "",
                        "field": "params",
                        "old": committed_params,
                        "new": params,
                        "nodes": list(ids),
                        "operation": "update",
                        "source_object": obj.name,
                    }
                )
        for key in sorted(set(original_face_by_key) - set(current_face_by_key)):
            ids = original_face_by_key[key]
            if not all_nodes_owned_here(ids[:3]):
                continue
            mesh_changes.append(
                {
                    "file": obj.get("beamng_jbeam_path", ""),
                    "part": obj.get("beamng_part_name", ""),
                    "resolved_part_id": int(obj.get("beamng_resolved_part_id", -1)),
                    "section": "triangles",
                    "row": "",
                    "field": "nodes",
                    "old": list(ids),
                    "new": [],
                    "operation": "delete",
                    "source_object": obj.name,
                }
            )
        existing_triangle_deletes = {
            face_identity_key(change.get("old", []))
            for change in mesh_changes
            if change.get("section") == "triangles" and change.get("operation") == "delete"
        }
        for key, ids in sorted(original_face_by_key.items()):
            if key in existing_triangle_deletes:
                continue
            if not any(str(node_id) in deleted_node_ids for node_id in ids[:3]):
                continue
            mesh_changes.append(
                {
                    "file": obj.get("beamng_jbeam_path", ""),
                    "part": obj.get("beamng_part_name", ""),
                    "resolved_part_id": int(obj.get("beamng_resolved_part_id", -1)),
                    "section": "triangles",
                    "row": "",
                    "field": "nodes",
                    "old": list(ids),
                    "new": [],
                    "operation": "delete",
                    "source_object": obj.name,
                    "reason": "deleted_node_reference",
                }
            )

        mesh_topology_changes = [
            change for change in mesh_changes if change.get("section") in {"beams", "triangles"}
        ]
        topology_change_count += len(mesh_topology_changes)
        if mesh_changes:
            dirty_mesh_count += 1
        mesh["beamng_node_move_changes_json"] = json.dumps(mesh_changes)
        obj["beamng_dirty_node_move_count"] = len(mesh_changes)
        obj["beamng_dirty_topology_change_count"] = len(mesh_topology_changes)
        changes.extend(mesh_changes)

    cascade_changes = []
    removed_proxy_count = 0
    for source_object_name, deleted_node_ids in owned_node_deletes_by_object.items():
        cascade_changes.extend(
            proxy_reference_delete_changes_for_owned_deletes(scene, source_object_name, deleted_node_ids)
        )
        removed = remove_matching_proxy_vertices_for_owned_deletes(scene, source_object_name, deleted_node_ids)
        removed_proxy_count += int(removed.get("removed", 0))
    if cascade_changes:
        cascade_by_object = defaultdict(list)
        for change in cascade_changes:
            cascade_by_object[change.get("source_object", "")].append(change)
        for object_name, object_changes in cascade_by_object.items():
            obj = bpy.data.objects.get(object_name)
            if obj is None or obj.type != "MESH":
                continue
            existing = mesh_json_list(obj.data, "beamng_node_move_changes_json")
            existing.extend(object_changes)
            obj.data["beamng_node_move_changes_json"] = json.dumps(existing)
            obj["beamng_dirty_node_move_count"] = len(existing)
            obj["beamng_dirty_topology_change_count"] = int(obj.get("beamng_dirty_topology_change_count", 0)) + len(object_changes)
        topology_change_count += len(cascade_changes)
        changes.extend(cascade_changes)
        dirty_mesh_count += len(cascade_by_object)
    if removed_proxy_count:
        scene["beamng_jbeam_removed_proxy_node_count"] = (
            int(scene.get("beamng_jbeam_removed_proxy_node_count", 0)) + removed_proxy_count
        )

    scene["beamng_jbeam_pending_node_moves_json"] = json.dumps(changes)
    scene["beamng_jbeam_pending_node_move_count"] = len(changes)
    scene["beamng_jbeam_pending_topology_change_count"] = topology_change_count
    scene["beamng_jbeam_restored_proxy_move_count"] = restored_proxy_count
    scene["beamng_jbeam_non_triangle_face_count"] = non_triangle_face_count
    return {
        "changes": changes,
        "restored_proxy_count": restored_proxy_count,
        "topology_change_count": topology_change_count,
        "scanned_mesh_count": scanned_mesh_count,
        "dirty_mesh_count": dirty_mesh_count,
        "non_triangle_face_count": non_triangle_face_count,
    }


def accept_experimental_jbeam_node_moves(scene):
    try:
        changes = json.loads(scene.get("beamng_jbeam_pending_node_moves_json", "[]"))
    except (TypeError, json.JSONDecodeError):
        changes = []
    if not changes:
        return {"accepted_count": 0, "history_count": int(scene.get("beamng_jbeam_operation_history_count", 0))}

    try:
        history = json.loads(scene.get("beamng_jbeam_operation_history_json", "[]"))
    except (TypeError, json.JSONDecodeError):
        history = []

    accepted_at = datetime.now().isoformat(timespec="seconds")
    accepted_changes = []
    for change in changes:
        operation = dict(change)
        operation["accepted_at"] = accepted_at
        operation["status"] = "accepted"
        accepted_changes.append(operation)

    changes_by_object = defaultdict(list)
    for change in accepted_changes:
        changes_by_object[change.get("source_object", "")].append(change)

    for object_name, object_changes in changes_by_object.items():
        obj = bpy.data.objects.get(object_name)
        if obj is None or obj.type != "MESH":
            continue
        mesh = obj.data
        ensure_experimental_mesh_identity(obj, scene)
        try:
            original_positions = json.loads(mesh.get("beamng_original_node_positions_json", "[]"))
        except (TypeError, json.JSONDecodeError):
            original_positions = []
        committed_flags = [bool(value) for value in mesh_json_list(mesh, "beamng_node_committed_flags_json")]
        try:
            original_edges = json.loads(mesh.get("beamng_edge_node_ids_json", "[]"))
        except (TypeError, json.JSONDecodeError):
            original_edges = []
        node_uids = ensure_experimental_topology_uids(obj, allow_write=True).get("nodes", [])
        uid_to_baseline = mesh_json_dict(mesh, "beamng_node_uid_to_original_position_json")
        uid_to_committed = mesh_json_dict(mesh, "beamng_node_uid_to_committed_json")
        node_params = mesh_json_list(mesh, "beamng_node_params_json")
        edge_params = mesh_json_list(mesh, "beamng_edge_params_json")
        face_params = mesh_json_list(mesh, "beamng_face_params_json")
        for change in object_changes:
            if change.get("section") != "nodes" or change.get("field") != "position":
                continue
            vertex_index = change.get("vertex_index")
            if not isinstance(vertex_index, int) or vertex_index < 0:
                continue
            while vertex_index >= len(original_positions):
                original_positions.append(change.get("new", [0, 0, 0]))
            original_positions[vertex_index] = change.get("new", original_positions[vertex_index])
            while vertex_index >= len(committed_flags):
                committed_flags.append(True)
            committed_flags[vertex_index] = True
            uid_key = topology_uid_key(node_uids[vertex_index]) if vertex_index < len(node_uids) else ""
            if uid_key:
                uid_to_baseline[uid_key] = change.get("new", original_positions[vertex_index])
                uid_to_committed[uid_key] = True
        if original_positions:
            mesh["beamng_original_node_positions_json"] = json.dumps(original_positions)
        if committed_flags:
            mesh["beamng_node_committed_flags_json"] = json.dumps(committed_flags)
            mesh["beamng_node_uid_to_original_position_json"] = json.dumps(uid_to_baseline)
            mesh["beamng_node_uid_to_committed_json"] = json.dumps(uid_to_committed)
        if node_params:
            mesh["beamng_node_committed_params_json"] = json.dumps(node_params)
            mesh["beamng_node_uid_to_committed_params_json"] = json.dumps(
                {topology_uid_key(uid): params for uid, params in zip(node_uids, node_params) if topology_uid_key(uid)}
            )
        current_edges, current_faces = read_experimental_mesh_topology(obj)
        if any(change.get("section") in {"beams", "triangles"} for change in object_changes):
            mesh["beamng_mesh_edge_node_ids_json"] = json.dumps([list(edge) for edge in current_edges])
        if any(change.get("section") == "beams" for change in object_changes):
            beam_edges_by_key = {edge_key(edge): list(edge) for edge in original_edges if len(edge) >= 2}
            for change in object_changes:
                if change.get("section") != "beams":
                    continue
                if change.get("operation") == "delete":
                    beam_edges_by_key.pop(edge_key(change.get("old", [])), None)
                elif change.get("operation") == "insert":
                    nodes = list(change.get("new", []))
                    if len(nodes) >= 2:
                        beam_edges_by_key[edge_key(nodes)] = nodes[:2]
            mesh["beamng_edge_node_ids_json"] = json.dumps(list(beam_edges_by_key.values()))
            mesh["beamng_edge_committed_params_json"] = json.dumps(edge_params)
            edge_uids = ensure_experimental_topology_uids(obj, allow_write=True).get("edges", [])
            mesh["beamng_edge_uid_to_committed_params_json"] = json.dumps(
                {topology_uid_key(uid): params for uid, params in zip(edge_uids, edge_params) if topology_uid_key(uid)}
            )
        if any(change.get("section") == "triangles" for change in object_changes):
            mesh["beamng_face_node_ids_json"] = json.dumps([list(face) for face in current_faces])
            mesh["beamng_face_committed_params_json"] = json.dumps(face_params)
            face_uids = ensure_experimental_topology_uids(obj, allow_write=True).get("faces", [])
            mesh["beamng_face_uid_to_committed_params_json"] = json.dumps(
                {topology_uid_key(uid): params for uid, params in zip(face_uids, face_params) if topology_uid_key(uid)}
            )
        semantic_topology_snapshot_for_object(obj, scene, allow_write=True)
        mesh["beamng_node_move_changes_json"] = json.dumps([])
        obj["beamng_dirty_node_move_count"] = 0
        obj["beamng_dirty_topology_change_count"] = 0

    history.extend(accepted_changes)
    scene["beamng_jbeam_operation_history_json"] = json.dumps(history)
    scene["beamng_jbeam_operation_history_count"] = len(history)
    scene["beamng_jbeam_pending_node_moves_json"] = json.dumps([])
    scene["beamng_jbeam_pending_node_move_count"] = 0
    scene["beamng_jbeam_pending_topology_change_count"] = 0
    scene["beamng_jbeam_restored_proxy_move_count"] = 0
    scene["beamng_jbeam_dirty"] = True
    refresh_authoring_model_operations_from_history(scene)
    return {"accepted_count": len(accepted_changes), "history_count": len(history)}


def raw_jbeam_operation_history(scene):
    try:
        history = json.loads(scene.get("beamng_jbeam_operation_history_json", "[]"))
    except (TypeError, json.JSONDecodeError):
        history = []
    return history if isinstance(history, list) else []


def jbeam_operation_history(scene):
    model = current_authoring_model(scene)
    if model is not None and getattr(model, "operations", None):
        return model.operation_dicts(status="accepted")
    return raw_jbeam_operation_history(scene)


def jbeam_history_counts(history):
    counts = defaultdict(int)
    for operation in history:
        section = operation.get("section", "")
        operation_type = operation.get("operation", "")
        counts[section] += 1
        counts[f"{section}_{operation_type}"] += 1
    return counts


def commit_exported_jbeam_mesh_baselines(scene, exported_history):
    changes_by_object = defaultdict(list)
    for operation in exported_history:
        object_name = operation.get("source_object", "")
        if object_name:
            changes_by_object[object_name].append(operation)

    for object_name, object_changes in changes_by_object.items():
        obj = bpy.data.objects.get(object_name)
        if obj is None or obj.type != "MESH" or obj.get("beamng_visual_type") != "experimental_jbeam_mesh":
            continue
        mesh = obj.data
        identity = ensure_experimental_mesh_identity(obj, scene, allow_write=True)
        node_ids = [str(node_id) for node_id in identity.get("node_ids", [])]
        node_uids = ensure_experimental_topology_uids(obj, allow_write=True).get("nodes", [])
        _edit_mesh, current_positions = read_experimental_mesh_vertices(obj)
        current_edges, current_faces = read_experimental_mesh_topology(obj)
        current_beam_edges = semantic_beam_edges_for_object(
            obj, scene, current_edges=current_edges, allow_write=True
        )
        node_params = list(identity.get("node_params", []))
        edge_params = mesh_json_list(mesh, "beamng_edge_params_json")
        face_params = mesh_json_list(mesh, "beamng_face_params_json")

        deleted_node_ids = {
            str(change.get("row", change.get("node", "")))
            for change in object_changes
            if change.get("section") == "nodes" and change.get("operation") == "delete"
        }
        if deleted_node_ids:
            for map_key in (
                "beamng_node_uid_to_id_json",
                "beamng_node_uid_to_kind_json",
                "beamng_node_uid_to_owner_part_id_json",
                "beamng_node_uid_to_original_position_json",
                "beamng_node_uid_to_generated_json",
                "beamng_node_uid_to_committed_json",
                "beamng_node_uid_to_params_json",
                "beamng_node_uid_to_committed_params_json",
            ):
                uid_map = mesh_json_dict(mesh, map_key)
                if not uid_map:
                    continue
                id_map = mesh_json_dict(mesh, "beamng_node_uid_to_id_json")
                mesh[map_key] = json.dumps(
                    {
                        uid: value
                        for uid, value in uid_map.items()
                        if str(id_map.get(uid, "")) not in deleted_node_ids
                    }
                )

        mesh["beamng_original_node_positions_json"] = json.dumps(
            [rounded_position_list(position) for position in current_positions]
        )
        mesh["beamng_node_committed_flags_json"] = json.dumps([True for _node_id in node_ids])
        mesh["beamng_node_committed_params_json"] = json.dumps(node_params)
        mesh["beamng_edge_node_ids_json"] = json.dumps([list(edge) for edge in current_beam_edges])
        mesh["beamng_mesh_edge_node_ids_json"] = json.dumps([list(edge) for edge in current_edges])
        mesh["beamng_edge_committed_params_json"] = json.dumps(edge_params)
        mesh["beamng_face_node_ids_json"] = json.dumps([list(face) for face in current_faces])
        mesh["beamng_face_committed_params_json"] = json.dumps(face_params)

        uid_to_baseline = mesh_json_dict(mesh, "beamng_node_uid_to_original_position_json")
        uid_to_committed = mesh_json_dict(mesh, "beamng_node_uid_to_committed_json")
        uid_to_committed_params = mesh_json_dict(mesh, "beamng_node_uid_to_committed_params_json")
        for index, uid in enumerate(node_uids):
            key = topology_uid_key(uid)
            if not key:
                continue
            if index < len(current_positions):
                uid_to_baseline[key] = rounded_position_list(current_positions[index])
            uid_to_committed[key] = True
            if index < len(node_params):
                uid_to_committed_params[key] = node_params[index]
        mesh["beamng_node_uid_to_original_position_json"] = json.dumps(uid_to_baseline)
        mesh["beamng_node_uid_to_committed_json"] = json.dumps(uid_to_committed)
        mesh["beamng_node_uid_to_committed_params_json"] = json.dumps(uid_to_committed_params)

        edge_uids = ensure_experimental_topology_uids(obj, allow_write=True).get("edges", [])
        face_uids = ensure_experimental_topology_uids(obj, allow_write=True).get("faces", [])
        mesh["beamng_edge_uid_to_committed_params_json"] = json.dumps(
            {topology_uid_key(uid): params for uid, params in zip(edge_uids, edge_params) if topology_uid_key(uid)}
        )
        mesh["beamng_face_uid_to_committed_params_json"] = json.dumps(
            {topology_uid_key(uid): params for uid, params in zip(face_uids, face_params) if topology_uid_key(uid)}
        )
        mesh["beamng_node_move_changes_json"] = json.dumps([])
        obj["beamng_dirty_node_move_count"] = 0
        obj["beamng_dirty_topology_change_count"] = 0


def checkpoint_exported_jbeam_operation_history(
    scene,
    stage_manifest_path,
    stage_manifest,
    label,
    selected_virtual_paths=None,
    current_folder=None,
):
    history = jbeam_operation_history(scene)
    if not history:
        return {"cleared": False, "checkpoint_path": "", "reason": "No operation history to checkpoint"}
    if int(stage_manifest.get("skipped_file_count", 0)) != 0:
        return {
            "cleared": False,
            "checkpoint_path": "",
            "reason": "Skipped files remain; keeping operation history active",
        }

    selected_set = {normalize_virtual_path(path) for path in selected_virtual_paths or [] if path}
    if selected_set:
        exported_history = []
        remaining_history = []
        for operation in history:
            virtual_path = virtual_jbeam_path_from_source(operation.get("file", ""), current_folder=current_folder)
            if normalize_virtual_path(virtual_path) in selected_set:
                exported_history.append(operation)
            else:
                remaining_history.append(operation)
        if not exported_history:
            return {
                "cleared": False,
                "checkpoint_path": "",
                "reason": "No operation history entries matched the selected export files",
            }
    else:
        exported_history = list(history)
        remaining_history = []

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    checkpoint_dir = persistent_cache_dir() / "jbeam_editor" / "exported_operation_checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = checkpoint_dir / f"jbeam_exported_operations_{stamp}.json"
    checkpoint = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "label": label,
        "stage_manifest_path": str(stage_manifest_path),
        "operation_count": len(exported_history),
        "remaining_operation_count": len(remaining_history),
        "selected_virtual_paths": sorted(selected_set),
        "operations": exported_history,
    }
    checkpoint_path.write_text(json.dumps(checkpoint, indent=2), encoding="utf-8")
    commit_exported_jbeam_mesh_baselines(scene, exported_history)

    stage_manifest["operation_history_checkpoint_path"] = str(checkpoint_path)
    stage_manifest["operation_history_cleared"] = len(remaining_history) == 0
    stage_manifest["operation_history_remaining_count"] = len(remaining_history)
    Path(stage_manifest_path).write_text(json.dumps(stage_manifest, indent=2), encoding="utf-8")
    text = bpy.data.texts.get("BeamNG Staged JBeam Mod Overrides") or bpy.data.texts.new(
        "BeamNG Staged JBeam Mod Overrides"
    )
    text.clear()
    text.write("\n".join(jbeam_user_override_stage_lines(stage_manifest)))
    text.write("\n")

    scene["beamng_jbeam_last_export_checkpoint_path"] = str(checkpoint_path)
    scene["beamng_jbeam_export_checkpoint_count"] = int(scene.get("beamng_jbeam_export_checkpoint_count", 0)) + 1
    scene["beamng_jbeam_operation_history_json"] = json.dumps(remaining_history)
    scene["beamng_jbeam_operation_history_count"] = len(remaining_history)
    scene["beamng_jbeam_pending_node_moves_json"] = json.dumps([])
    scene["beamng_jbeam_pending_node_move_count"] = 0
    scene["beamng_jbeam_pending_topology_change_count"] = 0
    scene["beamng_jbeam_dirty"] = bool(remaining_history)
    refresh_authoring_model_operations_from_history(scene)
    return {"cleared": len(remaining_history) == 0, "checkpoint_path": str(checkpoint_path), "reason": ""}


def build_jbeam_edit_preview(history):
    grouped_files = {}
    for operation in history:
        file_key = operation.get("file", "") or "<unknown file>"
        part_key = operation.get("part", "") or "<unknown part>"
        file_group = grouped_files.setdefault(file_key, {"file": file_key, "parts": {}})
        part_group = file_group["parts"].setdefault(part_key, {"part": part_key, "operations": []})
        part_group["operations"].append(operation)

    files = []
    for file_key in sorted(grouped_files):
        file_group = grouped_files[file_key]
        parts = []
        for part_key in sorted(file_group["parts"]):
            part_group = file_group["parts"][part_key]
            parts.append(
                {
                    "part": part_group["part"],
                    "operation_count": len(part_group["operations"]),
                    "operations": part_group["operations"],
                }
            )
        files.append(
            {
                "file": file_group["file"],
                "operation_count": sum(part["operation_count"] for part in parts),
                "parts": parts,
            }
        )

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "operation_count": len(history),
        "files": files,
    }


def jbeam_edit_preview_lines(preview):
    lines = [
        "[BeamNG JBeam Edit Preview]",
        f"Generated: {preview['generated_at']}",
        f"Accepted operations: {preview['operation_count']}",
        "",
    ]
    for file_group in preview["files"]:
        lines.append(f"File: {file_group['file']}")
        lines.append(f"Operations: {file_group['operation_count']}")
        for part_group in file_group["parts"]:
            lines.append(f"  Part: {part_group['part']} ({part_group['operation_count']} operation(s))")
            for operation in part_group["operations"]:
                lines.append(
                    "    "
                    f"{operation.get('operation', '')} "
                    f"{operation.get('section', '')}.{operation.get('row', '')}.{operation.get('field', '')}: "
                    f"{operation.get('old', '')} -> {operation.get('new', '')}"
                )
        lines.append("")
    if not preview["files"]:
        lines.append("No accepted JBeam edit operations are recorded.")
    return lines


def write_jbeam_edit_preview_report(scene):
    history = jbeam_operation_history(scene)
    preview = build_jbeam_edit_preview(history)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_dir = persistent_cache_dir() / "jbeam_editor"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"jbeam_edit_preview_{stamp}.json"
    report_path.write_text(json.dumps(preview, indent=2), encoding="utf-8")

    text = bpy.data.texts.get("BeamNG JBeam Edit Preview") or bpy.data.texts.new("BeamNG JBeam Edit Preview")
    text.clear()
    text.write("\n".join(jbeam_edit_preview_lines(preview)))
    text.write("\n")

    scene["beamng_jbeam_last_edit_preview_path"] = str(report_path)
    return report_path, preview


def build_jbeam_node_patch_draft(history):
    editable_operations = [
        operation
        for operation in history
        if (
            operation.get("section") == "nodes"
            and operation.get("field") in {"position", "params"}
            and operation.get("operation") in {"insert", "update", "delete"}
        )
        or (
            operation.get("section") in {"beams", "triangles"}
            and operation.get("field") in {"nodes", "params"}
            and operation.get("operation") in {"insert", "delete", "update"}
        )
    ]

    grouped_files = {}
    for operation in editable_operations:
        file_key = operation.get("file", "") or "<unknown file>"
        part_key = operation.get("part", "") or "<unknown part>"
        file_group = grouped_files.setdefault(
            file_key,
            {
                "source_file": file_key,
                "safe_to_write": False,
                "parts": {},
            },
        )
        part_group = file_group["parts"].setdefault(
            part_key,
            {
                "part": part_key,
                "node_inserts": [],
                "node_updates": [],
                "node_deletes": [],
                "node_param_updates": [],
                "beam_inserts": [],
                "beam_deletes": [],
                "beam_param_updates": [],
                "triangle_inserts": [],
                "triangle_deletes": [],
                "triangle_node_updates": [],
                "triangle_param_updates": [],
            },
        )
        if operation.get("section") == "nodes":
            if operation.get("field") == "params":
                item = {
                    "node": operation.get("row", ""),
                    "old_position": [],
                    "new_position": [],
                    "accepted_at": operation.get("accepted_at", ""),
                    "source_object": operation.get("source_object", ""),
                    "resolved_part_id": operation.get("resolved_part_id", -1),
                    "vertex_index": operation.get("vertex_index", -1),
                    "params": operation.get("new", {}) if isinstance(operation.get("new", {}), dict) else {},
                }
                item["old_params"] = operation.get("old", {})
                part_group.setdefault("node_param_updates", []).append(item)
            else:
                item = {
                    "node": operation.get("row", ""),
                    "old_position": rounded_position_list(operation.get("old", [])) if operation.get("old") else [],
                    "new_position": rounded_position_list(operation.get("new", [])),
                    "accepted_at": operation.get("accepted_at", ""),
                    "source_object": operation.get("source_object", ""),
                    "resolved_part_id": operation.get("resolved_part_id", -1),
                    "vertex_index": operation.get("vertex_index", -1),
                    "params": operation.get("params", {}) if isinstance(operation.get("params", {}), dict) else {},
                }
                if operation.get("operation") == "insert":
                    part_group["node_inserts"].append(item)
                elif operation.get("operation") == "delete":
                    part_group["node_deletes"].append(item)
                else:
                    part_group["node_updates"].append(item)
        elif operation.get("section") == "beams":
            if operation.get("field") == "params":
                key = "beam_param_updates"
                node_ids = operation.get("nodes", [])
            else:
                key = "beam_inserts" if operation.get("operation") == "insert" else "beam_deletes"
                node_ids = operation.get("new") if operation.get("operation") == "insert" else operation.get("old")
            part_group[key].append(
                {
                    "nodes": [str(node_id) for node_id in (node_ids or [])[:2]],
                    "params": operation.get("new", operation.get("params", {})) if operation.get("field") == "params" else operation.get("params", {}),
                    "accepted_at": operation.get("accepted_at", ""),
                    "source_object": operation.get("source_object", ""),
                    "resolved_part_id": operation.get("resolved_part_id", -1),
                }
            )
        elif operation.get("section") == "triangles":
            if operation.get("field") == "params":
                key = "triangle_param_updates"
                node_ids = operation.get("nodes", [])
            elif operation.get("operation") == "update":
                key = "triangle_node_updates"
                node_ids = operation.get("new", [])
            else:
                key = "triangle_inserts" if operation.get("operation") == "insert" else "triangle_deletes"
                node_ids = operation.get("new") if operation.get("operation") == "insert" else operation.get("old")
            part_group[key].append(
                {
                    "nodes": [str(node_id) for node_id in (node_ids or [])[:3]],
                    "old_nodes": [str(node_id) for node_id in (operation.get("old", []) or [])[:3]],
                    "params": operation.get("new", operation.get("params", {})) if operation.get("field") == "params" else operation.get("params", {}),
                    "accepted_at": operation.get("accepted_at", ""),
                    "source_object": operation.get("source_object", ""),
                    "resolved_part_id": operation.get("resolved_part_id", -1),
                }
            )

    files = []
    for file_key in sorted(grouped_files):
        file_group = grouped_files[file_key]
        parts = []
        for part_key in sorted(file_group["parts"]):
            part_group = file_group["parts"][part_key]
            node_insert_count = len(part_group["node_inserts"])
            node_update_count = len(part_group["node_updates"])
            node_delete_count = len(part_group["node_deletes"])
            beam_insert_count = len(part_group["beam_inserts"])
            beam_delete_count = len(part_group["beam_deletes"])
            beam_param_update_count = len(part_group["beam_param_updates"])
            triangle_insert_count = len(part_group["triangle_inserts"])
            triangle_delete_count = len(part_group["triangle_deletes"])
            triangle_node_update_count = len(part_group["triangle_node_updates"])
            triangle_param_update_count = len(part_group["triangle_param_updates"])
            parts.append(
                {
                    "part": part_group["part"],
                    "operation_count": (
                        node_insert_count
                        + node_update_count
                        + node_delete_count
                        + beam_insert_count
                        + beam_delete_count
                        + beam_param_update_count
                        + triangle_insert_count
                        + triangle_delete_count
                        + triangle_node_update_count
                        + triangle_param_update_count
                    ),
                    "node_insert_count": node_insert_count,
                    "node_update_count": node_update_count,
                    "node_delete_count": node_delete_count,
                    "beam_insert_count": beam_insert_count,
                    "beam_delete_count": beam_delete_count,
                    "beam_param_update_count": beam_param_update_count,
                    "triangle_insert_count": triangle_insert_count,
                    "triangle_delete_count": triangle_delete_count,
                    "triangle_node_update_count": triangle_node_update_count,
                    "triangle_param_update_count": triangle_param_update_count,
                    "node_inserts": part_group["node_inserts"],
                    "node_updates": part_group["node_updates"],
                    "node_deletes": part_group["node_deletes"],
                    "node_param_updates": part_group["node_param_updates"],
                    "beam_inserts": part_group["beam_inserts"],
                    "beam_deletes": part_group["beam_deletes"],
                    "beam_param_updates": part_group["beam_param_updates"],
                    "triangle_inserts": part_group["triangle_inserts"],
                    "triangle_deletes": part_group["triangle_deletes"],
                    "triangle_node_updates": part_group["triangle_node_updates"],
                    "triangle_param_updates": part_group["triangle_param_updates"],
                }
            )
        node_update_count = sum(
            part["node_insert_count"] + part["node_update_count"] + part["node_delete_count"]
            for part in parts
        )
        topology_update_count = sum(
            part["beam_insert_count"]
            + part["beam_delete_count"]
            + part["triangle_insert_count"]
            + part["triangle_delete_count"]
            + part["triangle_node_update_count"]
            for part in parts
        )
        files.append(
            {
                "source_file": file_group["source_file"],
                "safe_to_write": False,
                "operation_count": node_update_count + topology_update_count,
                "node_update_count": node_update_count,
                "topology_update_count": topology_update_count,
                "parts": parts,
            }
        )

    node_update_count = sum(1 for operation in editable_operations if operation.get("section") == "nodes")
    topology_update_count = len(editable_operations) - node_update_count
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "precision": JBEAM_POSITION_PRECISION,
        "cache_only": True,
        "operation_count": len(editable_operations),
        "node_update_count": node_update_count,
        "topology_update_count": topology_update_count,
        "warnings": [
            "Draft only: no vanilla, mod, or user override files were modified.",
            "Source-preserving text patching is available for node positions and Beam insert/delete edits; other topology edits use clean JSON fallback.",
        ],
        "files": files,
    }


def jbeam_node_patch_draft_lines(draft):
    lines = [
        "[BeamNG JBeam Node Patch Draft]",
        f"Generated: {draft['generated_at']}",
        f"Operations: {draft.get('operation_count', draft['node_update_count'])}",
        f"Node edits: {draft['node_update_count']}",
        f"Topology updates: {draft.get('topology_update_count', 0)}",
        f"Precision: {draft['precision']} decimal places",
        "Cache only: yes",
        "",
    ]
    for warning in draft["warnings"]:
        lines.append(f"Warning: {warning}")
    lines.append("")

    for file_group in draft["files"]:
        lines.append(f"File: {file_group['source_file']}")
        lines.append(f"Operations: {file_group.get('operation_count', file_group['node_update_count'])}")
        lines.append(f"Node edits: {file_group['node_update_count']}")
        lines.append(f"Topology updates: {file_group.get('topology_update_count', 0)}")
        lines.append("Write target: not selected yet")
        for part_group in file_group["parts"]:
            lines.append(
                f"  Part: {part_group['part']} "
                f"({part_group.get('operation_count', part_group['node_update_count'])} operation(s))"
            )
            for update in part_group.get("node_inserts", []):
                lines.append(
                    "    "
                    f"insert node {update.get('node', '')}: {update.get('new_position', '')}"
                )
            for update in part_group["node_updates"]:
                lines.append(
                    "    "
                    f"{update.get('node', '')}: "
                    f"{update.get('old_position', '')} -> {update.get('new_position', '')}"
                )
            for update in part_group.get("node_deletes", []):
                lines.append(f"    delete node {update.get('node', '')}: {update.get('old_position', '')}")
            for update in part_group.get("beam_inserts", []):
                lines.append(f"    insert beam: {update.get('nodes', '')}")
            for update in part_group.get("beam_deletes", []):
                lines.append(f"    delete beam: {update.get('nodes', '')}")
            for update in part_group.get("triangle_inserts", []):
                lines.append(f"    insert triangle: {update.get('nodes', '')}")
            for update in part_group.get("triangle_deletes", []):
                lines.append(f"    delete triangle: {update.get('nodes', '')}")
        lines.append("")
    if not draft["files"]:
        lines.append("No accepted JBeam edits are recorded.")
    return lines


def write_jbeam_node_patch_draft_report(scene):
    history = jbeam_operation_history(scene)
    draft = build_jbeam_node_patch_draft(history)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_dir = persistent_cache_dir() / "jbeam_editor"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"jbeam_node_patch_draft_{stamp}.json"
    report_path.write_text(json.dumps(draft, indent=2), encoding="utf-8")

    text = bpy.data.texts.get("BeamNG JBeam Node Patch Draft") or bpy.data.texts.new("BeamNG JBeam Node Patch Draft")
    text.clear()
    text.write("\n".join(jbeam_node_patch_draft_lines(draft)))
    text.write("\n")

    scene["beamng_jbeam_last_node_patch_draft_path"] = str(report_path)
    return report_path, draft


def virtual_jbeam_path_from_source(source_file, current_folder=None):
    source_text = normalize_virtual_path(source_file)
    if not source_text:
        return ""

    lower = source_text.lower()
    marker = "/vehicles/"
    if marker in lower:
        start = lower.index(marker) + 1
        return normalize_virtual_path(source_text[start:])
    if lower.startswith("vehicles/"):
        return normalize_virtual_path(source_text)

    if current_folder is not None:
        try:
            relative = Path(source_file).resolve().relative_to(current_folder.resolve())
            relative_text = normalize_virtual_path(relative)
            if relative_text.lower().startswith("vehicles/"):
                return relative_text
        except (OSError, ValueError):
            pass

    return ""


DEFAULT_JBEAM_EXPORT_MOD_NAME = "beamng_pc_importer_edits"


def safe_mod_folder_name(value):
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    safe_name = safe_name.strip("._")
    return safe_name or DEFAULT_JBEAM_EXPORT_MOD_NAME


def jbeam_export_mod_name(context):
    prefs = get_addon_preferences(context)
    configured = getattr(prefs, "jbeam_export_mod_name", "") if prefs else ""
    return safe_mod_folder_name(configured)


def existing_unpacked_mod_names(context):
    current_folder = user_current_folder_from_preferences(context)
    if current_folder is None:
        return []
    root = current_folder / "mods" / "unpacked"
    if not root.exists():
        return []
    try:
        return sorted(path.name for path in root.iterdir() if path.is_dir())
    except OSError:
        return []


def loose_current_vehicle_jbeam_path(current_folder, virtual_path):
    normalized = normalize_virtual_path(virtual_path)
    if current_folder is None or not normalized.lower().endswith(".jbeam"):
        return None
    if not normalized.lower().startswith("vehicles/"):
        return None
    return current_folder / Path(normalized)


def safe_jbeam_mod_override_target_for_virtual_path(context, current_folder, virtual_path):
    if current_folder is None or not virtual_path:
        return None, "BeamNG user folder is not configured" if current_folder is None else "Could not infer vehicles/... virtual path"

    normalized = normalize_virtual_path(virtual_path)
    if not normalized.lower().startswith("vehicles/"):
        return None, f"Refusing non-vehicle virtual path: {normalized}"
    if not normalized.lower().endswith(".jbeam"):
        return None, f"Refusing non-JBeam virtual path for JBeam export: {normalized}"

    mod_name = jbeam_export_mod_name(context)
    target_root = current_folder / "mods" / "unpacked" / mod_name
    target = target_root / Path(normalized)
    try:
        target.resolve().relative_to((target_root / "vehicles").resolve())
    except (OSError, ValueError):
        return None, f"Refusing target outside user current/mods/unpacked/{mod_name}/vehicles: {target}"
    return target, ""


def jbeam_authoring_vehicle_name(context):
    model = authoring_model_for_context(context)
    if model is not None and str(getattr(model, "vehicle_model", "")).strip():
        return safe_mod_folder_name(model.vehicle_model)
    scene_model = str(context.scene.get("beamng_slot_editor_model", "") or "").strip()
    if scene_model:
        return safe_mod_folder_name(scene_model)
    pc_path = current_import_pc_path(context.scene)
    if pc_path and pc_path.parent.name:
        return safe_mod_folder_name(pc_path.parent.name)
    return "vehicle"


def safe_jbeam_identifier(value, fallback):
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "").strip())
    safe = safe.strip("_")
    return safe or fallback


def new_jbeam_part_payload(part_name, slot_type, display_name="", child_slot_type="", child_default="", child_description=""):
    part = {
        "information": {
            "name": display_name or part_name,
            "authors": "BeamNG PC Importer",
        },
        "slotType": slot_type,
        "nodes": [
            ["id", "posX", "posY", "posZ"],
        ],
        "beams": [
            ["id1:", "id2:"],
        ],
        "triangles": [
            ["id1:", "id2:", "id3:"],
        ],
    }
    if child_slot_type:
        part["slots"] = [
            ["type", "default", "description"],
            [child_slot_type, child_default, child_description or child_slot_type],
        ]
    return {part_name: part}


def next_authored_jbeam_color_index(context):
    used = set()
    for obj in getattr(context.scene, "objects", []) or []:
        if obj.type != "MESH" or obj.get("beamng_visual_type") != "experimental_jbeam_mesh":
            continue
        for key in ("beamng_color_part_index", "beamng_resolved_part_id"):
            try:
                value = int(obj.get(key, -1))
            except (TypeError, ValueError):
                value = -1
            if value >= 0:
                used.add(value)
    index = 0
    while index in used:
        index += 1
    return index


def create_empty_experimental_jbeam_mesh_object(context, part_name, virtual_path):
    mesh = bpy.data.meshes.new(f"Experimental_JBeam_Mesh_{part_name}")
    mesh.from_pydata([], [], [])
    mesh.update()
    color_index = next_authored_jbeam_color_index(context)
    color = color_for_resolved_part(color_index)
    obj = bpy.data.objects.new(f"Experimental_JBeam_Mesh_{part_name}", mesh)
    context.collection.objects.link(obj)
    obj["beamng_visual_type"] = "experimental_jbeam_mesh"
    obj["beamng_layer"] = "jbeam"
    obj["beamng_part_name"] = part_name
    obj["beamng_jbeam_path"] = normalize_virtual_path(virtual_path)
    obj["beamng_is_new_jbeam_file"] = True
    obj["beamng_resolved_part_id"] = -1
    obj["beamng_color_part_index"] = color_index
    obj["beamng_owned_node_count"] = 0
    obj["beamng_proxy_node_count"] = 0
    obj["beamng_dirty_node_move_count"] = 0
    obj["beamng_dirty_topology_change_count"] = 0
    obj["beamng_proxy_sync_identity_ready"] = False
    obj.display_type = "TEXTURED"
    obj.show_in_front = False
    obj.show_wire = True
    obj.color = color
    mesh.materials.append(get_or_create_jbeam_mesh_material(f"Experimental JBeam Mesh Authored {color_index:03d}", color))
    mesh.materials.append(get_or_create_jbeam_edge_material(f"Experimental JBeam Mesh Authored {color_index:03d} Edges", color))
    mesh["beamng_node_ids_json"] = json.dumps([])
    mesh["beamng_node_kinds_json"] = json.dumps([])
    mesh["beamng_node_owner_part_ids_json"] = json.dumps([])
    mesh["beamng_original_node_positions_json"] = json.dumps([])
    mesh["beamng_node_generated_flags_json"] = json.dumps([])
    mesh["beamng_node_committed_flags_json"] = json.dumps([])
    mesh["beamng_node_params_json"] = json.dumps([])
    mesh["beamng_node_committed_params_json"] = json.dumps([])
    mesh["beamng_original_edge_node_ids_json"] = json.dumps([])
    mesh["beamng_mesh_edge_node_ids_json"] = json.dumps([])
    mesh["beamng_original_triangle_node_ids_json"] = json.dumps([])
    mesh["beamng_edge_params_json"] = json.dumps([])
    mesh["beamng_edge_committed_params_json"] = json.dumps([])
    mesh["beamng_face_params_json"] = json.dumps([])
    mesh["beamng_face_committed_params_json"] = json.dumps([])
    mesh["beamng_edge_uid_to_semantic_type_json"] = json.dumps({})
    mesh["beamng_edge_uid_to_semantic_state_json"] = json.dumps({})
    mesh["beamng_face_uid_to_semantic_type_json"] = json.dumps({})
    mesh["beamng_face_uid_to_semantic_state_json"] = json.dumps({})
    mesh["beamng_topology_revision"] = 0
    mesh["beamng_topology_signature_json"] = json.dumps({})
    mesh["beamng_semantic_topology_json"] = json.dumps({})
    mesh["beamng_previous_semantic_topology_json"] = json.dumps({})
    mesh["beamng_semantic_topology_delta_json"] = json.dumps({})
    mesh["beamng_semantic_topology_delta_count"] = 0
    ensure_experimental_topology_uids(obj, allow_write=True)
    semantic_topology_snapshot_for_object(obj, context.scene, allow_write=True)
    context.view_layer.objects.active = obj
    obj.select_set(True)
    return obj


def new_jbeam_payload_for_virtual_path(scene, virtual_path):
    normalized = normalize_virtual_path(virtual_path)
    if not normalized:
        return None
    for obj in getattr(scene, "objects", []) or []:
        if obj.type != "MESH" or obj.get("beamng_visual_type") != "experimental_jbeam_mesh":
            continue
        if normalize_virtual_path(obj.get("beamng_jbeam_path", "")) != normalized:
            continue
        payload_text = obj.data.get("beamng_new_jbeam_payload_json", "")
        if not payload_text:
            continue
        try:
            payload = json.loads(payload_text)
        except (TypeError, json.JSONDecodeError):
            continue
        return payload if isinstance(payload, dict) else None
    return None


def set_new_jbeam_payload_for_object(obj, payload):
    if obj is None or obj.type != "MESH":
        return
    obj["beamng_is_new_jbeam_file"] = True
    obj.data["beamng_new_jbeam_payload_json"] = json.dumps(payload, indent=2)


def staged_new_jbeam_file_groups(context):
    current_folder = user_current_folder_from_preferences(context)
    files = []
    seen = set()
    for obj in experimental_jbeam_mesh_objects(context.scene, active_only=False):
        if not bool(obj.get("beamng_is_new_jbeam_file", False)):
            continue
        virtual_path = normalize_virtual_path(obj.get("beamng_jbeam_path", ""))
        part_name = str(obj.get("beamng_part_name", "")) or "<new part>"
        if not virtual_path or virtual_path in seen:
            continue
        payload = new_jbeam_payload_for_virtual_path(context.scene, virtual_path)
        if payload is None:
            continue
        target_path, target_warning = safe_jbeam_mod_override_target_for_virtual_path(context, current_folder, virtual_path)
        files.append(
            {
                "source_file": virtual_path,
                "virtual_path": virtual_path,
                "planned_target_path": str(target_path) if target_path else "",
                "can_stage_override": target_path is not None,
                "is_new_file": True,
                "file_create_count": 1,
                "operation_count": 1,
                "node_update_count": 0,
                "topology_update_count": 0,
                "parts": [
                    {
                        "part": part_name,
                        "operation_count": 0,
                        "node_insert_count": 0,
                        "node_update_count": 0,
                        "node_delete_count": 0,
                        "beam_insert_count": 0,
                        "beam_delete_count": 0,
                        "beam_param_update_count": 0,
                        "triangle_insert_count": 0,
                        "triangle_delete_count": 0,
                        "triangle_param_update_count": 0,
                        "node_inserts": [],
                        "node_updates": [],
                        "node_deletes": [],
                        "node_param_updates": [],
                        "beam_inserts": [],
                        "beam_deletes": [],
                        "beam_param_updates": [],
                        "triangle_inserts": [],
                        "triangle_deletes": [],
                        "triangle_param_updates": [],
                    }
                ],
                "warnings": [target_warning] if target_warning else [],
            }
        )
        seen.add(virtual_path)
    return files


def build_jbeam_override_export_plan(context, history):
    draft = build_jbeam_node_patch_draft(history)
    current_folder = user_current_folder_from_preferences(context)
    mod_name = jbeam_export_mod_name(context)
    export_root = current_folder / "mods" / "unpacked" / mod_name if current_folder else None
    files_by_virtual_path = {
        normalize_virtual_path(file_group["virtual_path"]): file_group
        for file_group in staged_new_jbeam_file_groups(context)
    }
    warning_set = set(draft["warnings"])

    for file_group in draft["files"]:
        source_file = file_group["source_file"]
        virtual_path = virtual_jbeam_path_from_source(source_file, current_folder=current_folder)
        target_path, target_warning = safe_jbeam_mod_override_target_for_virtual_path(context, current_folder, virtual_path)
        can_stage = target_path is not None
        if target_warning:
            warning_set.add(target_warning)
        misplaced_path = loose_current_vehicle_jbeam_path(current_folder, virtual_path)
        file_warnings = [target_warning] if target_warning else []
        if misplaced_path and misplaced_path.exists():
            misplaced_warning = (
                f"Ignoring misplaced loose JBeam at {misplaced_path}; JBeam exports target "
                f"current/mods/unpacked/{mod_name}/vehicles instead."
            )
            warning_set.add(misplaced_warning)
            file_warnings.append(misplaced_warning)
        planned_group = files_by_virtual_path.setdefault(
            normalize_virtual_path(virtual_path),
            {
                "source_file": source_file,
                "virtual_path": virtual_path,
                "planned_target_path": str(target_path) if target_path else "",
                "can_stage_override": can_stage,
                "is_new_file": False,
                "file_create_count": 0,
                "operation_count": 0,
                "node_update_count": 0,
                "topology_update_count": 0,
                "parts": [],
                "warnings": [],
            },
        )
        planned_group["source_file"] = source_file
        planned_group["planned_target_path"] = str(target_path) if target_path else planned_group.get("planned_target_path", "")
        planned_group["can_stage_override"] = can_stage or bool(planned_group.get("can_stage_override"))
        planned_group["operation_count"] = int(planned_group.get("operation_count", 0)) + int(file_group.get("operation_count", file_group["node_update_count"]))
        planned_group["node_update_count"] = int(planned_group.get("node_update_count", 0)) + int(file_group["node_update_count"])
        planned_group["topology_update_count"] = int(planned_group.get("topology_update_count", 0)) + int(file_group.get("topology_update_count", 0))
        planned_group["parts"].extend(file_group["parts"])
        planned_group["warnings"] = sorted(set(planned_group.get("warnings", []) + file_warnings))

    files = [files_by_virtual_path[key] for key in sorted(files_by_virtual_path)]
    can_stage_count = sum(1 for file_group in files if file_group["can_stage_override"])
    file_create_count = sum(int(file_group.get("file_create_count", 0)) for file_group in files)
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "precision": JBEAM_POSITION_PRECISION,
        "cache_only": True,
        "operation_count": draft.get("operation_count", draft["node_update_count"]) + file_create_count,
        "file_create_count": file_create_count,
        "node_update_count": draft["node_update_count"],
        "topology_update_count": draft.get("topology_update_count", 0),
        "source_file_count": len(files),
        "stageable_file_count": can_stage_count,
        "user_current_folder": str(current_folder) if current_folder else "",
        "export_mod_folder": mod_name,
        "export_root": str(export_root) if export_root else "",
        "warnings": sorted(warning_set),
        "files": files,
    }


def jbeam_override_export_plan_lines(plan):
    lines = [
        "[BeamNG JBeam Override Export Plan]",
        f"Generated: {plan['generated_at']}",
        f"Operations: {plan.get('operation_count', plan['node_update_count'])}",
        f"New files: {plan.get('file_create_count', 0)}",
        f"Node edits: {plan['node_update_count']}",
        f"Topology updates: {plan.get('topology_update_count', 0)}",
        f"Source files: {plan['source_file_count']}",
        f"Stageable files: {plan['stageable_file_count']}",
        f"User current folder: {plan['user_current_folder'] or '(not configured)'}",
        f"JBeam export mod: {plan.get('export_mod_folder', '') or '(not configured)'}",
        f"JBeam export root: {plan.get('export_root', '') or '(not configured)'}",
        "Cache only: yes",
        "",
    ]
    for warning in plan["warnings"]:
        lines.append(f"Warning: {warning}")
    lines.append("")

    for file_group in plan["files"]:
        lines.append(f"Source: {file_group['source_file']}")
        lines.append(f"Virtual: {file_group['virtual_path'] or '(unknown)'}")
        lines.append(f"Planned target: {file_group['planned_target_path'] or '(not stageable)'}")
        lines.append(f"Can stage override: {'yes' if file_group['can_stage_override'] else 'no'}")
        if file_group.get("is_new_file"):
            lines.append("File status: new staged JBeam file")
        lines.append(f"Operations: {file_group.get('operation_count', file_group['node_update_count'])}")
        lines.append(f"Node edits: {file_group['node_update_count']}")
        lines.append(f"Topology updates: {file_group.get('topology_update_count', 0)}")
        for warning in file_group["warnings"]:
            lines.append(f"File warning: {warning}")
        for part_group in file_group["parts"]:
            lines.append(
                f"  Part: {part_group['part']} "
                f"({part_group.get('operation_count', part_group['node_update_count'])} operation(s))"
            )
            for update in part_group.get("node_inserts", []):
                lines.append(
                    "    "
                    f"insert node {update.get('node', '')}: {update.get('new_position', '')}"
                )
            for update in part_group["node_updates"]:
                lines.append(
                    "    "
                    f"{update.get('node', '')}: "
                    f"{update.get('old_position', '')} -> {update.get('new_position', '')}"
                )
            for update in part_group.get("node_deletes", []):
                lines.append(f"    delete node {update.get('node', '')}: {update.get('old_position', '')}")
            for update in part_group.get("beam_inserts", []):
                lines.append(f"    insert beam: {update.get('nodes', '')}")
            for update in part_group.get("beam_deletes", []):
                lines.append(f"    delete beam: {update.get('nodes', '')}")
            for update in part_group.get("triangle_inserts", []):
                lines.append(f"    insert triangle: {update.get('nodes', '')}")
            for update in part_group.get("triangle_deletes", []):
                lines.append(f"    delete triangle: {update.get('nodes', '')}")
        lines.append("")
    if not plan["files"]:
        lines.append("No accepted JBeam edits are recorded.")
    return lines


def write_jbeam_override_export_plan_report(context):
    history = jbeam_operation_history(context.scene)
    plan = build_jbeam_override_export_plan(context, history)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_dir = persistent_cache_dir() / "jbeam_editor"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"jbeam_override_export_plan_{stamp}.json"
    report_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")

    text = bpy.data.texts.get("BeamNG JBeam Override Export Plan") or bpy.data.texts.new(
        "BeamNG JBeam Override Export Plan"
    )
    text.clear()
    text.write("\n".join(jbeam_override_export_plan_lines(plan)))
    text.write("\n")

    context.scene["beamng_jbeam_last_override_export_plan_path"] = str(report_path)
    return report_path, plan


def populate_jbeam_export_file_selection(context):
    scene = context.scene
    if not hasattr(scene, "beamng_jbeam_export_file_items"):
        return []

    history = jbeam_operation_history(scene)
    plan = build_jbeam_override_export_plan(context, history)
    items = scene.beamng_jbeam_export_file_items
    previous_selection = {
        item.virtual_path: bool(item.include)
        for item in items
        if item.virtual_path
    }
    items.clear()

    for file_group in plan.get("files", []):
        virtual_path = normalize_virtual_path(file_group.get("virtual_path", ""))
        if not virtual_path:
            continue
        parts = [
            part_group.get("part", "")
            for part_group in file_group.get("parts", [])
            if part_group.get("part", "")
        ]
        parts_label = ", ".join(parts[:4])
        if len(parts) > 4:
            parts_label += f", +{len(parts) - 4} more"
        item = items.add()
        item.include = previous_selection.get(virtual_path, True)
        item.virtual_path = virtual_path
        item.source_file = file_group.get("source_file", "")
        item.parts_label = parts_label
        item.planned_target_path = file_group.get("planned_target_path", "")
        item.node_update_count = int(file_group.get("operation_count", file_group.get("node_update_count", 0)))
        item.topology_update_count = int(file_group.get("topology_update_count", 0))
        prefix = "NEW " if file_group.get("is_new_file") else ""
        item.label = (
            f"{prefix}{Path(virtual_path).name} "
            f"({item.node_update_count} edit(s), {item.topology_update_count} topology)"
        )

    scene["beamng_jbeam_export_selection_count"] = len(items)
    return list(items)


def selected_jbeam_export_virtual_paths(scene):
    if not hasattr(scene, "beamng_jbeam_export_file_items"):
        return None
    if len(scene.beamng_jbeam_export_file_items) == 0:
        return None
    selected = [
        normalize_virtual_path(item.virtual_path)
        for item in scene.beamng_jbeam_export_file_items
        if item.include and item.virtual_path
    ]
    return selected


class BEAMNG_OT_set_jbeam_export_selection(Operator):
    bl_idname = "beamng_pc_importer.set_jbeam_export_selection"
    bl_label = "Set JBeam Export Selection"
    bl_description = "Select or deselect all changed JBeam files in the export checklist"
    bl_options = {"REGISTER", "UNDO"}

    include: BoolProperty(default=True)

    def execute(self, context):
        items = getattr(context.scene, "beamng_jbeam_export_file_items", [])
        for item in items:
            item.include = bool(self.include)
        self.report({"INFO"}, f"{'Selected' if self.include else 'Deselected'} {len(items)} JBeam export file(s)")
        return {"FINISHED"}


class BEAMNG_OT_review_jbeam_export(Operator):
    bl_idname = "beamng_pc_importer.review_jbeam_export"
    bl_label = "Review JBeam Export"
    bl_description = "Open the changed-file export checklist and write a non-destructive review report"
    bl_options = {"REGISTER"}

    def invoke(self, context, _event):
        populate_jbeam_export_file_selection(context)
        return context.window_manager.invoke_props_dialog(self, width=820)

    def draw(self, context):
        draw_jbeam_export_selection(self.layout, context, overwrite_existing=False)

    def execute(self, context):
        selected_virtual_paths = selected_jbeam_export_virtual_paths(context.scene)
        if selected_virtual_paths is not None and not selected_virtual_paths:
            self.report({"WARNING"}, "No JBeam files selected for export review")
            return {"CANCELLED"}
        report_path, plan = write_jbeam_override_export_plan_report(context)
        if selected_virtual_paths is not None:
            plan = filter_plan_files_for_selected_virtual_paths(plan, selected_virtual_paths)
        if plan.get("operation_count", plan.get("node_update_count", 0)) == 0:
            self.report({"WARNING"}, "No accepted JBeam edits are recorded")
            return {"CANCELLED"}
        text = bpy.data.texts.get("BeamNG JBeam Export Review") or bpy.data.texts.new("BeamNG JBeam Export Review")
        text.clear()
        text.write("\n".join(jbeam_override_export_plan_lines(plan)))
        text.write("\n")
        context.scene["beamng_jbeam_last_export_review_path"] = str(report_path)
        self.report({"INFO"}, f"Reviewed {len(plan.get('files', []))} changed JBeam file(s): {report_path}")
        return {"FINISHED"}


def draw_jbeam_export_selection(layout, context, overwrite_existing=False):
    items = getattr(context.scene, "beamng_jbeam_export_file_items", [])
    box = layout.box()
    box.label(text=f"JBeam export mod: {jbeam_export_mod_name(context)}")
    box.label(text="Target: current/mods/unpacked/<mod>/vehicles")
    box.label(text="Changed JBeam files")

    if not items:
        box.label(text="No accepted JBeam edits are recorded.")
        return

    row = box.row(align=True)
    op = row.operator(BEAMNG_OT_set_jbeam_export_selection.bl_idname, text="All")
    op.include = True
    op = row.operator(BEAMNG_OT_set_jbeam_export_selection.bl_idname, text="None")
    op.include = False

    selected_paths = [
        normalize_virtual_path(item.virtual_path)
        for item in items
        if item.include and item.virtual_path
    ]
    history = jbeam_operation_history(context.scene)
    plan = build_jbeam_override_export_plan(context, history)
    plan = filter_plan_files_for_selected_virtual_paths(plan, selected_paths)
    counts = jbeam_export_preflight_counts(plan)
    summary = box.box()
    summary.label(text="Preflight selected edits")
    if counts.get("file_creates", 0):
        summary.label(text=f"New JBeam files: {counts.get('file_creates', 0)}")
    summary.label(
        text=(
            f"Nodes +{counts.get('node_inserts', 0)} / "
            f"update {counts.get('node_updates', 0)} / "
            f"-{counts.get('node_deletes', 0)}"
        )
    )
    summary.label(
        text=(
            f"Beams +{counts.get('beam_inserts', 0)} / "
            f"params {counts.get('beam_param_updates', 0)} / "
            f"-{counts.get('beam_deletes', 0)}"
        )
    )
    summary.label(
        text=(
            f"Triangles +{counts.get('triangle_inserts', 0)} / "
            f"params {counts.get('triangle_param_updates', 0)} / "
            f"-{counts.get('triangle_deletes', 0)}"
        )
    )

    for item in items:
        row = box.row(align=True)
        row.prop(item, "include", text="")
        col = row.column(align=True)
        col.label(text=item.label)
        if item.parts_label:
            col.label(text=f"Parts: {item.parts_label}")
        if item.planned_target_path:
            col.label(text=f"To: {item.planned_target_path}")
    selected_count = sum(1 for item in items if item.include)
    box.label(text=f"Selected: {selected_count}/{len(items)} file(s)")
    if overwrite_existing:
        warn = box.box()
        warn.alert = True
        warn.label(text="Existing unpacked mod JBeam files may be overwritten after backup.")


def filter_plan_files_for_selected_virtual_paths(plan, selected_virtual_paths):
    if selected_virtual_paths is None:
        return plan
    selected = {normalize_virtual_path(path) for path in selected_virtual_paths if path}
    filtered = dict(plan)
    filtered_files = [
        file_group
        for file_group in plan.get("files", [])
        if normalize_virtual_path(file_group.get("virtual_path", "")) in selected
    ]
    filtered["files"] = filtered_files
    filtered["source_file_count"] = len(filtered_files)
    filtered["stageable_file_count"] = sum(1 for item in filtered_files if item.get("can_stage_override"))
    filtered["operation_count"] = sum(int(item.get("operation_count", item.get("node_update_count", 0))) for item in filtered_files)
    filtered["file_create_count"] = sum(int(item.get("file_create_count", 0)) for item in filtered_files)
    filtered["node_update_count"] = sum(int(item.get("node_update_count", 0)) for item in filtered_files)
    filtered["topology_update_count"] = sum(int(item.get("topology_update_count", 0)) for item in filtered_files)
    if selected and not filtered_files:
        warnings = set(filtered.get("warnings", []))
        warnings.add("Selected export files did not match any accepted JBeam edit files.")
        filtered["warnings"] = sorted(warnings)
    return filtered


def jbeam_export_preflight_counts(plan):
    counts = defaultdict(int)
    for file_group in plan.get("files", []):
        counts["file_creates"] += int(file_group.get("file_create_count", 0))
        for part_group in file_group.get("parts", []):
            counts["node_inserts"] += len(part_group.get("node_inserts", []))
            counts["node_updates"] += len(part_group.get("node_updates", []))
            counts["node_deletes"] += len(part_group.get("node_deletes", []))
            counts["beam_inserts"] += len(part_group.get("beam_inserts", []))
            counts["beam_deletes"] += len(part_group.get("beam_deletes", []))
            counts["beam_param_updates"] += len(part_group.get("beam_param_updates", []))
            counts["triangle_inserts"] += len(part_group.get("triangle_inserts", []))
            counts["triangle_deletes"] += len(part_group.get("triangle_deletes", []))
            counts["triangle_param_updates"] += len(part_group.get("triangle_param_updates", []))
    return counts


def payload_node_ids(payload):
    node_ids = set()
    if not isinstance(payload, dict):
        return node_ids
    for part_data in payload.values():
        if not isinstance(part_data, dict):
            continue
        rows = part_data.get("nodes", [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, list) and row:
                node_ids.add(str(row[0]))
    return node_ids


def proxy_node_ids_for_file_part(scene, source_file, part_name):
    proxy_ids = set()
    normalized_source = normalize_virtual_path(source_file)
    for obj in experimental_jbeam_mesh_objects(scene, active_only=False):
        if normalize_virtual_path(obj.get("beamng_jbeam_path", "")) != normalized_source:
            continue
        if str(obj.get("beamng_part_name", "")) != str(part_name):
            continue
        identity = ensure_experimental_mesh_identity(obj, scene, allow_write=False)
        for node_id, kind in zip(identity.get("node_ids", []), identity.get("node_kinds", [])):
            if str(kind) == "proxy":
                proxy_ids.add(str(node_id))
    return proxy_ids


def changed_insert_refs_to_deleted_nodes(changed_operations):
    deleted_nodes = {
        str(change.get("node", ""))
        for change in changed_operations
        if change.get("section", "nodes") == "nodes" and change.get("operation") == "delete"
    }
    bad_refs = []
    for change in changed_operations:
        if change.get("section") not in {"beams", "triangles"} or change.get("operation") != "insert":
            continue
        refs = [str(node_id) for node_id in change.get("new", [])]
        if any(node_id in deleted_nodes for node_id in refs):
            bad_refs.append(change)
    return bad_refs


def validate_jbeam_payload_references(context, payload, file_group):
    errors = []
    known_nodes = payload_node_ids(payload)
    source_file = file_group.get("source_file", "")
    for part_group in file_group.get("parts", []):
        part_name = part_group.get("part", "")
        proxy_ids = proxy_node_ids_for_file_part(context.scene, source_file, part_name)
        allowed_external = set(proxy_ids)
        checks = []
        for update in part_group.get("beam_inserts", []):
            checks.append(("beam insert", update.get("nodes", []), 2))
        for update in part_group.get("triangle_inserts", []):
            checks.append(("triangle insert", update.get("nodes", []), 3))
        for update in part_group.get("beam_param_updates", []):
            checks.append(("beam params", update.get("nodes", []), 2))
        for update in part_group.get("triangle_param_updates", []):
            checks.append(("triangle params", update.get("nodes", []), 3))
        for label, nodes, expected_count in checks:
            node_ids = [str(node_id) for node_id in nodes]
            if len(node_ids) != expected_count:
                errors.append(f"{part_name}: {label} has {len(node_ids)} node(s), expected {expected_count}: {node_ids}")
                continue
            missing = [node_id for node_id in node_ids if node_id not in known_nodes and node_id not in allowed_external]
            if missing:
                errors.append(f"{part_name}: {label} references missing non-proxy node(s): {missing}")
    return errors


def validate_experimental_mesh_topology_for_export(context, file_group):
    errors = []
    warnings = []
    source_file = normalize_virtual_path(file_group.get("source_file", ""))
    part_names = {str(part_group.get("part", "")) for part_group in file_group.get("parts", [])}
    for obj in experimental_jbeam_mesh_objects(context.scene, active_only=False):
        if normalize_virtual_path(obj.get("beamng_jbeam_path", "")) != source_file:
            continue
        if str(obj.get("beamng_part_name", "")) not in part_names:
            continue
        identity = ensure_experimental_mesh_identity(obj, context.scene, allow_write=False)
        node_ids = [str(node_id) for node_id in identity.get("node_ids", [])]
        edges, faces = read_experimental_mesh_topology(obj, allow_identity_write=False)
        semantic_snapshot = semantic_topology_snapshot_for_object(obj, context.scene, allow_write=False)
        semantic_by_key = semantic_edge_types_by_key(semantic_snapshot)
        beam_edges = [
            edge for edge in edges
            if semantic_by_key.get(edge_key(edge), JBEAM_EDGE_SEMANTIC_RELATIONSHIP) == JBEAM_EDGE_SEMANTIC_BEAM
        ]
        node_set = set(node_ids)
        duplicate_nodes = duplicate_items(node_ids)
        duplicate_edges = duplicate_items(edge_key(edge) for edge in beam_edges)
        duplicate_faces = duplicate_items(face_key(face) for face in faces)
        if duplicate_nodes:
            errors.append(f"{obj.name}: duplicate node id(s): {duplicate_nodes[:8]}")
        if duplicate_edges:
            warnings.append(f"{obj.name}: duplicate mesh edge(s) map to same beam key: {duplicate_edges[:8]}")
        if duplicate_faces:
            errors.append(f"{obj.name}: duplicate triangle winding key(s): {duplicate_faces[:8]}")
        for edge in beam_edges:
            missing = [str(node_id) for node_id in edge if str(node_id) not in node_set]
            if missing:
                errors.append(f"{obj.name}: beam edge references missing node(s): {missing}")
        for face in faces:
            missing = [str(node_id) for node_id in face if str(node_id) not in node_set]
            if missing:
                errors.append(f"{obj.name}: triangle references missing node(s): {missing}")
            if len(set(str(node_id) for node_id in face)) < 3:
                errors.append(f"{obj.name}: triangle has duplicate node reference: {face}")
        non_triangles = experimental_mesh_non_triangle_face_count(obj)
        if non_triangles:
            warnings.append(f"{obj.name}: {non_triangles} non-triangle face(s) ignored")
        orphan_provisional = orphan_provisional_node_indices(obj)
        if orphan_provisional:
            warnings.append(f"{obj.name}: {len(orphan_provisional)} orphan provisional node(s); use Clear Orphans if accidental")
    return errors, warnings


def experimental_jbeam_topology_health(scene):
    summary = {
        "mesh_count": 0,
        "node_count": 0,
        "edge_count": 0,
        "face_count": 0,
        "proxy_count": 0,
        "proxy_drift_count": 0,
        "generated_uncommitted_node_count": 0,
        "orphan_provisional_node_count": 0,
        "semantic_delta_count": 0,
        "non_triangle_face_count": 0,
        "duplicate_node_id_count": 0,
        "duplicate_edge_key_count": 0,
        "duplicate_face_key_count": 0,
        "missing_reference_count": 0,
        "dirty_param_count": 0,
        "warnings": [],
        "errors": [],
    }
    for obj in experimental_jbeam_mesh_objects(scene, active_only=False):
        summary["mesh_count"] += 1
        identity = ensure_experimental_mesh_identity(obj, scene, allow_write=False)
        node_ids = [str(node_id) for node_id in identity.get("node_ids", [])]
        node_kinds = [str(kind or "owned") for kind in identity.get("node_kinds", [])]
        baselines = identity.get("original_positions", [])
        current_positions = identity.get("current_positions", [])
        generated_flags = identity.get("generated_flags", [])
        committed_flags = identity.get("committed_flags", [])
        node_params = identity.get("node_params", [])
        committed_node_params = identity.get("committed_node_params", [])
        edges, faces = read_experimental_mesh_topology(obj, allow_identity_write=False)
        semantic_snapshot = semantic_topology_snapshot_for_object(obj, scene, allow_write=False)
        semantic_by_key = semantic_edge_types_by_key(semantic_snapshot)
        beam_edges = [
            edge for edge in edges
            if semantic_by_key.get(edge_key(edge), JBEAM_EDGE_SEMANTIC_RELATIONSHIP) == JBEAM_EDGE_SEMANTIC_BEAM
        ]
        edge_uids = ensure_experimental_topology_uids(obj, allow_write=False).get("edges", [])
        face_uids = ensure_experimental_topology_uids(obj, allow_write=False).get("faces", [])
        edge_params = topology_params_for_current_elements(obj.data, edge_uids, "beamng_edge_params_json", "beamng_edge_uid_to_params_json", allow_write=False)
        committed_edge_params = topology_params_for_current_elements(obj.data, edge_uids, "beamng_edge_committed_params_json", "beamng_edge_uid_to_committed_params_json", allow_write=False)
        face_params = topology_params_for_current_elements(obj.data, face_uids, "beamng_face_params_json", "beamng_face_uid_to_params_json", allow_write=False)
        committed_face_params = topology_params_for_current_elements(obj.data, face_uids, "beamng_face_committed_params_json", "beamng_face_uid_to_committed_params_json", allow_write=False)

        summary["node_count"] += len(node_ids)
        summary["edge_count"] += len(edges)
        summary["face_count"] += len(faces)
        non_triangles = experimental_mesh_non_triangle_face_count(obj)
        summary["non_triangle_face_count"] += non_triangles
        if non_triangles:
            summary["warnings"].append(f"{obj.name}: {non_triangles} non-triangle face(s)")

        duplicate_nodes = duplicate_items(node_ids)
        duplicate_edges = duplicate_items(edge_key(edge) for edge in beam_edges)
        duplicate_faces = duplicate_items(face_key(face) for face in faces)
        summary["duplicate_node_id_count"] += len(duplicate_nodes)
        summary["duplicate_edge_key_count"] += len(duplicate_edges)
        summary["duplicate_face_key_count"] += len(duplicate_faces)
        if duplicate_nodes:
            summary["errors"].append(f"{obj.name}: duplicate node ids {duplicate_nodes[:8]}")
        if duplicate_edges:
            summary["warnings"].append(f"{obj.name}: duplicate beam keys {duplicate_edges[:8]}")
        if duplicate_faces:
            summary["errors"].append(f"{obj.name}: duplicate triangle keys {duplicate_faces[:8]}")

        known_node_ids = set(node_ids)
        for label, rows, expected_count in (("beam edge", beam_edges, 2), ("face", faces, 3)):
            for row in rows:
                ids = [str(node_id) for node_id in row]
                if len(ids) < expected_count or any(not node_id or node_id not in known_node_ids for node_id in ids[:expected_count]):
                    summary["missing_reference_count"] += 1
                    summary["errors"].append(f"{obj.name}: {label} references missing node(s): {ids}")

        for index, kind in enumerate(node_kinds):
            if kind == "proxy":
                summary["proxy_count"] += 1
                if index < len(baselines) and index < len(current_positions):
                    baseline = rounded_position_list(baselines[index])
                    current = rounded_position_list(current_positions[index])
                    if len(baseline) == 3 and len(current) == 3 and (Vector(current) - Vector(baseline)).length > 0.0005:
                        summary["proxy_drift_count"] += 1
            if index < len(generated_flags) and index < len(committed_flags):
                if bool(generated_flags[index]) and not bool(committed_flags[index]):
                    summary["generated_uncommitted_node_count"] += 1
            params = node_params[index] if index < len(node_params) and isinstance(node_params[index], dict) else {}
            committed_params = committed_node_params[index] if index < len(committed_node_params) and isinstance(committed_node_params[index], dict) else {}
            if params != committed_params:
                summary["dirty_param_count"] += 1
        orphan_provisional = orphan_provisional_node_indices(obj)
        summary["orphan_provisional_node_count"] += len(orphan_provisional)
        if orphan_provisional:
            summary["warnings"].append(f"{obj.name}: {len(orphan_provisional)} orphan provisional node(s)")
        summary["semantic_delta_count"] += int(obj.data.get("beamng_semantic_topology_delta_count", 0) or 0)
        for index, params in enumerate(edge_params):
            committed_params = committed_edge_params[index] if index < len(committed_edge_params) and isinstance(committed_edge_params[index], dict) else {}
            if isinstance(params, dict) and params != committed_params:
                summary["dirty_param_count"] += 1
        for index, params in enumerate(face_params):
            committed_params = committed_face_params[index] if index < len(committed_face_params) and isinstance(committed_face_params[index], dict) else {}
            if isinstance(params, dict) and params != committed_params:
                summary["dirty_param_count"] += 1

    if summary["proxy_drift_count"]:
        summary["warnings"].append(f"{summary['proxy_drift_count']} proxy node(s) are away from their stored positions")
    return summary


def store_experimental_jbeam_topology_health(scene, summary):
    for key, value in summary.items():
        if isinstance(value, int):
            scene[f"beamng_jbeam_health_{key}"] = int(value)
    scene["beamng_jbeam_last_topology_health_json"] = json.dumps(summary, indent=2)


def build_jbeam_export_validation(context, selected_virtual_paths=None):
    history = jbeam_operation_history(context.scene)
    plan = build_jbeam_override_export_plan(context, history)
    plan = filter_plan_files_for_selected_virtual_paths(plan, selected_virtual_paths)
    source_index, source_warning = jbeam_asset_source_index_for_context(context)
    warnings = set(plan.get("warnings", []))
    errors = []
    infos = []
    files = []
    health = experimental_jbeam_topology_health(context.scene)
    store_experimental_jbeam_topology_health(context.scene, health)

    if source_warning:
        warnings.add(source_warning)
    if not history and int(plan.get("file_create_count", 0)) == 0:
        warnings.add("No accepted JBeam edits are recorded.")
    history_counts = jbeam_history_counts(history)
    if not plan.get("user_current_folder"):
        errors.append("BeamNG user folder/current folder is not configured.")
    if not plan.get("export_mod_folder"):
        errors.append("JBeam export mod folder is not configured.")
    non_triangle_face_count = int(context.scene.get("beamng_jbeam_non_triangle_face_count", 0) or 0)
    if non_triangle_face_count:
        warnings.add(
            f"{non_triangle_face_count} non-triangle mesh face(s) found and ignored; JBeam collision triangles require exactly 3 nodes."
        )
    for error in health.get("errors", []):
        errors.append(f"Topology health: {error}")
    for warning in health.get("warnings", []):
        warnings.add(f"Topology health: {warning}")

    for file_group in plan.get("files", []):
        virtual_path = normalize_virtual_path(file_group.get("virtual_path", ""))
        file_errors = []
        file_warnings = list(file_group.get("warnings", []))
        file_infos = []
        stale_source_checks = []
        semantic_diff = semantic_diff_for_file_group(file_group)
        round_trip_validation = {"status": "not_run", "errors": [], "warnings": [], "infos": []}
        file_infos.extend(semantic_diff_summary_lines(semantic_diff))
        patch_mode = ""
        source = jbeam_source_for_file_group(file_group, source_index)
        expected_node_update_count = sum(
            len(part_group.get("node_inserts", []))
            + len(part_group.get("node_updates", []))
            + len(part_group.get("node_deletes", []))
            for part_group in file_group.get("parts", [])
        )
        expected_topology_update_count = sum(
            len(part_group.get("beam_inserts", []))
            + len(part_group.get("beam_deletes", []))
            + len(part_group.get("triangle_inserts", []))
            + len(part_group.get("triangle_deletes", []))
            for part_group in file_group.get("parts", [])
        )
        expected_update_count = expected_node_update_count + expected_topology_update_count

        if not file_group.get("can_stage_override"):
            file_errors.append("No safe unpacked mod target path is available.")
        target_path = file_group.get("planned_target_path", "")
        normalized_target = normalize_virtual_path(target_path)
        if target_path and "/current/vehicles/" in normalized_target.lower():
            file_errors.append("Refusing JBeam target under current/vehicles; that folder is for .pc configurations.")
        if target_path and "/current/mods/unpacked/" not in normalized_target.lower():
            file_errors.append("Refusing JBeam target outside current/mods/unpacked/<mod>/vehicles.")
        if target_path and Path(target_path).exists():
            file_infos.append("Target already exists; stage without overwrite will skip it, update will back it up first.")
        mesh_errors, mesh_warnings = validate_experimental_mesh_topology_for_export(context, file_group)
        file_errors.extend(mesh_errors)
        file_warnings.extend(mesh_warnings)

        new_file_payload = new_jbeam_payload_for_virtual_path(context.scene, virtual_path)
        if new_file_payload is not None:
            try:
                payload = json.loads(json.dumps(new_file_payload))
                clean_changed, clean_skipped = apply_jbeam_updates_to_payload(payload, file_group)
                if clean_changed:
                    round_trip_validation = round_trip_validate_patched_jbeam_text(compact_jbeam_json(payload), file_group)
                    file_errors.extend(round_trip_validation.get("errors", []))
                    file_warnings.extend(round_trip_validation.get("warnings", []))
                    file_infos.extend(round_trip_validation.get("infos", []))
                reference_errors = validate_jbeam_payload_references(context, payload, file_group)
                inconsistent_inserts = changed_insert_refs_to_deleted_nodes(clean_changed)
                if inconsistent_inserts:
                    file_errors.append("Patched JBeam would insert topology referencing a node deleted by the same export.")
                for ref_error in reference_errors:
                    file_errors.append(ref_error)
                if clean_changed and not reference_errors and not inconsistent_inserts:
                    patch_mode = "new_file_clean_json"
                    file_infos.append("New JBeam file is staged in Blender and will be written only on export.")
                    if clean_skipped:
                        file_warnings.append(
                            f"{len(clean_skipped)} stale/non-applicable accepted operation(s) will not affect this export."
                        )
                else:
                    has_create_only = bool(file_group.get("is_new_file")) and expected_update_count == 0
                    if has_create_only and not reference_errors and not inconsistent_inserts:
                        patch_mode = "new_file_clean_json"
                        file_infos.append("New empty JBeam file is staged in Blender and will be written only on export.")
                    else:
                        file_errors.append("Accepted JBeam edits could not be applied safely to staged new-file payload.")
                        for skipped in clean_skipped:
                            file_warnings.append(
                                f"Skipped {skipped.get('part', '')}.{skipped.get('node', '')}: {skipped.get('reason', '')}"
                            )
            except Exception as exc:
                file_errors.append(f"Could not validate staged new JBeam payload: {exc}")
        elif source is None:
            file_errors.append("Could not find matching source JBeam asset.")
        else:
            try:
                stale_source_checks = stale_source_checks_for_file_group(context, file_group, source)
                for check in stale_source_checks:
                    if check.get("status") == "stale_external_data":
                        file_errors.append(
                            "Stale External Data: source JBeam changed since import; review semantic differences "
                            "before exporting."
                        )
                    elif check.get("status") == "unknown":
                        file_warnings.append(f"Could not verify source freshness: {check.get('reason', '')}")
                source_text = read_jbeam_asset_source_text(source)
                _patched_text, text_changed, text_skipped = apply_jbeam_updates_to_source_text(source_text, file_group)
                has_node_structural_changes = any(
                    part_group.get("node_inserts") or part_group.get("node_deletes")
                    for part_group in file_group.get("parts", [])
                )
                if not has_node_structural_changes and text_changed and not text_skipped and len(text_changed) == expected_update_count:
                    round_trip_validation = round_trip_validate_patched_jbeam_text(_patched_text, file_group)
                    file_errors.extend(round_trip_validation.get("errors", []))
                    file_warnings.extend(round_trip_validation.get("warnings", []))
                    file_infos.extend(round_trip_validation.get("infos", []))
                    patch_mode = "source_preserving_text"
                else:
                    try:
                        payload = load_jsonc_text(source_text)
                        clean_changed, clean_skipped = apply_jbeam_updates_to_payload(payload, file_group)
                        reference_errors = validate_jbeam_payload_references(context, payload, file_group)
                        inconsistent_inserts = changed_insert_refs_to_deleted_nodes(clean_changed)
                    except Exception as exc:
                        clean_changed = []
                        clean_skipped = [{"reason": f"Clean JSON fallback parse failed: {exc}"}]
                        reference_errors = []
                        inconsistent_inserts = []
                    if inconsistent_inserts:
                        file_errors.append("Patched JBeam would insert topology referencing a node deleted by the same export.")
                    for ref_error in reference_errors:
                        file_errors.append(ref_error)
                    if clean_changed and not reference_errors and not inconsistent_inserts:
                        round_trip_validation = round_trip_validate_patched_jbeam_text(compact_jbeam_json(payload), file_group)
                        file_errors.extend(round_trip_validation.get("errors", []))
                        file_warnings.extend(round_trip_validation.get("warnings", []))
                        file_infos.extend(round_trip_validation.get("infos", []))
                        patch_mode = "clean_json_fallback"
                        file_warnings.append("Clean JSON fallback will be used for this file.")
                        if clean_skipped:
                            file_warnings.append(
                                f"{len(clean_skipped)} stale/non-applicable accepted operation(s) will not affect this export."
                            )
                        for skipped in text_skipped:
                            file_infos.append(
                                f"Text patch skipped {skipped.get('part', '')}.{skipped.get('node', '')}: "
                                f"{skipped.get('reason', '')}"
                            )
                    else:
                        file_errors.append("Accepted JBeam edits could not be applied safely to source text or clean JSON.")
                        for skipped in list(text_skipped) + list(clean_skipped):
                            file_warnings.append(
                                f"Skipped {skipped.get('part', '')}.{skipped.get('node', '')}: {skipped.get('reason', '')}"
                            )
            except Exception as exc:
                file_errors.append(f"Could not read source JBeam: {exc}")

        files.append(
            {
                "source_file": file_group.get("source_file", ""),
                "virtual_path": virtual_path,
                "planned_target_path": target_path,
                "operation_count": file_group.get("operation_count", file_group.get("node_update_count", 0)),
                "node_update_count": file_group.get("node_update_count", 0),
                "topology_update_count": file_group.get("topology_update_count", 0),
                "expected_update_count": expected_update_count,
                "is_new_file": bool(file_group.get("is_new_file")),
                "patch_mode": patch_mode,
                "stale_source_checks": stale_source_checks,
                "semantic_diff": semantic_diff,
                "round_trip_validation": round_trip_validation,
                "errors": file_errors,
                "warnings": sorted(set(file_warnings)),
                "infos": file_infos,
            }
        )
        errors.extend(file_errors)
        warnings.update(file_warnings)
        infos.extend(file_infos)

    source_preserving_count = sum(1 for item in files if item.get("patch_mode") == "source_preserving_text")
    clean_json_count = sum(1 for item in files if item.get("patch_mode") == "clean_json_fallback")
    preflight_counts = jbeam_export_preflight_counts(plan)
    status = "pass" if not errors else "fail"
    if status == "pass" and warnings:
        status = "warning"

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "status": status,
        "precision": JBEAM_POSITION_PRECISION,
        "operation_count": plan.get("operation_count", plan.get("node_update_count", 0)),
        "file_create_count": plan.get("file_create_count", 0),
        "node_update_count": plan.get("node_update_count", 0),
        "topology_update_count": plan.get("topology_update_count", 0),
        "preflight_counts": dict(preflight_counts),
        "source_file_count": plan.get("source_file_count", 0),
        "stageable_file_count": plan.get("stageable_file_count", 0),
        "source_preserving_file_count": source_preserving_count,
        "clean_json_fallback_file_count": clean_json_count,
        "user_current_folder": plan.get("user_current_folder", ""),
        "export_mod_folder": plan.get("export_mod_folder", ""),
        "export_root": plan.get("export_root", ""),
        "errors": sorted(set(errors)),
        "warnings": sorted(set(warnings)),
        "infos": sorted(set(infos)),
        "topology_health": health,
        "files": files,
    }


def jbeam_export_validation_lines(validation):
    lines = [
        "[BeamNG JBeam Export Validation]",
        f"Generated: {validation['generated_at']}",
        f"Status: {validation['status']}",
        f"Operations: {validation.get('operation_count', validation['node_update_count'])}",
        f"New files: {validation.get('file_create_count', 0)}",
        f"Node edits: {validation['node_update_count']}",
        f"Topology updates: {validation.get('topology_update_count', 0)}",
        (
            "Preflight: "
            f"+nodes {validation.get('preflight_counts', {}).get('node_inserts', 0)}, "
            f"move/params nodes {validation.get('preflight_counts', {}).get('node_updates', 0)}, "
            f"-nodes {validation.get('preflight_counts', {}).get('node_deletes', 0)}, "
            f"+beams {validation.get('preflight_counts', {}).get('beam_inserts', 0)}, "
            f"-beams {validation.get('preflight_counts', {}).get('beam_deletes', 0)}, "
            f"+triangles {validation.get('preflight_counts', {}).get('triangle_inserts', 0)}, "
            f"-triangles {validation.get('preflight_counts', {}).get('triangle_deletes', 0)}"
        ),
        f"Source files: {validation['source_file_count']}",
        f"Stageable files: {validation['stageable_file_count']}",
        f"Source-preserving files: {validation['source_preserving_file_count']}",
        f"Clean JSON fallback files: {validation['clean_json_fallback_file_count']}",
        f"User current folder: {validation['user_current_folder'] or '(not configured)'}",
        f"JBeam export mod: {validation['export_mod_folder'] or '(not configured)'}",
        f"JBeam export root: {validation['export_root'] or '(not configured)'}",
        (
            "Topology health: "
            f"meshes {validation.get('topology_health', {}).get('mesh_count', 0)}, "
            f"nodes {validation.get('topology_health', {}).get('node_count', 0)}, "
            f"proxy drift {validation.get('topology_health', {}).get('proxy_drift_count', 0)}, "
            f"missing refs {validation.get('topology_health', {}).get('missing_reference_count', 0)}, "
            f"dirty params {validation.get('topology_health', {}).get('dirty_param_count', 0)}"
        ),
        "",
    ]
    for error in validation["errors"]:
        lines.append(f"Error: {error}")
    for warning in validation["warnings"]:
        lines.append(f"Warning: {warning}")
    for info in validation["infos"]:
        lines.append(f"Info: {info}")
    lines.append("")

    for file_group in validation["files"]:
        lines.append(f"Virtual: {file_group['virtual_path'] or '(unknown)'}")
        lines.append(f"Target: {file_group['planned_target_path'] or '(not stageable)'}")
        if file_group.get("is_new_file"):
            lines.append("File status: new staged JBeam file")
        lines.append(f"Patch mode: {file_group['patch_mode'] or '(none)'}")
        lines.append(f"Operations: {file_group.get('operation_count', file_group['node_update_count'])}")
        lines.append(f"Node edits: {file_group['node_update_count']}")
        lines.append(f"Topology updates: {file_group.get('topology_update_count', 0)}")
        for error in file_group["errors"]:
            lines.append(f"  Error: {error}")
        for warning in file_group["warnings"]:
            lines.append(f"  Warning: {warning}")
        for info in file_group["infos"]:
            lines.append(f"  Info: {info}")
        lines.append("")
    if not validation["files"]:
        lines.append("No accepted JBeam edits are recorded.")
    return lines


def draw_param_summary(layout, params, empty_text="none"):
    if not params:
        layout.label(text=empty_text)
        return
    for key in sorted(params):
        layout.label(text=f"{key}: {params[key]}")


def write_jbeam_export_validation_report(context, selected_virtual_paths=None):
    validation = build_jbeam_export_validation(context, selected_virtual_paths=selected_virtual_paths)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_dir = persistent_cache_dir() / "jbeam_editor"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"jbeam_export_validation_{stamp}.json"
    report_path.write_text(json.dumps(validation, indent=2), encoding="utf-8")

    text = bpy.data.texts.get("BeamNG JBeam Export Validation") or bpy.data.texts.new(
        "BeamNG JBeam Export Validation"
    )
    text.clear()
    text.write("\n".join(jbeam_export_validation_lines(validation)))
    text.write("\n")

    context.scene["beamng_jbeam_last_export_validation_path"] = str(report_path)
    context.scene["beamng_jbeam_last_export_validation_status"] = validation["status"]
    return report_path, validation


def validate_selected_jbeam_export_or_report(context, selected_virtual_paths=None):
    report_path, validation = write_jbeam_export_validation_report(
        context,
        selected_virtual_paths=selected_virtual_paths,
    )
    return report_path, validation


def current_import_pc_path(scene):
    source_path = scene.get("beamng_slot_editor_source_pc_path", "")
    if source_path:
        return Path(source_path)
    roots = find_beamng_import_collections(scene)
    if roots:
        return Path(roots[0].get("beamng_pc_source_path", roots[0].get("beamng_pc_path", "")))
    return None


def jbeam_asset_source_index_for_context(context):
    pc_path = current_import_pc_path(context.scene)
    if pc_path is None or not str(pc_path):
        return {}, "No imported .pc source path is available"

    prefs = get_addon_preferences(context)
    beamng_user_folder = prefs.beamng_user_folder if prefs else ""
    vanilla_vehicles_folder = prefs.vanilla_vehicles_folder if prefs else ""
    cache_asset_catalogs = prefs.cache_asset_catalogs if prefs else True
    try:
        jbeam_sources, _dae_sources, _virtual_vehicle_root = collect_beamng_asset_sources(
            pc_path,
            beamng_user_folder,
            vanilla_vehicles_folder,
            cache_asset_catalogs,
        )
    except Exception as exc:
        return {}, f"Could not collect JBeam sources: {exc}"

    source_index = {}
    for source in sorted(jbeam_sources, key=lambda item: item.precedence):
        source_index[normalize_virtual_path(source.virtual_path)] = source
    return source_index, ""


def jbeam_source_for_file_group(file_group, source_index):
    virtual_path = normalize_virtual_path(file_group.get("virtual_path", ""))
    source = source_index.get(virtual_path)
    if source is not None:
        return source

    source_file = file_group.get("source_file", "")
    if source_file:
        try:
            path = Path(source_file)
            if path.exists() and path.is_file():
                return BeamNGAssetSource(
                    asset_type="file",
                    virtual_path=virtual_path,
                    path=str(path),
                )
        except (OSError, ValueError):
            pass
    return None


def read_jbeam_asset_source_text(source):
    if source.asset_type == "file":
        return Path(source.path).read_text(encoding="utf-8-sig")
    with zipfile.ZipFile(source.zip_path, "r") as archive:
        return archive.read(source.zip_entry).decode("utf-8", errors="ignore")


def read_jbeam_asset_source_bytes(source):
    if source.asset_type == "file":
        return Path(source.path).read_bytes()
    with zipfile.ZipFile(source.zip_path, "r") as archive:
        return archive.read(source.zip_entry)


def jbeam_asset_source_sha256(source):
    return hashlib.sha256(read_jbeam_asset_source_bytes(source)).hexdigest()


def experimental_jbeam_meshes_for_virtual_path(scene, virtual_path):
    normalized = normalize_virtual_path(virtual_path)
    if not normalized:
        return []
    return [
        obj
        for obj in experimental_jbeam_mesh_objects(scene, active_only=False)
        if normalize_virtual_path(obj.get("beamng_jbeam_path", "")) == normalized
    ]


def stale_source_checks_for_file_group(context, file_group, source):
    if source is None:
        return []
    virtual_path = normalize_virtual_path(file_group.get("virtual_path", ""))
    checks = []
    try:
        current_hash = jbeam_asset_source_sha256(source)
    except Exception as exc:
        return [
            {
                "status": "unknown",
                "virtual_path": virtual_path,
                "reason": f"Could not hash current source: {exc}",
            }
        ]

    for obj in experimental_jbeam_meshes_for_virtual_path(context.scene, virtual_path):
        cached_hash = str(obj.data.get("beamng_source_sha256", "") or "")
        if not cached_hash:
            continue
        if cached_hash != current_hash:
            checks.append(
                {
                    "status": "stale_external_data",
                    "object": obj.name,
                    "virtual_path": virtual_path,
                    "cached_sha256": cached_hash,
                    "current_sha256": current_hash,
                    "action": "review_required_no_mutation",
                }
            )
    return checks


def semantic_diff_for_file_group(file_group):
    diff = {
        "node_updates": [],
        "node_inserts": [],
        "node_deletes": [],
        "beam_inserts": [],
        "beam_deletes": [],
        "triangle_changes": [],
        "change_count": 0,
    }
    for part_group in file_group.get("parts", []):
        part_name = part_group.get("part", "")
        for update in part_group.get("node_updates", []):
            diff["node_updates"].append(
                {
                    "part": part_name,
                    "node": update.get("node", ""),
                    "old_position": update.get("old_position", []),
                    "new_position": update.get("new_position", []),
                }
            )
        for update in part_group.get("node_inserts", []):
            diff["node_inserts"].append(
                {"part": part_name, "node": update.get("node", ""), "position": update.get("new_position", [])}
            )
        for update in part_group.get("node_deletes", []):
            diff["node_deletes"].append(
                {"part": part_name, "node": update.get("node", ""), "position": update.get("old_position", [])}
            )
        for update in part_group.get("beam_inserts", []):
            diff["beam_inserts"].append({"part": part_name, "nodes": update.get("nodes", [])})
        for update in part_group.get("beam_deletes", []):
            diff["beam_deletes"].append({"part": part_name, "nodes": update.get("nodes", [])})
        for key, label in (
            ("triangle_inserts", "insert"),
            ("triangle_deletes", "delete"),
            ("triangle_node_updates", "update"),
            ("triangle_param_updates", "params"),
        ):
            for update in part_group.get(key, []):
                diff["triangle_changes"].append(
                    {"part": part_name, "operation": label, "nodes": update.get("nodes", update.get("old_nodes", []))}
                )
    diff["change_count"] = sum(len(value) for key, value in diff.items() if isinstance(value, list))
    return diff


def semantic_diff_summary_lines(diff):
    lines = []
    if not diff or not diff.get("change_count"):
        return ["Semantic Diff: no JBeam topology or node edits"]
    lines.append(f"Semantic Diff: {diff['change_count']} JBeam change(s)")
    for update in diff.get("node_updates", []):
        lines.append(
            f"Node moved: {update.get('part', '')}.{update.get('node', '')} "
            f"{update.get('old_position', [])} -> {update.get('new_position', [])}"
        )
    for update in diff.get("node_inserts", []):
        lines.append(f"Node added: {update.get('part', '')}.{update.get('node', '')} at {update.get('position', [])}")
    for update in diff.get("node_deletes", []):
        lines.append(f"Node deleted: {update.get('part', '')}.{update.get('node', '')}")
    for update in diff.get("beam_inserts", []):
        lines.append(f"Beam added: {update.get('part', '')} {update.get('nodes', [])}")
    for update in diff.get("beam_deletes", []):
        lines.append(f"Beam deleted: {update.get('part', '')} {update.get('nodes', [])}")
    for update in diff.get("triangle_changes", []):
        lines.append(
            f"Triangle {update.get('operation', '')}: {update.get('part', '')} {update.get('nodes', [])}"
        )
    return lines


def imported_topology_part_by_name(imported_jbeam, part_name):
    for part in getattr(imported_jbeam, "parts", []) or []:
        if getattr(part, "part_name", "") == part_name:
            return part
    return None


def imported_topology_node_map(part):
    return {str(node.node_id): node for node in getattr(part, "nodes", []) or []}


def imported_topology_beam_keys(part):
    return {
        tuple(sorted((str(beam.id1), str(beam.id2))))
        for beam in getattr(part, "beams", []) or []
        if getattr(beam, "id1", "") and getattr(beam, "id2", "")
    }


def imported_topology_triangle_keys(part):
    return {
        tuple(str(node_id) for node_id in (triangle.id1, triangle.id2, triangle.id3))
        for triangle in getattr(part, "triangles", []) or []
        if getattr(triangle, "id1", "") and getattr(triangle, "id2", "") and getattr(triangle, "id3", "")
    }


def round_trip_validate_patched_jbeam_text(patched_text, file_group):
    imported = import_jbeam_topology_subset(patched_text, source_path=file_group.get("virtual_path", "<patched>"))
    errors = []
    warnings = []
    infos = []
    for diagnostic in getattr(imported, "diagnostics", []) or []:
        if getattr(diagnostic, "level", "") == "error":
            errors.append(f"Round-trip import error: {getattr(diagnostic, 'message', diagnostic)}")

    for part_group in file_group.get("parts", []):
        part_name = part_group.get("part", "")
        part = imported_topology_part_by_name(imported, part_name)
        if part is None:
            errors.append(f"Round-trip missing part: {part_name}")
            continue

        nodes = imported_topology_node_map(part)
        beams = imported_topology_beam_keys(part)
        triangles = imported_topology_triangle_keys(part)

        for update in part_group.get("node_updates", []):
            node_id = str(update.get("node", ""))
            node = nodes.get(node_id)
            if node is None:
                errors.append(f"Round-trip missing moved node: {part_name}.{node_id}")
                continue
            expected = rounded_position_list(update.get("new_position", []))
            actual = rounded_position_list(getattr(node, "position", []))
            if actual != expected:
                errors.append(f"Round-trip node position mismatch: {part_name}.{node_id} {actual} != {expected}")
        for update in part_group.get("node_inserts", []):
            node_id = str(update.get("node", ""))
            if node_id not in nodes:
                errors.append(f"Round-trip missing inserted node: {part_name}.{node_id}")
        for update in part_group.get("node_deletes", []):
            node_id = str(update.get("node", ""))
            if node_id in nodes:
                errors.append(f"Round-trip deleted node still present: {part_name}.{node_id}")

        for update in part_group.get("beam_inserts", []):
            node_ids = [str(node_id) for node_id in update.get("nodes", [])[:2]]
            if len(node_ids) == 2 and tuple(sorted(node_ids)) not in beams:
                errors.append(f"Round-trip missing inserted Beam: {part_name} {node_ids}")
        for update in part_group.get("beam_deletes", []):
            node_ids = [str(node_id) for node_id in update.get("nodes", [])[:2]]
            if len(node_ids) == 2 and tuple(sorted(node_ids)) in beams:
                errors.append(f"Round-trip deleted Beam still present: {part_name} {node_ids}")

        for update in part_group.get("triangle_inserts", []):
            node_ids = tuple(str(node_id) for node_id in update.get("nodes", [])[:3])
            if len(node_ids) == 3 and node_ids not in triangles:
                errors.append(f"Round-trip missing inserted Triangle: {part_name} {list(node_ids)}")
        for update in part_group.get("triangle_deletes", []):
            node_ids = tuple(str(node_id) for node_id in update.get("nodes", [])[:3])
            if len(node_ids) == 3 and node_ids in triangles:
                errors.append(f"Round-trip deleted Triangle still present: {part_name} {list(node_ids)}")

    if not errors:
        infos.append("Round-trip validation: patched JBeam re-imported with expected semantic edits.")
    return {"status": "fail" if errors else "pass", "errors": errors, "warnings": warnings, "infos": infos}


def write_text_without_newline_translation(path, text, encoding="utf-8"):
    with Path(path).open("w", encoding=encoding, newline="") as handle:
        handle.write(text)


def compact_json_scalar(value):
    return isinstance(value, (str, int, float, bool)) or value is None


def compact_jbeam_json(value, level=0):
    indent = "  " * level
    child_indent = "  " * (level + 1)
    if compact_json_scalar(value):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        if not value:
            return "[]"
        if all(compact_json_scalar(item) for item in value):
            return json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
        if all(isinstance(item, list) and all(compact_json_scalar(cell) for cell in item) for item in value):
            rows = [
                child_indent + json.dumps(item, ensure_ascii=False, separators=(", ", ": "))
                for item in value
            ]
            return "[\n" + ",\n".join(rows) + "\n" + indent + "]"
        if all(isinstance(item, dict) and len(json.dumps(item, ensure_ascii=False)) <= 120 for item in value):
            rows = [
                child_indent + json.dumps(item, ensure_ascii=False, separators=(", ", ": "))
                for item in value
            ]
            return "[\n" + ",\n".join(rows) + "\n" + indent + "]"
        rows = [child_indent + compact_jbeam_json(item, level + 1) for item in value]
        return "[\n" + ",\n".join(rows) + "\n" + indent + "]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        if all(compact_json_scalar(item) for item in value.values()) and len(json.dumps(value, ensure_ascii=False)) <= 120:
            return json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
        rows = []
        for key, item in value.items():
            rows.append(
                child_indent
                + json.dumps(str(key), ensure_ascii=False)
                + ": "
                + compact_jbeam_json(item, level + 1)
            )
        return "{\n" + ",\n".join(rows) + "\n" + indent + "}"
    return json.dumps(value, ensure_ascii=False)


def write_compact_jbeam_json(path, payload):
    write_text_without_newline_translation(path, compact_jbeam_json(payload) + "\n")


JBEAM_NUMBER_PATTERN = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"


def node_row_position_pattern(node_id):
    escaped_node = re.escape(str(node_id))
    return re.compile(
        rf"^(?P<prefix>\s*\[\s*(?P<quote>[\"']){escaped_node}(?P=quote)\s*,\s*)"
        rf"(?P<x>{JBEAM_NUMBER_PATTERN})(?P<after_x>\s*,\s*)"
        rf"(?P<y>{JBEAM_NUMBER_PATTERN})(?P<after_y>\s*,\s*)"
        rf"(?P<z>{JBEAM_NUMBER_PATTERN})(?P<suffix>.*)$"
    )


def apply_node_updates_to_jbeam_text(source_text, file_group):
    lines = source_text.splitlines(keepends=True)
    changed = []
    skipped = []

    for part_group in file_group.get("parts", []):
        part_name = part_group.get("part", "")
        for update in part_group.get("node_updates", []):
            node_id = str(update.get("node", ""))
            try:
                new_position = [float(value) for value in update.get("new_position", [])]
            except (TypeError, ValueError):
                new_position = []
            if len(new_position) != 3:
                skipped.append({"part": part_name, "node": node_id, "reason": "New position is not xyz"})
                continue

            pattern = node_row_position_pattern(node_id)
            matches = []
            for line_index, line in enumerate(lines):
                stripped = line.lstrip()
                if stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*"):
                    continue
                line_body = line[:-1] if line.endswith("\n") else line
                newline = "\n" if line.endswith("\n") else ""
                if line_body.endswith("\r"):
                    line_body = line_body[:-1]
                    newline = "\r" + newline
                match = pattern.match(line_body)
                if match:
                    matches.append((line_index, line_body, newline, match))

            if not matches:
                skipped.append({"part": part_name, "node": node_id, "reason": "Node row not found in source text"})
                continue
            if len(matches) > 1:
                skipped.append({"part": part_name, "node": node_id, "reason": "Node row is ambiguous in source text"})
                continue

            line_index, _line_body, newline, match = matches[0]
            old_position = rounded_position_list([match.group("x"), match.group("y"), match.group("z")])
            source_precision = max(
                jbeam_decimal_places(match.group("x")),
                jbeam_decimal_places(match.group("y")),
                jbeam_decimal_places(match.group("z")),
            )
            precision = max(JBEAM_POSITION_PRECISION, source_precision)
            new_position = rounded_position_list(new_position, precision)
            replacement = (
                match.group("prefix")
                + formatted_jbeam_position_number(new_position[0], precision)
                + match.group("after_x")
                + formatted_jbeam_position_number(new_position[1], precision)
                + match.group("after_y")
                + formatted_jbeam_position_number(new_position[2], precision)
                + match.group("suffix")
                + newline
            )
            lines[line_index] = replacement
            changed.append(
                {
                    "part": part_name,
                    "node": node_id,
                    "old_position": old_position,
                    "new_position": new_position,
                }
            )

    return "".join(lines), changed, skipped


def find_jsonc_value_for_key_in_range(text, key, start, end, expected_open=None):
    index = start
    while index < end:
        if text.startswith("//", index):
            newline = text.find("\n", index + 2, end)
            index = end if newline == -1 else newline + 1
            continue
        if text.startswith("/*", index):
            close = text.find("*/", index + 2, end)
            index = end if close == -1 else close + 2
            continue

        char = text[index]
        if char in {'"', "'"}:
            token_end = scan_jsonc_string(text, index)
            token = text[index + 1 : token_end - 1]
            next_index = skip_jsonc_ws_comments(text, token_end)
        elif char.isalpha() or char in "_$":
            token, token_end = scan_jsonc_identifier(text, index)
            next_index = skip_jsonc_ws_comments(text, token_end)
        else:
            index += 1
            continue

        if token == key and next_index < end and text[next_index] == ":":
            value_start = skip_jsonc_ws_comments(text, next_index + 1)
            if value_start >= end:
                return None
            opener = text[value_start]
            if expected_open is not None and opener != expected_open:
                return None
            if opener in "{[":
                value_end = find_matching_jsonc_brace(text, value_start)
                return None if value_end == -1 else (value_start, value_end + 1)
            value_end = value_start
            while value_end < end and text[value_end] not in ",}\n\r":
                value_end += 1
            return value_start, value_end
        index = token_end
    return None


def find_jbeam_part_object_bounds(source_text, part_name):
    bounds = find_jsonc_value_for_key_in_range(source_text, part_name, 0, len(source_text), expected_open="{")
    if bounds is None:
        return None
    return bounds


def find_jbeam_part_section_array_bounds(source_text, part_name, section_name):
    part_bounds = find_jbeam_part_object_bounds(source_text, part_name)
    if part_bounds is None:
        return None
    part_start, part_end = part_bounds
    return find_jsonc_value_for_key_in_range(source_text, section_name, part_start, part_end, expected_open="[")


def line_bounds_for_index(text, index):
    line_start = text.rfind("\n", 0, index) + 1
    line_end = text.find("\n", index)
    if line_end == -1:
        return line_start, len(text)
    return line_start, line_end + 1


def line_text_without_newline(line):
    return line.rstrip("\r\n")


def jbeam_node_row_text(node_ids):
    return json.dumps([str(node_id) for node_id in node_ids], ensure_ascii=False, separators=(", ", ": "))


def jbeam_row_line_indent(section_text):
    for line in section_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and not stripped.startswith("[]"):
            return line[: len(line) - len(line.lstrip())]
    return "  "


def jbeam_section_closing_indent(source_text, section_bounds):
    close_index = section_bounds[1] - 1
    return text_indent_before_index(source_text, close_index)


def beam_row_nodes_pattern(node_ids):
    if len(node_ids) < 2:
        return None
    left = re.escape(str(node_ids[0]))
    right = re.escape(str(node_ids[1]))
    return re.compile(
        rf"^\s*\[\s*([\"']){left}\1\s*,\s*([\"']){right}\2(?:\s*,[^\]]*)?\]\s*,?\s*(?://.*)?$"
    )


def row_line_matches_beam_nodes(line, node_ids):
    pattern = beam_row_nodes_pattern(node_ids)
    if pattern is None:
        return False
    return bool(pattern.match(line_text_without_newline(line)))


def find_unique_beam_row_line(source_text, section_bounds, node_ids):
    section_start, section_end = section_bounds
    matches = []
    search_index = section_start
    while search_index < section_end:
        line_start, line_end = line_bounds_for_index(source_text, search_index)
        if line_end <= section_start:
            search_index = line_end + 1
            continue
        if line_start >= section_end:
            break
        line = source_text[line_start:min(line_end, section_end)]
        if row_line_matches_beam_nodes(line, node_ids):
            matches.append((line_start, line_end))
        search_index = line_end if line_end > search_index else search_index + 1
    return matches


def remove_text_line_preserving_surroundings(text, line_start, line_end):
    line = text[line_start:line_end]
    if line_text_without_newline(line).rstrip().endswith(","):
        return text[:line_start] + text[line_end:]

    previous_line_end = line_start
    previous_line_start = text.rfind("\n", 0, max(0, previous_line_end - 1)) + 1
    previous_line = text[previous_line_start:previous_line_end]
    previous_body = line_text_without_newline(previous_line)
    if previous_body.rstrip().endswith(","):
        comma_index = previous_line_start + previous_body.rstrip().rfind(",")
        return text[:comma_index] + text[comma_index + 1:line_start] + text[line_end:]
    return text[:line_start] + text[line_end:]


def insert_beam_row_into_section_text(source_text, section_bounds, node_ids, params=None):
    section_text = source_text[section_bounds[0]:section_bounds[1]]
    row_indent = jbeam_row_line_indent(section_text)
    closing_indent = jbeam_section_closing_indent(source_text, section_bounds)
    insert_at = section_bounds[1] - 1
    while insert_at > section_bounds[0] and source_text[insert_at - 1] in " \t\r\n":
        insert_at -= 1

    rows = []
    cleaned_params = clean_jbeam_params(params or {})
    if cleaned_params:
        rows.append(json.dumps(cleaned_params, ensure_ascii=False, separators=(", ", ": ")))
    rows.append(jbeam_node_row_text(node_ids))

    previous_char = source_text[insert_at - 1] if insert_at > section_bounds[0] else "["
    prefix = "" if previous_char in "[," else ","
    insertion = prefix + "\n" + "\n".join(row_indent + row for row in rows) + "\n" + closing_indent
    return source_text[:insert_at] + insertion + source_text[section_bounds[1] - 1:]


def apply_topology_updates_to_jbeam_text(source_text, file_group):
    text = source_text
    changed = []
    skipped = []

    for part_group in file_group.get("parts", []):
        part_name = part_group.get("part", "")
        beam_inserts = list(part_group.get("beam_inserts", []))
        beam_deletes = list(part_group.get("beam_deletes", []))
        unsupported_count = (
            len(part_group.get("beam_param_updates", []))
            + len(part_group.get("triangle_inserts", []))
            + len(part_group.get("triangle_deletes", []))
            + len(part_group.get("triangle_node_updates", []))
            + len(part_group.get("triangle_param_updates", []))
        )
        if unsupported_count:
            skipped.append(
                {
                    "part": part_name,
                    "section": "topology",
                    "reason": "Source-preserving text patch currently supports Beam insert/delete only",
                }
            )

        for update in beam_deletes:
            node_ids = [str(node_id) for node_id in update.get("nodes", [])[:2]]
            section_bounds = find_jbeam_part_section_array_bounds(text, part_name, "beams")
            if section_bounds is None:
                skipped.append({"part": part_name, "section": "beams", "nodes": node_ids, "reason": "Beams section not found"})
                continue
            matches = find_unique_beam_row_line(text, section_bounds, node_ids)
            if len(matches) != 1:
                skipped.append(
                    {
                        "part": part_name,
                        "section": "beams",
                        "nodes": node_ids,
                        "reason": "Beam row not found uniquely in source text",
                    }
                )
                continue
            line_start, line_end = matches[0]
            old_row = line_text_without_newline(text[line_start:line_end]).strip().rstrip(",")
            text = remove_text_line_preserving_surroundings(text, line_start, line_end)
            changed.append({"part": part_name, "section": "beams", "operation": "delete", "old": old_row, "new": []})

        for update in beam_inserts:
            node_ids = [str(node_id) for node_id in update.get("nodes", [])[:2]]
            if len(node_ids) != 2:
                skipped.append({"part": part_name, "section": "beams", "nodes": node_ids, "reason": "Beam insert is not two nodes"})
                continue
            section_bounds = find_jbeam_part_section_array_bounds(text, part_name, "beams")
            if section_bounds is None:
                skipped.append({"part": part_name, "section": "beams", "nodes": node_ids, "reason": "Beams section not found"})
                continue
            if find_unique_beam_row_line(text, section_bounds, node_ids):
                skipped.append({"part": part_name, "section": "beams", "nodes": node_ids, "reason": "Beam already exists"})
                continue
            text = insert_beam_row_into_section_text(text, section_bounds, node_ids, update.get("params", {}))
            changed.append({"part": part_name, "section": "beams", "operation": "insert", "old": [], "new": node_ids})

    return text, changed, skipped


def apply_jbeam_updates_to_source_text(source_text, file_group):
    text, node_changed, node_skipped = apply_node_updates_to_jbeam_text(source_text, file_group)
    text, topology_changed, topology_skipped = apply_topology_updates_to_jbeam_text(text, file_group)
    return text, node_changed + topology_changed, node_skipped + topology_skipped


def apply_node_updates_to_jbeam_payload(payload, file_group):
    changed = []
    skipped = []
    if not isinstance(payload, dict):
        return changed, [{"reason": "JBeam payload is not an object"}]

    for part_group in file_group.get("parts", []):
        part_name = part_group.get("part", "")
        part_data = payload.get(part_name)
        if not isinstance(part_data, dict):
            skipped.append({"part": part_name, "reason": "Part not found in source payload"})
            continue
        rows = part_data.get("nodes")
        if not isinstance(rows, list):
            skipped.append({"part": part_name, "reason": "Part has no nodes list"})
            continue

        rows_by_node = {}
        for row in rows:
            if isinstance(row, list) and len(row) >= 4:
                rows_by_node[str(row[0])] = row

        for update in part_group.get("node_inserts", []):
            node_id = str(update.get("node", ""))
            if not node_id:
                skipped.append({"part": part_name, "reason": "Node insert has no id"})
                continue
            if node_id in rows_by_node:
                skipped.append({"part": part_name, "node": node_id, "reason": "Node already exists"})
                continue
            new_position = rounded_position_list(update.get("new_position", []))
            if len(new_position) != 3:
                skipped.append({"part": part_name, "node": node_id, "reason": "New node position is not xyz"})
                continue
            row = [node_id, *new_position]
            insert_index = len(rows)
            if rows and isinstance(rows[-1], dict) and "group" in rows[-1]:
                insert_index = len(rows) - 1
            params = {
                str(key): coerce_jbeam_param_value(value)
                for key, value in (update.get("params") or {}).items()
                if str(key).strip() and value is not None and value != ""
            }
            if params:
                rows.insert(insert_index, params)
                insert_index += 1
            rows.insert(insert_index, row)
            rows_by_node[node_id] = row
            changed.append(
                {
                    "part": part_name,
                    "section": "nodes",
                    "operation": "insert",
                    "node": node_id,
                    "old": [],
                    "new": row,
                    "new_position": new_position,
                }
            )

        for update in part_group.get("node_deletes", []):
            node_id = str(update.get("node", ""))
            row = rows_by_node.get(node_id)
            if row is None:
                skipped.append({"part": part_name, "node": node_id, "reason": "Node row not found for delete"})
                continue
            rows.remove(row)
            rows_by_node.pop(node_id, None)
            changed.append(
                {
                    "part": part_name,
                    "section": "nodes",
                    "operation": "delete",
                    "node": node_id,
                    "old": row,
                    "new": [],
                }
            )

        for update in part_group.get("node_updates", []):
            node_id = str(update.get("node", ""))
            row = rows_by_node.get(node_id)
            if row is None:
                skipped.append({"part": part_name, "node": node_id, "reason": "Node row not found"})
                continue
            new_position = rounded_position_list(update.get("new_position", []))
            if len(new_position) != 3:
                skipped.append({"part": part_name, "node": node_id, "reason": "New position is not xyz"})
                continue
            old_position = rounded_position_list(row[1:4])
            row[1:4] = new_position
            changed.append(
                {
                    "part": part_name,
                    "node": node_id,
                    "old_position": old_position,
                    "new_position": new_position,
                }
            )
        for update in part_group.get("node_param_updates", []):
            node_id = str(update.get("node", ""))
            row = rows_by_node.get(node_id)
            if row is None:
                skipped.append({"part": part_name, "node": node_id, "reason": "Node row not found for params"})
                continue
            row_index = rows.index(row)
            if insert_params_before_row(rows, row_index, update.get("params", {})):
                changed.append(
                    {
                        "part": part_name,
                        "section": "nodes",
                        "operation": "update",
                        "field": "params",
                        "node": node_id,
                        "new": update.get("params", {}),
                    }
                )
    return changed, skipped


def row_matches_beam_nodes(row, node_ids):
    if not isinstance(row, list) or len(row) < 2 or len(node_ids) < 2:
        return False
    return set(str(value) for value in row[:2]) == set(str(value) for value in node_ids[:2])


def row_matches_triangle_nodes(row, node_ids):
    if not isinstance(row, list) or len(row) < 3 or len(node_ids) < 3:
        return False
    return [str(value) for value in row[:3]] == [str(value) for value in node_ids[:3]]


def row_matches_triangle_identity(row, node_ids):
    if not isinstance(row, list) or len(row) < 3 or len(node_ids) < 3:
        return False
    return face_identity_key(row) == face_identity_key(node_ids)


def clean_jbeam_params(params):
    return {
        str(key): coerce_jbeam_param_value(value)
        for key, value in (params or {}).items()
        if str(key).strip() and value is not None and value != ""
    }


def insert_params_before_row(rows, row_index, params):
    cleaned = clean_jbeam_params(params)
    if not cleaned:
        return False
    rows.insert(row_index, cleaned)
    return True


def apply_topology_updates_to_jbeam_payload(payload, file_group):
    changed = []
    skipped = []
    if not isinstance(payload, dict):
        return changed, [{"reason": "JBeam payload is not an object"}]

    for part_group in file_group.get("parts", []):
        part_name = part_group.get("part", "")
        part_data = payload.get(part_name)
        if not isinstance(part_data, dict):
            skipped.append({"part": part_name, "reason": "Part not found in source payload"})
            continue

        beam_deletes = list(part_group.get("beam_deletes", []))
        beam_inserts = list(part_group.get("beam_inserts", []))
        beam_param_updates = list(part_group.get("beam_param_updates", []))
        triangle_deletes = list(part_group.get("triangle_deletes", []))
        triangle_inserts = list(part_group.get("triangle_inserts", []))
        triangle_node_updates = list(part_group.get("triangle_node_updates", []))
        triangle_param_updates = list(part_group.get("triangle_param_updates", []))

        if beam_deletes or beam_inserts or beam_param_updates:
            beams = part_data.setdefault("beams", [])
            if not isinstance(beams, list):
                skipped.append({"part": part_name, "section": "beams", "reason": "Part beams section is not a list"})
                beams = None
        else:
            beams = None

        if triangle_deletes or triangle_inserts or triangle_node_updates or triangle_param_updates:
            triangles = part_data.setdefault("triangles", [])
            if not isinstance(triangles, list):
                skipped.append({"part": part_name, "section": "triangles", "reason": "Part triangles section is not a list"})
                triangles = None
        else:
            triangles = None

        if beams is not None:
            for update in beam_deletes:
                node_ids = update.get("nodes", [])
                matches = [index for index, row in enumerate(beams) if row_matches_beam_nodes(row, node_ids)]
                if len(matches) != 1:
                    skipped.append(
                        {
                            "part": part_name,
                            "section": "beams",
                            "nodes": node_ids,
                            "reason": "Beam row not found uniquely for delete",
                        }
                    )
                    continue
                old_row = beams.pop(matches[0])
                changed.append({"part": part_name, "section": "beams", "operation": "delete", "old": old_row, "new": []})
            for update in beam_inserts:
                node_ids = update.get("nodes", [])
                if len(node_ids) != 2:
                    skipped.append({"part": part_name, "section": "beams", "nodes": node_ids, "reason": "Beam insert is not two nodes"})
                    continue
                if any(row_matches_beam_nodes(row, node_ids) for row in beams):
                    skipped.append({"part": part_name, "section": "beams", "nodes": node_ids, "reason": "Beam already exists"})
                    continue
                row = [str(node_ids[0]), str(node_ids[1])]
                params = clean_jbeam_params(update.get("params", {}))
                if params:
                    beams.append(params)
                beams.append(row)
                changed.append({"part": part_name, "section": "beams", "operation": "insert", "old": [], "new": row})
            for update in beam_param_updates:
                node_ids = update.get("nodes", [])
                matches = [index for index, row in enumerate(beams) if row_matches_beam_nodes(row, node_ids)]
                if len(matches) != 1:
                    skipped.append({"part": part_name, "section": "beams", "nodes": node_ids, "reason": "Beam row not found uniquely for params"})
                    continue
                if insert_params_before_row(beams, matches[0], update.get("params", {})):
                    changed.append({"part": part_name, "section": "beams", "operation": "update", "field": "params", "nodes": node_ids, "new": update.get("params", {})})

        if triangles is not None:
            for update in triangle_deletes:
                node_ids = update.get("nodes", [])
                matches = [index for index, row in enumerate(triangles) if row_matches_triangle_nodes(row, node_ids)]
                if len(matches) != 1:
                    skipped.append(
                        {
                            "part": part_name,
                            "section": "triangles",
                            "nodes": node_ids,
                            "reason": "Triangle row not found uniquely for delete",
                        }
                    )
                    continue
                old_row = triangles.pop(matches[0])
                changed.append({"part": part_name, "section": "triangles", "operation": "delete", "old": old_row, "new": []})
            for update in triangle_inserts:
                node_ids = update.get("nodes", [])
                if len(node_ids) != 3:
                    skipped.append({"part": part_name, "section": "triangles", "nodes": node_ids, "reason": "Triangle insert is not three nodes"})
                    continue
                if any(row_matches_triangle_nodes(row, node_ids) for row in triangles):
                    skipped.append({"part": part_name, "section": "triangles", "nodes": node_ids, "reason": "Triangle already exists"})
                    continue
                row = [str(node_ids[0]), str(node_ids[1]), str(node_ids[2])]
                params = clean_jbeam_params(update.get("params", {}))
                if params:
                    triangles.append(params)
                triangles.append(row)
                changed.append({"part": part_name, "section": "triangles", "operation": "insert", "old": [], "new": row})
            for update in triangle_node_updates:
                old_node_ids = update.get("old_nodes", [])
                new_node_ids = update.get("nodes", [])
                if len(old_node_ids) != 3 or len(new_node_ids) != 3:
                    skipped.append({"part": part_name, "section": "triangles", "nodes": new_node_ids, "reason": "Triangle winding update is not three nodes"})
                    continue
                matches = [index for index, row in enumerate(triangles) if row_matches_triangle_identity(row, old_node_ids)]
                if len(matches) != 1:
                    skipped.append({"part": part_name, "section": "triangles", "nodes": old_node_ids, "reason": "Triangle row not found uniquely for winding"})
                    continue
                old_row = list(triangles[matches[0]])
                new_row = [str(new_node_ids[0]), str(new_node_ids[1]), str(new_node_ids[2])]
                triangles[matches[0]] = new_row
                changed.append({"part": part_name, "section": "triangles", "operation": "update", "field": "nodes", "old": old_row, "new": new_row})
            for update in triangle_param_updates:
                node_ids = update.get("nodes", [])
                matches = [index for index, row in enumerate(triangles) if row_matches_triangle_nodes(row, node_ids)]
                if len(matches) != 1:
                    skipped.append({"part": part_name, "section": "triangles", "nodes": node_ids, "reason": "Triangle row not found uniquely for params"})
                    continue
                if insert_params_before_row(triangles, matches[0], update.get("params", {})):
                    changed.append({"part": part_name, "section": "triangles", "operation": "update", "field": "params", "nodes": node_ids, "new": update.get("params", {})})

    return changed, skipped


def apply_jbeam_updates_to_payload(payload, file_group):
    node_changed, node_skipped = apply_node_updates_to_jbeam_payload(payload, file_group)
    topology_changed, topology_skipped = apply_topology_updates_to_jbeam_payload(payload, file_group)
    return node_changed + topology_changed, node_skipped + topology_skipped


def build_jbeam_patched_cache_copies(context, selected_virtual_paths=None):
    history = jbeam_operation_history(context.scene)
    plan = build_jbeam_override_export_plan(context, history)
    plan = filter_plan_files_for_selected_virtual_paths(plan, selected_virtual_paths)
    source_index, source_warning = jbeam_asset_source_index_for_context(context)
    if source_warning:
        plan["warnings"].append(source_warning)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_root = persistent_cache_dir() / "jbeam_editor" / f"patched_jbeam_cache_{stamp}"
    output_root.mkdir(parents=True, exist_ok=True)

    files = []
    for file_group in plan["files"]:
        virtual_path = normalize_virtual_path(file_group.get("virtual_path", ""))
        source = jbeam_source_for_file_group(file_group, source_index)
        new_file_payload = new_jbeam_payload_for_virtual_path(context.scene, virtual_path)
        file_result = {
            "source_file": file_group.get("source_file", ""),
            "virtual_path": virtual_path,
            "planned_target_path": file_group.get("planned_target_path", ""),
            "cache_output_path": "",
            "patch_mode": "",
            "accepted_operation_count": file_group.get("operation_count", file_group.get("node_update_count", 0)),
            "accepted_node_update_count": file_group.get("node_update_count", 0),
            "accepted_topology_update_count": file_group.get("topology_update_count", 0),
            "changed_operation_count": 0,
            "changed_node_count": 0,
            "changed_topology_count": 0,
            "skipped_update_count": 0,
            "warnings": [],
            "changed_operations": [],
            "changed_nodes": [],
            "skipped_updates": [],
        }
        if new_file_payload is not None:
            output_path = output_root / Path(virtual_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                payload = json.loads(json.dumps(new_file_payload))
                changed, skipped = apply_jbeam_updates_to_payload(payload, file_group)
                file_result["patch_mode"] = "new_file_clean_json"
                write_compact_jbeam_json(output_path, payload)
            except Exception as exc:
                file_result["warnings"].append(f"Could not build staged new JBeam file: {exc}")
                files.append(file_result)
                continue
            file_result["cache_output_path"] = str(output_path)
            file_result["changed_operation_count"] = len(changed) or 1
            file_result["changed_node_count"] = len([item for item in changed if item.get("section", "nodes") == "nodes"])
            file_result["changed_topology_count"] = len([item for item in changed if item.get("section") in {"beams", "triangles"}])
            file_result["is_new_file"] = True
            file_result["file_create_count"] = 1
            file_result["skipped_update_count"] = len(skipped)
            file_result["changed_operations"] = changed
            file_result["changed_nodes"] = changed
            file_result["skipped_updates"] = skipped
            if skipped:
                file_result["warnings"].append("Some accepted JBeam updates could not be applied to the staged new-file payload")
            files.append(file_result)
            continue
        if source is None:
            file_result["warnings"].append("Could not find matching JBeam asset source for virtual path")
            files.append(file_result)
            continue

        try:
            source_text = read_jbeam_asset_source_text(source)
        except Exception as exc:
            file_result["warnings"].append(f"Could not read source JBeam: {exc}")
            files.append(file_result)
            continue

        output_path = output_root / Path(virtual_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        patched_text, text_changed, text_skipped = apply_jbeam_updates_to_source_text(source_text, file_group)
        expected_node_update_count = sum(
            len(part_group.get("node_inserts", []))
            + len(part_group.get("node_updates", []))
            + len(part_group.get("node_deletes", []))
            for part_group in file_group.get("parts", [])
        )
        expected_topology_update_count = sum(
            len(part_group.get("beam_inserts", []))
            + len(part_group.get("beam_deletes", []))
            + len(part_group.get("triangle_inserts", []))
            + len(part_group.get("triangle_deletes", []))
            for part_group in file_group.get("parts", [])
        )
        expected_update_count = expected_node_update_count + expected_topology_update_count
        has_node_structural_changes = any(
            part_group.get("node_inserts") or part_group.get("node_deletes")
            for part_group in file_group.get("parts", [])
        )
        if not has_node_structural_changes and text_changed and not text_skipped and len(text_changed) == expected_update_count:
            changed = text_changed
            skipped = []
            file_result["patch_mode"] = "source_preserving_text"
            write_text_without_newline_translation(output_path, patched_text)
        else:
            try:
                payload = load_jsonc_text(source_text)
                changed, skipped = apply_jbeam_updates_to_payload(payload, file_group)
                file_result["patch_mode"] = "clean_json_fallback"
                write_compact_jbeam_json(output_path, payload)
            except Exception as exc:
                file_result["warnings"].append(f"Could not parse source JBeam for clean JSON fallback: {exc}")
                file_result["skipped_updates"] = text_skipped
                file_result["skipped_update_count"] = len(text_skipped)
                files.append(file_result)
                continue
            if text_skipped:
                file_result["warnings"].append("Source-preserving text patch was not safe; used clean JSON fallback")
                file_result["source_preserving_skipped_updates"] = text_skipped

        file_result["cache_output_path"] = str(output_path)
        file_result["changed_operation_count"] = len(changed)
        file_result["changed_node_count"] = len([item for item in changed if item.get("section", "nodes") == "nodes"])
        file_result["changed_topology_count"] = len([item for item in changed if item.get("section") in {"beams", "triangles"}])
        file_result["skipped_update_count"] = len(skipped)
        file_result["changed_operations"] = changed
        file_result["changed_nodes"] = changed
        file_result["skipped_updates"] = skipped
        if skipped:
            file_result["warnings"].append("Some accepted JBeam updates could not be applied to the parsed source")
        files.append(file_result)

    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "precision": JBEAM_POSITION_PRECISION,
        "cache_only": True,
        "output_root": str(output_root),
        "operation_count": plan.get("operation_count", plan["node_update_count"]),
        "file_create_count": plan.get("file_create_count", 0),
        "node_update_count": plan["node_update_count"],
        "topology_update_count": plan.get("topology_update_count", 0),
        "changed_operation_count": sum(file_group["changed_operation_count"] for file_group in files),
        "file_create_count": sum(int(file_group.get("file_create_count", 0)) for file_group in files),
        "changed_node_count": sum(file_group["changed_node_count"] for file_group in files),
        "changed_topology_count": sum(file_group["changed_topology_count"] for file_group in files),
        "skipped_update_count": sum(file_group["skipped_update_count"] for file_group in files),
        "warnings": sorted(set(plan["warnings"] + [
            "Patched cache copies prefer source-preserving text patches for node positions and Beam insert/delete edits; other topology edits use clean JSON fallback.",
            "No BeamNG user, mod, or vanilla files were modified.",
        ])),
        "files": files,
    }
    manifest_path = output_root / "patched_jbeam_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest_path, manifest


def jbeam_patched_cache_copy_lines(manifest):
    lines = [
        "[BeamNG Patched JBeam Cache Copies]",
        f"Generated: {manifest['generated_at']}",
        f"Output root: {manifest['output_root']}",
        f"Accepted operations: {manifest.get('operation_count', manifest['node_update_count'])}",
        f"New files: {manifest.get('file_create_count', 0)}",
        f"Accepted node edits: {manifest['node_update_count']}",
        f"Accepted topology updates: {manifest.get('topology_update_count', 0)}",
        f"Applied operations: {manifest.get('changed_operation_count', manifest['changed_node_count'])}",
        f"Applied node edits: {manifest['changed_node_count']}",
        f"Applied topology updates: {manifest.get('changed_topology_count', 0)}",
        f"Skipped updates: {manifest['skipped_update_count']}",
        "Cache only: yes",
        "",
    ]
    for warning in manifest["warnings"]:
        lines.append(f"Warning: {warning}")
    lines.append("")

    for file_group in manifest["files"]:
        lines.append(f"Virtual: {file_group['virtual_path'] or '(unknown)'}")
        lines.append(f"Output: {file_group['cache_output_path'] or '(not written)'}")
        if file_group.get("is_new_file"):
            lines.append("File status: new staged JBeam file")
        lines.append(f"Patch mode: {file_group.get('patch_mode', '') or '(none)'}")
        lines.append(f"Accepted: {file_group.get('accepted_operation_count', file_group.get('changed_operation_count', 0))}")
        lines.append(f"Applied: {file_group.get('changed_operation_count', file_group['changed_node_count'])}")
        lines.append(f"Skipped: {file_group['skipped_update_count']}")
        for warning in file_group["warnings"]:
            lines.append(f"File warning: {warning}")
        for skipped in file_group.get("source_preserving_skipped_updates", []):
            lines.append(
                "  Source-preserving skipped "
                f"{skipped.get('part', '')}.{skipped.get('node', '')}: {skipped.get('reason', '')}"
            )
        for changed in file_group.get("changed_operations", file_group["changed_nodes"]):
            lines.append(
                "  "
                f"{changed.get('part', '')}.{changed.get('section', 'nodes')}.{changed.get('node', '')}: "
                f"{changed.get('old_position', '')} -> {changed.get('new_position', '')}"
                if changed.get("section", "nodes") == "nodes"
                else f"  {changed.get('operation', '')} {changed.get('section', '')}: {changed.get('old', '')} -> {changed.get('new', '')}"
            )
        for skipped in file_group["skipped_updates"]:
            lines.append(
                "  Skipped "
                f"{skipped.get('part', '')}.{skipped.get('node', '')}: {skipped.get('reason', '')}"
            )
        lines.append("")
    if not manifest["files"]:
        lines.append("No accepted JBeam edits are recorded.")
    return lines


def write_jbeam_patched_cache_copy_report(context, selected_virtual_paths=None):
    manifest_path, manifest = build_jbeam_patched_cache_copies(context, selected_virtual_paths=selected_virtual_paths)
    text = bpy.data.texts.get("BeamNG Patched JBeam Cache Copies") or bpy.data.texts.new(
        "BeamNG Patched JBeam Cache Copies"
    )
    text.clear()
    text.write("\n".join(jbeam_patched_cache_copy_lines(manifest)))
    text.write("\n")

    context.scene["beamng_jbeam_last_patched_cache_manifest_path"] = str(manifest_path)
    return manifest_path, manifest


def stage_jbeam_patched_cache_copies_to_user_folder(context, overwrite_existing=False, selected_virtual_paths=None):
    manifest_path, manifest = build_jbeam_patched_cache_copies(
        context,
        selected_virtual_paths=selected_virtual_paths,
    )
    staged_files = []
    skipped_files = []
    current_folder = user_current_folder_from_preferences(context)
    mod_name = jbeam_export_mod_name(context)
    export_root = current_folder / "mods" / "unpacked" / mod_name if current_folder else None
    backup_root = Path(manifest["output_root"]) / "overwritten_mod_override_backups"

    for file_group in manifest["files"]:
        cache_output_path = Path(file_group.get("cache_output_path", ""))
        planned_target = file_group.get("planned_target_path", "")
        if not cache_output_path or not cache_output_path.exists():
            skipped_files.append(
                {
                    "virtual_path": file_group.get("virtual_path", ""),
                    "target_path": planned_target,
                    "reason": "Patched cache output was not written",
                }
            )
            continue
        if int(file_group.get("changed_operation_count", file_group.get("changed_node_count", 0))) == 0:
            skipped_files.append(
                {
                    "virtual_path": file_group.get("virtual_path", ""),
                    "target_path": planned_target,
                    "reason": "Patched cache output contained no applied JBeam changes",
                }
            )
            continue
        deleted_changed_nodes = {
            str(change.get("node", ""))
            for change in file_group.get("changed_operations", [])
            if change.get("section", "nodes") == "nodes" and change.get("operation") == "delete"
        }
        inserted_deleted_refs = [
            change
            for change in file_group.get("changed_operations", [])
            if change.get("section") in {"beams", "triangles"}
            and change.get("operation") == "insert"
            and any(str(node_id) in deleted_changed_nodes for node_id in change.get("new", []))
        ]
        if inserted_deleted_refs:
            skipped_files.append(
                {
                    "virtual_path": file_group.get("virtual_path", ""),
                    "target_path": planned_target,
                    "reason": "Refusing to stage an internally inconsistent JBeam patch",
                }
            )
            continue

        target_path, target_warning = safe_jbeam_mod_override_target_for_virtual_path(
            context,
            current_folder,
            file_group.get("virtual_path", ""),
        )
        if target_path is None:
            skipped_files.append(
                {
                    "virtual_path": file_group.get("virtual_path", ""),
                    "target_path": planned_target,
                    "reason": target_warning,
                }
            )
            continue
        if target_path.exists():
            if not overwrite_existing:
                skipped_files.append(
                    {
                        "virtual_path": file_group.get("virtual_path", ""),
                        "target_path": str(target_path),
                        "reason": "Refusing to overwrite existing unpacked mod JBeam file",
                    }
                )
                continue
            backup_path = backup_root / Path(file_group.get("virtual_path", ""))
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            backup_path.write_bytes(target_path.read_bytes())
        else:
            backup_path = None

        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(cache_output_path.read_bytes())
        staged_files.append(
            {
                "virtual_path": file_group.get("virtual_path", ""),
                "cache_output_path": str(cache_output_path),
                "target_path": str(target_path),
                "backup_path": str(backup_path) if backup_path else "",
                "overwrote_existing": backup_path is not None,
                "patch_mode": file_group.get("patch_mode", ""),
                "changed_operation_count": file_group.get("changed_operation_count", file_group.get("changed_node_count", 0)),
                "is_new_file": bool(file_group.get("is_new_file")),
                "file_create_count": int(file_group.get("file_create_count", 0)),
                "changed_node_count": file_group.get("changed_node_count", 0),
                "changed_topology_count": file_group.get("changed_topology_count", 0),
                "skipped_update_count": file_group.get("skipped_update_count", 0),
                "warnings": list(file_group.get("warnings", [])),
            }
        )

    stage_manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "precision": JBEAM_POSITION_PRECISION,
        "source_cache_manifest_path": str(manifest_path),
        "user_current_folder": str(current_folder) if current_folder else "",
        "export_mod_folder": mod_name,
        "export_root": str(export_root) if export_root else "",
        "overwrite_existing": overwrite_existing,
        "selected_virtual_paths": list(selected_virtual_paths) if selected_virtual_paths is not None else [],
        "operation_history_checkpoint_path": "",
        "operation_history_cleared": False,
        "staged_file_count": len(staged_files),
        "skipped_file_count": len(skipped_files),
        "changed_operation_count": manifest.get("changed_operation_count", manifest["changed_node_count"]),
        "file_create_count": manifest.get("file_create_count", 0),
        "changed_node_count": manifest["changed_node_count"],
        "changed_topology_count": manifest.get("changed_topology_count", 0),
        "warnings": sorted(set(manifest["warnings"] + [
            (
                "Experimental staging overwrote existing unpacked mod JBeam files only after backing them up."
                if overwrite_existing
                else "Experimental staging refused to overwrite existing unpacked mod JBeam files."
            ),
            "Staged files use source-preserving text patches for node-only files when safe; topology edits use clean JSON fallback.",
            "JBeam files are staged under current/mods/unpacked; current/vehicles is reserved for .pc configurations.",
        ])),
        "staged_files": staged_files,
        "skipped_files": skipped_files,
    }

    stage_manifest_path = Path(manifest["output_root"]) / "staged_mod_override_manifest.json"
    stage_manifest_path.write_text(json.dumps(stage_manifest, indent=2), encoding="utf-8")
    return stage_manifest_path, stage_manifest


def jbeam_user_override_stage_lines(stage_manifest):
    lines = [
        "[BeamNG Staged JBeam Unpacked Mod Overrides]",
        f"Generated: {stage_manifest['generated_at']}",
        f"User current folder: {stage_manifest['user_current_folder'] or '(not configured)'}",
        f"JBeam export mod: {stage_manifest.get('export_mod_folder', '') or '(not configured)'}",
        f"JBeam export root: {stage_manifest.get('export_root', '') or '(not configured)'}",
        f"Staged files: {stage_manifest['staged_file_count']}",
        f"Skipped files: {stage_manifest['skipped_file_count']}",
        f"New files: {stage_manifest.get('file_create_count', 0)}",
        f"Changed operations in source cache: {stage_manifest.get('changed_operation_count', stage_manifest['changed_node_count'])}",
        f"Changed node edits in source cache: {stage_manifest['changed_node_count']}",
        f"Changed topology in source cache: {stage_manifest.get('changed_topology_count', 0)}",
        f"Operation history cleared: {'yes' if stage_manifest.get('operation_history_cleared') else 'no'}",
        f"Remaining history operations: {stage_manifest.get('operation_history_remaining_count', 0)}",
        "",
    ]
    if stage_manifest.get("operation_history_checkpoint_path"):
        lines.append(f"Operation checkpoint: {stage_manifest['operation_history_checkpoint_path']}")
        lines.append("")
    for warning in stage_manifest["warnings"]:
        lines.append(f"Warning: {warning}")
    lines.append("")

    for staged in stage_manifest["staged_files"]:
        lines.append(f"Staged: {staged['virtual_path']}")
        if staged.get("is_new_file"):
            lines.append("  File status: new staged JBeam file")
        lines.append(f"  From: {staged['cache_output_path']}")
        lines.append(f"  To: {staged['target_path']}")
        if staged.get("backup_path"):
            lines.append(f"  Backup: {staged['backup_path']}")
        lines.append(f"  Overwrote existing: {'yes' if staged.get('overwrote_existing') else 'no'}")
        lines.append(f"  Patch mode: {staged.get('patch_mode', '') or '(unknown)'}")
        lines.append(f"  Changed operations: {staged.get('changed_operation_count', staged['changed_node_count'])}")
        lines.append(f"  Changed node edits: {staged['changed_node_count']}")
        lines.append(f"  Changed topology: {staged.get('changed_topology_count', 0)}")
        if staged.get("skipped_update_count", 0):
            lines.append(f"  Skipped stale/non-applicable history updates: {staged['skipped_update_count']}")
        for warning in staged.get("warnings", []):
            lines.append(f"  Warning: {warning}")
    if stage_manifest["staged_files"]:
        lines.append("")

    for skipped in stage_manifest["skipped_files"]:
        lines.append(f"Skipped: {skipped.get('virtual_path', '') or '(unknown)'}")
        lines.append(f"  Target: {skipped.get('target_path', '') or '(none)'}")
        lines.append(f"  Reason: {skipped.get('reason', '')}")
    if not stage_manifest["staged_files"] and not stage_manifest["skipped_files"]:
        lines.append("No accepted JBeam edits are recorded.")
    return lines


def write_jbeam_user_override_stage_report(context, overwrite_existing=False, selected_virtual_paths=None):
    stage_manifest_path, stage_manifest = stage_jbeam_patched_cache_copies_to_user_folder(
        context,
        overwrite_existing=overwrite_existing,
        selected_virtual_paths=selected_virtual_paths,
    )
    text = bpy.data.texts.get("BeamNG Staged JBeam Mod Overrides") or bpy.data.texts.new(
        "BeamNG Staged JBeam Mod Overrides"
    )
    text.clear()
    text.write("\n".join(jbeam_user_override_stage_lines(stage_manifest)))
    text.write("\n")

    context.scene["beamng_jbeam_last_mod_override_stage_manifest_path"] = str(stage_manifest_path)
    context.scene["beamng_jbeam_last_user_override_stage_manifest_path"] = str(stage_manifest_path)
    return stage_manifest_path, stage_manifest


class BEAMNG_OT_scan_experimental_jbeam_mesh_edits(Operator):
    bl_idname = "beamng_pc_importer.scan_experimental_jbeam_mesh_edits"
    bl_label = "Scan JBeam Mesh Edits"
    bl_description = "Detect moved owned nodes in experimental JBeam meshes and restore moved proxy/reference nodes"
    bl_options = {"REGISTER", "UNDO"}

    active_only: BoolProperty(
        name="Active Mesh Only",
        description="Scan only the active experimental JBeam mesh when possible",
        default=False,
    )

    def execute(self, context):
        result = scan_experimental_jbeam_mesh_edits(context.scene, active_only=self.active_only)
        self.report(
            {"INFO"},
            (
                f"Scanned {result['scanned_mesh_count']} JBeam mesh(es); "
                f"recorded {len(result['changes'])} edit(s) "
                f"({result['topology_change_count']} topology); "
                f"restored {result['restored_proxy_count']} proxy node move(s)"
            ),
        )
        return {"FINISHED"}


class BEAMNG_OT_refresh_jbeam_assembly_parts(Operator):
    bl_idname = "beamng_pc_importer.refresh_jbeam_assembly_parts"
    bl_label = "Refresh Assembly Parts"
    bl_description = "Refresh the assembly part list from imported/editable JBeam mesh objects"
    bl_options = {"REGISTER"}

    def execute(self, context):
        objects = refresh_jbeam_assembly_parts(context.scene)
        self.report({"INFO"}, f"Found {len(objects)} JBeam assembly part(s)")
        return {"FINISHED"}


class BEAMNG_OT_set_active_jbeam_part_from_selection(Operator):
    bl_idname = "beamng_pc_importer.set_active_jbeam_part_from_selection"
    bl_label = "Set Active Part From Selection"
    bl_description = "Make the selected imported JBeam mesh the Active Part for ordinary topology editing"
    bl_options = {"REGISTER", "UNDO"}

    clear: BoolProperty(name="Clear Active Part", default=False)

    def execute(self, context):
        if self.clear:
            set_active_jbeam_assembly_part(context.scene, None)
            apply_jbeam_active_part_reference_display(context.scene)
            self.report({"INFO"}, "Cleared Active Part")
            return {"FINISHED"}
        obj = active_experimental_jbeam_mesh(context)
        if obj is None:
            self.report({"WARNING"}, "Select an imported/experimental JBeam mesh first")
            return {"CANCELLED"}
        refresh_jbeam_assembly_parts(context.scene)
        set_active_jbeam_assembly_part(context.scene, obj)
        apply_jbeam_active_part_reference_display(context.scene)
        self.report({"INFO"}, f"Active Part: {context.scene.get('beamng_active_jbeam_part_name', '')}")
        return {"FINISHED"}


class BEAMNG_OT_activate_jbeam_assembly_part(Operator):
    bl_idname = "beamng_pc_importer.activate_jbeam_assembly_part"
    bl_label = "Activate Assembly Part"
    bl_description = "Select and activate a JBeam assembly part"
    bl_options = {"REGISTER", "UNDO"}

    part_key: StringProperty(default="")

    def execute(self, context):
        refresh_jbeam_assembly_parts(context.scene)
        for obj in experimental_jbeam_part_objects(context.scene):
            if jbeam_assembly_part_key_for_object(obj) == self.part_key:
                if context.object and context.object.mode != "OBJECT":
                    bpy.ops.object.mode_set(mode="OBJECT")
                for selected in context.selected_objects:
                    selected.select_set(False)
                obj.select_set(True)
                context.view_layer.objects.active = obj
                set_active_jbeam_assembly_part(context.scene, obj)
                apply_jbeam_active_part_reference_display(context.scene)
                self.report({"INFO"}, f"Active Part: {obj.get('beamng_part_name', obj.name)}")
                return {"FINISHED"}
        self.report({"WARNING"}, "Assembly part is no longer available")
        return {"CANCELLED"}


class BEAMNG_OT_accept_experimental_jbeam_node_moves(Operator):
    bl_idname = "beamng_pc_importer.accept_experimental_jbeam_node_moves"
    bl_label = "Accept JBeam Edits"
    bl_description = "Accept pending experimental JBeam node/topology edits into the operation history and update mesh baselines"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        result = accept_experimental_jbeam_node_moves(context.scene)
        if result["accepted_count"] == 0:
            self.report({"WARNING"}, "No pending experimental JBeam edits to accept")
            return {"CANCELLED"}
        self.report(
            {"INFO"},
            f"Accepted {result['accepted_count']} JBeam edit(s); operation history now has {result['history_count']} item(s)",
        )
        return {"FINISHED"}


class BEAMNG_OT_mark_selected_edges_as_jbeam_beams(Operator):
    bl_idname = "beamng_pc_importer.mark_selected_edges_as_jbeam_beams"
    bl_label = "Mark Selected Edges As Beams"
    bl_description = "Treat selected experimental mesh edges as intentional JBeam beams when scanning topology edits"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = active_experimental_jbeam_mesh(context)
        if obj is None or obj.type != "MESH" or obj.get("beamng_visual_type") != "experimental_jbeam_mesh":
            self.report({"WARNING"}, "Select an experimental JBeam mesh edge first")
            return {"CANCELLED"}
        if not require_active_part_for_topology_edit(self, context, obj):
            return {"CANCELLED"}
        keys = selected_experimental_jbeam_edge_node_keys(obj)
        if not keys:
            self.report({"WARNING"}, "No selected JBeam mesh edges found")
            return {"CANCELLED"}
        result = set_selected_edge_semantic_type(obj, context.scene, JBEAM_EDGE_SEMANTIC_BEAM)
        self.report({"INFO"}, f"Marked {result.get('changed', len(keys))} selected edge(s) as intentional JBeam beam(s)")
        return {"FINISHED"}


class BEAMNG_OT_set_selected_jbeam_edge_semantic(Operator):
    bl_idname = "beamng_pc_importer.set_selected_jbeam_edge_semantic"
    bl_label = "Set Selected Edge Semantic"
    bl_description = "Classify selected topology edges as beams, triangle boundaries, or generic relationships"
    bl_options = {"REGISTER", "UNDO"}

    semantic_type: EnumProperty(
        name="Semantic Type",
        items=(
            (JBEAM_EDGE_SEMANTIC_BEAM, "Beam", "Export selected edges as JBeam beams"),
            (JBEAM_EDGE_SEMANTIC_TRIANGLE_BOUNDARY, "Triangle Boundary", "Treat selected edges as face boundaries only"),
            (JBEAM_EDGE_SEMANTIC_RELATIONSHIP, "Relationship", "Keep selected edges as non-exporting topology relationships"),
        ),
        default=JBEAM_EDGE_SEMANTIC_BEAM,
    )

    def execute(self, context):
        obj = active_experimental_jbeam_mesh(context)
        if obj is None or obj.type != "MESH" or obj.get("beamng_visual_type") != "experimental_jbeam_mesh":
            self.report({"WARNING"}, "Select an experimental JBeam mesh edge first")
            return {"CANCELLED"}
        if not require_active_part_for_topology_edit(self, context, obj):
            return {"CANCELLED"}
        result = set_selected_edge_semantic_type(obj, context.scene, self.semantic_type)
        changed = int(result.get("changed", 0))
        if not changed:
            self.report({"WARNING"}, "No selected JBeam mesh edges found")
            return {"CANCELLED"}
        label = self.semantic_type.replace("_", " ")
        self.report({"INFO"}, f"Set {changed} selected edge(s) to {label}")
        return {"FINISHED"}


class BEAMNG_OT_report_experimental_jbeam_selection(Operator):
    bl_idname = "beamng_pc_importer.report_experimental_jbeam_selection"
    bl_label = "Report JBeam Mesh Selection"
    bl_description = "Report the active experimental JBeam mesh and selected vertex/edge/face counts for UI debugging"
    bl_options = {"REGISTER"}

    def execute(self, context):
        obj = active_experimental_jbeam_mesh(context)
        if obj is None:
            self.report({"WARNING"}, active_object_debug_label(context))
            return {"CANCELLED"}
        vertices = selected_experimental_jbeam_vertex_indices(obj)
        edges = selected_experimental_jbeam_edge_indices(obj)
        faces = selected_experimental_jbeam_face_indices(obj)
        snapshot = semantic_topology_snapshot_for_object(obj, context.scene, allow_write=False)
        edge_counts = defaultdict(int)
        for item in snapshot.get("edges", []) if isinstance(snapshot, dict) else []:
            edge_counts[str(item.get("semantic_type", JBEAM_EDGE_SEMANTIC_RELATIONSHIP))] += 1
        self.report(
            {"INFO"},
            (
                f"{obj.name} mode={obj.mode}; "
                f"selected vertices={len(vertices)}, edges={len(edges)}, faces={len(faces)}; "
                f"edge semantics beam={edge_counts[JBEAM_EDGE_SEMANTIC_BEAM]}, "
                f"boundary={edge_counts[JBEAM_EDGE_SEMANTIC_TRIANGLE_BOUNDARY]}, "
                f"relationship={edge_counts[JBEAM_EDGE_SEMANTIC_RELATIONSHIP]}"
            ),
        )
        return {"FINISHED"}


class BEAMNG_OT_write_semantic_topology_snapshot(Operator):
    bl_idname = "beamng_pc_importer.write_semantic_topology_snapshot"
    bl_label = "Write Semantic Topology Snapshot"
    bl_description = "Write the active experimental JBeam mesh UID and semantic topology graph to a Blender text block"
    bl_options = {"REGISTER"}

    def execute(self, context):
        obj = active_experimental_jbeam_mesh(context)
        if obj is None or obj.type != "MESH" or obj.get("beamng_visual_type") != "experimental_jbeam_mesh":
            self.report({"WARNING"}, "Select an experimental JBeam mesh first")
            return {"CANCELLED"}
        snapshot = semantic_topology_snapshot_for_object(obj, context.scene, allow_write=True)
        text = bpy.data.texts.get("BeamNG Semantic Topology Snapshot") or bpy.data.texts.new(
            "BeamNG Semantic Topology Snapshot"
        )
        text.clear()
        text.write(json.dumps(snapshot, indent=2, sort_keys=True))
        text.write("\n")
        self.report(
            {"INFO"},
            (
                f"Semantic topology snapshot: {len(snapshot.get('vertices', []))} node(s), "
                f"{len(snapshot.get('edges', []))} edge(s), {len(snapshot.get('faces', []))} face(s), "
                f"revision {snapshot.get('topology_revision', 0)}, "
                f"delta {snapshot.get('delta', {}).get('change_count', 0)}"
            ),
        )
        return {"FINISHED"}


class BEAMNG_OT_repair_experimental_jbeam_topology_uids(Operator):
    bl_idname = "beamng_pc_importer.repair_experimental_jbeam_topology_uids"
    bl_label = "Repair JBeam Topology UIDs"
    bl_description = "Populate persistent Blender topology UID attributes and UID-keyed JBeam metadata maps for the active experimental mesh"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = active_experimental_jbeam_mesh(context)
        if obj is None or obj.type != "MESH" or obj.get("beamng_visual_type") != "experimental_jbeam_mesh":
            self.report({"WARNING"}, "Select an experimental JBeam mesh first")
            return {"CANCELLED"}

        identity = ensure_experimental_mesh_identity(obj, context.scene, allow_write=True)
        topology_uids = ensure_experimental_topology_uids(obj, allow_write=True)
        mesh = obj.data
        topology_params_for_current_elements(
            mesh,
            topology_uids.get("edges", []),
            "beamng_edge_params_json",
            "beamng_edge_uid_to_params_json",
            allow_write=True,
        )
        topology_params_for_current_elements(
            mesh,
            topology_uids.get("faces", []),
            "beamng_face_params_json",
            "beamng_face_uid_to_params_json",
            allow_write=True,
        )
        semantic_snapshot = semantic_topology_snapshot_for_object(obj, context.scene, allow_write=True)

        def health(values):
            nonzero = [topology_uid_key(uid) for uid in values if topology_uid_key(uid)]
            return len(nonzero), len(set(nonzero))

        node_count, node_unique = health(topology_uids.get("nodes", []))
        edge_count, edge_unique = health(topology_uids.get("edges", []))
        face_count, face_unique = health(topology_uids.get("faces", []))
        node_map_count = len(mesh_json_dict(mesh, "beamng_node_uid_to_id_json"))
        topology_revision = int(semantic_snapshot.get("topology_revision", mesh.get("beamng_topology_revision", 0)) or 0)
        self.report(
            {"INFO"},
            (
                f"UIDs repaired for {obj.name}: "
                f"nodes {node_unique}/{node_count} unique, "
                f"edges {edge_unique}/{edge_count} unique, "
                f"faces {face_unique}/{face_count} unique, "
                f"revision {topology_revision}, "
                f"node map entries {node_map_count}, "
                f"JBeam nodes {len(identity.get('node_ids', []))}"
            ),
        )
        return {"FINISHED"}


class BEAMNG_OT_repair_experimental_jbeamzzz(Operator):
    bl_idname = "beamng_pc_importer.repair_experimental_jbeamzzz"
    bl_label = "Repair Semantic Topology"
    bl_description = "Rebuild semantic topology snapshots and remove stale UID-keyed metadata for experimental JBeam meshes"
    bl_options = {"REGISTER", "UNDO"}

    active_only: BoolProperty(name="Active Only", default=False)

    def execute(self, context):
        result = repair_experimental_jbeam_semantic_topology(context.scene, active_only=self.active_only)
        context.scene["beamng_jbeam_last_semantic_repair_message"] = (
            f"Semantic repair: {result['repaired_mesh_count']} mesh(es), "
            f"pruned {result['pruned_entry_count']} stale UID map entrie(s)"
        )
        self.report({"INFO"}, context.scene["beamng_jbeam_last_semantic_repair_message"])
        return {"FINISHED"}


class BEAMNG_OT_check_experimental_jbeam_topology_health(Operator):
    bl_idname = "beamng_pc_importer.check_experimental_jbeam_topology_health"
    bl_label = "Check JBeam Topology Health"
    bl_description = "Summarize experimental JBeam mesh identity, proxy, topology, and parameter health"
    bl_options = {"REGISTER"}

    def execute(self, context):
        summary = experimental_jbeam_topology_health(context.scene)
        store_experimental_jbeam_topology_health(context.scene, summary)
        text = bpy.data.texts.get("BeamNG JBeam Topology Health") or bpy.data.texts.new(
            "BeamNG JBeam Topology Health"
        )
        text.clear()
        text.write(json.dumps(summary, indent=2))
        text.write("\n")
        level = {"ERROR"} if summary.get("errors") else {"WARNING"} if summary.get("warnings") else {"INFO"}
        self.report(
            level,
            (
                f"JBeam health: {summary['mesh_count']} mesh(es), "
                f"{summary['proxy_drift_count']} proxy drift, "
                f"{summary['missing_reference_count']} missing ref, "
                f"{summary['dirty_param_count']} dirty param"
            ),
        )
        return {"FINISHED"}


class BEAMNG_OT_create_jbeam_part_file(Operator):
    bl_idname = "beamng_pc_importer.create_jbeam_part_file"
    bl_label = "Create JBeam Part File"
    bl_description = "Stage a new starter JBeam file/part in Blender; export writes the file"
    bl_options = {"REGISTER", "UNDO"}

    vehicle_name: StringProperty(name="Vehicle", default="")
    file_name: StringProperty(name="JBeam File", default="")
    part_name: StringProperty(name="Part Name", default="")
    display_name: StringProperty(name="Display Name", default="")
    slot_type: StringProperty(name="slotType", default="")
    child_slot_type: StringProperty(name="Child Slot Type", default="")
    child_default: StringProperty(name="Child Default", default="")
    child_description: StringProperty(name="Child Description", default="")

    def invoke(self, context, _event):
        vehicle = jbeam_authoring_vehicle_name(context)
        self.vehicle_name = vehicle
        if not self.part_name:
            self.part_name = f"{vehicle}_custom_part"
        if not self.file_name:
            self.file_name = f"{self.part_name}.jbeam"
        if not self.slot_type:
            self.slot_type = self.part_name
        return context.window_manager.invoke_props_dialog(self, width=520)

    def draw(self, _context):
        layout = self.layout
        layout.label(text=f"Target mod: {jbeam_export_mod_name(_context)}")
        layout.prop(self, "vehicle_name")
        layout.prop(self, "file_name")
        layout.prop(self, "part_name")
        layout.prop(self, "display_name")
        layout.prop(self, "slot_type")
        box = layout.box()
        box.label(text="Optional child slot row")
        box.prop(self, "child_slot_type")
        box.prop(self, "child_default")
        box.prop(self, "child_description")

    def execute(self, context):
        current_folder = user_current_folder_from_preferences(context)
        if current_folder is None:
            self.report({"ERROR"}, "Set BeamNG user folder in add-on preferences first")
            return {"CANCELLED"}
        vehicle = safe_jbeam_identifier(self.vehicle_name, "vehicle")
        part_name = safe_jbeam_identifier(self.part_name, f"{vehicle}_custom_part")
        slot_type = safe_jbeam_identifier(self.slot_type, part_name)
        file_name = safe_mod_folder_name(self.file_name)
        if not file_name.lower().endswith(".jbeam"):
            file_name += ".jbeam"
        virtual_path = normalize_virtual_path(Path("vehicles") / vehicle / file_name)
        target_path, warning = safe_jbeam_mod_override_target_for_virtual_path(context, current_folder, virtual_path)
        if warning or target_path is None:
            self.report({"ERROR"}, warning or "Could not build target path")
            return {"CANCELLED"}
        payload = new_jbeam_part_payload(
            part_name,
            slot_type,
            self.display_name.strip(),
            safe_jbeam_identifier(self.child_slot_type, "") if self.child_slot_type.strip() else "",
            safe_jbeam_identifier(self.child_default, "") if self.child_default.strip() else "",
            self.child_description.strip(),
        )
        obj = create_empty_experimental_jbeam_mesh_object(context, part_name, virtual_path)
        set_new_jbeam_payload_for_object(obj, payload)
        obj["beamng_planned_target_path"] = str(target_path)
        context.scene["beamng_last_created_jbeam_part_path"] = str(target_path)
        context.scene["beamng_last_created_jbeam_part_name"] = part_name
        self.report({"INFO"}, f"Staged new JBeam {part_name}; file writes only on export")
        return {"FINISHED"}


class BEAMNG_OT_write_active_jbeam_slot_metadata(Operator):
    bl_idname = "beamng_pc_importer.write_active_jbeam_slot_metadata"
    bl_label = "Write Active Part Slot Metadata"
    bl_description = "Stage slotType and optional child slot metadata; export writes the file"
    bl_options = {"REGISTER", "UNDO"}

    slot_type: StringProperty(name="slotType", default="")
    child_slot_type: StringProperty(name="Child Slot Type", default="")
    child_default: StringProperty(name="Child Default", default="")
    child_description: StringProperty(name="Child Description", default="")

    def invoke(self, context, _event):
        obj = active_experimental_jbeam_mesh(context)
        part_name = str(obj.get("beamng_part_name", "")) if obj else ""
        self.slot_type = part_name
        return context.window_manager.invoke_props_dialog(self, width=480)

    def draw(self, _context):
        layout = self.layout
        obj = active_experimental_jbeam_mesh(_context)
        layout.label(text=f"Part: {obj.get('beamng_part_name', '') if obj else '(none)'}")
        layout.label(text=f"Target mod: {jbeam_export_mod_name(_context)}")
        layout.prop(self, "slot_type")
        box = layout.box()
        box.label(text="Optional child slot row")
        box.prop(self, "child_slot_type")
        box.prop(self, "child_default")
        box.prop(self, "child_description")

    def execute(self, context):
        obj = active_experimental_jbeam_mesh(context)
        if obj is None:
            self.report({"ERROR"}, "Activate an experimental JBeam mesh first")
            return {"CANCELLED"}
        part_name = safe_jbeam_identifier(obj.get("beamng_part_name", ""), "")
        if not part_name:
            self.report({"ERROR"}, "Active JBeam mesh has no part name")
            return {"CANCELLED"}
        current_folder = user_current_folder_from_preferences(context)
        if current_folder is None:
            self.report({"ERROR"}, "Set BeamNG user folder in add-on preferences first")
            return {"CANCELLED"}
        virtual_path = normalize_virtual_path(obj.get("beamng_jbeam_path", ""))
        if not virtual_path:
            vehicle = jbeam_authoring_vehicle_name(context)
            virtual_path = normalize_virtual_path(Path("vehicles") / vehicle / f"{part_name}.jbeam")
        _target_path, warning = safe_jbeam_mod_override_target_for_virtual_path(context, current_folder, virtual_path)
        if warning or _target_path is None:
            self.report({"ERROR"}, warning or "Could not build target path")
            return {"CANCELLED"}

        payload = new_jbeam_payload_for_virtual_path(context.scene, virtual_path)
        if payload is None:
            payload = new_jbeam_part_payload(part_name, safe_jbeam_identifier(self.slot_type, part_name))
        part = payload.setdefault(part_name, {})
        if not isinstance(part, dict):
            self.report({"ERROR"}, f"Target part is not an object: {part_name}")
            return {"CANCELLED"}
        part["slotType"] = safe_jbeam_identifier(self.slot_type, part_name)
        child_slot = safe_jbeam_identifier(self.child_slot_type, "") if self.child_slot_type.strip() else ""
        if child_slot:
            slots = part.setdefault("slots", [["type", "default", "description"]])
            if not isinstance(slots, list):
                slots = [["type", "default", "description"]]
                part["slots"] = slots
            if not slots:
                slots.append(["type", "default", "description"])
            slots.append([
                child_slot,
                safe_jbeam_identifier(self.child_default, "") if self.child_default.strip() else "",
                self.child_description.strip() or child_slot,
            ])
        set_new_jbeam_payload_for_object(obj, payload)
        self.report({"INFO"}, "Staged slot metadata; file writes only on export")
        return {"FINISHED"}


class BEAMNG_OT_add_active_jbeam_child_slot(Operator):
    bl_idname = "beamng_pc_importer.add_active_jbeam_child_slot"
    bl_label = "Add Active Part Child Slot"
    bl_description = "Stage one child slot row on the active JBeam part; export writes the file"
    bl_options = {"REGISTER", "UNDO"}

    child_slot_type: StringProperty(name="Child Slot Type", default="")
    child_default: StringProperty(name="Child Default", default="")
    child_description: StringProperty(name="Child Description", default="")

    def invoke(self, context, _event):
        obj = active_experimental_jbeam_mesh(context)
        part_name = str(obj.get("beamng_part_name", "")) if obj else ""
        if not self.child_slot_type:
            self.child_slot_type = f"{part_name}_slot" if part_name else ""
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, _context):
        layout = self.layout
        obj = active_experimental_jbeam_mesh(_context)
        layout.label(text=f"Part: {obj.get('beamng_part_name', '') if obj else '(none)'}")
        layout.prop(self, "child_slot_type")
        layout.prop(self, "child_default")
        layout.prop(self, "child_description")

    def execute(self, context):
        obj = active_experimental_jbeam_mesh(context)
        if obj is None or obj.get("beamng_visual_type") != "experimental_jbeam_mesh":
            self.report({"ERROR"}, "Activate an experimental JBeam mesh first")
            return {"CANCELLED"}
        part_name = safe_jbeam_identifier(obj.get("beamng_part_name", ""), "")
        if not part_name:
            self.report({"ERROR"}, "Active JBeam mesh has no part name")
            return {"CANCELLED"}
        child_slot = safe_jbeam_identifier(self.child_slot_type, "")
        if not child_slot:
            self.report({"ERROR"}, "Enter a child slot type")
            return {"CANCELLED"}

        virtual_path = normalize_virtual_path(obj.get("beamng_jbeam_path", ""))
        if not virtual_path:
            vehicle = jbeam_authoring_vehicle_name(context)
            virtual_path = normalize_virtual_path(Path("vehicles") / vehicle / f"{part_name}.jbeam")
            obj["beamng_jbeam_path"] = virtual_path
        payload = new_jbeam_payload_for_virtual_path(context.scene, virtual_path)
        if payload is None:
            payload = new_jbeam_part_payload(part_name, part_name)
        part = payload.setdefault(part_name, {})
        if not isinstance(part, dict):
            self.report({"ERROR"}, f"Target part is not an object: {part_name}")
            return {"CANCELLED"}
        part.setdefault("slotType", part_name)
        slots = part.setdefault("slots", [["type", "default", "description"]])
        if not isinstance(slots, list):
            slots = [["type", "default", "description"]]
            part["slots"] = slots
        if not slots:
            slots.append(["type", "default", "description"])
        existing_types = {str(row[0]) for row in slots[1:] if isinstance(row, list) and row}
        if child_slot in existing_types:
            self.report({"WARNING"}, f"Child slot already exists: {child_slot}")
            return {"CANCELLED"}
        slots.append(
            [
                child_slot,
                safe_jbeam_identifier(self.child_default, "") if self.child_default.strip() else "",
                self.child_description.strip() or child_slot,
            ]
        )
        set_new_jbeam_payload_for_object(obj, payload)
        self.report({"INFO"}, f"Staged child slot: {child_slot}")
        return {"FINISHED"}


class BEAMNG_OT_add_standalone_jbeam_node(Operator):
    bl_idname = "beamng_pc_importer.add_standalone_jbeam_node"
    bl_label = "Add Standalone Node"
    bl_description = "Add one owned JBeam node at the 3D cursor without creating a beam edge"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = active_experimental_jbeam_mesh(context)
        if obj is None or obj.type != "MESH" or obj.get("beamng_visual_type") != "experimental_jbeam_mesh":
            self.report({"WARNING"}, "Select an experimental JBeam mesh first")
            return {"CANCELLED"}
        if not require_active_part_for_topology_edit(self, context, obj):
            return {"CANCELLED"}
        if obj.mode != "EDIT":
            self.report({"WARNING"}, "Enter Edit Mode first")
            return {"CANCELLED"}
        import bmesh

        mesh = obj.data
        edit_mesh = bmesh.from_edit_mesh(mesh)
        edit_mesh.verts.ensure_lookup_table()
        local_position = obj.matrix_world.inverted() @ context.scene.cursor.location
        for vertex in edit_mesh.verts:
            vertex.select = False
        vertex = edit_mesh.verts.new(local_position)
        edit_mesh.verts.ensure_lookup_table()
        vertex.select = True
        bmesh.update_edit_mesh(mesh, loop_triangles=True, destructive=True)
        ensure_experimental_mesh_identity(obj, context.scene, allow_write=True)
        self.report({"INFO"}, "Added standalone JBeam node at 3D cursor")
        return {"FINISHED"}


class BEAMNG_OT_import_selected_nodes_as_proxies(Operator):
    bl_idname = "beamng_pc_importer.import_selected_nodes_as_proxies"
    bl_label = "Import Selected Nodes As Proxies"
    bl_description = "Add selected nodes from other JBeam objects as locked proxy/reference vertices in the active JBeam mesh"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        target_obj = active_experimental_jbeam_mesh(context)
        if target_obj is None or target_obj.type != "MESH" or target_obj.get("beamng_visual_type") != "experimental_jbeam_mesh":
            self.report({"WARNING"}, "Activate target experimental JBeam mesh first")
            return {"CANCELLED"}
        if not require_active_part_for_topology_edit(self, context, target_obj):
            return {"CANCELLED"}
        sources = selected_proxy_node_sources(context, target_obj)
        if not sources:
            self.report({"WARNING"}, "Select source JBeam node object(s), then make the target JBeam mesh active")
            return {"CANCELLED"}
        result = add_proxy_nodes_to_experimental_mesh(context, target_obj, sources)
        added = int(result.get("added", 0))
        skipped = int(result.get("skipped", 0))
        if added == 0:
            self.report({"WARNING"}, f"No proxies imported; skipped {skipped} existing node(s)")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Imported {added} proxy node(s), skipped {skipped}; select proxy + owned node, then Beam 2")
        return {"FINISHED"}


class BEAMNG_OT_mark_selected_nodes_for_proxy_import(Operator):
    bl_idname = "beamng_pc_importer.mark_selected_nodes_for_proxy_import"
    bl_label = "Mark Selected Nodes"
    bl_description = "Store selected JBeam nodes in a temporary proxy-import clipboard"
    bl_options = {"REGISTER", "UNDO"}

    append: BoolProperty(name="Append", default=False)

    def execute(self, context):
        selected_sources = selected_nodes_for_proxy_clipboard(context)
        if not selected_sources:
            self.report({"WARNING"}, "Select one or more JBeam nodes or JBeam mesh vertices to mark")
            return {"CANCELLED"}
        nodes_by_id = {}
        if self.append:
            for node in proxy_clipboard_nodes(context.scene):
                node_id = str(node.get("node_id", "") or "")
                if node_id:
                    nodes_by_id[node_id] = node
        for node in selected_sources:
            nodes_by_id[str(node["node_id"])] = node
        nodes = list(nodes_by_id.values())
        store_proxy_clipboard_nodes(context.scene, nodes)
        preview = ", ".join(str(node.get("node_id", "")) for node in nodes[:5])
        if len(nodes) > 5:
            preview += f", +{len(nodes) - 5} more"
        self.report({"INFO"}, f"Marked {len(nodes)} node(s) for proxy import: {preview}")
        return {"FINISHED"}


class BEAMNG_OT_import_marked_nodes_as_proxies(Operator):
    bl_idname = "beamng_pc_importer.import_marked_nodes_as_proxies"
    bl_label = "Import Marked Nodes"
    bl_description = "Create proxy/reference vertices in the active JBeam mesh from the marked-node clipboard"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        target_obj = active_experimental_jbeam_mesh(context)
        if target_obj is None or target_obj.type != "MESH" or target_obj.get("beamng_visual_type") != "experimental_jbeam_mesh":
            self.report({"WARNING"}, "Activate target experimental JBeam mesh first")
            return {"CANCELLED"}
        if not require_active_part_for_topology_edit(self, context, target_obj):
            return {"CANCELLED"}
        nodes = proxy_clipboard_nodes(context.scene)
        if not nodes:
            self.report({"WARNING"}, "No marked proxy nodes. Select source nodes, then click Mark Selected Nodes first")
            return {"CANCELLED"}
        sources = []
        for node in nodes:
            try:
                position = Vector(node.get("world_position", []))
            except Exception:
                continue
            if len(position) != 3:
                continue
            sources.append(
                {
                    "node_id": str(node.get("node_id", "")),
                    "world_position": position,
                    "owner_part_id": int(node.get("owner_part_id", -1) or -1),
                    "source_object": str(node.get("source_object", "")),
                }
            )
        result = add_proxy_nodes_to_experimental_mesh(context, target_obj, sources)
        added = int(result.get("added", 0))
        skipped = int(result.get("skipped", 0))
        if added == 0:
            self.report({"WARNING"}, f"No marked proxies imported; skipped {skipped} existing node(s)")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Imported {added} marked proxy node(s), skipped {skipped}")
        return {"FINISHED"}


class BEAMNG_OT_create_crossbeam_to_marked_node(Operator):
    bl_idname = "beamng_pc_importer.create_crossbeam_to_marked_node"
    bl_label = "Crossbeam To Marked Node"
    bl_description = "Create/reuse a proxy from one marked external node and create a JBeam beam from the selected owned node"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        target_obj = active_experimental_jbeam_mesh(context)
        if target_obj is None or target_obj.type != "MESH" or target_obj.get("beamng_visual_type") != "experimental_jbeam_mesh":
            self.report({"WARNING"}, "Activate target JBeam mesh first")
            return {"CANCELLED"}
        if not require_active_part_for_topology_edit(self, context, target_obj):
            return {"CANCELLED"}
        selected_indices = selected_experimental_jbeam_vertex_indices(target_obj)
        if len(selected_indices) != 1:
            self.report({"WARNING"}, "Select exactly one owned node in the Active Part")
            return {"CANCELLED"}
        identity = ensure_experimental_mesh_identity(target_obj, context.scene, allow_write=True)
        node_kinds = identity.get("node_kinds", [])
        local_index = selected_indices[0]
        if local_index >= len(node_kinds) or str(node_kinds[local_index]) != "owned":
            self.report({"WARNING"}, "Selected Active Part node must be owned, not proxy")
            return {"CANCELLED"}
        marked = proxy_clipboard_nodes(context.scene)
        if len(marked) != 1:
            self.report({"WARNING"}, "Mark exactly one external node first")
            return {"CANCELLED"}
        external = marked[0]
        external_node_id = str(external.get("node_id", "") or "")
        if not external_node_id:
            self.report({"WARNING"}, "Marked node has no node id")
            return {"CANCELLED"}
        owner_part_id = int(external.get("owner_part_id", -1) or -1)
        active_part_id = int(target_obj.get("beamng_resolved_part_id", -1) or -1)
        if owner_part_id == active_part_id:
            self.report({"WARNING"}, "Marked node belongs to the Active Part; use Beam 2 for local beams")
            return {"CANCELLED"}
        try:
            position = Vector(external.get("world_position", []))
        except Exception:
            position = Vector()
        if len(position) != 3:
            self.report({"WARNING"}, "Marked node has no valid world position")
            return {"CANCELLED"}
        proxy_result = add_proxy_nodes_to_experimental_mesh(
            context,
            target_obj,
            [{
                "node_id": external_node_id,
                "world_position": position,
                "owner_part_id": owner_part_id,
                "source_object": str(external.get("source_object", "")),
            }],
        )
        proxy_index = experimental_node_index_by_id(target_obj, external_node_id, {"proxy"})
        if proxy_index < 0:
            if int(proxy_result.get("added", 0) or 0):
                remove_proxy_vertices_by_node_ids(target_obj, context.scene, {external_node_id})
            self.report({"ERROR"}, "Could not create or find proxy node")
            return {"CANCELLED"}
        result = create_or_mark_jbeam_beam_between_indices(target_obj, context.scene, local_index, proxy_index)
        key = result.get("key", ())
        if not key:
            if int(proxy_result.get("added", 0) or 0):
                remove_proxy_vertices_by_node_ids(target_obj, context.scene, {external_node_id})
            self.report({"ERROR"}, "Could not create crossbeam; rolled back new proxy")
            return {"CANCELLED"}
        semantic_topology_snapshot_for_object(target_obj, context.scene, allow_write=True)
        self.report({"INFO"}, f"Created crossbeam: {key[0]} -> {key[1]}" if key else "Created crossbeam")
        return {"FINISHED"}


class BEAMNG_OT_clear_marked_proxy_nodes(Operator):
    bl_idname = "beamng_pc_importer.clear_marked_proxy_nodes"
    bl_label = "Clear Marked Nodes"
    bl_description = "Clear the temporary marked-node proxy-import clipboard"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        count = len(proxy_clipboard_nodes(context.scene))
        store_proxy_clipboard_nodes(context.scene, [])
        self.report({"INFO"}, f"Cleared {count} marked proxy node(s)")
        return {"FINISHED"}


class BEAMNG_OT_clear_unused_proxy_nodes(Operator):
    bl_idname = "beamng_pc_importer.clear_unused_proxy_nodes"
    bl_label = "Clear Unused Proxies"
    bl_description = "Remove proxy/reference vertices from the active JBeam mesh when they are not used by beams or triangles"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = active_experimental_jbeam_mesh(context)
        if obj is None or obj.type != "MESH" or obj.get("beamng_visual_type") != "experimental_jbeam_mesh":
            self.report({"WARNING"}, "Activate an experimental JBeam mesh first")
            return {"CANCELLED"}
        if not require_active_part_for_topology_edit(self, context, obj):
            return {"CANCELLED"}
        identity = ensure_experimental_mesh_identity(obj, context.scene, allow_write=True)
        node_kinds = identity.get("node_kinds", [])
        if not node_kinds:
            self.report({"WARNING"}, "No proxy nodes found")
            return {"CANCELLED"}
        import bmesh

        mesh = obj.data
        if obj.mode == "EDIT":
            edit_mesh = bmesh.from_edit_mesh(mesh)
            edit_mesh.verts.ensure_lookup_table()
            unused = [
                vertex
                for vertex in edit_mesh.verts
                if vertex.index < len(node_kinds)
                and str(node_kinds[vertex.index]) == "proxy"
                and not vertex.link_edges
                and not vertex.link_faces
            ]
            if not unused:
                self.report({"INFO"}, "No unused proxy nodes to clear")
                return {"FINISHED"}
            bmesh.ops.delete(edit_mesh, geom=unused, context="VERTS")
            bmesh.update_edit_mesh(mesh, loop_triangles=True, destructive=True)
        else:
            edit_mesh = bmesh.new()
            edit_mesh.from_mesh(mesh)
            edit_mesh.verts.ensure_lookup_table()
            unused = [
                vertex
                for vertex in edit_mesh.verts
                if vertex.index < len(node_kinds)
                and str(node_kinds[vertex.index]) == "proxy"
                and not vertex.link_edges
                and not vertex.link_faces
            ]
            if not unused:
                edit_mesh.free()
                self.report({"INFO"}, "No unused proxy nodes to clear")
                return {"FINISHED"}
            bmesh.ops.delete(edit_mesh, geom=unused, context="VERTS")
            edit_mesh.to_mesh(mesh)
            edit_mesh.free()
            mesh.update()
        ensure_experimental_mesh_identity(obj, context.scene, allow_write=True)
        self.report({"INFO"}, f"Cleared {len(unused)} unused proxy node(s)")
        return {"FINISHED"}


class BEAMNG_OT_clear_orphan_provisional_nodes(Operator):
    bl_idname = "beamng_pc_importer.clear_orphan_provisional_nodes"
    bl_label = "Clear Orphan Provisional Nodes"
    bl_description = "Remove new uncommitted owned vertices that are not referenced by any edge or face"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = active_experimental_jbeam_mesh(context)
        if obj is None or obj.type != "MESH" or obj.get("beamng_visual_type") != "experimental_jbeam_mesh":
            self.report({"WARNING"}, "Activate an experimental JBeam mesh first")
            return {"CANCELLED"}
        if not require_active_part_for_topology_edit(self, context, obj):
            return {"CANCELLED"}
        indices = set(orphan_provisional_node_indices(obj))
        if not indices:
            self.report({"INFO"}, "No orphan provisional nodes to clear")
            return {"FINISHED"}
        import bmesh

        mesh = obj.data
        if obj.mode == "EDIT":
            edit_mesh = bmesh.from_edit_mesh(mesh)
            edit_mesh.verts.ensure_lookup_table()
            edit_mesh.verts.index_update()
            verts = [vertex for vertex in edit_mesh.verts if vertex.index in indices]
            bmesh.ops.delete(edit_mesh, geom=verts, context="VERTS")
            bmesh.update_edit_mesh(mesh, loop_triangles=True, destructive=True)
        else:
            edit_mesh = bmesh.new()
            edit_mesh.from_mesh(mesh)
            edit_mesh.verts.ensure_lookup_table()
            edit_mesh.verts.index_update()
            verts = [vertex for vertex in edit_mesh.verts if vertex.index in indices]
            bmesh.ops.delete(edit_mesh, geom=verts, context="VERTS")
            edit_mesh.to_mesh(mesh)
            edit_mesh.free()
            mesh.update()
        ensure_experimental_mesh_identity(obj, context.scene, allow_write=True)
        semantic_topology_snapshot_for_object(obj, context.scene, allow_write=True)
        self.report({"INFO"}, f"Cleared {len(indices)} orphan provisional node(s)")
        return {"FINISHED"}


class BEAMNG_OT_create_jbeam_beam_from_selected_nodes(Operator):
    bl_idname = "beamng_pc_importer.create_jbeam_beam_from_selected_nodes"
    bl_label = "Create Beam From 2 Nodes"
    bl_description = "Create/mark a JBeam beam between exactly two selected mesh vertices"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = active_experimental_jbeam_mesh(context)
        if obj is None or obj.type != "MESH" or obj.get("beamng_visual_type") != "experimental_jbeam_mesh":
            self.report({"WARNING"}, "Select an experimental JBeam mesh first")
            return {"CANCELLED"}
        if not require_active_part_for_topology_edit(self, context, obj):
            return {"CANCELLED"}
        if obj.mode != "EDIT":
            self.report({"WARNING"}, "Enter Edit Mode first")
            return {"CANCELLED"}
        selected_indices = selected_experimental_jbeam_vertex_indices(obj)
        if len(selected_indices) != 2:
            self.report({"WARNING"}, "Select exactly 2 vertices")
            return {"CANCELLED"}
        identity = ensure_experimental_mesh_identity(obj, context.scene, allow_write=True)
        node_ids = identity.get("node_ids", [])
        if any(index >= len(node_ids) for index in selected_indices):
            self.report({"ERROR"}, "Selected vertex has no JBeam node identity")
            return {"CANCELLED"}
        import bmesh

        mesh = obj.data
        edit_mesh = bmesh.from_edit_mesh(mesh)
        edit_mesh.verts.ensure_lookup_table()
        edit_mesh.edges.ensure_lookup_table()
        verts = [edit_mesh.verts[index] for index in selected_indices]
        try:
            edit_mesh.edges.new(verts)
        except ValueError:
            pass
        bmesh.update_edit_mesh(mesh, loop_triangles=True, destructive=True)
        key = edge_key((node_ids[selected_indices[0]], node_ids[selected_indices[1]]))
        existing = {
            tuple(str(item) for item in item_key)
            for item_key in mesh_json_list(mesh, "beamng_explicit_beam_edge_keys_json")
            if isinstance(item_key, (list, tuple)) and len(item_key) >= 2
        }
        existing.add(key)
        mesh["beamng_explicit_beam_edge_keys_json"] = json.dumps([list(item_key) for item_key in sorted(existing)])
        ensure_experimental_mesh_identity(obj, context.scene, allow_write=True)
        self.report({"INFO"}, f"Created/marked beam: {key[0]} -> {key[1]}")
        return {"FINISHED"}


class BEAMNG_OT_create_jbeam_triangle_from_selected_nodes(Operator):
    bl_idname = "beamng_pc_importer.create_jbeam_triangle_from_selected_nodes"
    bl_label = "Create Triangle From 3 Nodes"
    bl_description = "Create one JBeam collision triangle from exactly three selected mesh vertices"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = active_experimental_jbeam_mesh(context)
        if obj is None or obj.type != "MESH" or obj.get("beamng_visual_type") != "experimental_jbeam_mesh":
            self.report({"WARNING"}, "Select an experimental JBeam mesh first")
            return {"CANCELLED"}
        if not require_active_part_for_topology_edit(self, context, obj):
            return {"CANCELLED"}
        if obj.mode != "EDIT":
            self.report({"WARNING"}, "Enter Edit Mode first")
            return {"CANCELLED"}
        selected_indices = selected_experimental_jbeam_vertex_indices(obj)
        if len(selected_indices) != 3:
            self.report({"WARNING"}, "Select exactly 3 vertices")
            return {"CANCELLED"}
        ensure_experimental_mesh_identity(obj, context.scene, allow_write=True)
        import bmesh

        mesh = obj.data
        edit_mesh = bmesh.from_edit_mesh(mesh)
        edit_mesh.verts.ensure_lookup_table()
        verts = [edit_mesh.verts[index] for index in selected_indices]
        try:
            face = edit_mesh.faces.new(verts)
        except ValueError:
            self.report({"WARNING"}, "Triangle face already exists")
            return {"CANCELLED"}
        for item in edit_mesh.faces:
            item.select = False
        face.select = True
        bmesh.update_edit_mesh(mesh, loop_triangles=True, destructive=True)
        ensure_experimental_mesh_identity(obj, context.scene, allow_write=True)
        self.report({"INFO"}, "Created JBeam collision triangle from selected nodes")
        return {"FINISHED"}


class BEAMNG_OT_delete_selected_jbeam_elements(Operator):
    bl_idname = "beamng_pc_importer.delete_selected_jbeam_elements"
    bl_label = "Delete Selected JBeam Elements"
    bl_description = "Delete selected JBeam faces/edges first; delete nodes only when no beam or triangle is selected"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = active_experimental_jbeam_mesh(context)
        if obj is None or obj.type != "MESH" or obj.get("beamng_visual_type") != "experimental_jbeam_mesh":
            self.report({"WARNING"}, "Select an experimental JBeam mesh first")
            return {"CANCELLED"}
        if not require_active_part_for_topology_edit(self, context, obj):
            return {"CANCELLED"}
        if obj.mode != "EDIT":
            self.report({"WARNING"}, "Enter Edit Mode first")
            return {"CANCELLED"}
        import bmesh

        mesh = obj.data
        edit_mesh = bmesh.from_edit_mesh(mesh)
        edit_mesh.verts.ensure_lookup_table()
        edit_mesh.edges.ensure_lookup_table()
        edit_mesh.faces.ensure_lookup_table()
        verts = [vertex for vertex in edit_mesh.verts if vertex.select]
        edges = [edge for edge in edit_mesh.edges if edge.select]
        faces = [face for face in edit_mesh.faces if face.select]

        # Blender edge/face selection commonly leaves endpoint vertices selected too.
        # Prefer deleting JBeam relationships before treating selected vertices as node deletion.
        if faces:
            bmesh.ops.delete(edit_mesh, geom=faces, context="FACES_ONLY")
            deleted = f"{len(faces)} triangle face(s)"
        elif edges:
            removable_edges = [edge for edge in edges if edge.is_valid and not edge.link_faces]
            blocked_edges = len(edges) - len(removable_edges)
            if not removable_edges:
                self.report({"WARNING"}, "Selected beam edge is also a triangle boundary; select the triangle face to delete it")
                return {"CANCELLED"}
            for edge in removable_edges:
                edit_mesh.edges.remove(edge)
            deleted = f"{len(removable_edges)} beam edge(s)"
            if blocked_edges:
                self.report({"WARNING"}, f"Deleted {deleted}; skipped {blocked_edges} triangle boundary edge(s)")
        elif verts:
            bmesh.ops.delete(edit_mesh, geom=verts, context="VERTS")
            deleted = f"{len(verts)} node(s)"
        else:
            self.report({"WARNING"}, "No JBeam mesh elements selected")
            return {"CANCELLED"}
        bmesh.update_edit_mesh(mesh, loop_triangles=True, destructive=True)
        ensure_experimental_mesh_identity(obj, context.scene, allow_write=True)
        self.report({"INFO"}, f"Deleted {deleted}")
        return {"FINISHED"}


class BEAMNG_OT_triangulate_selected_jbeam_faces(Operator):
    bl_idname = "beamng_pc_importer.triangulate_selected_jbeam_faces"
    bl_label = "Triangulate Selected Faces"
    bl_description = "Triangulate selected experimental JBeam mesh faces so they can export as JBeam collision triangles"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = active_experimental_jbeam_mesh(context)
        if obj is None or obj.type != "MESH" or obj.get("beamng_visual_type") != "experimental_jbeam_mesh":
            self.report({"WARNING"}, "Select an experimental JBeam mesh first")
            return {"CANCELLED"}
        if obj.mode != "EDIT":
            self.report({"WARNING"}, "Enter Edit Mode and select faces to triangulate")
            return {"CANCELLED"}
        import bmesh

        mesh = obj.data
        edit_mesh = bmesh.from_edit_mesh(mesh)
        faces = [face for face in edit_mesh.faces if face.select and len(face.verts) > 3]
        if not faces:
            self.report({"WARNING"}, "No selected non-triangle faces found")
            return {"CANCELLED"}
        bmesh.ops.triangulate(edit_mesh, faces=faces)
        bmesh.update_edit_mesh(mesh, loop_triangles=True, destructive=True)
        ensure_experimental_mesh_identity(obj, context.scene, allow_write=True)
        self.report({"INFO"}, f"Triangulated {len(faces)} selected face(s)")
        return {"FINISHED"}


class BEAMNG_OT_flip_selected_jbeam_triangles(Operator):
    bl_idname = "beamng_pc_importer.flip_selected_jbeam_triangles"
    bl_label = "Flip Selected Triangle Winding"
    bl_description = "Reverse selected experimental JBeam triangle winding"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = active_experimental_jbeam_mesh(context)
        if obj is None or obj.type != "MESH" or obj.get("beamng_visual_type") != "experimental_jbeam_mesh":
            self.report({"WARNING"}, "Select an experimental JBeam mesh first")
            return {"CANCELLED"}
        if obj.mode != "EDIT":
            self.report({"WARNING"}, "Enter Edit Mode and select triangle faces to flip")
            return {"CANCELLED"}
        import bmesh

        mesh = obj.data
        edit_mesh = bmesh.from_edit_mesh(mesh)
        faces = [face for face in edit_mesh.faces if face.select and len(face.verts) == 3]
        if not faces:
            self.report({"WARNING"}, "No selected triangle faces found")
            return {"CANCELLED"}
        bmesh.ops.reverse_faces(edit_mesh, faces=faces)
        bmesh.update_edit_mesh(mesh, loop_triangles=True, destructive=False)
        self.report({"INFO"}, f"Flipped winding for {len(faces)} triangle(s)")
        return {"FINISHED"}


class BEAMNG_OT_apply_selected_jbeam_node_properties(Operator):
    bl_idname = "beamng_pc_importer.apply_selected_jbeam_node_properties"
    bl_label = "Apply Selected Node Properties"
    bl_description = "Store the panel's JBeam node option overrides on the selected experimental mesh node"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = active_experimental_jbeam_mesh(context)
        if obj is None or obj.type != "MESH" or obj.get("beamng_visual_type") != "experimental_jbeam_mesh":
            self.report({"WARNING"}, "Select an experimental JBeam mesh node first")
            return {"CANCELLED"}
        identity = ensure_experimental_mesh_identity(obj, context.scene)
        selected_indices = selected_experimental_jbeam_vertex_indices(obj)
        if not selected_indices:
            self.report({"WARNING"}, "No selected JBeam mesh vertices found")
            return {"CANCELLED"}
        node_params = list(identity.get("node_params", []))
        node_uids = ensure_experimental_topology_uids(obj, allow_write=True).get("nodes", [])
        uid_to_params = mesh_json_dict(obj.data, "beamng_node_uid_to_params_json")
        params = selected_node_params_from_scene(context.scene)
        for index in selected_indices:
            while index >= len(node_params):
                node_params.append({})
            node_params[index] = dict(params)
            uid_key = topology_uid_key(node_uids[index]) if index < len(node_uids) else ""
            if uid_key:
                uid_to_params[uid_key] = dict(params)
        obj.data["beamng_node_params_json"] = json.dumps(node_params)
        obj.data["beamng_node_uid_to_params_json"] = json.dumps(uid_to_params)
        self.report({"INFO"}, f"Applied JBeam node properties to {len(selected_indices)} selected node(s)")
        return {"FINISHED"}


class BEAMNG_OT_load_selected_jbeam_node_properties(Operator):
    bl_idname = "beamng_pc_importer.load_selected_jbeam_node_properties"
    bl_label = "Load Selected Node Properties"
    bl_description = "Copy the first selected experimental mesh node's stored JBeam options into the panel fields"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = active_experimental_jbeam_mesh(context)
        if obj is None or obj.type != "MESH" or obj.get("beamng_visual_type") != "experimental_jbeam_mesh":
            self.report({"WARNING"}, "Select an experimental JBeam mesh node first")
            return {"CANCELLED"}
        identity = ensure_experimental_mesh_identity(obj, context.scene)
        selected_indices = selected_experimental_jbeam_vertex_indices(obj)
        if not selected_indices:
            self.report({"WARNING"}, "No selected JBeam mesh vertices found")
            return {"CANCELLED"}
        index = selected_indices[0]
        node_params = identity.get("node_params", [])
        selected_info = experimental_jbeam_node_info_for_selection(context, limit=1)
        params = selected_info[0].get("params", {}) if selected_info else {}
        if not params:
            params = node_params[index] if index < len(node_params) and isinstance(node_params[index], dict) else {}
        context.scene.beamng_jbeam_node_weight = str(params.get("nodeWeight", ""))
        context.scene.beamng_jbeam_node_material = str(params.get("nodeMaterial", ""))
        context.scene.beamng_jbeam_node_group = str(params.get("group", ""))
        context.scene.beamng_jbeam_node_friction = str(params.get("frictionCoef", ""))
        context.scene.beamng_jbeam_node_collision_override = "collision" in params
        context.scene.beamng_jbeam_node_collision = bool(params.get("collision", True))
        context.scene.beamng_jbeam_node_self_collision_override = "selfCollision" in params
        context.scene.beamng_jbeam_node_self_collision = bool(params.get("selfCollision", False))
        self.report({"INFO"}, "Loaded selected node properties into the panel")
        return {"FINISHED"}


class BEAMNG_OT_apply_selected_jbeam_beam_properties(Operator):
    bl_idname = "beamng_pc_importer.apply_selected_jbeam_beam_properties"
    bl_label = "Apply Selected Beam Properties"
    bl_description = "Store the panel's JBeam beam option overrides on selected experimental mesh edges"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = active_experimental_jbeam_mesh(context)
        if obj is None:
            self.report({"WARNING"}, "Select an experimental JBeam mesh edge first")
            return {"CANCELLED"}
        selected_indices = selected_experimental_jbeam_edge_indices(obj)
        if not selected_indices:
            self.report({"WARNING"}, "No selected JBeam mesh edges found")
            return {"CANCELLED"}
        params = selected_beam_params_from_scene(context.scene)
        edge_uids = ensure_experimental_topology_uids(obj, allow_write=True).get("edges", [])
        edge_params = topology_params_for_current_elements(obj.data, edge_uids, "beamng_edge_params_json", "beamng_edge_uid_to_params_json", allow_write=True)
        uid_to_params = mesh_json_dict(obj.data, "beamng_edge_uid_to_params_json")
        for index in selected_indices:
            while index >= len(edge_params):
                edge_params.append({})
            edge_params[index] = dict(params)
            uid_key = topology_uid_key(edge_uids[index]) if index < len(edge_uids) else ""
            if uid_key:
                uid_to_params[uid_key] = dict(params)
        obj.data["beamng_edge_params_json"] = json.dumps(edge_params)
        obj.data["beamng_edge_uid_to_params_json"] = json.dumps(uid_to_params)
        self.report({"INFO"}, f"Applied beam properties to {len(selected_indices)} selected edge(s)")
        return {"FINISHED"}


class BEAMNG_OT_load_selected_jbeam_beam_properties(Operator):
    bl_idname = "beamng_pc_importer.load_selected_jbeam_beam_properties"
    bl_label = "Load Selected Beam Properties"
    bl_description = "Copy the first selected experimental mesh edge's stored JBeam options into the panel fields"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = active_experimental_jbeam_mesh(context)
        if obj is None:
            self.report({"WARNING"}, "Select an experimental JBeam mesh edge first")
            return {"CANCELLED"}
        selected_indices = selected_experimental_jbeam_edge_indices(obj)
        if not selected_indices:
            self.report({"WARNING"}, "No selected JBeam mesh edges found")
            return {"CANCELLED"}
        edge_uids = ensure_experimental_topology_uids(obj, allow_write=False).get("edges", [])
        edge_params = topology_params_for_current_elements(obj.data, edge_uids, "beamng_edge_params_json", "beamng_edge_uid_to_params_json", allow_write=False)
        index = selected_indices[0]
        selected_info = experimental_jbeam_edge_info_for_selection(context, limit=1)
        params = selected_info[0].get("params", {}) if selected_info else {}
        if not params:
            params = edge_params[index] if index < len(edge_params) and isinstance(edge_params[index], dict) else {}
        context.scene.beamng_jbeam_beam_spring = str(params.get("beamSpring", ""))
        context.scene.beamng_jbeam_beam_damp = str(params.get("beamDamp", ""))
        context.scene.beamng_jbeam_beam_deform = str(params.get("beamDeform", ""))
        context.scene.beamng_jbeam_beam_strength = str(params.get("beamStrength", ""))
        context.scene.beamng_jbeam_beam_precompression = str(params.get("beamPrecompression", ""))
        context.scene.beamng_jbeam_beam_type = str(params.get("beamType", ""))
        context.scene.beamng_jbeam_beam_break_group = str(params.get("breakGroup", ""))
        self.report({"INFO"}, "Loaded selected beam properties into the panel")
        return {"FINISHED"}


class BEAMNG_OT_apply_selected_jbeam_triangle_properties(Operator):
    bl_idname = "beamng_pc_importer.apply_selected_jbeam_triangle_properties"
    bl_label = "Apply Selected Triangle Properties"
    bl_description = "Store the panel's JBeam triangle option overrides on selected experimental mesh faces"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = active_experimental_jbeam_mesh(context)
        if obj is None:
            self.report({"WARNING"}, "Select an experimental JBeam mesh face first")
            return {"CANCELLED"}
        selected_indices = selected_experimental_jbeam_face_indices(obj)
        if not selected_indices:
            self.report({"WARNING"}, "No selected JBeam mesh faces found")
            return {"CANCELLED"}
        params = selected_triangle_params_from_scene(context.scene)
        face_uids = ensure_experimental_topology_uids(obj, allow_write=True).get("faces", [])
        face_params = topology_params_for_current_elements(obj.data, face_uids, "beamng_face_params_json", "beamng_face_uid_to_params_json", allow_write=True)
        uid_to_params = mesh_json_dict(obj.data, "beamng_face_uid_to_params_json")
        for index in selected_indices:
            while index >= len(face_params):
                face_params.append({})
            face_params[index] = dict(params)
            uid_key = topology_uid_key(face_uids[index]) if index < len(face_uids) else ""
            if uid_key:
                uid_to_params[uid_key] = dict(params)
        obj.data["beamng_face_params_json"] = json.dumps(face_params)
        obj.data["beamng_face_uid_to_params_json"] = json.dumps(uid_to_params)
        self.report({"INFO"}, f"Applied triangle properties to {len(selected_indices)} selected face(s)")
        return {"FINISHED"}


class BEAMNG_OT_load_selected_jbeam_triangle_properties(Operator):
    bl_idname = "beamng_pc_importer.load_selected_jbeam_triangle_properties"
    bl_label = "Load Selected Triangle Properties"
    bl_description = "Copy the first selected experimental mesh face's stored JBeam options into the panel fields"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = active_experimental_jbeam_mesh(context)
        if obj is None:
            self.report({"WARNING"}, "Select an experimental JBeam mesh face first")
            return {"CANCELLED"}
        selected_indices = selected_experimental_jbeam_face_indices(obj)
        if not selected_indices:
            self.report({"WARNING"}, "No selected JBeam mesh faces found")
            return {"CANCELLED"}
        face_uids = ensure_experimental_topology_uids(obj, allow_write=False).get("faces", [])
        face_params = topology_params_for_current_elements(obj.data, face_uids, "beamng_face_params_json", "beamng_face_uid_to_params_json", allow_write=False)
        index = selected_indices[0]
        selected_info = experimental_jbeam_face_info_for_selection(context, limit=1)
        params = selected_info[0].get("params", {}) if selected_info else {}
        if not params:
            params = face_params[index] if index < len(face_params) and isinstance(face_params[index], dict) else {}
        context.scene.beamng_jbeam_triangle_group = str(params.get("group", ""))
        context.scene.beamng_jbeam_triangle_drag_coef = str(params.get("dragCoef", ""))
        context.scene.beamng_jbeam_triangle_ground_model = str(params.get("groundModel", ""))
        context.scene.beamng_jbeam_triangle_collision_override = "collision" in params
        context.scene.beamng_jbeam_triangle_collision = bool(params.get("collision", True))
        self.report({"INFO"}, "Loaded selected triangle properties into the panel")
        return {"FINISHED"}


class BEAMNG_OT_clear_jbeam_edit_session(Operator):
    bl_idname = "beamng_pc_importer.clear_jbeam_edit_session"
    bl_label = "Clear Accepted JBeam Edits"
    bl_description = "Discard pending and accepted JBeam edit operations from this Blender session without changing meshes"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        previous_count = int(context.scene.get("beamng_jbeam_operation_history_count", 0))
        pending_count = int(context.scene.get("beamng_jbeam_pending_node_move_count", 0))
        reset_jbeam_edit_session(context.scene)
        self.report(
            {"INFO"},
            f"Cleared {previous_count} accepted and {pending_count} pending JBeam edit operation(s)",
        )
        return {"FINISHED"}


class BEAMNG_OT_create_jbeam_export_mod_folder(Operator):
    bl_idname = "beamng_pc_importer.create_jbeam_export_mod_folder"
    bl_label = "Create Export Mod Folder"
    bl_description = "Create the configured current/mods/unpacked/<mod>/vehicles folder if it does not exist"
    bl_options = {"REGISTER"}

    def execute(self, context):
        current_folder = user_current_folder_from_preferences(context)
        if current_folder is None:
            self.report({"ERROR"}, "Set BeamNG user folder in add-on preferences first")
            return {"CANCELLED"}
        mod_name = jbeam_export_mod_name(context)
        if not mod_name:
            self.report({"ERROR"}, "Set a JBeam export mod folder name first")
            return {"CANCELLED"}
        target = current_folder / "mods" / "unpacked" / mod_name / "vehicles"
        target.mkdir(parents=True, exist_ok=True)
        context.scene["beamng_jbeam_last_export_mod_folder_path"] = str(target)
        self.report({"INFO"}, f"Ready export mod folder: {target}")
        return {"FINISHED"}


class BEAMNG_OT_set_jbeam_export_mod_folder(Operator):
    bl_idname = "beamng_pc_importer.set_jbeam_export_mod_folder"
    bl_label = "Use Export Mod Folder"
    bl_description = "Use an existing unpacked mod folder as the JBeam/asset export target"
    bl_options = {"REGISTER"}

    mod_name: StringProperty(default="")

    def execute(self, context):
        prefs = get_addon_preferences(context)
        if prefs is None:
            self.report({"ERROR"}, "Could not access BeamNG PC Importer preferences")
            return {"CANCELLED"}
        safe_name = safe_mod_folder_name(self.mod_name)
        prefs.jbeam_export_mod_name = safe_name
        context.scene["beamng_jbeam_last_export_mod_folder_path"] = ""
        self.report({"INFO"}, f"JBeam export mod: {safe_name}")
        return {"FINISHED"}


def assembly_validation_report(context):
    scene = context.scene
    objects = experimental_jbeam_part_objects(scene)
    lines = [
        "[BeamNG Assembly Validation]",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"Assembly parts: {len(objects)}",
        f"Active part: {scene.get('beamng_active_jbeam_part_name', '') or '(none)'}",
        f"Export mod: {jbeam_export_mod_name(context)}",
        "",
    ]
    issues = []
    if not objects:
        issues.append("No imported JBeam topology parts are loaded.")
    if objects and not scene.get("beamng_active_jbeam_part_key", ""):
        issues.append("No Active Part is selected.")
    current_folder = user_current_folder_from_preferences(context)
    if current_folder is None:
        issues.append("BeamNG user folder is not configured.")
    else:
        export_root = current_folder / "mods" / "unpacked" / jbeam_export_mod_name(context) / "vehicles"
        lines.append(f"JBeam asset export root: {export_root}")
    if getattr(scene, "beamng_slot_editor_items", None):
        lines.append(f"Slot rows loaded: {len(scene.beamng_slot_editor_items)}")
        if scene.get("beamng_slot_editor_dirty", False):
            issues.append("Slot editor has unapplied/unsaved dirty changes; assembly may require reload.")
    else:
        lines.append("Slot rows loaded: 0")
    lines.append("")
    lines.append("Parts:")
    for obj in objects:
        lines.append(
            f"- {obj.get('beamng_part_name', obj.name)} | "
            f"source={normalize_virtual_path(obj.get('beamng_jbeam_path', '')) or '(unknown)'} | "
            f"owned={int(obj.get('beamng_owned_node_count', 0) or 0)} | "
            f"proxy={int(obj.get('beamng_proxy_node_count', 0) or 0)} | "
            f"state={obj.get('beamng_active_part_state', '') or 'unknown'}"
        )
    history = jbeam_operation_history(scene)
    grouped = defaultdict(lambda: defaultdict(int))
    for operation in history:
        grouped[str(operation.get("source_file", "") or "(unknown)")][str(operation.get("type", "update"))] += 1
    lines.append("")
    lines.append("Accepted edit groups:")
    if grouped:
        for source_file, counts in sorted(grouped.items()):
            summary = ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
            lines.append(f"- {source_file}: {summary}")
    else:
        lines.append("- none")
    lines.append("")
    if issues:
        lines.append("Issues:")
        for issue in issues:
            lines.append(f"- {issue}")
    else:
        lines.append("Issues: none")
    return {"status": "blocked" if issues else "ok", "issues": issues, "lines": lines}


class BEAMNG_OT_validate_jbeam_assembly(Operator):
    bl_idname = "beamng_pc_importer.validate_jbeam_assembly"
    bl_label = "Validate Assembly"
    bl_description = "Write an assembly-level validation and semantic edit grouping report"
    bl_options = {"REGISTER"}

    def execute(self, context):
        report = assembly_validation_report(context)
        text = bpy.data.texts.get("BeamNG Assembly Validation") or bpy.data.texts.new("BeamNG Assembly Validation")
        text.clear()
        text.write("\n".join(report["lines"]))
        text.write("\n")
        report_path = persistent_cache_dir() / "jbeam_editor" / f"assembly_validation_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("\n".join(report["lines"]) + "\n", encoding="utf-8")
        context.scene["beamng_jbeam_last_assembly_validation_path"] = str(report_path)
        context.scene["beamng_jbeam_last_assembly_validation_status"] = report["status"]
        level = {"WARNING"} if report["status"] == "blocked" else {"INFO"}
        self.report(level, f"Assembly validation: {report['status']}")
        return {"FINISHED"}


def authoring_workflow_report_lines(context):
    scene = context.scene
    counts = jbeam_counts_for_panel(context)
    active_object = active_jbeam_assembly_part_object(scene)
    active_name = str(scene.get("beamng_active_jbeam_part_name", "") or "")
    part_count = len(experimental_jbeam_part_objects(scene))
    slot_count = len(getattr(scene, "beamng_slot_editor_items", []) or [])
    selected_nodes = experimental_jbeam_node_info_for_selection(context, limit=3)
    selected_edges = experimental_jbeam_edge_info_for_selection(context, limit=3)
    selected_faces = experimental_jbeam_face_info_for_selection(context, limit=3)
    current_folder = user_current_folder_from_preferences(context)
    export_root = (
        current_folder / "mods" / "unpacked" / jbeam_export_mod_name(context) / "vehicles"
        if current_folder is not None
        else None
    )
    lines = [
        "[BeamNG Authoring Workflow]",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "1. UI workflow",
        f"- Active part: {active_name or '(none)'}",
        f"- Active object: {active_object.name if active_object else '(none)'}",
        f"- Assembly parts: {part_count}",
        "",
        "2. Slot authoring",
        f"- Slot rows loaded: {slot_count}",
        f"- Slot dirty: {bool(scene.get('beamng_slot_editor_dirty', False))}",
        "- Current support: configuration slot choice apply/save plus new-part slot metadata draft.",
        "- Remaining: full slot type/default/child-slot authoring workflow.",
        "",
        "3. Property inheritance",
        f"- Selected nodes/edges/faces sampled: {len(selected_nodes)}/{len(selected_edges)}/{len(selected_faces)}",
        "- Effective params shown in the selection panel are parsed/inherited context where available.",
        "- Remaining: stronger section-scope inheritance model for every JBeam parameter family.",
        "",
        "4. Export review",
        f"- Accepted operations: {counts['history']}",
        f"- Export mod: {jbeam_export_mod_name(context)}",
        f"- Export root: {export_root if export_root else '(BeamNG user folder not configured)'}",
        "- Current support: validate, file selection, stage new files, quick export.",
        "- Remaining: richer semantic diff/review window before every write.",
        "",
        "5. Validation",
        f"- Missing refs: {counts['missing_refs']}",
        f"- Proxy drift: {counts['proxy_drift']}",
        f"- Dirty params: {counts['dirty_params']}",
        "- Current support: active mesh health, assembly validation, export preflight.",
        "- Remaining: diagnostic freshness/conflict UX and BeamNG-side smoke testing.",
        "",
        "6. Non-core JBeam semantics",
        "- Hydros/sliders/props/flexbodies are visible/imported in existing workflows but not full semantic authoring targets.",
        "- Remaining: rails/sliders/hydros/props/flexbodies editing PRD and data model.",
        "",
        "7. Refactor spine",
        "- Current risk: many model/export/UI concerns still live in __init__.py.",
        "- Remaining: extract model, import, export, validation, and UI modules after next stable checkpoint.",
    ]
    return lines


class BEAMNG_OT_write_authoring_workflow_report(Operator):
    bl_idname = "beamng_pc_importer.write_authoring_workflow_report"
    bl_label = "Write Authoring Workflow Report"
    bl_description = "Write a report summarizing the seven current completion/productization milestones"
    bl_options = {"REGISTER"}

    def execute(self, context):
        lines = authoring_workflow_report_lines(context)
        text = bpy.data.texts.get("BeamNG Authoring Workflow") or bpy.data.texts.new("BeamNG Authoring Workflow")
        text.clear()
        text.write("\n".join(lines) + "\n")
        report_path = persistent_cache_dir() / "jbeam_editor" / f"authoring_workflow_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        context.scene["beamng_jbeam_last_authoring_workflow_report_path"] = str(report_path)
        self.report({"INFO"}, f"Wrote authoring workflow report: {report_path}")
        return {"FINISHED"}


class BEAMNG_OT_write_jbeam_edit_preview(Operator):
    bl_idname = "beamng_pc_importer.write_jbeam_edit_preview"
    bl_label = "Write JBeam Edit Preview"
    bl_description = "Write accepted experimental JBeam edits grouped by source file and part"
    bl_options = {"REGISTER"}

    def execute(self, context):
        report_path, preview = write_jbeam_edit_preview_report(context.scene)
        if preview["operation_count"] == 0:
            self.report({"WARNING"}, "No accepted JBeam edits are recorded")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Wrote JBeam edit preview with {preview['operation_count']} operation(s): {report_path}")
        return {"FINISHED"}


class BEAMNG_OT_write_jbeam_node_patch_draft(Operator):
    bl_idname = "beamng_pc_importer.write_jbeam_node_patch_draft"
    bl_label = "Write JBeam Patch Draft"
    bl_description = "Write a cache-only node/topology patch draft grouped by source JBeam file and part"
    bl_options = {"REGISTER"}

    def execute(self, context):
        report_path, draft = write_jbeam_node_patch_draft_report(context.scene)
        if draft.get("operation_count", draft["node_update_count"]) == 0:
            self.report({"WARNING"}, "No accepted JBeam edits are recorded")
            return {"CANCELLED"}
        self.report(
            {"INFO"},
            f"Wrote cache-only JBeam patch draft with {draft.get('operation_count', draft['node_update_count'])} update(s): {report_path}",
        )
        return {"FINISHED"}


class BEAMNG_OT_write_jbeam_override_export_plan(Operator):
    bl_idname = "beamng_pc_importer.write_jbeam_override_export_plan"
    bl_label = "Write JBeam Override Export Plan"
    bl_description = "Write a cache-only plan mapping accepted JBeam edits to safe unpacked mod override targets"
    bl_options = {"REGISTER"}

    def execute(self, context):
        report_path, plan = write_jbeam_override_export_plan_report(context)
        if plan.get("operation_count", plan["node_update_count"]) == 0:
            self.report({"WARNING"}, "No accepted JBeam edits are recorded")
            return {"CANCELLED"}
        self.report(
            {"INFO"},
            (
                f"Wrote cache-only override export plan for {plan.get('operation_count', plan['node_update_count'])} update(s), "
                f"{plan['stageable_file_count']}/{plan['source_file_count']} file(s) stageable: {report_path}"
            ),
        )
        return {"FINISHED"}


class BEAMNG_OT_validate_jbeam_export(Operator):
    bl_idname = "beamng_pc_importer.validate_jbeam_export"
    bl_label = "Validate JBeam Export"
    bl_description = "Preflight accepted JBeam edits, source patchability, and unpacked mod export targets without writing vehicle files"
    bl_options = {"REGISTER"}

    def execute(self, context):
        report_path, validation = write_jbeam_export_validation_report(context)
        if validation.get("operation_count", validation["node_update_count"]) == 0:
            self.report({"WARNING"}, "No accepted JBeam edits are recorded")
            return {"CANCELLED"}
        message = (
            f"JBeam export validation {validation['status']}: "
            f"{validation['source_preserving_file_count']} source-preserving, "
            f"{validation['clean_json_fallback_file_count']} fallback, "
            f"{len(validation['errors'])} error(s), {len(validation['warnings'])} warning(s): {report_path}"
        )
        self.report({"ERROR"} if validation["status"] == "fail" else {"WARNING"} if validation["status"] == "warning" else {"INFO"}, message)
        return {"CANCELLED"} if validation["status"] == "fail" else {"FINISHED"}


class BEAMNG_OT_write_jbeam_patched_cache_copies(Operator):
    bl_idname = "beamng_pc_importer.write_jbeam_patched_cache_copies"
    bl_label = "Write Patched JBeam Cache Copies"
    bl_description = "Write cache-only JBeam files with accepted node/topology edits applied for review"
    bl_options = {"REGISTER"}

    def execute(self, context):
        manifest_path, manifest = write_jbeam_patched_cache_copy_report(context)
        if manifest.get("operation_count", manifest["node_update_count"]) == 0:
            self.report({"WARNING"}, "No accepted JBeam edits are recorded")
            return {"CANCELLED"}
        self.report(
            {"INFO"},
            (
                f"Wrote patched JBeam cache copies with {manifest.get('changed_operation_count', manifest['changed_node_count'])} applied "
                f"and {manifest['skipped_update_count']} skipped update(s): {manifest_path}"
            ),
        )
        return {"FINISHED"}


class BEAMNG_OT_stage_jbeam_user_override_copies(Operator):
    bl_idname = "beamng_pc_importer.stage_jbeam_user_override_copies"
    bl_label = "Stage JBeam Mod Override Copies"
    bl_description = "Copy patched cache JBeam files into user current/mods/unpacked without overwriting existing files"
    bl_options = {"REGISTER"}

    def invoke(self, context, event):
        populate_jbeam_export_file_selection(context)
        return context.window_manager.invoke_props_dialog(self, width=760)

    def draw(self, context):
        draw_jbeam_export_selection(self.layout, context, overwrite_existing=False)

    def execute(self, context):
        selected_virtual_paths = selected_jbeam_export_virtual_paths(context.scene)
        if selected_virtual_paths is not None and not selected_virtual_paths:
            self.report({"WARNING"}, "No JBeam files selected for export")
            return {"CANCELLED"}
        validation_path, validation = validate_selected_jbeam_export_or_report(
            context,
            selected_virtual_paths=selected_virtual_paths,
        )
        if validation["status"] == "fail":
            self.report({"ERROR"}, f"JBeam export preflight failed: {validation_path}")
            return {"CANCELLED"}
        stage_manifest_path, stage_manifest = write_jbeam_user_override_stage_report(
            context,
            selected_virtual_paths=selected_virtual_paths,
        )
        if stage_manifest.get("changed_operation_count", stage_manifest["changed_node_count"]) == 0:
            self.report({"WARNING"}, "No accepted JBeam edits are recorded")
            return {"CANCELLED"}
        if stage_manifest["staged_file_count"] == 0:
            self.report(
                {"WARNING"},
                f"No JBeam override files were staged; skipped {stage_manifest['skipped_file_count']} file(s): {stage_manifest_path}",
            )
            return {"CANCELLED"}
        checkpoint = checkpoint_exported_jbeam_operation_history(
            context.scene,
            stage_manifest_path,
            stage_manifest,
            "stage_user_override_copies",
            selected_virtual_paths=selected_virtual_paths,
            current_folder=user_current_folder_from_preferences(context),
        )
        self.report(
            {"INFO"},
            (
                f"Staged {stage_manifest['staged_file_count']} JBeam override file(s); "
                f"skipped {stage_manifest['skipped_file_count']}; "
                f"history {'cleared' if checkpoint['cleared'] else 'kept'}: {stage_manifest_path}"
            ),
        )
        return {"FINISHED"}


class BEAMNG_OT_update_jbeam_user_override_copies(Operator):
    bl_idname = "beamng_pc_importer.update_jbeam_user_override_copies"
    bl_label = "Update JBeam Mod Override Copies"
    bl_description = "Overwrite existing user current/mods/unpacked JBeam override files after backing them up into the addon cache"
    bl_options = {"REGISTER"}

    def invoke(self, context, event):
        populate_jbeam_export_file_selection(context)
        return context.window_manager.invoke_props_dialog(self, width=760)

    def draw(self, context):
        draw_jbeam_export_selection(self.layout, context, overwrite_existing=True)

    def execute(self, context):
        selected_virtual_paths = selected_jbeam_export_virtual_paths(context.scene)
        if selected_virtual_paths is not None and not selected_virtual_paths:
            self.report({"WARNING"}, "No JBeam files selected for export")
            return {"CANCELLED"}
        validation_path, validation = validate_selected_jbeam_export_or_report(
            context,
            selected_virtual_paths=selected_virtual_paths,
        )
        if validation["status"] == "fail":
            self.report({"ERROR"}, f"JBeam export preflight failed: {validation_path}")
            return {"CANCELLED"}
        stage_manifest_path, stage_manifest = write_jbeam_user_override_stage_report(
            context,
            overwrite_existing=True,
            selected_virtual_paths=selected_virtual_paths,
        )
        if stage_manifest.get("changed_operation_count", stage_manifest["changed_node_count"]) == 0:
            self.report({"WARNING"}, "No accepted JBeam edits are recorded")
            return {"CANCELLED"}
        if stage_manifest["staged_file_count"] == 0:
            self.report(
                {"WARNING"},
                f"No JBeam override files were updated; skipped {stage_manifest['skipped_file_count']} file(s): {stage_manifest_path}",
            )
            return {"CANCELLED"}
        overwritten = sum(1 for item in stage_manifest["staged_files"] if item.get("overwrote_existing"))
        checkpoint = checkpoint_exported_jbeam_operation_history(
            context.scene,
            stage_manifest_path,
            stage_manifest,
            "update_user_override_copies",
            selected_virtual_paths=selected_virtual_paths,
            current_folder=user_current_folder_from_preferences(context),
        )
        self.report(
            {"INFO"},
            (
                f"Updated {stage_manifest['staged_file_count']} JBeam override file(s), "
                f"backed up {overwritten} existing file(s), skipped {stage_manifest['skipped_file_count']}: "
                f"history {'cleared' if checkpoint['cleared'] else 'kept'}: {stage_manifest_path}"
            ),
        )
        return {"FINISHED"}


class BEAMNG_OT_quick_export_jbeam_node_moves(Operator):
    bl_idname = "beamng_pc_importer.quick_export_jbeam_node_moves"
    bl_label = "Quick Export JBeam Edits"
    bl_description = "Scan all experimental JBeam meshes, accept moved owned nodes, and update unpacked mod overrides with backups"
    bl_options = {"REGISTER", "UNDO"}

    accepted_count: IntProperty(default=0)
    scanned_mesh_count: IntProperty(default=0)
    restored_proxy_count: IntProperty(default=0)

    def invoke(self, context, event):
        scan_result = scan_experimental_jbeam_mesh_edits(context.scene, active_only=False)
        accept_result = accept_experimental_jbeam_node_moves(context.scene)
        self.accepted_count = int(accept_result.get("accepted_count", 0))
        self.scanned_mesh_count = int(scan_result.get("scanned_mesh_count", 0))
        self.restored_proxy_count = int(scan_result.get("restored_proxy_count", 0))
        new_file_count = len(staged_new_jbeam_file_groups(context))
        if self.accepted_count == 0 and int(context.scene.get("beamng_jbeam_operation_history_count", 0)) == 0 and new_file_count == 0:
            self.report(
                {"WARNING"},
                (
                    f"No new JBeam edits found after scanning {self.scanned_mesh_count} JBeam mesh(es); "
                    f"restored {self.restored_proxy_count} proxy move(s)"
                ),
            )
            return {"CANCELLED"}
        populate_jbeam_export_file_selection(context)
        return context.window_manager.invoke_props_dialog(self, width=760)

    def draw(self, context):
        layout = self.layout
        if self.accepted_count:
            layout.label(text=f"Accepted new JBeam edits: {self.accepted_count}")
        else:
            layout.label(text="No new edits accepted; exporting existing history/new staged files.")
        draw_jbeam_export_selection(layout, context, overwrite_existing=True)

    def execute(self, context):
        selected_virtual_paths = selected_jbeam_export_virtual_paths(context.scene)
        if selected_virtual_paths is not None and not selected_virtual_paths:
            self.report({"WARNING"}, "No JBeam files selected for export")
            return {"CANCELLED"}
        validation_path, validation = validate_selected_jbeam_export_or_report(
            context,
            selected_virtual_paths=selected_virtual_paths,
        )
        if validation["status"] == "fail":
            self.report({"ERROR"}, f"JBeam export preflight failed: {validation_path}")
            return {"CANCELLED"}

        stage_manifest_path, stage_manifest = write_jbeam_user_override_stage_report(
            context,
            overwrite_existing=True,
            selected_virtual_paths=selected_virtual_paths,
        )
        if stage_manifest["staged_file_count"] == 0:
            self.report(
                {"WARNING"},
                (
                    f"Accepted {self.accepted_count} new JBeam edit(s), but no override files were updated; "
                    f"skipped {stage_manifest['skipped_file_count']} file(s): {stage_manifest_path}"
                ),
            )
            return {"CANCELLED"}

        overwritten = sum(1 for item in stage_manifest["staged_files"] if item.get("overwrote_existing"))
        checkpoint = checkpoint_exported_jbeam_operation_history(
            context.scene,
            stage_manifest_path,
            stage_manifest,
            "quick_export_jbeam_node_moves",
            selected_virtual_paths=selected_virtual_paths,
            current_folder=user_current_folder_from_preferences(context),
        )
        self.report(
            {"INFO"},
            (
                f"Quick exported {self.accepted_count} new JBeam edit(s); "
                f"updated {stage_manifest['staged_file_count']} file(s), "
                f"backed up {overwritten}, skipped {stage_manifest['skipped_file_count']}: "
                f"history {'cleared' if checkpoint['cleared'] else 'kept'}: {stage_manifest_path}"
            ),
        )
        return {"FINISHED"}


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
        root = find_jbeam_root_for_object(context, selected_active_object(context))
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
        root = find_jbeam_root_for_object(context, selected_active_object(context))
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


class BEAMNG_OT_show_jbeam_part_with_references(Operator):
    bl_idname = "beamng_pc_importer.show_jbeam_part_with_references"
    bl_label = "Show Part With Referenced JBeam Parts"
    bl_description = "Hide other JBeam visuals and show the selected part plus parts owning nodes referenced by its beams, triangles, hydros, rails, or slidenodes"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return selected_jbeam_part_id(context) is not None

    def execute(self, context):
        part_id = selected_jbeam_part_id(context)
        root = find_jbeam_root_for_object(context, selected_active_object(context))
        if root is None or part_id is None:
            self.report({"WARNING"}, "No BeamNG JBeam selection found")
            return {"CANCELLED"}

        selected_reference_objects = [
            obj
            for obj in context.selected_objects
            if obj.get("beamng_layer") == "jbeam"
            and str(obj.get("beamng_resolved_part_id")) == str(part_id)
            and jbeam_node_ids_for_object(obj)
        ]
        selected_node_ids = set()
        for obj in selected_reference_objects:
            selected_node_ids.update(jbeam_node_ids_for_object(obj))

        external_node_ids = jbeam_external_node_refs_for_part(root, part_id)
        if not external_node_ids:
            source_objects = jbeam_reference_objects_for_part(root, part_id)
            _source_part_ids, external_node_ids = jbeam_referenced_part_ids_for_objects(
                root,
                part_id,
                source_objects,
            )

        # A selected connector beam should refine selection, not hide the owning part's other references.
        reference_node_ids = set(external_node_ids)
        reference_node_ids.update(selected_node_ids)
        referenced_part_ids, referenced_node_ids = jbeam_referenced_part_ids_for_node_ids(
            root,
            part_id,
            reference_node_ids,
        )
        visible_part_ids = {part_id, *referenced_part_ids}

        set_all_jbeam_visibility(root, False)
        ensure_jbeam_container_visible(root)
        set_jbeam_collections_visibility(root, visible_part_ids, True)
        visible_objects = jbeam_objects_for_part_ids(root, visible_part_ids)
        for obj in visible_objects:
            obj.hide_set(False)
            obj.hide_render = obj.get("beamng_visual_type") == "node_label"

        for obj in context.scene.objects:
            obj.select_set(False)
        active_part_objects = jbeam_objects_for_part_ids(root, {part_id}, {"selectable_node", "selectable_beam"})
        for obj in active_part_objects:
            obj.select_set(True)
        if active_part_objects:
            context.view_layer.objects.active = active_part_objects[0]

        if not referenced_part_ids:
            self.report({"INFO"}, "Showing selected JBeam part only; no external referenced node owners found")
        else:
            self.report(
                {"INFO"},
                f"Showing selected part plus {len(referenced_part_ids)} referenced part(s) via {len(referenced_node_ids)} external node id(s)",
            )
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
            include_user_overrides = bool(context.scene.get("beamng_import_include_user_overrides", True))
            result = import_beamng_pc_path(
                context,
                self,
                edited_pc_path,
                True,
                True,
                False,
                False,
                f"Edited slot configuration from {source_pc_path}",
                None,
                include_user_overrides,
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


def slot_authoring_report_lines_for_context(context):
    scene = context.scene
    slot_items = getattr(scene, "beamng_slot_editor_items", [])
    active_mesh = active_experimental_jbeam_mesh(context)
    slot_rows = [
        {
            "slot_name": item.slot_name,
            "parent_part": item.parent_part,
            "selected_part": item.selected_part,
            "depth": item.depth,
            "is_core": item.is_core,
            "option_count": slot_option_count(item.options_json),
        }
        for item in slot_items
    ]
    active_part_metadata = None
    if active_mesh and active_mesh.get("beamng_visual_type") == "experimental_jbeam_mesh":
        payload = new_jbeam_payload_for_virtual_path(context.scene, normalize_virtual_path(active_mesh.get("beamng_jbeam_path", "")))
        part_name = str(active_mesh.get("beamng_part_name", "") or "")
        part_data = payload.get(part_name, {}) if isinstance(payload, dict) else {}
        if isinstance(part_data, dict) and part_data:
            slots = part_data.get("slots", [])
            child_slots = []
            if isinstance(slots, list) and len(slots) > 1:
                for row in slots[1:]:
                    if isinstance(row, list):
                        child_slots.append(row)
            active_part_metadata = {
                "part_name": part_name,
                "slot_type": part_data.get("slotType", ""),
                "child_slots": child_slots,
            }
    return slot_authoring_report_lines(
        root_part=scene.get("beamng_slot_editor_main_part", ""),
        vehicle_model=scene.get("beamng_slot_editor_model", ""),
        dirty=scene.get("beamng_slot_editor_dirty", False),
        slot_rows=slot_rows,
        active_part_metadata=active_part_metadata,
    )


class BEAMNG_OT_write_slot_authoring_report(Operator):
    bl_idname = "beamng_pc_importer.write_slot_authoring_report"
    bl_label = "Write Slot Authoring Report"
    bl_description = "Write a report describing current configuration slots and staged active-part slot metadata"
    bl_options = {"REGISTER"}

    def execute(self, context):
        lines = slot_authoring_report_lines_for_context(context)
        text = bpy.data.texts.get("BeamNG Slot Authoring Report") or bpy.data.texts.new("BeamNG Slot Authoring Report")
        text.clear()
        text.write("\n".join(lines) + "\n")
        report_path = persistent_cache_dir() / "jbeam_editor" / f"slot_authoring_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        context.scene["beamng_jbeam_last_slot_authoring_report_path"] = str(report_path)
        self.report({"INFO"}, f"Wrote slot authoring report: {report_path}")
        return {"FINISHED"}


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
    row = box.row(align=True)
    row.operator(BEAMNG_OT_write_slot_authoring_report.bl_idname, text="Slot Report")
    active_mesh = active_experimental_jbeam_mesh(context)
    if active_mesh and active_mesh.get("beamng_visual_type") == "experimental_jbeam_mesh":
        row.operator(BEAMNG_OT_write_active_jbeam_slot_metadata.bl_idname, text="Active Part Slot Metadata")
        box.operator(BEAMNG_OT_add_active_jbeam_child_slot.bl_idname, text="Add Active Part Child Slot")

    source_label = context.scene.get("beamng_slot_editor_source_pc_path", "")
    if source_label:
        box.label(text=f"Source: {Path(source_label).name}")
    if context.scene.get("beamng_slot_editor_dirty", False):
        box.label(text="Unsaved slot choices are dirty.", icon="ERROR")

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


def draw_beamng_visibility_controls(layout, context):
    row = layout.row(align=True)
    op = row.operator(BEAMNG_OT_set_visibility.bl_idname, text="Body")
    op.mode = "MESHES"
    op = row.operator(BEAMNG_OT_set_visibility.bl_idname, text="Props")
    op.mode = "PROPS"
    op = row.operator(BEAMNG_OT_set_visibility.bl_idname, text="JBeam")
    op.mode = "JBEAM"
    row = layout.row(align=True)
    op = row.operator(BEAMNG_OT_set_visibility.bl_idname, text="Body+Props")
    op.mode = "MESHES_PROPS"
    op = row.operator(BEAMNG_OT_set_visibility.bl_idname, text="Authoring")
    op.mode = "AUTHORING"
    op = row.operator(BEAMNG_OT_set_visibility.bl_idname, text="All")
    op.mode = "ALL"


def draw_beamng_view_controls(layout, context):
    box = layout.box()
    box.label(text="This View")
    row = box.row(align=True)
    for label, mode in (("Flex", "FLEX"), ("Props", "PROPS"), ("JBeam", "JBEAM")):
        op = row.operator(BEAMNG_OT_set_active_view_filter.bl_idname, text=label)
        op.mode = mode
    row = box.row(align=True)
    op = row.operator(BEAMNG_OT_set_active_view_filter.bl_idname, text="Flex+Props")
    op.mode = "FLEX_PROPS"
    op = row.operator(BEAMNG_OT_set_active_view_filter.bl_idname, text="All")
    op.mode = "ALL"
    row = box.row(align=True)
    row.operator(BEAMNG_OT_toggle_view_sync.bl_idname, text="Sync Views")
    row.operator(BEAMNG_OT_exit_split_local_views.bl_idname, text="Exit Local")


def jbeam_counts_for_panel(context):
    return {
        "pending": int(context.scene.get("beamng_jbeam_pending_node_move_count", 0)),
        "pending_topology": int(context.scene.get("beamng_jbeam_pending_topology_change_count", 0)),
        "history": int(context.scene.get("beamng_jbeam_operation_history_count", 0)),
        "model_ops": int(context.scene.get("beamng_authoring_model_operation_count", 0)),
        "proxy_drift": int(context.scene.get("beamng_jbeam_health_proxy_drift_count", 0)),
        "missing_refs": int(context.scene.get("beamng_jbeam_health_missing_reference_count", 0)),
        "dirty_params": int(context.scene.get("beamng_jbeam_health_dirty_param_count", 0)),
    }


def draw_jbeam_edit_status(layout, context):
    counts = jbeam_counts_for_panel(context)
    box = layout.box()
    box.label(text="Edit State")
    box.label(text=f"Pending: {counts['pending']} / topology {counts['pending_topology']}")
    box.label(text=f"Accepted: {counts['history']}")
    if counts["model_ops"] != counts["history"]:
        box.label(text=f"Model ops: {counts['model_ops']} (refresh on accept/clear)", icon="INFO")
    row = box.row(align=True)
    op = row.operator(BEAMNG_OT_scan_experimental_jbeam_mesh_edits.bl_idname, text="Scan Active")
    op.active_only = True
    op = row.operator(BEAMNG_OT_scan_experimental_jbeam_mesh_edits.bl_idname, text="Scan All")
    op.active_only = False
    row = box.row(align=True)
    row.enabled = counts["pending"] > 0
    row.operator(BEAMNG_OT_accept_experimental_jbeam_node_moves.bl_idname, text="Accept")
    row = box.row(align=True)
    row.enabled = counts["pending"] > 0 or counts["history"] > 0
    row.operator(BEAMNG_OT_clear_jbeam_edit_session.bl_idname, text="Discard")


def draw_jbeam_assembly_part_controls(layout, context):
    active_key = str(context.scene.get("beamng_active_jbeam_part_key", "") or "")
    active_name = str(context.scene.get("beamng_active_jbeam_part_name", "") or "")
    active_object = active_jbeam_assembly_part_object(context.scene)
    active_mesh = active_experimental_jbeam_mesh(context)
    box = layout.box()
    box.label(text="Assembly / Active Part")
    if active_key and active_object:
        box.label(text=f"Active: {active_name}", icon="CHECKMARK")
        box.label(text=f"Object: {active_object.name}")
        source = context.scene.get("beamng_active_jbeam_part_source", "")
        if source:
            box.label(text=f"Source: {Path(source).name}")
    elif active_key:
        box.label(text="Active Part missing; refresh or choose a part", icon="ERROR")
    else:
        box.label(text="Select a JBeam mesh to make it the Active Part.", icon="INFO")
    box.label(text="Selection drives edit ownership; other parts are reference context.")
    row = box.row(align=True)
    row.operator(BEAMNG_OT_refresh_jbeam_assembly_parts.bl_idname, text="Refresh")
    row.operator(BEAMNG_OT_set_active_jbeam_part_from_selection.bl_idname, text="Use Selected")
    clear = row.operator(BEAMNG_OT_set_active_jbeam_part_from_selection.bl_idname, text="Clear")
    clear.clear = True
    box.operator(BEAMNG_OT_validate_jbeam_assembly.bl_idname, text="Validate Assembly")
    slot_count = len(getattr(context.scene, "beamng_slot_editor_items", []) or [])
    if slot_count:
        slot_state = "dirty; apply/save or reload assembly" if context.scene.get("beamng_slot_editor_dirty", False) else "loaded"
        box.label(text=f"Slot context: {slot_count} row(s), {slot_state}")
    else:
        box.label(text="Slot context: none loaded")
    if active_mesh and active_key and not active_part_allows_topology_edit(context, active_mesh):
        box.label(text="Selected mesh is reference-only while another part is active.", icon="LOCKED")
    objects = experimental_jbeam_part_objects(context.scene)
    if not objects:
        box.label(text="Import JBeam topology to populate assembly parts.")
        return
    list_box = box.box()
    list_box.label(text=f"Parts: {len(objects)}")
    for obj in objects[:12]:
        part_key = jbeam_assembly_part_key_for_object(obj)
        part_name = str(obj.get("beamng_part_name", "") or obj.name)
        row = list_box.row(align=True)
        is_active = part_key == active_key
        row.alert = is_active
        op = row.operator(BEAMNG_OT_activate_jbeam_assembly_part.bl_idname, text=part_name)
        op.part_key = part_key
        row.label(
            text=(
                f"{'Active' if is_active else 'Ref'} "
                f"{int(obj.get('beamng_owned_node_count', 0) or 0)}/"
                f"{int(obj.get('beamng_proxy_node_count', 0) or 0)}"
            )
        )
    if len(objects) > 12:
        list_box.label(text=f"+ {len(objects) - 12} more part(s); use viewport selection.")


def draw_jbeam_topology_tools(layout, context, active_mesh):
    box = layout.box()
    box.label(text="Topology Tools")
    box.operator(BEAMNG_OT_create_jbeam_part_file.bl_idname, text="New JBeam Part/File")
    last_created = context.scene.get("beamng_last_created_jbeam_part_path", "")
    if last_created:
        box.label(text=f"Last new file: {Path(last_created).name}", icon="INFO")
    if not active_mesh or active_mesh.get("beamng_visual_type") != "experimental_jbeam_mesh":
        box.label(text="Activate experimental JBeam mesh.")
        box.label(text=f"Debug: {active_object_debug_label(context)}")
        return
    box.label(text=f"Part: {active_mesh.get('beamng_part_name', '')}")
    box.label(text=f"Mode: {active_mesh.mode}")
    if not active_part_allows_topology_edit(context, active_mesh):
        box.label(text="Reference-only: switch Active Part to edit this mesh.", icon="LOCKED")
        return
    box.label(text=f"Owned/proxy: {active_mesh.get('beamng_owned_node_count', 0)} / {active_mesh.get('beamng_proxy_node_count', 0)}")
    box.label(text=f"Topology revision: {active_mesh.data.get('beamng_topology_revision', 0)}")
    box.label(text=f"Topology delta: {active_mesh.data.get('beamng_semantic_topology_delta_count', 0)}")
    box.operator(BEAMNG_OT_write_active_jbeam_slot_metadata.bl_idname, text="Write Slot Metadata")
    box.operator(BEAMNG_OT_add_active_jbeam_child_slot.bl_idname, text="Add Child Slot")
    row = box.row(align=True)
    row.operator(BEAMNG_OT_add_standalone_jbeam_node.bl_idname, text="Add Node")
    row.operator(BEAMNG_OT_create_jbeam_beam_from_selected_nodes.bl_idname, text="Beam 2")
    row = box.row(align=True)
    row.operator(BEAMNG_OT_mark_selected_nodes_for_proxy_import.bl_idname, text="Mark Nodes")
    row.operator(BEAMNG_OT_import_marked_nodes_as_proxies.bl_idname, text="Import Marked Nodes")
    box.operator(BEAMNG_OT_create_crossbeam_to_marked_node.bl_idname, text="Crossbeam To Marked Node")
    row = box.row(align=True)
    op = row.operator(BEAMNG_OT_mark_selected_nodes_for_proxy_import.bl_idname, text="Append Marks")
    op.append = True
    row.operator(BEAMNG_OT_clear_marked_proxy_nodes.bl_idname, text="Clear Marks")
    row = box.row(align=True)
    row.operator(BEAMNG_OT_clear_unused_proxy_nodes.bl_idname, text="Clear Unused Proxies")
    row.operator(BEAMNG_OT_clear_orphan_provisional_nodes.bl_idname, text="Clear Orphans")
    marked_count = int(context.scene.get("beamng_proxy_import_clipboard_count", 0) or 0)
    if marked_count:
        box.label(text=f"Marked proxy nodes: {marked_count}", icon="PINNED")
    row = box.row(align=True)
    row.operator(BEAMNG_OT_create_jbeam_triangle_from_selected_nodes.bl_idname, text="Triangle 3")
    row.operator(BEAMNG_OT_delete_selected_jbeam_elements.bl_idname, text="Delete")
    row = box.row(align=True)
    op = row.operator(BEAMNG_OT_set_selected_jbeam_edge_semantic.bl_idname, text="Edge=Beam")
    op.semantic_type = JBEAM_EDGE_SEMANTIC_BEAM
    op = row.operator(BEAMNG_OT_set_selected_jbeam_edge_semantic.bl_idname, text="Edge=Boundary")
    op.semantic_type = JBEAM_EDGE_SEMANTIC_TRIANGLE_BOUNDARY
    op = row.operator(BEAMNG_OT_set_selected_jbeam_edge_semantic.bl_idname, text="Edge=Relation")
    op.semantic_type = JBEAM_EDGE_SEMANTIC_RELATIONSHIP
    row = box.row(align=True)
    row.operator(BEAMNG_OT_triangulate_selected_jbeam_faces.bl_idname, text="Triangulate")
    row = box.row(align=True)
    row.operator(BEAMNG_OT_flip_selected_jbeam_triangles.bl_idname, text="Flip Tri")
    row.operator(BEAMNG_OT_repair_experimental_jbeam_topology_uids.bl_idname, text="Repair UIDs")
    op = row.operator(BEAMNG_OT_repair_experimental_jbeamzzz.bl_idname, text="Repair Semantic")
    op.active_only = False
    box.operator(BEAMNG_OT_write_semantic_topology_snapshot.bl_idname, text="Write Semantic Snapshot")


def draw_jbeam_selected_elements(layout, context):
    selected_nodes = experimental_jbeam_node_info_for_selection(context)
    selected_edges = experimental_jbeam_edge_info_for_selection(context)
    selected_faces = experimental_jbeam_face_info_for_selection(context)
    if not selected_nodes and not selected_edges and not selected_faces:
        layout.label(text="Select vertices/edges/faces for params.")
        return
    element_box = layout.box()
    element_box.label(text="Selected Elements")
    if selected_nodes:
        node_box = element_box.box()
        node_box.label(text=f"Node{'s' if len(selected_nodes) != 1 else ''}")
        for node_info in selected_nodes:
            node_box.label(text=f"{node_info['node_id']} ({node_info['kind']}) v{node_info['vertex_index']}")
            node_box.label(text=f"UID: {node_info.get('topology_uid', 0)}")
            node_box.label(text=f"Model-backed: {'yes' if node_info.get('model_backed') else 'no'}")
            node_box.label(text=f"Refs: {node_info.get('model_reference_beam_count', 0)} beam, {node_info.get('model_reference_triangle_count', 0)} tri")
            node_box.label(text=f"Position: {node_info['current_position']}")
            params = node_info.get("params", {})
            param_box = node_box.box()
            param_box.label(text="Effective params")
            draw_param_summary(param_box, params, "none found")
        props_box = node_box.box()
        props_box.label(text="Node Params")
        for prop in ("beamng_jbeam_node_weight", "beamng_jbeam_node_material", "beamng_jbeam_node_group", "beamng_jbeam_node_friction"):
            props_box.prop(context.scene, prop)
        props_box.prop(context.scene, "beamng_jbeam_node_collision_override")
        if context.scene.beamng_jbeam_node_collision_override:
            props_box.prop(context.scene, "beamng_jbeam_node_collision")
        props_box.prop(context.scene, "beamng_jbeam_node_self_collision_override")
        if context.scene.beamng_jbeam_node_self_collision_override:
            props_box.prop(context.scene, "beamng_jbeam_node_self_collision")
        row = props_box.row(align=True)
        row.operator(BEAMNG_OT_load_selected_jbeam_node_properties.bl_idname, text="Load")
        row.operator(BEAMNG_OT_apply_selected_jbeam_node_properties.bl_idname, text="Apply")
    if selected_edges:
        edge_box = element_box.box()
        edge_box.label(text="Beam / Edge")
        for edge_info in selected_edges:
            edge_box.label(text=f"e{edge_info['edge_index']}: {edge_info['id1']} -> {edge_info['id2']}")
            edge_box.label(text=f"UID: {edge_info.get('topology_uid', 0)} / semantic: {edge_info.get('semantic_type', '')}")
            edge_box.label(text=f"Model-backed: {'yes' if edge_info.get('model_backed') else 'no'}")
            param_box = edge_box.box()
            param_box.label(text="Effective params")
            draw_param_summary(param_box, edge_info.get("params", {}), "none found")
        edge_box.label(text="Triangle boundary edges are not beams unless marked.")
        beam_props_box = edge_box.box()
        beam_props_box.label(text="Beam Params")
        for prop in ("beamng_jbeam_beam_spring", "beamng_jbeam_beam_damp", "beamng_jbeam_beam_deform", "beamng_jbeam_beam_strength", "beamng_jbeam_beam_precompression", "beamng_jbeam_beam_type", "beamng_jbeam_beam_break_group"):
            beam_props_box.prop(context.scene, prop)
        row = beam_props_box.row(align=True)
        row.operator(BEAMNG_OT_load_selected_jbeam_beam_properties.bl_idname, text="Load")
        row.operator(BEAMNG_OT_apply_selected_jbeam_beam_properties.bl_idname, text="Apply")
    if selected_faces:
        face_box = element_box.box()
        face_box.label(text="Collision Triangle")
        for face_info in selected_faces:
            face_box.label(text=f"f{face_info['face_index']}: {face_info['id1']} -> {face_info['id2']} -> {face_info['id3']}")
            face_box.label(text=f"UID: {face_info.get('topology_uid', 0)} / {face_info.get('semantic_type', '')} ({face_info.get('semantic_state', '')})")
            if face_info.get("normal"):
                face_box.label(text=f"Normal: {face_info['normal']}")
            face_box.label(text=f"Model-backed: {'yes' if face_info.get('model_backed') else 'no'}")
            param_box = face_box.box()
            param_box.label(text="Effective params")
            draw_param_summary(param_box, face_info.get("params", {}), "none found")
        face_box.label(text="Node order = collision winding.")
        tri_props_box = face_box.box()
        tri_props_box.label(text="Triangle Params")
        for prop in ("beamng_jbeam_triangle_group", "beamng_jbeam_triangle_drag_coef", "beamng_jbeam_triangle_ground_model"):
            tri_props_box.prop(context.scene, prop)
        tri_props_box.prop(context.scene, "beamng_jbeam_triangle_collision_override")
        if context.scene.beamng_jbeam_triangle_collision_override:
            tri_props_box.prop(context.scene, "beamng_jbeam_triangle_collision")
        row = tri_props_box.row(align=True)
        row.operator(BEAMNG_OT_load_selected_jbeam_triangle_properties.bl_idname, text="Load")
        row.operator(BEAMNG_OT_apply_selected_jbeam_triangle_properties.bl_idname, text="Apply")


def guid_display_text(uid, limit=36):
    value = topology_uid_key(uid)
    if not value:
        return "(none)"
    return value if len(value) <= limit else f"{value[:limit - 3]}..."


def draw_guid_map_inspector(layout, context):
    active_mesh = active_experimental_jbeam_mesh(context)
    box = layout.box()
    box.label(text="GUID Map Inspector")
    if active_mesh is None or active_mesh.type != "MESH" or active_mesh.get("beamng_visual_type") != "experimental_jbeam_mesh":
        box.label(text="Activate an experimental JBeam mesh.")
        return

    mesh = active_mesh.data
    identity = ensure_experimental_mesh_identity(active_mesh, context.scene, allow_write=False)
    topology_uids = ensure_experimental_topology_uids(active_mesh, allow_write=False)
    node_uids = topology_uids.get("nodes", [])
    edge_uids = topology_uids.get("edges", [])
    face_uids = topology_uids.get("faces", [])
    node_id_map = mesh_json_dict(mesh, "beamng_node_uid_to_id_json")
    node_kind_map = mesh_json_dict(mesh, "beamng_node_uid_to_kind_json")
    node_owner_map = mesh_json_dict(mesh, "beamng_node_uid_to_owner_part_id_json")
    edge_type_map = mesh_json_dict(mesh, "beamng_edge_uid_to_semantic_type_json")
    edge_state_map = mesh_json_dict(mesh, "beamng_edge_uid_to_semantic_state_json")
    face_type_map = mesh_json_dict(mesh, "beamng_face_uid_to_semantic_type_json")
    face_state_map = mesh_json_dict(mesh, "beamng_face_uid_to_semantic_state_json")

    summary = box.box()
    summary.label(text=f"Object: {active_mesh.name}")
    summary.label(text=f"Part: {active_mesh.get('beamng_part_name', '')}")
    node_guid_count = len([uid for uid in node_uids if topology_uid_key(uid)])
    edge_guid_count = len([uid for uid in edge_uids if topology_uid_key(uid)])
    face_guid_count = len([uid for uid in face_uids if topology_uid_key(uid)])
    summary.label(text=f"Node GUIDs/maps: {node_guid_count}/{len(node_id_map)}")
    summary.label(text=f"Edge GUIDs/maps: {edge_guid_count}/{len(edge_type_map)}")
    summary.label(text=f"Face GUIDs/maps: {face_guid_count}/{len(face_type_map)}")
    if node_guid_count == 0 or edge_guid_count == 0 and len(mesh.edges) > 0 or face_guid_count == 0 and len(mesh.polygons) > 0:
        warn = summary.row()
        warn.alert = True
        warn.label(text="Missing GUID attrs: use Repair UIDs / reload patched Blender.", icon="ERROR")

    selected_nodes = experimental_jbeam_node_info_for_selection(context)
    selected_edges = experimental_jbeam_edge_info_for_selection(context)
    selected_faces = experimental_jbeam_face_info_for_selection(context)
    if not selected_nodes and not selected_edges and not selected_faces:
        box.label(text="Select a vertex, edge, or face to inspect mapping.")
        return

    if selected_nodes:
        node_box = box.box()
        node_box.label(text="Selected Vertex GUID Aliases")
        for info in selected_nodes:
            uid = topology_uid_key(info.get("topology_uid", ""))
            vertex_index = int(info.get("vertex_index", -1))
            node_ids = identity.get("node_ids", [])
            node_kinds = identity.get("node_kinds", [])
            owner_part_ids = identity.get("owner_part_ids", [])
            node_box.label(text=f"GUID: {guid_display_text(uid)}")
            node_alias = node_id_map.get(uid) or (node_ids[vertex_index] if 0 <= vertex_index < len(node_ids) else info.get("node_id", ""))
            node_kind = node_kind_map.get(uid) or (node_kinds[vertex_index] if 0 <= vertex_index < len(node_kinds) else info.get("kind", ""))
            node_owner = node_owner_map.get(uid) if uid in node_owner_map else (owner_part_ids[vertex_index] if 0 <= vertex_index < len(owner_part_ids) else info.get("owner_part_id", -1))
            node_box.label(text=f"Node alias: {node_alias}")
            node_box.label(text=f"Kind/owner: {node_kind} / {node_owner}")
            params = info.get("params", {})
            if params:
                draw_param_summary(node_box, params, "no params")

    if selected_edges:
        edge_box = box.box()
        edge_box.label(text="Selected Edge GUID Aliases")
        for info in selected_edges:
            uid = topology_uid_key(info.get("topology_uid", ""))
            edge_box.label(text=f"GUID: {guid_display_text(uid)}")
            edge_box.label(text=f"Nodes: {info.get('id1', '')} -> {info.get('id2', '')}")
            edge_box.label(text=f"Semantic: {edge_type_map.get(uid, info.get('semantic_type', ''))} ({edge_state_map.get(uid, '')})")
            params = info.get("params", {})
            if params:
                draw_param_summary(edge_box, params, "no params")

    if selected_faces:
        face_box = box.box()
        face_box.label(text="Selected Face GUID Aliases")
        for info in selected_faces:
            uid = topology_uid_key(info.get("topology_uid", ""))
            face_box.label(text=f"GUID: {guid_display_text(uid)}")
            face_box.label(text=f"Winding: {info.get('id1', '')} -> {info.get('id2', '')} -> {info.get('id3', '')}")
            face_box.label(text=f"Semantic: {face_type_map.get(uid, info.get('semantic_type', ''))} ({face_state_map.get(uid, info.get('semantic_state', ''))})")
            params = info.get("params", {})
            if params:
                draw_param_summary(face_box, params, "no params")


def draw_selected_jbeam_legacy_info(layout, context):
    active = selected_active_object(context)
    if not active or active.get("beamng_layer") != "jbeam":
        return
    visual_type = active.get("beamng_visual_type", "")
    box = layout.box()
    box.label(text="Selected Legacy JBeam")
    if visual_type in {"selectable_node", "node_label"}:
        box.label(text=f"Node: {active.get('beamng_node_id', '')}")
    elif visual_type == "selectable_beam":
        box.label(text=f"Beam: {active.get('beamng_beam_name', '')}")
        box.label(text=f"From/To: {active.get('beamng_beam_id1', '')} -> {active.get('beamng_beam_id2', '')}")
    elif visual_type == "selectable_triangle":
        box.label(text=f"Triangle: {active.get('beamng_triangle_name', '')}")
        box.label(text=f"Nodes: {active.get('beamng_triangle_id1', '')}, {active.get('beamng_triangle_id2', '')}, {active.get('beamng_triangle_id3', '')}")
    else:
        box.label(text=f"Type: {visual_type}")
    box.label(text=f"Part: {active.get('beamng_part_name', '')}")
    box.operator(BEAMNG_OT_select_jbeam_body_structure.bl_idname, text="Select Same Body")


class VIEW3D_PT_beamng_pc_importer(Panel):
    bl_label = "BeamNG Session"
    bl_idname = "VIEW3D_PT_beamng_pc_importer"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BeamNG"

    def draw(self, context):
        layout = self.layout
        roots = find_beamng_import_collections(context.scene)
        counts = jbeam_counts_for_panel(context)
        layout.label(text=f"Version: {addon_version_label()}")
        layout.label(text=f"Imports: {len(roots)}")
        layout.label(text=f"Export mod: {jbeam_export_mod_name(context)}")
        status = "Blocked" if counts["missing_refs"] else "Dirty" if counts["pending"] or counts["history"] else "Clean"
        layout.label(text=f"Status: {status}", icon="ERROR" if status == "Blocked" else "INFO")
        layout.operator(IMPORT_OT_beamng_jbeam_topology.bl_idname, text="Import JBeam Topology")
        draw_beamng_visibility_controls(layout, context)
        draw_beamng_view_controls(layout, context)
        layout.separator()
        draw_vehicle_slot_editor(layout, context)


class VIEW3D_PT_beamng_jbeam_edit(Panel):
    bl_label = "JBeam Edit"
    bl_idname = "VIEW3D_PT_beamng_jbeam_edit"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "JBeam Edit"

    def draw(self, context):
        layout = self.layout
        active_mesh = active_experimental_jbeam_mesh(context)
        draw_jbeam_assembly_part_controls(layout, context)
        draw_jbeam_edit_status(layout, context)
        draw_jbeam_topology_tools(layout, context, active_mesh)
        draw_jbeam_selected_elements(layout, context)
        draw_guid_map_inspector(layout, context)


class VIEW3D_PT_beamng_jbeam_health(Panel):
    bl_label = "JBeam Health"
    bl_idname = "VIEW3D_PT_beamng_jbeam_health"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "JBeam Health"

    def draw(self, context):
        layout = self.layout
        counts = jbeam_counts_for_panel(context)
        box = layout.box()
        box.label(text="Health Summary")
        box.label(text=f"Proxy drift: {counts['proxy_drift']}")
        box.label(text=f"Missing refs: {counts['missing_refs']}", icon="ERROR" if counts["missing_refs"] else "NONE")
        box.label(text=f"Dirty params: {counts['dirty_params']}")
        row = box.row(align=True)
        row.operator(BEAMNG_OT_check_experimental_jbeam_topology_health.bl_idname, text="Check Health")
        op = row.operator(BEAMNG_OT_scan_experimental_jbeam_mesh_edits.bl_idname, text="Scan All")
        op.active_only = False
        validation_box = layout.box()
        validation_box.label(text="Active Mesh Live Check")
        for level, message in active_experimental_mesh_validation_summary(context):
            row = validation_box.row()
            row.alert = level == "ERROR"
            row.label(text=f"{level}: {message}")


class VIEW3D_PT_beamng_jbeam_workflow(Panel):
    bl_label = "JBeam Workflow"
    bl_idname = "VIEW3D_PT_beamng_jbeam_workflow"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "JBeam Workflow"

    def draw(self, context):
        draw_authoring_workflow_panel(self.layout, context)


def draw_jbeam_export_mod_target_controls(layout, context, *, compact=False):
    target_box = layout if compact else layout.box()
    target_box.label(text="Working Mod Target", icon="FILE_FOLDER")
    target_box.label(text=f"Selected: {jbeam_export_mod_name(context)}")
    target_box.label(text="JBeam/DAE target: current/mods/unpacked/<mod>/vehicles")

    current_folder = user_current_folder_from_preferences(context)
    if current_folder is None:
        target_box.label(text="Set BeamNG user folder in preferences first", icon="ERROR")
    else:
        unpacked_root = current_folder / "mods" / "unpacked"
        target_box.label(text=f"Unpacked mods: {unpacked_root}")

    target_box.operator(BEAMNG_OT_create_jbeam_export_mod_folder.bl_idname, text="Create/Verify Selected Mod")

    mod_names = existing_unpacked_mod_names(context)
    if mod_names:
        if not compact:
            target_box.label(text="Existing unpacked mods")
        for mod_name in mod_names[:8]:
            row = target_box.row(align=True)
            row.label(text=mod_name, icon="FILE_FOLDER")
            op = row.operator(BEAMNG_OT_set_jbeam_export_mod_folder.bl_idname, text="Use")
            op.mod_name = mod_name
        if len(mod_names) > 8:
            target_box.label(text=f"{len(mod_names) - 8} more mod folder(s) hidden; use preferences for exact name.")
    elif current_folder is not None:
        target_box.label(text="No existing unpacked mod folders found.")


def draw_authoring_workflow_panel(layout, context):
    counts = jbeam_counts_for_panel(context)
    part_count = len(experimental_jbeam_part_objects(context.scene))
    slot_count = len(getattr(context.scene, "beamng_slot_editor_items", []) or [])
    active_name = str(context.scene.get("beamng_active_jbeam_part_name", "") or "")

    box = layout.box()
    box.label(text="Authoring Workflow", icon="MODIFIER")
    box.label(text=f"Active: {active_name or '(none)'} / parts: {part_count} / slots: {slot_count}")
    box.label(text=f"Pending: {counts['pending']} / accepted: {counts['history']}")
    row = box.row(align=True)
    row.operator(IMPORT_OT_beamng_jbeam_topology.bl_idname, text="Import JBeam")
    row.operator(BEAMNG_OT_set_active_jbeam_part_from_selection.bl_idname, text="Use Selected Part")
    row = box.row(align=True)
    row.operator(BEAMNG_OT_validate_jbeam_assembly.bl_idname, text="Validate Assembly")
    row.operator(BEAMNG_OT_write_authoring_workflow_report.bl_idname, text="Workflow Report")
    row = box.row(align=True)
    row.operator(BEAMNG_OT_write_slot_authoring_report.bl_idname, text="Slot Report")
    row.operator(BEAMNG_OT_write_jbeam_edit_preview.bl_idname, text="Semantic Edit Preview")

    flow_box = layout.box()
    flow_box.label(text="Milestones 1-7")
    for label, state, icon in (
        ("1 UI workflow: central authoring/status panel", "in place", "CHECKMARK"),
        ("2 Slot authoring: choice/apply/save plus metadata draft", "partial", "INFO"),
        ("3 Property inheritance: effective selected params shown", "partial", "INFO"),
        ("4 Export review: validate/select/stage/quick export", "in place", "CHECKMARK"),
        ("5 Validation: health, assembly, export preflight", "in place", "CHECKMARK"),
        ("6 Non-core semantics: visible but not full authoring", "planned", "FILE_TEXT"),
        ("7 Refactor spine: risk documented, pending extraction", "planned", "FILE_TEXT"),
    ):
        row = flow_box.row(align=True)
        row.label(text=label, icon=icon)
        row.label(text=state)

    export_box = layout.box()
    export_box.label(text="Review / Export")
    draw_jbeam_export_mod_target_controls(export_box, context, compact=True)
    row = export_box.row(align=True)
    row.enabled = counts["history"] > 0
    row.operator(BEAMNG_OT_validate_jbeam_export.bl_idname, text="Validate")
    row.operator(BEAMNG_OT_review_jbeam_export.bl_idname, text="Review")
    row = export_box.row(align=True)
    row.enabled = counts["history"] > 0
    row.operator(BEAMNG_OT_stage_jbeam_user_override_copies.bl_idname, text="Stage New")
    row = export_box.row(align=True)
    row.enabled = counts["history"] > 0
    row.operator(BEAMNG_OT_quick_export_jbeam_node_moves.bl_idname, text="Quick Export")

    next_box = layout.box()
    next_box.label(text="Known Remaining Product Work")
    next_box.label(text="Slot type/default/child-slot editor")
    next_box.label(text="Hydro/slider/rail/prop/flexbody authoring")
    next_box.label(text="Richer semantic diff and verified export flow")
    next_box.label(text="Module extraction from __init__.py")


class VIEW3D_PT_beamng_jbeam_export(Panel):
    bl_label = "JBeam Export"
    bl_idname = "VIEW3D_PT_beamng_jbeam_export"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "JBeam Export"

    def draw(self, context):
        layout = self.layout
        history_count = int(context.scene.get("beamng_jbeam_operation_history_count", 0))
        draw_jbeam_export_mod_target_controls(layout, context)
        row = layout.row(align=True)
        row.enabled = history_count > 0
        row.operator(BEAMNG_OT_validate_jbeam_export.bl_idname, text="Validate")
        row.operator(BEAMNG_OT_review_jbeam_export.bl_idname, text="Review")
        row = layout.row(align=True)
        row.enabled = history_count > 0
        row.operator(BEAMNG_OT_stage_jbeam_user_override_copies.bl_idname, text="Stage New")
        row.operator(BEAMNG_OT_quick_export_jbeam_node_moves.bl_idname, text="Quick Export")
        last_validation_path = context.scene.get("beamng_jbeam_last_export_validation_path", "")
        if last_validation_path:
            layout.label(text=f"Last validation: {context.scene.get('beamng_jbeam_last_export_validation_status', '')} / {Path(last_validation_path).name}")
        last_stage_manifest_path = context.scene.get("beamng_jbeam_last_mod_override_stage_manifest_path", context.scene.get("beamng_jbeam_last_user_override_stage_manifest_path", ""))
        if last_stage_manifest_path:
            layout.label(text=f"Last stage: {Path(last_stage_manifest_path).name}")


class VIEW3D_PT_beamng_advanced(Panel):
    bl_label = "BeamNG Advanced"
    bl_idname = "VIEW3D_PT_beamng_advanced"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BeamNG Advanced"

    def draw(self, context):
        layout = self.layout
        row = layout.row(align=True)
        row.operator(BEAMNG_OT_setup_split_prop_flexbody_views.bl_idname, text="Split Props/Flex")
        row.operator(BEAMNG_OT_exit_split_local_views.bl_idname, text="Exit Split")
        layout.operator(BEAMNG_OT_show_all_jbeams.bl_idname, text="Show All JBeams")
        layout.operator(BEAMNG_OT_hide_selected_jbeam_items.bl_idname, text="Hide Selected JBeam")
        for visual_group, label in (("triangles", "Triangles"), ("hydros", "Hydros"), ("sliders", "Sliders")):
            row = layout.row(align=True)
            op = row.operator(BEAMNG_OT_set_jbeam_visual_visibility.bl_idname, text=f"Show {label}")
            op.visual_group = visual_group
            op.action = "show"
            op = row.operator(BEAMNG_OT_set_jbeam_visual_visibility.bl_idname, text=f"Hide {label}")
            op.visual_group = visual_group
            op.action = "hide"
        draw_selected_jbeam_legacy_info(layout, context)
        layout.separator()
        layout.operator(BEAMNG_OT_toggle_relationship_lines.bl_idname, text="Toggle Parent Lines")
        layout.operator(BEAMNG_OT_print_prop_transforms.bl_idname, text="Write Prop Debug File")

        box = layout.box()
        box.label(text="JBeam Session")
        precision = jbeam_project_position_precision(context)
        box.label(text=f"Project/export precision: {precision} decimal place(s)", icon="INFO")
        box.label(text="Import preserves higher source precision; normalization will be explicit.")
        pending_count = int(context.scene.get("beamng_jbeam_pending_node_move_count", 0))
        pending_topology_count = int(context.scene.get("beamng_jbeam_pending_topology_change_count", 0))
        restored_count = int(context.scene.get("beamng_jbeam_restored_proxy_move_count", 0))
        synced_proxy_count = int(context.scene.get("beamng_jbeam_synced_proxy_node_count", 0))
        removed_proxy_count = int(context.scene.get("beamng_jbeam_removed_proxy_node_count", 0))
        proxy_sync_message = context.scene.get("beamng_jbeam_last_proxy_sync_message", "")
        auto_scan_message = context.scene.get("beamng_jbeam_last_auto_scan_message", "")
        model_operation_count = int(context.scene.get("beamng_authoring_model_operation_count", 0))
        health_proxy_drift = int(context.scene.get("beamng_jbeam_health_proxy_drift_count", 0))
        health_missing_refs = int(context.scene.get("beamng_jbeam_health_missing_reference_count", 0))
        health_dirty_params = int(context.scene.get("beamng_jbeam_health_dirty_param_count", 0))
        history_count = int(context.scene.get("beamng_jbeam_operation_history_count", 0))
        history_breakdown = jbeam_history_counts(jbeam_operation_history(context.scene))
        checkpoint_count = int(context.scene.get("beamng_jbeam_export_checkpoint_count", 0))
        box.label(text=f"Pending edits: {pending_count}")
        if pending_topology_count:
            box.label(text=f"Pending topology edits: {pending_topology_count}")
        box.label(text=f"Accepted operations: {history_count}")
        if model_operation_count != history_count:
            box.label(text=f"Authoring model ops: {model_operation_count} (refresh on accept/clear)", icon="INFO")
        if history_breakdown.get("beams") or history_breakdown.get("triangles"):
            box.label(
                text=(
                    f"Accepted topology: {history_breakdown.get('beams', 0)} beam, "
                    f"{history_breakdown.get('triangles', 0)} triangle"
                )
            )
        if checkpoint_count:
            box.label(text=f"Export checkpoints: {checkpoint_count}")
        if restored_count:
            box.label(text=f"Proxy moves restored: {restored_count}")
        if synced_proxy_count:
            box.label(text=f"Proxy nodes synced: {synced_proxy_count}")
        if removed_proxy_count:
            box.label(text=f"Proxy nodes removed: {removed_proxy_count}")
        if proxy_sync_message:
            box.label(text=str(proxy_sync_message), icon="INFO")
        if auto_scan_message:
            box.label(text=str(auto_scan_message), icon="FILE_REFRESH")
        if health_proxy_drift or health_missing_refs or health_dirty_params:
            box.label(
                text=(
                    f"Health: proxy drift {health_proxy_drift}, "
                    f"missing refs {health_missing_refs}, dirty params {health_dirty_params}"
                ),
                icon="ERROR" if health_missing_refs else "INFO",
            )
        row = box.row(align=True)
        op = row.operator(BEAMNG_OT_scan_experimental_jbeam_mesh_edits.bl_idname, text="Scan Active")
        op.active_only = True
        op = row.operator(BEAMNG_OT_scan_experimental_jbeam_mesh_edits.bl_idname, text="Scan All")
        op.active_only = False
        row.operator(BEAMNG_OT_check_experimental_jbeam_topology_health.bl_idname, text="Health")
        row = box.row(align=True)
        row.enabled = pending_count > 0
        row.operator(BEAMNG_OT_accept_experimental_jbeam_node_moves.bl_idname, text="Accept Pending Edits")
        row = box.row(align=True)
        row.enabled = pending_count > 0 or history_count > 0
        row.operator(BEAMNG_OT_clear_jbeam_edit_session.bl_idname, text="Discard Session Edits")

        box.separator()
        draw_jbeam_export_mod_target_controls(box, context, compact=True)
        last_mod_path = context.scene.get("beamng_jbeam_last_export_mod_folder_path", "")
        if last_mod_path:
            box.label(text=f"Export folder: {Path(last_mod_path).name}")
        row = box.row(align=True)
        row.enabled = history_count > 0
        row.operator(BEAMNG_OT_validate_jbeam_export.bl_idname, text="Validate Export")
        row = box.row(align=True)
        row.enabled = history_count > 0
        row.operator(BEAMNG_OT_stage_jbeam_user_override_copies.bl_idname, text="Stage New Files")
        row = box.row(align=True)
        row.operator(BEAMNG_OT_quick_export_jbeam_node_moves.bl_idname, text="Quick Export Selected")
        last_validation_path = context.scene.get("beamng_jbeam_last_export_validation_path", "")
        if last_validation_path:
            status = context.scene.get("beamng_jbeam_last_export_validation_status", "")
            box.label(text=f"Last validation: {status} / {Path(last_validation_path).name}")
        last_stage_manifest_path = context.scene.get(
            "beamng_jbeam_last_mod_override_stage_manifest_path",
            context.scene.get("beamng_jbeam_last_user_override_stage_manifest_path", ""),
        )
        if last_stage_manifest_path:
            box.label(text=f"Last mod stage: {Path(last_stage_manifest_path).name}")
        last_checkpoint_path = context.scene.get("beamng_jbeam_last_export_checkpoint_path", "")
        if last_checkpoint_path:
            box.label(text=f"Last checkpoint: {Path(last_checkpoint_path).name}")

        active_mesh = active_experimental_jbeam_mesh(context)
        model_box = box.box()
        model_box.label(text="Authoring Model")
        model_box.label(
            text=(
                f"Nodes {context.scene.get('beamng_authoring_model_node_count', 0)} / "
                f"Beams {context.scene.get('beamng_authoring_model_beam_count', 0)} / "
                f"Triangles {context.scene.get('beamng_authoring_model_triangle_count', 0)} / "
                f"Ops {context.scene.get('beamng_authoring_model_operation_count', 0)}"
            )
        )
        generated_at = context.scene.get("beamng_authoring_model_generated_at", "")
        if generated_at:
            model_box.label(text=f"Snapshot: {generated_at}")
        if active_mesh and active_mesh.get("beamng_visual_type") == "experimental_jbeam_mesh":
            box.label(text=f"Active: {active_mesh.get('beamng_part_name', '')}")
            box.label(text=f"Object: {active_mesh.name}")
            box.label(text=f"Mode: {active_mesh.mode}")
            box.label(text=f"Owned nodes: {active_mesh.get('beamng_owned_node_count', 0)}")
            box.label(text=f"Proxy nodes: {active_mesh.get('beamng_proxy_node_count', 0)}")
            box.label(text=f"Topology revision: {active_mesh.data.get('beamng_topology_revision', 0)}")
            box.label(text=f"Topology delta: {active_mesh.data.get('beamng_semantic_topology_delta_count', 0)}")
            row = box.row(align=True)
            row.operator(BEAMNG_OT_add_standalone_jbeam_node.bl_idname, text="Add Node")
            row.operator(BEAMNG_OT_create_jbeam_beam_from_selected_nodes.bl_idname, text="Beam 2")
            row = box.row(align=True)
            row.operator(BEAMNG_OT_mark_selected_nodes_for_proxy_import.bl_idname, text="Mark Nodes")
            row.operator(BEAMNG_OT_import_marked_nodes_as_proxies.bl_idname, text="Import Marked Nodes")
            row = box.row(align=True)
            op = row.operator(BEAMNG_OT_mark_selected_nodes_for_proxy_import.bl_idname, text="Append Marks")
            op.append = True
            row.operator(BEAMNG_OT_clear_marked_proxy_nodes.bl_idname, text="Clear Marks")
            row = box.row(align=True)
            row.operator(BEAMNG_OT_clear_unused_proxy_nodes.bl_idname, text="Clear Proxies")
            row.operator(BEAMNG_OT_clear_orphan_provisional_nodes.bl_idname, text="Clear Orphans")
            marked_count = int(context.scene.get("beamng_proxy_import_clipboard_count", 0) or 0)
            if marked_count:
                box.label(text=f"Marked proxy nodes: {marked_count}", icon="PINNED")
            row = box.row(align=True)
            row.operator(BEAMNG_OT_create_jbeam_triangle_from_selected_nodes.bl_idname, text="Triangle 3")
            row.operator(BEAMNG_OT_delete_selected_jbeam_elements.bl_idname, text="Delete JBeam")
            row = box.row(align=True)
            op = row.operator(BEAMNG_OT_set_selected_jbeam_edge_semantic.bl_idname, text="Edge=Beam")
            op.semantic_type = JBEAM_EDGE_SEMANTIC_BEAM
            op = row.operator(BEAMNG_OT_set_selected_jbeam_edge_semantic.bl_idname, text="Edge=Boundary")
            op.semantic_type = JBEAM_EDGE_SEMANTIC_TRIANGLE_BOUNDARY
            op = row.operator(BEAMNG_OT_set_selected_jbeam_edge_semantic.bl_idname, text="Edge=Relation")
            op.semantic_type = JBEAM_EDGE_SEMANTIC_RELATIONSHIP
            row = box.row(align=True)
            row.operator(BEAMNG_OT_report_experimental_jbeam_selection.bl_idname, text="Report Selection")
            row = box.row(align=True)
            row.operator(BEAMNG_OT_triangulate_selected_jbeam_faces.bl_idname, text="Triangulate Faces")
            row.operator(BEAMNG_OT_flip_selected_jbeam_triangles.bl_idname, text="Flip Triangles")
            row = box.row(align=True)
            row.operator(BEAMNG_OT_repair_experimental_jbeam_topology_uids.bl_idname, text="Repair / Report UIDs")
            row.operator(BEAMNG_OT_write_semantic_topology_snapshot.bl_idname, text="Semantic Snapshot")
            validation_box = box.box()
            validation_box.label(text="Live Topology Check")
            for level, message in active_experimental_mesh_validation_summary(context):
                row = validation_box.row()
                row.alert = level == "ERROR"
                row.label(text=f"{level}: {message}")
            selected_nodes = experimental_jbeam_node_info_for_selection(context)
            selected_edges = experimental_jbeam_edge_info_for_selection(context)
            selected_faces = experimental_jbeam_face_info_for_selection(context)
            if selected_nodes or selected_edges or selected_faces:
                element_box = box.box()
                element_box.label(text="Selected JBeam elements")
            if selected_nodes:
                node_box = element_box.box()
                node_box.label(text=f"Node{'s' if len(selected_nodes) != 1 else ''}")
                for node_info in selected_nodes:
                    row = node_box.row(align=True)
                    row.label(text=f"{node_info['node_id']} ({node_info['kind']})")
                    row.label(text=f"v{node_info['vertex_index']}")
                    if not node_info.get("committed", True):
                        node_box.label(text="Status: new provisional node")
                    node_box.label(text=f"Model-backed: {'yes' if node_info.get('model_backed') else 'no'}")
                    node_box.label(
                        text=(
                            f"References: {node_info.get('model_reference_beam_count', 0)} beam(s), "
                            f"{node_info.get('model_reference_triangle_count', 0)} triangle(s)"
                        )
                    )
                    node_box.label(text=f"Position: {node_info['current_position']}")
                    if node_info["baseline_position"] and node_info["baseline_position"] != node_info["current_position"]:
                        node_box.label(text=f"Baseline: {node_info['baseline_position']}")
                    if node_info["pending_position"]:
                        node_box.label(text=f"Pending move: {node_info['pending_position']}")
                    if node_info["accepted_position"]:
                        node_box.label(text=f"Accepted move: {node_info['accepted_position']}")
                    if node_info["kind"] == "proxy":
                        node_box.label(text=f"Owner part id: {node_info['owner_part_id']}")
                    params = node_info.get("params", {})
                    if params:
                        param_box = node_box.box()
                        param_box.label(text="Effective params")
                        draw_param_summary(param_box, params)
                    elif not node_info.get("committed", True):
                        node_box.label(text="Params: none yet, uses surrounding JBeam context unless set below")
                    else:
                        node_box.label(text="Effective params: none found in source context")
                props_box = node_box.box()
                props_box.label(text="Selected/New Node Parameters")
                props_box.prop(context.scene, "beamng_jbeam_node_weight")
                props_box.prop(context.scene, "beamng_jbeam_node_material")
                props_box.prop(context.scene, "beamng_jbeam_node_group")
                props_box.prop(context.scene, "beamng_jbeam_node_friction")
                props_box.prop(context.scene, "beamng_jbeam_node_collision_override")
                if context.scene.beamng_jbeam_node_collision_override:
                    props_box.prop(context.scene, "beamng_jbeam_node_collision")
                props_box.prop(context.scene, "beamng_jbeam_node_self_collision_override")
                if context.scene.beamng_jbeam_node_self_collision_override:
                    props_box.prop(context.scene, "beamng_jbeam_node_self_collision")
                row = props_box.row(align=True)
                row.operator(BEAMNG_OT_load_selected_jbeam_node_properties.bl_idname, text="Load From Selected")
                row.operator(BEAMNG_OT_apply_selected_jbeam_node_properties.bl_idname, text="Apply To Selected")
            if selected_edges:
                edge_box = element_box.box()
                edge_box.label(text=f"Selected edge{'s' if len(selected_edges) != 1 else ''}")
                for edge_info in selected_edges:
                    edge_box.label(
                        text=f"e{edge_info['edge_index']}: {edge_info['id1']} -> {edge_info['id2']}"
                    )
                    edge_box.label(
                        text=(
                            f"Semantic: {edge_info.get('semantic_type', JBEAM_EDGE_SEMANTIC_RELATIONSHIP)} "
                            f"({edge_info.get('semantic_state', '')})"
                        )
                    )
                    edge_box.label(text=f"Model-backed: {'yes' if edge_info.get('model_backed') else 'no'}")
                    params = edge_info.get("params", {})
                    param_box = edge_box.box()
                    param_box.label(text="Effective params")
                    draw_param_summary(param_box, params, "none found in source context")
                edge_box.label(text="New triangle boundary edges are not beams unless marked.")
                beam_props_box = edge_box.box()
                beam_props_box.label(text="Selected/New Beam Parameters")
                beam_props_box.prop(context.scene, "beamng_jbeam_beam_spring")
                beam_props_box.prop(context.scene, "beamng_jbeam_beam_damp")
                beam_props_box.prop(context.scene, "beamng_jbeam_beam_deform")
                beam_props_box.prop(context.scene, "beamng_jbeam_beam_strength")
                beam_props_box.prop(context.scene, "beamng_jbeam_beam_precompression")
                beam_props_box.prop(context.scene, "beamng_jbeam_beam_type")
                beam_props_box.prop(context.scene, "beamng_jbeam_beam_break_group")
                row = beam_props_box.row(align=True)
                row.operator(BEAMNG_OT_load_selected_jbeam_beam_properties.bl_idname, text="Load From Selected")
                row.operator(BEAMNG_OT_apply_selected_jbeam_beam_properties.bl_idname, text="Apply To Selected")
            if selected_faces:
                face_box = element_box.box()
                face_box.label(text=f"Collision triangle{'s' if len(selected_faces) != 1 else ''}")
                for face_info in selected_faces:
                    face_box.label(
                        text=(
                            f"f{face_info['face_index']}: "
                            f"{face_info['id1']} -> {face_info['id2']} -> {face_info['id3']}"
                        )
                    )
                    if face_info.get("normal"):
                        face_box.label(text=f"Normal: {face_info['normal']}")
                    face_box.label(text=f"Model-backed: {'yes' if face_info.get('model_backed') else 'no'}")
                    params = face_info.get("params", {})
                    param_box = face_box.box()
                    param_box.label(text="Effective params")
                    draw_param_summary(param_box, params, "none found in source context")
                face_box.label(text="Node order is collision winding; preserve unless intentional.")
                tri_props_box = face_box.box()
                tri_props_box.label(text="Selected/New Triangle Parameters")
                tri_props_box.prop(context.scene, "beamng_jbeam_triangle_group")
                tri_props_box.prop(context.scene, "beamng_jbeam_triangle_drag_coef")
                tri_props_box.prop(context.scene, "beamng_jbeam_triangle_ground_model")
                tri_props_box.prop(context.scene, "beamng_jbeam_triangle_collision_override")
                if context.scene.beamng_jbeam_triangle_collision_override:
                    tri_props_box.prop(context.scene, "beamng_jbeam_triangle_collision")
                row = tri_props_box.row(align=True)
                row.operator(BEAMNG_OT_load_selected_jbeam_triangle_properties.bl_idname, text="Load From Selected")
                row.operator(BEAMNG_OT_apply_selected_jbeam_triangle_properties.bl_idname, text="Apply To Selected")
            if not selected_nodes and not selected_edges and not selected_faces:
                box.label(text="Select JBeam mesh vertices/edges/faces to inspect elements.")
                box.label(text=f"Debug: {active_object_debug_label(context)}")
            legend_box = box.box()
            legend_box.label(text="Viewport Semantic Legend")
            legend_box.label(text="Green: exported JBeam beam")
            legend_box.label(text="Amber: triangle boundary helper edge")
            legend_box.label(text="Grey: non-exporting relationship/helper edge")
            legend_box.label(text="Orange cross: proxy/reference node")
        else:
            box.label(text="No experimental JBeam mesh is active.")
            box.label(text=f"Debug: {active_object_debug_label(context)}")

        layout.separator()
        draw_vehicle_slot_editor(layout, context)

        layout.separator()
        active = selected_active_object(context)
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
    auto_sync_proxy_nodes: BoolProperty(
        name="Auto Restore/Sync Proxy Nodes",
        description=(
            "After JBeam mesh movement settles, restore moved proxy/reference nodes and sync matching proxies "
            "when owned nodes move. Disable for very large vehicles if editing feels slow"
        ),
        default=True,
    )
    auto_scan_jbeam_edits: BoolProperty(
        name="Auto Scan JBeam Edits",
        description=(
            "After experimental JBeam mesh edits settle, automatically run Scan All so pending node, beam, "
            "triangle, and proxy-delete changes stay current without pressing the button"
        ),
        default=True,
    )
    show_proxy_node_overlay: BoolProperty(
        name="Show Proxy Node Crosses",
        description="Draw proxy/reference JBeam nodes as orange crosses in the 3D View",
        default=True,
    )
    show_jbeam_semantic_overlay: BoolProperty(
        name="Show JBeam Semantic Colors",
        description="Draw colored overlay points/edges for owned nodes, proxy nodes, beams, triangle boundaries, and relationship edges",
        default=True,
    )
    jbeam_position_precision: IntProperty(
        name="JBeam Project Decimal Places",
        description=(
            "Default decimal places for newly authored/exported JBeam coordinates. Imports preserve higher source "
            "precision and can raise this setting when accepted"
        ),
        default=JBEAM_POSITION_PRECISION,
        min=0,
        max=12,
    )
    jbeam_export_mod_name: StringProperty(
        name="JBeam Export Mod Folder",
        description="Folder under BeamNG user current/mods/unpacked used for staged JBeam overrides",
        default=DEFAULT_JBEAM_EXPORT_MOD_NAME,
    )

    def draw(self, _context):
        layout = self.layout
        layout.label(text="BeamNG Asset Roots")
        layout.prop(self, "beamng_user_folder")
        layout.prop(self, "vanilla_vehicles_folder")
        layout.prop(self, "cache_asset_catalogs")
        layout.prop(self, "auto_sync_proxy_nodes")
        layout.prop(self, "auto_scan_jbeam_edits")
        layout.prop(self, "show_proxy_node_overlay")
        layout.prop(self, "show_jbeam_semantic_overlay")
        layout.prop(self, "jbeam_position_precision")
        layout.prop(self, "jbeam_export_mod_name")


def get_addon_preferences(context):
    addon = context.preferences.addons.get(__name__)
    return addon.preferences if addon else None


def write_import_report(lines):
    text = bpy.data.texts.get("BeamNG Import Report") or bpy.data.texts.new("BeamNG Import Report")
    text.clear()
    text.write("\n".join(str(line) for line in lines))
    text.write("\n")
    return text


def write_resolved_vehicle_model_report(model):
    text = bpy.data.texts.get("BeamNG Resolved Vehicle Model") or bpy.data.texts.new(
        "BeamNG Resolved Vehicle Model"
    )
    text.clear()
    text.write("\n".join(resolved_vehicle_model_report_lines(model)))
    text.write("\n")
    return text


def write_authoring_model_report(model):
    text = bpy.data.texts.get("BeamNG Authoring Model") or bpy.data.texts.new("BeamNG Authoring Model")
    text.clear()
    text.write("\n".join(authoring_model_report_lines(model)))
    text.write("\n")
    return text


def store_authoring_model_snapshot(scene, model, root_collection=None):
    snapshot = model.to_json()
    scene["beamng_authoring_model_json"] = snapshot
    scene["beamng_authoring_model_generated_at"] = model.generated_at
    scene["beamng_authoring_model_node_count"] = len(model.nodes)
    scene["beamng_authoring_model_beam_count"] = len(model.beams)
    scene["beamng_authoring_model_triangle_count"] = len(model.triangles)
    scene["beamng_authoring_model_operation_count"] = len(model.operations)
    if root_collection is not None:
        root_collection["beamng_authoring_model_json"] = snapshot
        root_collection["beamng_authoring_model_generated_at"] = model.generated_at


def current_authoring_model(scene):
    try:
        text = scene.get("beamng_authoring_model_json", "")
        return ResolvedVehicleAuthoringModel.from_json(text) if text else None
    except Exception:
        return None


def refresh_authoring_model_operations_from_history(scene):
    model = current_authoring_model(scene)
    if model is None:
        return None
    operations = []
    for operation in raw_jbeam_operation_history(scene):
        operations.append(
            EditOperation(
                operation=str(operation.get("operation", "")),
                file=str(operation.get("file", "")),
                part=str(operation.get("part", "")),
                section=str(operation.get("section", "")),
                row=str(operation.get("row", operation.get("node", ""))),
                field=str(operation.get("field", "")),
                old=operation.get("old"),
                new=operation.get("new"),
                status=str(operation.get("status", "accepted")),
                created_at=str(operation.get("accepted_at", "")),
                source_object=str(operation.get("source_object", "")),
                vertex_index=int(operation.get("vertex_index", -1)) if str(operation.get("vertex_index", "-1")).lstrip("-").isdigit() else -1,
                resolved_part_id=int(operation.get("resolved_part_id", -1)) if str(operation.get("resolved_part_id", "-1")).lstrip("-").isdigit() else -1,
                owner_resolved_part_id=(
                    int(operation.get("owner_resolved_part_id", -1))
                    if str(operation.get("owner_resolved_part_id", "-1")).lstrip("-").isdigit()
                    else -1
                ),
                params=operation.get("params", {}) if isinstance(operation.get("params", {}), dict) else {},
                nodes=list(operation.get("nodes", [])) if isinstance(operation.get("nodes", []), (list, tuple)) else [],
                reason=str(operation.get("reason", "")),
            )
        )
    model.operations = operations
    store_authoring_model_snapshot(scene, model)
    write_authoring_model_report(model)
    return model


def import_beamng_pc_path(
    context,
    operator,
    pc_path: Path,
    clear_existing=True,
    include_jbeam_visuals=True,
    selectable_jbeam_debug=False,
    show_jbeam_node_labels=False,
    create_experimental_jbeam_meshes_option=False,
    source_description="",
    source_asset=None,
    include_user_overrides=True,
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
    context.scene["beamng_import_include_user_overrides"] = include_user_overrides
    reset_jbeam_edit_session(context.scene)
    report_lines.append("JBeam edit session reset for this import.")

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
            include_user_overrides,
        )
        report_lines.extend(
            [
                f"Vehicle folder inferred as: {pc_path.parent.name}",
                f"User folder preference: {beamng_user_folder or '(not set)'}",
                f"Vanilla vehicles preference: {vanilla_vehicles_folder or '(not set)'}",
                f"User mods/overrides in resolver: {'enabled' if include_user_overrides else 'ignored'}",
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
        resolved_vehicle_model = build_resolved_vehicle_model(
            pc_path,
            pc_data,
            source_description,
            main_part_name,
            resolved_parts,
            flexbodies,
            visual_nodes,
            visual_beams,
            visual_triangles,
            visual_hydros,
            visual_rails,
            visual_slidenodes,
        )
        write_resolved_vehicle_model_report(resolved_vehicle_model)
        report_lines.append("Resolved vehicle model report: BeamNG Resolved Vehicle Model")
        authoring_model = build_authoring_model_from_import(
            pc_path,
            pc_data,
            source_description,
            main_part_name,
            resolved_parts,
            visual_nodes,
            visual_beams,
            visual_triangles,
            operations=[],
        )
        store_authoring_model_snapshot(context.scene, authoring_model)
        write_authoring_model_report(authoring_model)
        report_lines.append("Authoring model snapshot: BeamNG Authoring Model")
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
        root_collection["beamng_resolved_node_owner_part_ids"] = json.dumps(
            {
                node_id: list(part_ids)
                for node_id, part_ids in resolved_vehicle_model.node_owner_part_ids.items()
            },
            sort_keys=True,
        )
        root_collection["beamng_resolved_part_external_node_refs"] = json.dumps(
            {
                str(part_id): list(node_ids)
                for part_id, node_ids in resolved_vehicle_model.part_external_node_refs.items()
            },
            sort_keys=True,
        )
        store_authoring_model_snapshot(context.scene, authoring_model, root_collection)
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
        experimental_mesh_count = 0
        if create_experimental_jbeam_meshes_option:
            experimental_mesh_count = create_experimental_jbeam_meshes(
                visual_nodes,
                visual_beams,
                visual_triangles,
                root_collection,
                resolved_parts,
            )
            report_lines.append(f"Experimental JBeam mesh parts: {experimental_mesh_count}")
            report_lines.append(
                f"Experimental JBeam mesh node positions preserve source coordinates on import. "
                f"Project/export precision is {jbeam_project_position_precision(context)} decimal places."
            )
            operator.report(
                {"INFO"},
                f"Experimental JBeam mesh node positions preserve source precision; export precision {jbeam_project_position_precision(context)}",
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
    create_experimental_jbeam_meshes: BoolProperty(
        name="Experimental JBeam Meshes",
        description="Create one editable Blender mesh per resolved JBeam part using nodes as vertices, beams as edges, and triangles as faces",
        default=False,
    )
    include_user_overrides: BoolProperty(
        name="Use User Mods/Overrides",
        description="Resolve JBeam/DAE data from BeamNG user mods as well as vanilla sources",
        default=True,
    )
    vanilla_data_only: BoolProperty(
        name="Vanilla Data Only (No Mods/Overrides)",
        description="Ignore user mods while resolving JBeam/DAE data",
        default=False,
    )

    def draw(self, _context):
        layout = self.layout
        layout.prop(self, "clear_existing")
        layout.prop(self, "include_jbeam_visuals")
        layout.prop(self, "selectable_jbeam_debug")
        layout.prop(self, "show_jbeam_node_labels")
        layout.prop(self, "create_experimental_jbeam_meshes")
        layout.prop(self, "vanilla_data_only")
        if not self.vanilla_data_only:
            layout.prop(self, "include_user_overrides")

    def execute(self, context):
        filepath = Path(self.filepath)
        context.scene["beamng_slot_editor_source_pc_path"] = str(filepath)
        context.scene["beamng_slot_editor_source_virtual_path"] = normalize_virtual_path(Path("vehicles") / filepath.parent.name / filepath.name)
        context.scene["beamng_slot_editor_source_asset_type"] = "file"
        context.scene["beamng_slot_editor_source_label_prefix"] = ""
        context.scene["beamng_slot_editor_source_zip_path"] = ""
        context.scene["beamng_slot_editor_source_zip_entry"] = ""
        include_user_overrides = bool(self.include_user_overrides and not self.vanilla_data_only)
        context.scene["beamng_import_include_user_overrides"] = include_user_overrides
        return import_beamng_pc_path(
            context,
            self,
            filepath,
            self.clear_existing,
            self.include_jbeam_visuals,
            self.selectable_jbeam_debug,
            self.show_jbeam_node_labels,
            self.create_experimental_jbeam_meshes,
            str(filepath),
            None,
            include_user_overrides,
        )


JBEAM_TOPOLOGY_IMPORT_PART_ITEMS_CACHE = {}


def jbeam_topology_import_part_names(filepath):
    path = Path(filepath)
    if not path.exists():
        return []
    try:
        stat = path.stat()
        cache_key = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        cache_key = (str(path), 0, 0)
    cached = JBEAM_TOPOLOGY_IMPORT_PART_ITEMS_CACHE.get(cache_key)
    if cached is not None:
        return cached
    try:
        imported = import_jbeam_topology_subset(path)
    except Exception:
        return []
    names = [str(part.part_name) for part in imported.parts if str(part.part_name)]
    JBEAM_TOPOLOGY_IMPORT_PART_ITEMS_CACHE.clear()
    JBEAM_TOPOLOGY_IMPORT_PART_ITEMS_CACHE[cache_key] = names
    return names


def jbeam_topology_import_part_items(self, _context):
    names = jbeam_topology_import_part_names(getattr(self, "filepath", ""))
    items = [("__ALL__", "All parts", "Import every supported part from this JBeam file")]
    for index, name in enumerate(names):
        items.append((f"PART_{index}", name, f"Import only {name}"))
    return items


def jbeam_import_diagnostic_key(diagnostic):
    return (
        str(getattr(diagnostic, "level", "")),
        str(getattr(diagnostic, "code", "")),
        str(getattr(diagnostic, "part_name", "")),
        str(getattr(diagnostic, "section", "")),
        str(getattr(diagnostic, "message", "")),
    )


def filtered_jbeam_import_diagnostics(diagnostics, selected_part_name=""):
    if not selected_part_name:
        return list(diagnostics)
    filtered = []
    seen = set()
    for diagnostic in diagnostics:
        part_name = str(getattr(diagnostic, "part_name", ""))
        if part_name not in {"", selected_part_name}:
            continue
        key = jbeam_import_diagnostic_key(diagnostic)
        if key in seen:
            continue
        seen.add(key)
        filtered.append(diagnostic)
    return filtered


def jbeam_import_diagnostic_counts(diagnostics):
    counts = defaultdict(int)
    for diagnostic in diagnostics:
        level = str(getattr(diagnostic, "level", "") or "unknown")
        code = str(getattr(diagnostic, "code", "") or "unknown")
        counts[(level, code)] += 1
    return counts


def max_precision_from_jbeam_import_diagnostics(diagnostics):
    max_precision = 0
    for diagnostic in diagnostics:
        if str(getattr(diagnostic, "code", "")) != "node_precision_exceeds_project":
            continue
        match = re.search(r"uses\s+(\d+)\s+decimal", str(getattr(diagnostic, "message", "")))
        if match:
            max_precision = max(max_precision, int(match.group(1)))
    return max_precision


def write_jbeam_import_diagnostic_log(text, diagnostics, sample_limit=8):
    counts = jbeam_import_diagnostic_counts(diagnostics)
    text.write("\nDiagnostic summary:\n")
    if not counts:
        text.write("- none\n")
        return
    for (level, code), count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        text.write(f"- {level}: {code}: {count}\n")

    text.write("\nDiagnostic samples:\n")
    shown_by_code = defaultdict(int)
    for diagnostic in diagnostics:
        key = (str(getattr(diagnostic, "level", "")), str(getattr(diagnostic, "code", "")))
        if shown_by_code[key] >= sample_limit:
            continue
        shown_by_code[key] += 1
        text.write(
            f"- {getattr(diagnostic, 'level', '')}: "
            f"{getattr(diagnostic, 'code', '')}: "
            f"part={getattr(diagnostic, 'part_name', '')} "
            f"section={getattr(diagnostic, 'section', '')}: "
            f"{getattr(diagnostic, 'message', diagnostic)}\n"
        )


def filter_imported_jbeam_topology_parts(imported, selection):
    if selection == "__ALL__":
        return imported, ""
    names = [str(part.part_name) for part in imported.parts]
    if not str(selection).startswith("PART_"):
        return imported, ""
    try:
        index = int(str(selection).split("_", 1)[1])
    except (IndexError, ValueError):
        return imported, "Invalid JBeam part selection"
    if index < 0 or index >= len(imported.parts):
        return imported, "Selected JBeam part no longer exists"
    imported.parts = [imported.parts[index]]
    imported.diagnostics = filtered_jbeam_import_diagnostics(imported.diagnostics, names[index])
    return imported, names[index]


class IMPORT_OT_beamng_jbeam_topology(Operator, ImportHelper):
    bl_idname = "import_scene.beamng_jbeam_topology"
    bl_label = "Import BeamNG JBeam Topology"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".jbeam"
    filter_glob: StringProperty(default="*.jbeam", options={"HIDDEN"})
    clear_existing: BoolProperty(
        name="Clear Existing Direct JBeam Imports",
        description="Remove previous direct JBeam topology import collections before importing",
        default=False,
    )
    part_selection: EnumProperty(
        name="Part",
        description="Choose one JBeam part to import, or import all parts in the file",
        items=jbeam_topology_import_part_items,
    )
    part_selection_confirmed: BoolProperty(default=False, options={"HIDDEN"})
    precision_confirmed: BoolProperty(default=False, options={"HIDDEN"})

    def draw(self, context):
        self.layout.prop(self, "clear_existing")
        names = jbeam_topology_import_part_names(self.filepath)
        if len(names) > 1:
            self.layout.separator()
            self.layout.label(text=f"{len(names)} JBeam parts found")
            self.layout.prop(self, "part_selection")
        elif len(names) == 1:
            self.layout.label(text=f"Part: {names[0]}")
        source_precision = int(context.scene.get("beamng_pending_import_source_precision", 0) or 0)
        project_precision = jbeam_project_position_precision(context)
        if source_precision > project_precision:
            self.layout.separator()
            self.layout.label(text="High precision coordinates found", icon="ERROR")
            self.layout.label(text=f"Source uses up to {source_precision} decimal place(s).")
            self.layout.label(text=f"Current project setting is {project_precision}.")
            self.layout.label(text="OK imports and raises project precision to match.")

    def invoke(self, context, event):
        self.part_selection_confirmed = False
        self.precision_confirmed = False
        self.part_selection = "__ALL__"
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        filepath = Path(self.filepath)
        if not filepath.exists():
            self.report({"ERROR"}, f"JBeam file does not exist: {filepath}")
            return {"CANCELLED"}

        part_names = jbeam_topology_import_part_names(filepath)
        if len(part_names) > 1 and not self.part_selection_confirmed:
            self.part_selection_confirmed = True
            return context.window_manager.invoke_props_dialog(self, width=420)

        imported = import_jbeam_topology_subset(
            filepath,
            coordinate_precision=jbeam_project_position_precision(context),
        )
        imported, selected_part_name = filter_imported_jbeam_topology_parts(imported, self.part_selection)
        if selected_part_name:
            context.scene["beamng_last_direct_jbeam_import_part_filter"] = selected_part_name
        else:
            context.scene["beamng_last_direct_jbeam_import_part_filter"] = "All parts"

        source_precision = max_precision_from_jbeam_import_diagnostics(imported.diagnostics)
        project_precision = jbeam_project_position_precision(context)
        context.scene["beamng_pending_import_source_precision"] = source_precision
        if source_precision > project_precision and not self.precision_confirmed:
            self.precision_confirmed = True
            return context.window_manager.invoke_props_dialog(self, width=460)
        if source_precision > project_precision:
            set_jbeam_project_position_precision(context, source_precision)
            self.report({"INFO"}, f"Raised JBeam project precision to {source_precision} decimal places")

        if self.clear_existing:
            collections_to_remove = [
                collection
                for collection in list(bpy.data.collections)
                if collection.get("beamng_visual_type") == "direct_jbeam_topology_import"
            ]
            for collection in collections_to_remove:
                if collection.name not in bpy.data.collections:
                    continue
                remove_collection_tree(collection)
                bpy.data.collections.remove(collection)

        blocking = [
            diagnostic
            for diagnostic in imported.diagnostics
            if getattr(diagnostic, "level", "") == "error"
        ]
        if blocking or not imported.parts:
            message = "; ".join(getattr(diagnostic, "message", str(diagnostic)) for diagnostic in blocking[:3])
            self.report({"ERROR"}, message or "No supported JBeam parts were imported")
            return {"CANCELLED"}

        root = bpy.data.collections.new(f"BeamNG JBeam Import - {filepath.stem}")
        root["beamng_visual_type"] = "direct_jbeam_topology_import"
        root["beamng_source_path"] = str(filepath)
        root["beamng_source_sha256"] = imported.cached_source.get("sha256", "")
        root["beamng_part_filter"] = context.scene["beamng_last_direct_jbeam_import_part_filter"]
        context.scene.collection.children.link(root)

        created = create_imported_jbeam_topology_meshes(imported, root)
        mesh_collection = next(
            (
                collection
                for collection in root.children
                if collection.get("beamng_visual_type") == "imported_jbeam_topology_meshes"
            ),
            root,
        )
        objects = [
            obj
            for obj in context.scene.objects
            if obj.get("beamng_imported_topology_subset")
            and obj.get("beamng_source_sha256") == imported.cached_source.get("sha256", "")
        ]
        for obj in objects:
            obj.select_set(True)
        if objects:
            context.view_layer.objects.active = objects[0]
        context.scene["beamng_last_direct_jbeam_import_path"] = str(filepath)
        context.scene["beamng_last_direct_jbeam_import_part_count"] = len(imported.parts)

        text = bpy.data.texts.get("BeamNG Direct JBeam Import Report") or bpy.data.texts.new(
            "BeamNG Direct JBeam Import Report"
        )
        text.clear()
        text.write(f"Source: {filepath}\n")
        text.write(f"Part selection: {context.scene['beamng_last_direct_jbeam_import_part_filter']}\n")
        text.write(f"Parts: {len(imported.parts)}\n")
        text.write(f"Objects: {created}\n")
        text.write(f"Collection: {mesh_collection.name}\n")
        write_jbeam_import_diagnostic_log(text, imported.diagnostics)
        text.write("\nDiagnostics:\n")
        for diagnostic in imported.diagnostics:
            text.write(
                f"- {getattr(diagnostic, 'level', '')}: "
                f"{getattr(diagnostic, 'code', '')}: "
                f"{getattr(diagnostic, 'message', diagnostic)}\n"
            )

        warning_count = sum(1 for diagnostic in imported.diagnostics if getattr(diagnostic, "level", "") == "warning")
        info_count = sum(1 for diagnostic in imported.diagnostics if getattr(diagnostic, "level", "") == "info")
        error_count = sum(1 for diagnostic in imported.diagnostics if getattr(diagnostic, "level", "") == "error")
        print(
            "[BeamNG JBeam Import] "
            f"source={filepath} selection={context.scene['beamng_last_direct_jbeam_import_part_filter']} "
            f"parts={len(imported.parts)} objects={created} "
            f"errors={error_count} warnings={warning_count} infos={info_count}"
        )
        for (level, code), count in sorted(
            jbeam_import_diagnostic_counts(imported.diagnostics).items(),
            key=lambda item: (-item[1], item[0]),
        ):
            print(f"[BeamNG JBeam Import] diagnostic {level}:{code} count={count}")
        if warning_count:
            self.report({"WARNING"}, f"Imported {len(imported.parts)} JBeam part(s) with {warning_count} warning(s)")
        else:
            self.report({"INFO"}, f"Imported {len(imported.parts)} JBeam part(s)")
        return {"FINISHED"}


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
    source = PC_SOURCE_BY_KEY.get(self.pc_config_key)
    if source is not None:
        label_prefix = str(getattr(source, "label_prefix", "")).lower()
        self.include_user_overrides = not label_prefix.startswith("vanilla")
        self.vanilla_data_only = not self.include_user_overrides


def pc_config_updated(self, context):
    if not PC_VEHICLE_ENUM_ITEMS:
        refresh_pc_source_options(context)
    source = PC_SOURCE_BY_KEY.get(self.pc_config_key)
    if source is None:
        return
    label_prefix = str(getattr(source, "label_prefix", "")).lower()
    self.include_user_overrides = not label_prefix.startswith("vanilla")
    self.vanilla_data_only = not self.include_user_overrides


def source_is_vanilla_asset(source):
    label_prefix = str(getattr(source, "label_prefix", "")).lower()
    return label_prefix.startswith("vanilla")


def user_override_asset_summary_for_source(context, source):
    prefs = get_addon_preferences(context)
    user_folder = prefs.beamng_user_folder if prefs else ""
    cache_asset_catalogs = prefs.cache_asset_catalogs if prefs else True
    if not user_folder or source is None:
        return {"count": 0, "labels": []}

    vehicle_name = pc_vehicle_from_virtual_path(source.virtual_path)
    if not vehicle_name:
        return {"count": 0, "labels": []}

    current_folder = user_current_folder_from_preferences(context)
    if current_folder is None:
        return {"count": 0, "labels": []}

    labels = []
    total_count = 0
    mods_folder = current_folder / "mods"
    unpacked_folder = mods_folder / "unpacked"
    if unpacked_folder.exists():
        for mod_root in sorted(path for path in unpacked_folder.iterdir() if path.is_dir()):
            mod_count = 0
            for candidate_root in (
                mod_root / "vehicles" / vehicle_name,
                mod_root / "content" / "vehicles" / vehicle_name,
            ):
                if candidate_root.exists():
                    mod_count += sum(
                        1
                        for path in candidate_root.rglob("*")
                        if path.is_file() and path.suffix.lower() in {".jbeam", ".dae"}
                    )
            if mod_count:
                total_count += mod_count
                labels.append(f"current/mods/unpacked/{mod_root.name}: {mod_count} JBeam/DAE file(s)")

    if mods_folder.exists():
        wanted_prefixes = (
            f"vehicles/{vehicle_name.lower()}/",
            f"content/vehicles/{vehicle_name.lower()}/",
        )
        for zip_path in sorted(mods_folder.glob("*.zip")):
            zip_count = 0
            for entry in zip_contents_for_path(zip_path, cache_asset_catalogs):
                entry_name = normalize_virtual_path(entry.get("filename", ""))
                entry_lower = entry_name.lower()
                if not entry_lower.endswith((".jbeam", ".dae")):
                    continue
                if entry_lower.startswith(wanted_prefixes):
                    zip_count += 1
            if zip_count:
                total_count += zip_count
                labels.append(f"current/mods/{zip_path.name}: {zip_count} JBeam/DAE file(s)")

    return {"count": total_count, "labels": labels}


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
        update=pc_config_updated,
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
    create_experimental_jbeam_meshes: BoolProperty(
        name="Experimental JBeam Meshes",
        description="Create one editable Blender mesh per resolved JBeam part using nodes as vertices, beams as edges, and triangles as faces",
        default=False,
    )
    include_user_overrides: BoolProperty(
        name="Use User Mods/Overrides",
        description="Resolve JBeam/DAE data from BeamNG user mods as well as vanilla sources",
        default=True,
    )
    vanilla_data_only: BoolProperty(
        name="Vanilla Data Only (No Mods/Overrides)",
        description="Ignore user mods while resolving JBeam/DAE data",
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
        source = PC_SOURCE_BY_KEY.get(self.pc_config_key)
        if source is not None:
            label_prefix = str(getattr(source, "label_prefix", "")).lower()
            self.include_user_overrides = not label_prefix.startswith("vanilla")
            self.vanilla_data_only = not self.include_user_overrides
        return context.window_manager.invoke_props_dialog(self, width=650)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "pc_vehicle_key")
        layout.prop(self, "pc_config_key")
        source = PC_SOURCE_BY_KEY.get(self.pc_config_key)
        if source is not None and source_is_vanilla_asset(source):
            summary = user_override_asset_summary_for_source(context, source)
            if summary["count"]:
                box = layout.box()
                box.alert = True
                box.label(text="User mod assets found for this vanilla vehicle", icon="ERROR")
                box.label(text="Default: ignore them for a true vanilla import.")
                for label in summary["labels"][:4]:
                    box.label(text=label)
                if len(summary["labels"]) > 4:
                    box.label(text=f"... plus {len(summary['labels']) - 4} more source group(s)")
        layout.separator()
        layout.prop(self, "clear_existing")
        layout.prop(self, "include_jbeam_visuals")
        layout.prop(self, "selectable_jbeam_debug")
        layout.prop(self, "show_jbeam_node_labels")
        layout.prop(self, "create_experimental_jbeam_meshes")
        layout.prop(self, "vanilla_data_only")
        if not self.vanilla_data_only:
            layout.prop(self, "include_user_overrides")

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
        include_user_overrides = bool(self.include_user_overrides and not self.vanilla_data_only)
        context.scene["beamng_import_include_user_overrides"] = include_user_overrides
        if source_is_vanilla_asset(source):
            summary = user_override_asset_summary_for_source(context, source)
            if summary["count"] and include_user_overrides:
                self.report({"WARNING"}, f"Vanilla import is allowing {summary['count']} user mod asset(s)")
            elif summary["count"]:
                self.report({"INFO"}, f"Vanilla import is ignoring {summary['count']} user mod asset(s)")
        return import_beamng_pc_path(
            context,
            self,
            pc_path,
            self.clear_existing,
            self.include_jbeam_visuals,
            self.selectable_jbeam_debug,
            self.show_jbeam_node_labels,
            self.create_experimental_jbeam_meshes,
            source_description,
            source,
            include_user_overrides,
        )


def menu_func_import(self, _context):
    self.layout.operator(IMPORT_OT_beamng_pc.bl_idname, text="BeamNG Config (.pc File)")
    self.layout.operator(IMPORT_OT_beamng_pc_from_assets.bl_idname, text="BeamNG Config From BeamNG Assets")
    self.layout.operator(IMPORT_OT_beamng_jbeam_topology.bl_idname, text="BeamNG JBeam Topology (.jbeam)")


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
    box.operator(BEAMNG_OT_show_jbeam_part_with_references.bl_idname, text="Show Part + Referenced Parts")
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
    BeamNGJBeamExportFileItem,
    BeamNGAssemblyPartItem,
    BeamNGPCImporterPreferences,
    BEAMNG_OT_set_visibility,
    BEAMNG_OT_jbeam_relationship,
    BEAMNG_OT_select_jbeam_body_structure,
    BEAMNG_OT_show_jbeam_part_with_references,
    BEAMNG_OT_show_all_jbeams,
    BEAMNG_OT_hide_selected_jbeam_items,
    BEAMNG_OT_set_jbeam_visual_visibility,
    BEAMNG_OT_scan_experimental_jbeam_mesh_edits,
    BEAMNG_OT_refresh_jbeam_assembly_parts,
    BEAMNG_OT_set_active_jbeam_part_from_selection,
    BEAMNG_OT_activate_jbeam_assembly_part,
    BEAMNG_OT_accept_experimental_jbeam_node_moves,
    BEAMNG_OT_mark_selected_edges_as_jbeam_beams,
    BEAMNG_OT_set_selected_jbeam_edge_semantic,
    BEAMNG_OT_report_experimental_jbeam_selection,
    BEAMNG_OT_write_semantic_topology_snapshot,
    BEAMNG_OT_repair_experimental_jbeam_topology_uids,
    BEAMNG_OT_repair_experimental_jbeamzzz,
    BEAMNG_OT_check_experimental_jbeam_topology_health,
    BEAMNG_OT_create_jbeam_part_file,
    BEAMNG_OT_write_active_jbeam_slot_metadata,
    BEAMNG_OT_add_active_jbeam_child_slot,
    BEAMNG_OT_add_standalone_jbeam_node,
    BEAMNG_OT_import_selected_nodes_as_proxies,
    BEAMNG_OT_mark_selected_nodes_for_proxy_import,
    BEAMNG_OT_import_marked_nodes_as_proxies,
    BEAMNG_OT_create_crossbeam_to_marked_node,
    BEAMNG_OT_clear_marked_proxy_nodes,
    BEAMNG_OT_clear_unused_proxy_nodes,
    BEAMNG_OT_clear_orphan_provisional_nodes,
    BEAMNG_OT_create_jbeam_beam_from_selected_nodes,
    BEAMNG_OT_create_jbeam_triangle_from_selected_nodes,
    BEAMNG_OT_delete_selected_jbeam_elements,
    BEAMNG_OT_triangulate_selected_jbeam_faces,
    BEAMNG_OT_flip_selected_jbeam_triangles,
    BEAMNG_OT_apply_selected_jbeam_node_properties,
    BEAMNG_OT_load_selected_jbeam_node_properties,
    BEAMNG_OT_apply_selected_jbeam_beam_properties,
    BEAMNG_OT_load_selected_jbeam_beam_properties,
    BEAMNG_OT_apply_selected_jbeam_triangle_properties,
    BEAMNG_OT_load_selected_jbeam_triangle_properties,
    BEAMNG_OT_clear_jbeam_edit_session,
    BEAMNG_OT_create_jbeam_export_mod_folder,
    BEAMNG_OT_set_jbeam_export_mod_folder,
    BEAMNG_OT_validate_jbeam_assembly,
    BEAMNG_OT_write_authoring_workflow_report,
    BEAMNG_OT_write_jbeam_edit_preview,
    BEAMNG_OT_write_jbeam_node_patch_draft,
    BEAMNG_OT_write_jbeam_override_export_plan,
    BEAMNG_OT_set_jbeam_export_selection,
    BEAMNG_OT_review_jbeam_export,
    BEAMNG_OT_validate_jbeam_export,
    BEAMNG_OT_write_jbeam_patched_cache_copies,
    BEAMNG_OT_stage_jbeam_user_override_copies,
    BEAMNG_OT_update_jbeam_user_override_copies,
    BEAMNG_OT_quick_export_jbeam_node_moves,
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
    BEAMNG_OT_write_slot_authoring_report,
    VIEW3D_PT_beamng_pc_importer,
    VIEW3D_PT_beamng_jbeam_edit,
    VIEW3D_PT_beamng_jbeam_health,
    VIEW3D_PT_beamng_jbeam_workflow,
    VIEW3D_PT_beamng_jbeam_export,
    VIEW3D_PT_beamng_advanced,
    SCENE_PT_beamng_configuration_editor,
    IMPORT_OT_beamng_pc,
    IMPORT_OT_beamng_jbeam_topology,
    IMPORT_OT_beamng_pc_from_assets,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.beamng_slot_editor_items = CollectionProperty(type=BeamNGSlotEditorItem)
    bpy.types.Scene.beamng_jbeam_export_file_items = CollectionProperty(type=BeamNGJBeamExportFileItem)
    bpy.types.Scene.beamng_assembly_part_items = CollectionProperty(type=BeamNGAssemblyPartItem)
    bpy.types.Scene.beamng_jbeam_node_weight = StringProperty(
        name="nodeWeight",
        description="Optional JBeam nodeWeight to apply before inserted/new selected nodes",
        default="",
    )
    bpy.types.Scene.beamng_jbeam_node_material = StringProperty(
        name="nodeMaterial",
        description="Optional JBeam nodeMaterial to apply before inserted/new selected nodes",
        default="",
    )
    bpy.types.Scene.beamng_jbeam_node_group = StringProperty(
        name="group",
        description="Optional JBeam node group to apply before inserted/new selected nodes",
        default="",
    )
    bpy.types.Scene.beamng_jbeam_node_friction = StringProperty(
        name="frictionCoef",
        description="Optional JBeam frictionCoef to apply before inserted/new selected nodes",
        default="",
    )
    bpy.types.Scene.beamng_jbeam_node_collision_override = BoolProperty(
        name="Set collision",
        description="Write an explicit collision option for inserted/new selected nodes",
        default=False,
    )
    bpy.types.Scene.beamng_jbeam_node_collision = BoolProperty(
        name="collision",
        description="Explicit JBeam collision value for inserted/new selected nodes",
        default=True,
    )
    bpy.types.Scene.beamng_jbeam_node_self_collision_override = BoolProperty(
        name="Set selfCollision",
        description="Write an explicit selfCollision option for inserted/new selected nodes",
        default=False,
    )
    bpy.types.Scene.beamng_jbeam_node_self_collision = BoolProperty(
        name="selfCollision",
        description="Explicit JBeam selfCollision value for inserted/new selected nodes",
        default=False,
    )
    bpy.types.Scene.beamng_jbeam_beam_spring = StringProperty(name="beamSpring", default="")
    bpy.types.Scene.beamng_jbeam_beam_damp = StringProperty(name="beamDamp", default="")
    bpy.types.Scene.beamng_jbeam_beam_deform = StringProperty(name="beamDeform", default="")
    bpy.types.Scene.beamng_jbeam_beam_strength = StringProperty(name="beamStrength", default="")
    bpy.types.Scene.beamng_jbeam_beam_precompression = StringProperty(name="beamPrecompression", default="")
    bpy.types.Scene.beamng_jbeam_beam_type = StringProperty(name="beamType", default="")
    bpy.types.Scene.beamng_jbeam_beam_break_group = StringProperty(name="breakGroup", default="")
    bpy.types.Scene.beamng_jbeam_triangle_group = StringProperty(name="group", default="")
    bpy.types.Scene.beamng_jbeam_triangle_drag_coef = StringProperty(name="dragCoef", default="")
    bpy.types.Scene.beamng_jbeam_triangle_ground_model = StringProperty(name="groundModel", default="")
    bpy.types.Scene.beamng_jbeam_triangle_collision_override = BoolProperty(
        name="Set collision",
        description="Write an explicit collision option for inserted/new selected triangles",
        default=False,
    )
    bpy.types.Scene.beamng_jbeam_triangle_collision = BoolProperty(name="collision", default=True)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    bpy.types.VIEW3D_MT_view.append(menu_func_view_sync)
    bpy.types.VIEW3D_MT_object_context_menu.append(menu_func_jbeam_context)
    if clear_jbeam_edit_sessions_on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(clear_jbeam_edit_sessions_on_load)
    if experimental_jbeam_mesh_depsgraph_update_post not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(experimental_jbeam_mesh_depsgraph_update_post)
    mesh_topology_handlers = getattr(bpy.app.handlers, "mesh_topology_update_post", None)
    if mesh_topology_handlers is not None and experimental_jbeam_mesh_topology_update_post not in mesh_topology_handlers:
        mesh_topology_handlers.append(experimental_jbeam_mesh_topology_update_post)
    register_experimental_jbeam_proxy_overlay()
    if not bpy.app.timers.is_registered(experimental_jbeam_panel_redraw_timer):
        bpy.app.timers.register(experimental_jbeam_panel_redraw_timer, first_interval=0.5, persistent=True)
# Blender 4.2 restricts bpy.data during addon registration
try:
    scenes = getattr(bpy.data, "scenes", [])
    for scene in scenes:
        reset_jbeam_edit_session(scene)
except Exception:
    pass


def unregister():
    if bpy.app.timers.is_registered(experimental_jbeam_panel_redraw_timer):
        bpy.app.timers.unregister(experimental_jbeam_panel_redraw_timer)
    if bpy.app.timers.is_registered(experimental_jbeam_proxy_sync_timer):
        bpy.app.timers.unregister(experimental_jbeam_proxy_sync_timer)
    if bpy.app.timers.is_registered(experimental_jbeam_auto_scan_timer):
        bpy.app.timers.unregister(experimental_jbeam_auto_scan_timer)
    unregister_experimental_jbeam_proxy_overlay()
    if clear_jbeam_edit_sessions_on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(clear_jbeam_edit_sessions_on_load)
    if experimental_jbeam_mesh_depsgraph_update_post in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(experimental_jbeam_mesh_depsgraph_update_post)
    mesh_topology_handlers = getattr(bpy.app.handlers, "mesh_topology_update_post", None)
    if mesh_topology_handlers is not None and experimental_jbeam_mesh_topology_update_post in mesh_topology_handlers:
        mesh_topology_handlers.remove(experimental_jbeam_mesh_topology_update_post)
    bpy.types.VIEW3D_MT_object_context_menu.remove(menu_func_jbeam_context)
    bpy.types.VIEW3D_MT_view.remove(menu_func_view_sync)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    if hasattr(bpy.types.Scene, "beamng_slot_editor_items"):
        del bpy.types.Scene.beamng_slot_editor_items
    if hasattr(bpy.types.Scene, "beamng_jbeam_export_file_items"):
        del bpy.types.Scene.beamng_jbeam_export_file_items
    if hasattr(bpy.types.Scene, "beamng_assembly_part_items"):
        del bpy.types.Scene.beamng_assembly_part_items
    for prop_name in (
        "beamng_jbeam_node_weight",
        "beamng_jbeam_node_material",
        "beamng_jbeam_node_group",
        "beamng_jbeam_node_friction",
        "beamng_jbeam_node_collision_override",
        "beamng_jbeam_node_collision",
        "beamng_jbeam_node_self_collision_override",
        "beamng_jbeam_node_self_collision",
        "beamng_jbeam_beam_spring",
        "beamng_jbeam_beam_damp",
        "beamng_jbeam_beam_deform",
        "beamng_jbeam_beam_strength",
        "beamng_jbeam_beam_precompression",
        "beamng_jbeam_beam_type",
        "beamng_jbeam_beam_break_group",
        "beamng_jbeam_triangle_group",
        "beamng_jbeam_triangle_drag_coef",
        "beamng_jbeam_triangle_ground_model",
        "beamng_jbeam_triangle_collision_override",
        "beamng_jbeam_triangle_collision",
    ):
        if hasattr(bpy.types.Scene, prop_name):
            delattr(bpy.types.Scene, prop_name)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
