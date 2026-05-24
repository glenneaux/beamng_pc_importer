import hashlib
import json
import math
import re
import tempfile
import uuid
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import bpy
from mathutils import Euler, Matrix, Vector

COLLADA_NS = {"c": "http://www.collada.org/2005/11/COLLADASchema"}

MESH_EDITING_ENABLED = "beamng_importer_mesh_editing_enabled"
MESH_VEHICLE_MODEL = "beamng_importer_vehicle_model"
MESH_JBEAM_PART = "beamng_importer_jbeam_part"
MESH_JBEAM_FILE_PATH = "beamng_importer_jbeam_file_path"


@dataclass
class FlexbodySpec:
    mesh: str
    part_name: str
    jbeam_path: Path
    transform_matrix: Matrix = field(default_factory=lambda: Matrix.Identity(4))
    prop_rest_matrix: Matrix = field(default_factory=lambda: Matrix.Identity(4))
    pos: Vector = field(default_factory=lambda: Vector((0.0, 0.0, 0.0)))
    rot: Vector = field(default_factory=lambda: Vector((0.0, 0.0, 0.0)))
    scale: Vector = field(default_factory=lambda: Vector((1.0, 1.0, 1.0)))
    source_type: str = "flexbody"
    use_template_transform: bool = True
    keep_template_translation: bool = True
    resolved_part_id: int = -1
    debug_anchor_nodes: tuple = field(default_factory=tuple)
    debug_anchor_origin: tuple = field(default_factory=tuple)
    debug_anchor_x: tuple = field(default_factory=tuple)
    debug_anchor_y: tuple = field(default_factory=tuple)
    debug_missing_anchor_nodes: tuple = field(default_factory=tuple)
    debug_prop_base_translation: tuple = field(default_factory=tuple)
    debug_prop_anim_translation: tuple = field(default_factory=tuple)
    debug_prop_local_translation: tuple = field(default_factory=tuple)
    debug_prop_world_translation_offset: tuple = field(default_factory=tuple)
    debug_prop_global_translation: tuple = field(default_factory=tuple)
    debug_prop_base_rotation: tuple = field(default_factory=tuple)
    debug_prop_row_rotation: tuple = field(default_factory=tuple)
    debug_prop_anim_factor: float = 0.0
    debug_prop_anchor_x_axis: tuple = field(default_factory=tuple)
    debug_prop_anchor_y_axis: tuple = field(default_factory=tuple)
    debug_prop_anchor_z_axis: tuple = field(default_factory=tuple)
    debug_prop_anchor_determinant: float = 0.0


@dataclass
class PartDefinition:
    name: str
    data: dict
    source_path: Path


@dataclass
class JBeamImportDiagnostic:
    level: str
    code: str
    message: str
    part_name: str = ""
    section: str = ""


@dataclass
class ImportedJBeamNode:
    node_id: str
    position: tuple
    original_position: tuple = field(default_factory=tuple)
    topology_guid: str = ""
    options: dict = field(default_factory=dict)
    row_index: int = -1


@dataclass
class ImportedJBeamBeam:
    id1: str
    id2: str
    topology_guid: str = ""
    options: dict = field(default_factory=dict)
    row_index: int = -1
    missing_nodes: tuple = field(default_factory=tuple)


@dataclass
class ImportedJBeamTriangle:
    id1: str
    id2: str
    id3: str
    topology_guid: str = ""
    options: dict = field(default_factory=dict)
    row_index: int = -1
    missing_nodes: tuple = field(default_factory=tuple)


@dataclass
class ImportedJBeamPart:
    part_name: str
    part_guid: str = ""
    source_path: str = ""
    information: dict = field(default_factory=dict)
    slot_type: object = ""
    slots: list = field(default_factory=list)
    nodes: list = field(default_factory=list)
    beams: list = field(default_factory=list)
    triangles: list = field(default_factory=list)
    unknown_preserved_sections: dict = field(default_factory=dict)
    diagnostics: list = field(default_factory=list)


@dataclass
class JBeamTopologySubsetImport:
    source_path: str = ""
    coordinate_precision: int = 3
    cached_source: dict = field(default_factory=dict)
    source_map: dict = field(default_factory=dict)
    import_identity_map: dict = field(default_factory=dict)
    export_metadata_mode: str = "none"
    parts: list = field(default_factory=list)
    diagnostics: list = field(default_factory=list)


@dataclass
class ResolvedPart:
    id: int
    part_def: PartDefinition
    transform_matrix: Matrix
    local_transform_matrix: Matrix
    parent_id: int = -1
    slot_name: str = ""
    component_context: dict = field(default_factory=dict)


@dataclass
class JBeamNodeSpec:
    name: str
    position: Vector
    part_name: str
    resolved_part_id: int = -1
    options: dict = field(default_factory=dict)


@dataclass
class JBeamBeamSpec:
    id1: str
    id2: str
    start: Vector
    end: Vector
    part_name: str
    resolved_part_id: int = -1
    options: dict = field(default_factory=dict)


@dataclass
class JBeamTriangleSpec:
    id1: str
    id2: str
    id3: str
    p1: Vector
    p2: Vector
    p3: Vector
    part_name: str
    resolved_part_id: int = -1
    options: dict = field(default_factory=dict)


@dataclass
class JBeamHydroSpec:
    id1: str
    id2: str
    start: Vector
    end: Vector
    part_name: str
    resolved_part_id: int = -1
    input_source: str = ""
    factor: str = ""


@dataclass
class JBeamRailSpec:
    name: str
    node_ids: tuple
    points: tuple
    part_name: str
    resolved_part_id: int = -1
    capped: str = ""
    looped: str = ""


@dataclass
class JBeamSlidenodeSpec:
    node_id: str
    rail_name: str
    position: Vector
    rail_points: tuple
    part_name: str
    resolved_part_id: int = -1
    attached: str = ""
    fix_to_rail: str = ""


@dataclass
class ResolvedVehiclePartModel:
    resolved_part_id: int
    name: str
    source_path: str
    parent_id: int = -1
    slot_name: str = ""
    node_ids: tuple = field(default_factory=tuple)
    beam_count: int = 0
    triangle_count: int = 0
    hydro_count: int = 0
    rail_count: int = 0
    slidenode_count: int = 0
    flexbody_count: int = 0
    prop_count: int = 0
    external_node_refs: tuple = field(default_factory=tuple)
    ancestor_node_refs: tuple = field(default_factory=tuple)
    descendant_node_refs: tuple = field(default_factory=tuple)
    cross_branch_node_refs: tuple = field(default_factory=tuple)
    unresolved_node_refs: tuple = field(default_factory=tuple)


@dataclass
class ResolvedVehicleModel:
    pc_path: str
    source_description: str
    vehicle_model: str
    main_part: str
    parts: list = field(default_factory=list)
    node_count: int = 0
    beam_count: int = 0
    triangle_count: int = 0
    hydro_count: int = 0
    rail_count: int = 0
    slidenode_count: int = 0
    flexbody_count: int = 0
    prop_count: int = 0
    source_files: tuple = field(default_factory=tuple)
    external_node_ref_count: int = 0
    ancestor_node_ref_count: int = 0
    descendant_node_ref_count: int = 0
    cross_branch_node_ref_count: int = 0
    unresolved_node_ref_count: int = 0
    node_owner_part_ids: dict = field(default_factory=dict)
    part_external_node_refs: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DaeAssetSource:
    asset_type: str
    path: str
    zip_path: str = ""
    zip_entry: str = ""
    virtual_path: str = ""
    precedence: int = 0


@dataclass(frozen=True)
class BeamNGAssetSource:
    asset_type: str
    virtual_path: str
    path: str = ""
    zip_path: str = ""
    zip_entry: str = ""
    precedence: int = 0
    label_prefix: str = ""


@dataclass
class BeamNGAssetLayer:
    name: str
    root: Path
    virtual_prefix: Path = field(default_factory=lambda: Path(""))
    precedence: int = 0


DAE_CATALOG_CACHE = {}
DAE_NAME_INDEX_CACHE = {}
PART_INDEX_CACHE = {}
DISK_CACHE_DATA = {}
DISK_CACHE_DIRTY = set()


def persistent_cache_dir():
    cache_dir = Path(bpy.utils.user_resource("CONFIG", path="beamng_pc_importer_cache", create=True))
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def path_is_inside_plugin_cache(path: Path):
    try:
        path.resolve().relative_to(persistent_cache_dir().resolve())
        return True
    except (OSError, ValueError):
        return False


def persistent_cache_key(source):
    return json.dumps(source_signature(source), separators=(",", ":"))


def load_disk_cache(name):
    if name in DISK_CACHE_DATA:
        return DISK_CACHE_DATA[name]
    cache_path = persistent_cache_dir() / f"{name}.json"
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    DISK_CACHE_DATA[name] = data
    return data


def mark_disk_cache_dirty(name):
    DISK_CACHE_DIRTY.add(name)


def save_disk_cache(name):
    if name not in DISK_CACHE_DIRTY:
        return
    cache_path = persistent_cache_dir() / f"{name}.json"
    try:
        cache_path.write_text(json.dumps(DISK_CACHE_DATA.get(name, {}), separators=(",", ":")), encoding="utf-8")
        DISK_CACHE_DIRTY.discard(name)
    except Exception as exc:
        print(f"[BeamNG Importer] Failed to write cache {cache_path}: {exc}")


def save_dirty_disk_caches():
    for name in tuple(DISK_CACHE_DIRTY):
        save_disk_cache(name)


def zip_contents_for_path(zip_path: Path, cache_enabled=True):
    cache_name = "zip_contents"
    key = json.dumps(("zip", *path_signature(zip_path)), separators=(",", ":"))
    if cache_enabled:
        disk_cache = load_disk_cache(cache_name)
        cached = disk_cache.get(key)
        if isinstance(cached, list):
            return cached

    entries = []
    try:
        with zipfile.ZipFile(zip_path, "r") as archive:
            for entry in archive.infolist():
                if entry.is_dir():
                    continue
                entries.append(
                    {
                        "filename": entry.filename,
                        "length": int(entry.file_size),
                    }
                )
    except Exception as exc:
        print(f"[BeamNG Importer] Failed to inspect archive {zip_path}: {exc}")
        return []

    if cache_enabled:
        disk_cache = load_disk_cache(cache_name)
        disk_cache[key] = entries
        mark_disk_cache_dirty(cache_name)
    return entries


def strip_json_comments(text: str) -> str:
    result = []
    in_string = False
    escape = False
    i = 0
    length = len(text)
    while i < length:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < length else ""
        if in_string:
            result.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            result.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "/":
            i += 2
            while i < length and text[i] not in "\r\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            i += 2
            while i + 1 < length and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        result.append(ch)
        i += 1
    return "".join(result)


def strip_trailing_commas(text: str) -> str:
    previous = None
    cleaned = text
    while previous != cleaned:
        previous = cleaned
        cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
    return cleaned


def strip_leading_commas(text: str) -> str:
    result = []
    in_string = False
    escape = False
    i = 0
    length = len(text)
    while i < length:
        ch = text[i]
        result.append(ch)

        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
            i += 1
            continue

        if ch in "{[":
            j = i + 1
            while j < length and text[j] in " \t\r\n":
                result.append(text[j])
                j += 1
            if j < length and text[j] == ",":
                i = j + 1
                continue

        i += 1
    return "".join(result)


def insert_missing_commas(text: str) -> str:
    result = []
    stack = []
    i = 0
    length = len(text)

    def is_value_start(index: int) -> bool:
        if index >= length:
            return False
        ch = text[index]
        return ch == '"' or ch == "{" or ch == "[" or ch == "-" or ch.isdigit() or ch.isalpha() or ch == "_"

    while i < length:
        ch = text[i]

        if ch in " \t\r\n":
            result.append(ch)
            i += 1
            continue

        if ch == '"':
            if stack:
                top = stack[-1]
                if top["type"] == "object" and top["state"] == "after_value":
                    result.append(",")
                    top["state"] = "expect_key_or_end"
                elif top["type"] == "array" and top["state"] == "after_value":
                    result.append(",")
                    top["state"] = "expect_value_or_end"

            start = i
            i += 1
            escape = False
            while i < length:
                current = text[i]
                if escape:
                    escape = False
                elif current == "\\":
                    escape = True
                elif current == '"':
                    i += 1
                    break
                i += 1
            token = text[start:i]
            result.append(token)

            j = i
            while j < length and text[j] in " \t\r\n":
                j += 1

            if stack:
                top = stack[-1]
                if top["type"] == "object":
                    if top["state"] in {"expect_key_or_end", "expect_key"} and j < length and text[j] == ":":
                        top["state"] = "expect_colon"
                    elif top["state"] in {"expect_value", "expect_value_or_end", "expect_colon"}:
                        top["state"] = "after_value"
                elif top["type"] == "array":
                    top["state"] = "after_value"
            continue

        if ch == "{":
            if stack:
                top = stack[-1]
                if top["state"] == "after_value":
                    result.append(",")
                    if top["type"] == "object":
                        top["state"] = "expect_key_or_end"
                    else:
                        top["state"] = "expect_value_or_end"
            result.append(ch)
            stack.append({"type": "object", "state": "expect_key_or_end"})
            i += 1
            continue

        if ch == "[":
            if stack:
                top = stack[-1]
                if top["state"] == "after_value":
                    result.append(",")
                    if top["type"] == "object":
                        top["state"] = "expect_key_or_end"
                    else:
                        top["state"] = "expect_value_or_end"
            result.append(ch)
            stack.append({"type": "array", "state": "expect_value_or_end"})
            i += 1
            continue

        if ch == ":":
            result.append(ch)
            if stack and stack[-1]["type"] == "object":
                stack[-1]["state"] = "expect_value"
            i += 1
            continue

        if ch == ",":
            result.append(ch)
            if stack:
                if stack[-1]["type"] == "object":
                    stack[-1]["state"] = "expect_key"
                else:
                    stack[-1]["state"] = "expect_value_or_end"
            i += 1
            continue

        if ch == "}" or ch == "]":
            result.append(ch)
            if stack:
                stack.pop()
            if stack:
                stack[-1]["state"] = "after_value"
            i += 1
            continue

        if stack:
            top = stack[-1]
            if top["state"] == "after_value":
                result.append(",")
                if top["type"] == "object":
                    top["state"] = "expect_key_or_end"
                else:
                    top["state"] = "expect_value_or_end"

        start = i
        while i < length and text[i] not in " \t\r\n{}[]:,\"":
            i += 1
        token = text[start:i]
        result.append(token)
        if stack:
            stack[-1]["state"] = "after_value"

    return "".join(result)


def describe_json_error(cleaned: str, exc: Exception):
    pos = getattr(exc, "pos", None)
    if pos is None:
        return str(exc)
    start = max(0, pos - 80)
    end = min(len(cleaned), pos + 120)
    snippet = cleaned[start:end].replace("\r", "\\r").replace("\n", "\\n")
    return f"{exc}; near: {snippet}"


def load_jsonc(path: Path):
    text = path.read_text(encoding="utf-8-sig")
    cleaned = strip_leading_commas(strip_trailing_commas(insert_missing_commas(strip_json_comments(text))))
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Failed to parse JSONC file {path}: {describe_json_error(cleaned, exc)}") from exc


def load_jsonc_text(text: str):
    cleaned = strip_leading_commas(strip_trailing_commas(insert_missing_commas(strip_json_comments(text))))
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Failed to parse JSONC text: {describe_json_error(cleaned, exc)}") from exc


def load_jsonc_asset(source: BeamNGAssetSource):
    if source.asset_type == "file":
        return load_jsonc(Path(source.path))
    with zipfile.ZipFile(source.zip_path, "r") as archive:
        text = archive.read(source.zip_entry).decode("utf-8", errors="ignore")
    return load_jsonc_text(text)


def normalized_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


COMPONENT_EXPR_RE = re.compile(r"\$components\.([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*)")


def lookup_component_value(component_context, path: str):
    current = component_context
    for segment in path.split("."):
        if not isinstance(current, dict) or segment not in current:
            return None
        current = current[segment]
    return current


def coerce_number(value, fallback, component_context=None):
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        try:
            return float(stripped)
        except ValueError:
            pass

        if not stripped.startswith("$="):
            return float(fallback)

        expr = stripped[2:]

        def replace_component(match):
            resolved = lookup_component_value(component_context or {}, match.group(1))
            if isinstance(resolved, (int, float)):
                return repr(float(resolved))
            raise ValueError(match.group(1))

        try:
            expr = COMPONENT_EXPR_RE.sub(replace_component, expr)
        except ValueError:
            return float(fallback)

        if re.search(r"[^0-9eE\.\+\-\*\/\(\)\s]", expr):
            return float(fallback)
        if "**" in expr or "//" in expr:
            return float(fallback)

        try:
            return float(eval(expr, {"__builtins__": {}}, {}))
        except Exception:
            return float(fallback)
    return float(fallback)


def evaluate_jbeam_expression(value, component_context=None):
    if not isinstance(value, str):
        return value

    stripped = value.strip()
    if not stripped.startswith("$="):
        return value

    expr = stripped[2:]

    def replace_component(match):
        resolved = lookup_component_value(component_context or {}, match.group(1))
        if resolved is None:
            return "None"
        return repr(resolved)

    expr = COMPONENT_EXPR_RE.sub(replace_component, expr)
    expr = re.sub(r"\bnil\b", "None", expr)
    expr = re.sub(r"\btrue\b", "True", expr, flags=re.IGNORECASE)
    expr = re.sub(r"\bfalse\b", "False", expr, flags=re.IGNORECASE)

    if re.search(r"[^A-Za-z0-9_\'\"\.\+\-\*\/\(\)\s=!<>]", expr):
        return value
    if "**" in expr or "//" in expr:
        return value

    try:
        return eval(expr, {"__builtins__": {}}, {})
    except Exception:
        return value


def evaluate_mesh_name(value, component_context=None):
    resolved = evaluate_jbeam_expression(value, component_context)
    for _ in range(4):
        if not isinstance(resolved, str) or not resolved.strip().startswith("$="):
            break
        next_resolved = evaluate_jbeam_expression(resolved, component_context)
        if next_resolved == resolved:
            break
        resolved = next_resolved
    if resolved is None:
        return ""
    return str(resolved)


def is_disabled(options, component_context=None):
    if not isinstance(options, dict) or "disable" not in options:
        return False
    resolved = evaluate_jbeam_expression(options.get("disable"), component_context)
    if isinstance(resolved, bool):
        return resolved
    return False


def vector_from_dict(data, default, component_context=None):
    if not isinstance(data, dict):
        return Vector(default)

    return Vector(
        (
            coerce_number(data.get("x", default[0]), default[0], component_context),
            coerce_number(data.get("y", default[1]), default[1], component_context),
            coerce_number(data.get("z", default[2]), default[2], component_context),
        )
    )


def merge_options(base, extra):
    merged = dict(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            nested = dict(merged[key])
            nested.update(value)
            merged[key] = nested
        else:
            merged[key] = value
    return merged


def matrix_from_trs(pos=None, rot=None, scale=None):
    pos = pos if pos is not None else Vector((0.0, 0.0, 0.0))
    rot = rot if rot is not None else Vector((0.0, 0.0, 0.0))
    scale = scale if scale is not None else Vector((1.0, 1.0, 1.0))

    rotation = Euler(
        (math.radians(rot.x), math.radians(rot.y), math.radians(rot.z)),
        "XYZ",
    ).to_matrix().to_4x4()
    scale_matrix = Matrix.Diagonal((scale.x, scale.y, scale.z, 1.0))
    translation = Matrix.Translation(pos)
    return translation @ rotation @ scale_matrix


def axis_rotation_matrix(axis: str, degrees: float):
    radians = math.radians(float(degrees))
    if axis == "X":
        return Matrix.Rotation(radians, 4, "X")
    if axis == "Y":
        return Matrix.Rotation(radians, 4, "Y")
    if axis == "Z":
        return Matrix.Rotation(radians, 4, "Z")
    return Matrix.Identity(4)


def intrinsic_axis_rotation(sequence):
    result = Matrix.Identity(4)
    for axis, degrees in sequence:
        # Intrinsic rotations are applied around the prop's current local axes.
        result = result @ axis_rotation_matrix(axis, degrees)
    return result


def prop_base_rotation_matrix(rot: Vector):
    # BeamNG docs: props baseRotation uses intrinsic Euler -X -Z +Y.
    return intrinsic_axis_rotation((("X", -rot.x), ("Z", -rot.z), ("Y", rot.y)))


def prop_anim_rotation_matrix(rot: Vector):
    # BeamNG docs: props animated rotation uses intrinsic Euler -X -Z -Y.
    return intrinsic_axis_rotation((("X", -rot.x), ("Z", -rot.z), ("Y", -rot.y)))


def prop_global_rotation_matrix(rot: Vector):
    # BeamNG docs: props baseRotationGlobal uses intrinsic Euler +Y +Z +X.
    return intrinsic_axis_rotation((("Y", rot.y), ("Z", rot.z), ("X", rot.x)))


def matrix_from_axes(origin, x_axis, y_axis):
    x_axis = x_axis.normalized() if x_axis.length > 0.000001 else Vector((1.0, 0.0, 0.0))
    y_axis = y_axis - x_axis * y_axis.dot(x_axis)
    y_axis = y_axis.normalized() if y_axis.length > 0.000001 else Vector((0.0, 1.0, 0.0))
    z_axis = x_axis.cross(y_axis)
    z_axis = z_axis.normalized() if z_axis.length > 0.000001 else Vector((0.0, 0.0, 1.0))

    return Matrix(
        (
            (x_axis.x, y_axis.x, z_axis.x, origin.x),
            (x_axis.y, y_axis.y, z_axis.y, origin.y),
            (x_axis.z, y_axis.z, z_axis.z, origin.z),
            (0.0, 0.0, 0.0, 1.0),
        )
    )


def matrix_from_prop_axes(origin, x_axis, y_axis):
    # BeamNG prop translation follows the raw normalized idRef->idX and
    # idRef->idY axes. Some vanilla props are not perfectly orthogonal; forcing
    # a right angle moves dash needles away from their authored DAE rest pose.
    x_axis = x_axis.normalized() if x_axis.length > 0.000001 else Vector((1.0, 0.0, 0.0))
    y_axis = y_axis.normalized() if y_axis.length > 0.000001 else Vector((0.0, 1.0, 0.0))
    z_axis = y_axis.cross(x_axis)
    z_axis = z_axis.normalized() if z_axis.length > 0.000001 else Vector((0.0, 0.0, 1.0))

    return Matrix(
        (
            (x_axis.x, y_axis.x, z_axis.x, origin.x),
            (x_axis.y, y_axis.y, z_axis.y, origin.y),
            (x_axis.z, y_axis.z, z_axis.z, origin.z),
            (0.0, 0.0, 0.0, 1.0),
        )
    )


def get_option_transform(options, component_context=None):
    options = options or {}
    translation = (
        vector_from_dict(options.get("nodeMove"), (0.0, 0.0, 0.0), component_context)
        + vector_from_dict(options.get("nodeOffset"), (0.0, 0.0, 0.0), component_context)
        + vector_from_dict(options.get("pos"), (0.0, 0.0, 0.0), component_context)
    )
    rotation = vector_from_dict(options.get("rot"), (0.0, 0.0, 0.0), component_context)
    scale = vector_from_dict(options.get("scale"), (1.0, 1.0, 1.0), component_context)
    return matrix_from_trs(translation, rotation, scale)


def merge_component_context(base, extra):
    merged = dict(base or {})
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_component_context(merged[key], value)
        else:
            merged[key] = value
    return merged


def extract_component_context(part_data):
    components = part_data.get("components")
    if isinstance(components, dict):
        return components
    return {}


def normalize_virtual_path(value):
    return str(value).replace("\\", "/").strip("/")


def path_signature(path: Path):
    try:
        stat = path.stat()
    except OSError:
        return (str(path), 0, 0)
    return (str(path), int(stat.st_mtime), int(stat.st_size))


def source_signature(source):
    if source.asset_type == "file":
        return (
            source.asset_type,
            normalize_virtual_path(source.virtual_path),
            *path_signature(Path(source.path)),
            source.precedence,
        )
    return (
        source.asset_type,
        normalize_virtual_path(source.virtual_path),
        source.zip_entry,
        *path_signature(Path(source.zip_path)),
        source.precedence,
    )


def resolve_user_current_folder(user_folder: str, pc_path: Path):
    candidates = []
    if user_folder:
        supplied = Path(user_folder)
        candidates.append(supplied if supplied.name.lower() == "current" else supplied / "current")

    for parent in (pc_path.parent, *pc_path.parents):
        if parent.name.lower() == "current":
            candidates.append(parent)
            break

    for candidate in candidates:
        if candidate.exists() and candidate.is_dir():
            return candidate
    return None


def resolve_vanilla_vehicles_folder(vanilla_folder: str):
    if not vanilla_folder:
        return None
    root = Path(vanilla_folder)
    if (root / "content" / "vehicles").exists():
        root = root / "content" / "vehicles"
    if root.exists() and root.is_dir():
        return root
    return None


def vehicle_folder_from_vehicles_root(vehicles_root: Path, vehicle_name: str):
    if not vehicles_root:
        return None
    if vehicles_root.name.lower() == vehicle_name.lower():
        return vehicles_root
    candidate = vehicles_root / vehicle_name
    if candidate.exists() and candidate.is_dir():
        return candidate
    return None


def add_file_asset_sources(sources, root: Path, pattern: str, virtual_prefix: str, precedence: int, asset_type: str):
    if not root or not root.exists() or not root.is_dir():
        return
    if path_is_inside_plugin_cache(root):
        return
    prefix = Path(virtual_prefix)
    for path in root.rglob(pattern):
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(root)
        except ValueError:
            relative = Path(path.name)
        virtual_path = normalize_virtual_path(prefix / relative)
        sources.append(
            BeamNGAssetSource(
                asset_type="file",
                virtual_path=virtual_path,
                path=str(path),
                precedence=precedence,
            )
        )


def add_zip_asset_sources(sources, zip_path: Path, vehicle_name: str, suffix: str, precedence: int, cache_enabled=True):
    if not zip_path or not zip_path.exists() or not zip_path.is_file():
        return
    wanted_prefixes = (
        f"vehicles/{vehicle_name.lower()}/",
        "vehicles/common/",
        f"content/vehicles/{vehicle_name.lower()}/",
        "content/vehicles/common/",
    )

    for entry in zip_contents_for_path(zip_path, cache_enabled):
        entry_filename = str(entry.get("filename", ""))
        if not entry_filename.lower().endswith(suffix):
            continue
        entry_name = normalize_virtual_path(entry_filename)
        entry_lower = entry_name.lower()
        if not entry_lower.startswith(wanted_prefixes):
            continue
        if entry_lower.startswith("content/"):
            virtual_path = entry_name[len("content/") :]
        else:
            virtual_path = entry_name
        sources.append(
            BeamNGAssetSource(
                asset_type="zip",
                virtual_path=normalize_virtual_path(virtual_path),
                path=normalize_virtual_path(virtual_path),
                zip_path=str(zip_path),
                zip_entry=entry_filename,
                precedence=precedence,
            )
        )


def pc_vehicle_from_virtual_path(virtual_path: str):
    parts = normalize_virtual_path(virtual_path).split("/")
    lowered = [part.lower() for part in parts]
    if "vehicles" not in lowered:
        return ""
    index = lowered.index("vehicles")
    if index + 1 >= len(parts):
        return ""
    return parts[index + 1]


def add_file_pc_sources(sources, root: Path, precedence: int, label_prefix: str = ""):
    if not root or not root.exists() or not root.is_dir():
        return
    if path_is_inside_plugin_cache(root):
        return
    for path in sorted(root.rglob("*.pc")):
        if not path.is_file():
            continue
        virtual_path = normalize_virtual_path(path.relative_to(root))
        if not virtual_path.lower().startswith("vehicles/"):
            virtual_path = normalize_virtual_path(Path("vehicles") / virtual_path)
        vehicle_name = pc_vehicle_from_virtual_path(virtual_path)
        if not vehicle_name:
            continue
        source = BeamNGAssetSource(
            asset_type="file",
            virtual_path=virtual_path,
            path=str(path),
            precedence=precedence,
            label_prefix=label_prefix,
        )
        sources.append(source)


def add_zip_pc_sources(sources, zip_path: Path, precedence: int, cache_enabled=True, label_prefix: str = ""):
    if not zip_path or not zip_path.exists() or not zip_path.is_file():
        return
    for entry in zip_contents_for_path(zip_path, cache_enabled):
        entry_filename = str(entry.get("filename", ""))
        if not entry_filename.lower().endswith(".pc"):
            continue
        entry_name = normalize_virtual_path(entry_filename)
        entry_lower = entry_name.lower()
        if not entry_lower.startswith(("vehicles/", "content/vehicles/")):
            continue
        virtual_path = entry_name[len("content/") :] if entry_lower.startswith("content/") else entry_name
        vehicle_name = pc_vehicle_from_virtual_path(virtual_path)
        if not vehicle_name:
            continue
        source = BeamNGAssetSource(
            asset_type="zip",
            virtual_path=normalize_virtual_path(virtual_path),
            path=normalize_virtual_path(virtual_path),
            zip_path=str(zip_path),
            zip_entry=entry_filename,
            precedence=precedence,
            label_prefix=label_prefix,
        )
        sources.append(source)


def collect_beamng_pc_sources(user_folder: str = "", vanilla_folder: str = "", cache_enabled=True):
    sources = []
    vanilla_vehicles = resolve_vanilla_vehicles_folder(vanilla_folder)
    if vanilla_vehicles:
        add_file_pc_sources(sources, vanilla_vehicles, 0, "Vanilla")
        for zip_path in sorted(vanilla_vehicles.glob("*.zip")):
            add_zip_pc_sources(sources, zip_path, 0, cache_enabled, "Vanilla zip")

    current_folder = None
    if user_folder:
        supplied = Path(user_folder)
        current_candidate = supplied if supplied.name.lower() == "current" else supplied / "current"
        if current_candidate.exists() and current_candidate.is_dir():
            current_folder = current_candidate

    if current_folder:
        add_file_pc_sources(sources, current_folder / "vehicles", 40, "User")
        mods_folder = current_folder / "mods"
        unpacked_folder = mods_folder / "unpacked"
        if unpacked_folder.exists():
            for mod_index, mod_root in enumerate(sorted(path for path in unpacked_folder.iterdir() if path.is_dir())):
                add_file_pc_sources(sources, mod_root, 30 + mod_index, f"Unpacked mod: {mod_root.name}")
        if mods_folder.exists():
            for zip_index, zip_path in enumerate(sorted(mods_folder.glob("*.zip"))):
                add_zip_pc_sources(sources, zip_path, 20 + zip_index, cache_enabled, f"Mod zip: {zip_path.name}")

    return sorted(
        sources,
        key=lambda item: (
            normalize_virtual_path(item.virtual_path).lower(),
            -item.precedence,
            getattr(item, "label_prefix", ""),
        ),
    )


def materialize_pc_asset(source: BeamNGAssetSource):
    if source.asset_type == "file":
        return Path(source.path)

    virtual_path = normalize_virtual_path(source.virtual_path)
    safe_zip_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(source.zip_path).stem)
    output_path = persistent_cache_dir() / "pc_sources" / safe_zip_name / virtual_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source.zip_path, "r") as archive:
        output_path.write_bytes(archive.read(source.zip_entry))
    return output_path


def collect_beamng_asset_sources(
    pc_path: Path,
    user_folder: str = "",
    vanilla_folder: str = "",
    cache_enabled=True,
    include_user_assets=True,
):
    vehicle_name = pc_path.parent.name
    selected_vehicle_root = pc_path.parent
    current_folder = resolve_user_current_folder(user_folder, pc_path)
    vanilla_vehicles = resolve_vanilla_vehicles_folder(vanilla_folder)
    vanilla_vehicle_root = vehicle_folder_from_vehicles_root(vanilla_vehicles, vehicle_name) if vanilla_vehicles else None

    jbeam_sources = []
    dae_sources = []

    def is_loose_user_vehicle_root(root):
        if not current_folder or not root:
            return False
        try:
            root.resolve().relative_to((current_folder / "vehicles").resolve())
            return True
        except (OSError, ValueError):
            return False

    def add_vehicle_root(root, precedence):
        if not root:
            return
        add_file_asset_sources(jbeam_sources, root, "*.jbeam", f"vehicles/{vehicle_name}", precedence, "jbeam")
        add_file_asset_sources(dae_sources, root, "*.dae", f"vehicles/{vehicle_name}", precedence, "dae")

    def add_common_root(root, precedence):
        if root and root.exists() and root.is_dir():
            add_file_asset_sources(dae_sources, root, "*.dae", "vehicles/common", precedence, "dae")

    add_vehicle_root(vanilla_vehicle_root, 0)
    if vanilla_vehicles:
        add_common_root(vanilla_vehicles / "common", 0)
        for zip_path in sorted(vanilla_vehicles.glob("*.zip")):
            add_zip_asset_sources(jbeam_sources, zip_path, vehicle_name, ".jbeam", 0, cache_enabled)
            add_zip_asset_sources(dae_sources, zip_path, vehicle_name, ".dae", 0, cache_enabled)

    if include_user_assets and current_folder:
        mods_folder = current_folder / "mods"
        unpacked_folder = mods_folder / "unpacked"
        if unpacked_folder.exists():
            for mod_index, mod_root in enumerate(sorted(path for path in unpacked_folder.iterdir() if path.is_dir())):
                precedence = 30 + mod_index
                add_vehicle_root(mod_root / "vehicles" / vehicle_name, precedence)
                add_common_root(mod_root / "vehicles" / "common", precedence)
                add_vehicle_root(mod_root / "content" / "vehicles" / vehicle_name, precedence)
                add_common_root(mod_root / "content" / "vehicles" / "common", precedence)
        if mods_folder.exists():
            for zip_index, zip_path in enumerate(sorted(mods_folder.glob("*.zip"))):
                precedence = 20 + zip_index
                add_zip_asset_sources(jbeam_sources, zip_path, vehicle_name, ".jbeam", precedence, cache_enabled)
                add_zip_asset_sources(dae_sources, zip_path, vehicle_name, ".dae", precedence, cache_enabled)

    if not is_loose_user_vehicle_root(selected_vehicle_root):
        add_vehicle_root(selected_vehicle_root, 40)
    return jbeam_sources, dae_sources, Path(f"vehicles/{vehicle_name}")


def parse_parts_for_source(source: BeamNGAssetSource, cache_enabled=True):
    cache_name = "jbeam_parts"
    key = persistent_cache_key(source)
    if cache_enabled:
        disk_cache = load_disk_cache(cache_name)
        cached = disk_cache.get(key)
        if isinstance(cached, dict):
            return cached

    try:
        payload = load_jsonc_asset(source)
    except Exception as exc:
        print(f"[BeamNG Importer] Failed to parse {source.virtual_path}: {exc}")
        return {}
    if not isinstance(payload, dict):
        return {}

    parts = {part_name: part_data for part_name, part_data in payload.items() if isinstance(part_data, dict)}
    if cache_enabled:
        disk_cache = load_disk_cache(cache_name)
        disk_cache[key] = parts
        mark_disk_cache_dirty(cache_name)
    return parts


def parse_parts_index(vehicle_root: Path, asset_sources=None, cache_enabled=True):
    if asset_sources is not None:
        cache_key = tuple(source_signature(source) for source in asset_sources)
        if cache_enabled and cache_key in PART_INDEX_CACHE:
            return PART_INDEX_CACHE[cache_key]

        part_index = {}
        for source in sorted(asset_sources, key=lambda item: item.precedence):
            for part_name, part_data in parse_parts_for_source(source, cache_enabled).items():
                part_index[part_name] = PartDefinition(
                    name=part_name,
                    data=part_data,
                    source_path=Path(normalize_virtual_path(source.virtual_path)),
                )
        if cache_enabled:
            PART_INDEX_CACHE[cache_key] = part_index
            save_dirty_disk_caches()
        return part_index

    part_index = {}
    for jbeam_path in vehicle_root.rglob("*.jbeam"):
        try:
            payload = load_jsonc(jbeam_path)
        except Exception as exc:
            print(f"[BeamNG Importer] Failed to parse {jbeam_path}: {exc}")
            continue
        if not isinstance(payload, dict):
            continue
        for part_name, part_data in payload.items():
            if isinstance(part_data, dict):
                part_index[part_name] = PartDefinition(
                    name=part_name,
                    data=part_data,
                    source_path=jbeam_path,
                )
    return part_index


TOPOLOGY_SUBSET_PART_KEYS = {
    "information",
    "slotType",
    "slots",
    "slots2",
    "nodes",
    "beams",
    "triangles",
}
JBEAM_TOPOLOGY_DEFAULT_PRECISION = 3


def import_jbeam_topology_subset(source, source_path="", coordinate_precision=JBEAM_TOPOLOGY_DEFAULT_PRECISION):
    """Import the first supported topology subset from one external JBeam source.

    This path is intentionally separate from the vehicle resolver. It gives the
    new authoring pipeline a small, preservation-aware parse result without
    changing the existing slot/configuration workflow.
    """
    path = _existing_source_path(source)
    source_label = str(source_path or path or "")
    raw_bytes, text, source_diagnostics = _read_jbeam_import_source(source, path)
    cached_source = _cached_jbeam_source(raw_bytes, text, source_label)
    diagnostics = []
    diagnostics.extend(source_diagnostics)
    try:
        payload = load_jsonc_text(text)
    except Exception as exc:
        return JBeamTopologySubsetImport(
            source_path=source_label,
            cached_source=cached_source,
            diagnostics=[
                *diagnostics,
                JBeamImportDiagnostic(
                    level="error",
                    code="parse_failed",
                    message=f"Could not parse JBeam source: {exc}",
                )
            ],
        )

    if not isinstance(payload, dict):
        return JBeamTopologySubsetImport(
            source_path=source_label,
            cached_source=cached_source,
            diagnostics=[
                *diagnostics,
                JBeamImportDiagnostic(
                    level="error",
                    code="top_level_not_object",
                    message="JBeam source must be a top-level object mapping part names to part objects",
                )
            ],
        )

    parts = []
    for part_name, part_data in payload.items():
        if not isinstance(part_data, dict):
            diagnostic = JBeamImportDiagnostic(
                level="error",
                code="part_not_object",
                message=f"Skipping part '{part_name}' because its value is not an object",
                part_name=str(part_name),
            )
            diagnostics.append(diagnostic)
            continue
        part = import_jbeam_topology_subset_part(str(part_name), part_data, source_label, coordinate_precision)
        parts.append(part)
        diagnostics.extend(part.diagnostics)
    source_map = build_jbeam_topology_subset_source_map(text, payload, parts)
    import_identity_map = build_jbeam_topology_import_identity_map(parts, source_map)
    return JBeamTopologySubsetImport(
        source_path=source_label,
        coordinate_precision=coordinate_precision,
        cached_source=cached_source,
        source_map=source_map,
        import_identity_map=import_identity_map,
        export_metadata_mode="none",
        parts=parts,
        diagnostics=diagnostics,
    )


def _existing_source_path(source):
    if not isinstance(source, (str, Path)):
        return None
    try:
        path = Path(source)
        return path if path.exists() else None
    except (OSError, ValueError):
        return None


def _read_jbeam_import_source(source, path):
    diagnostics = []
    if path is not None:
        raw_bytes = path.read_bytes()
    elif isinstance(source, bytes):
        raw_bytes = source
    else:
        raw_bytes = str(source).encode("utf-8")

    try:
        text = raw_bytes.decode("utf-8-sig")
        if raw_bytes.startswith(b"\xef\xbb\xbf"):
            encoding = "utf-8-sig"
        else:
            encoding = "utf-8"
    except UnicodeDecodeError as exc:
        text = raw_bytes.decode("utf-8", errors="replace")
        encoding = "utf-8-replace"
        diagnostics.append(
            JBeamImportDiagnostic(
                level="warning",
                code="source_decode_uncertain",
                message=f"Source decoding used replacement characters: {exc}",
            )
        )
    diagnostics.append(
        JBeamImportDiagnostic(
            level="info",
            code="source_decoded",
            message=f"Source decoded as {encoding}",
        )
    )
    return raw_bytes, text, diagnostics


def _cached_jbeam_source(raw_bytes, text, source_path):
    return {
        "schema_version": 1,
        "source_path": str(source_path or ""),
        "original_bytes": raw_bytes,
        "decoded_text": text,
        "sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "encoding": "utf-8-sig" if raw_bytes.startswith(b"\xef\xbb\xbf") else "utf-8",
        "newline": _detect_newline_style(raw_bytes),
        "byte_count": len(raw_bytes),
        "line_count": len(text.splitlines()),
    }


def _detect_newline_style(raw_bytes):
    if b"\r\n" in raw_bytes:
        return "crlf"
    if b"\r" in raw_bytes:
        return "cr"
    if b"\n" in raw_bytes:
        return "lf"
    return "none"


def build_jbeam_topology_subset_source_map(text, payload, parts):
    lines = text.splitlines(keepends=True)
    source_map = {
        "schema_version": 1,
        "line_count": len(lines),
        "parts": {},
    }
    if not isinstance(payload, dict):
        return source_map

    line_ranges = _line_ranges(lines)
    for part in parts:
        part_name = part.part_name
        part_span = _span_for_named_json_value(text, line_ranges, part_name, "{")
        part_map = {
            "span": part_span,
            "sections": {},
            "unknown_preserved_sections": {},
        }
        part_data = payload.get(part_name, {})
        if isinstance(part_data, dict):
            for section_name in TOPOLOGY_SUBSET_PART_KEYS:
                if section_name in part_data:
                    section_span = _span_for_named_json_value(text, line_ranges, section_name)
                    section_map = {"span": section_span}
                    if section_name in {"nodes", "beams", "triangles"}:
                        section_map["rows"] = _row_spans_for_section(text, lines, line_ranges, section_span)
                    part_map["sections"][section_name] = section_map
            for section_name in part.unknown_preserved_sections:
                part_map["unknown_preserved_sections"][section_name] = {
                    "span": _span_for_named_json_value(text, line_ranges, section_name)
                }
        source_map["parts"][part_name] = part_map
    return source_map


def _line_ranges(lines):
    ranges = []
    offset = 0
    for line_number, line in enumerate(lines, start=1):
        start = offset
        offset += len(line)
        ranges.append((line_number, start, offset))
    return ranges


def _line_for_offset(line_ranges, offset):
    for line_number, start, end in line_ranges:
        if start <= offset < end:
            return line_number
    return line_ranges[-1][0] if line_ranges else 1


def _span_dict(line_ranges, start_offset, end_offset):
    if start_offset < 0:
        return {"start_line": -1, "end_line": -1, "start_offset": -1, "end_offset": -1}
    return {
        "start_line": _line_for_offset(line_ranges, start_offset),
        "end_line": _line_for_offset(line_ranges, max(start_offset, end_offset - 1)),
        "start_offset": start_offset,
        "end_offset": end_offset,
    }


def _span_for_named_json_value(text, line_ranges, name, preferred_open=""):
    pattern = re.compile(rf'"{re.escape(str(name))}"\s*:')
    match = pattern.search(text)
    if match is None:
        return _span_dict(line_ranges, -1, -1)
    value_start = match.end()
    while value_start < len(text) and text[value_start].isspace():
        value_start += 1
    if preferred_open and value_start < len(text) and text[value_start] != preferred_open:
        return _span_dict(line_ranges, match.start(), value_start)
    if value_start < len(text) and text[value_start] in "{[":
        value_end = _find_matching_jsonc_delimiter(text, value_start)
    else:
        value_end = _find_jsonc_scalar_end(text, value_start)
    return _span_dict(line_ranges, match.start(), value_end)


def _find_matching_jsonc_delimiter(text, start):
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    index = start
    in_string = False
    escape = False
    line_comment = False
    block_comment = False
    while index < len(text):
        char = text[index]
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if line_comment:
            if char in "\r\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if char == "*" and nxt == "/":
                block_comment = False
                index += 2
            else:
                index += 1
            continue
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == "/" and nxt == "/":
            line_comment = True
            index += 2
            continue
        if char == "/" and nxt == "*":
            block_comment = True
            index += 2
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return len(text)


def _find_jsonc_scalar_end(text, start):
    index = start
    while index < len(text) and text[index] not in ",\r\n]}":
        index += 1
    return index


def _row_spans_for_section(text, lines, line_ranges, section_span):
    rows = []
    start_line = section_span.get("start_line", -1)
    end_line = section_span.get("end_line", -1)
    if start_line < 1 or end_line < start_line:
        return rows
    for line_number in range(start_line, end_line + 1):
        line_text = lines[line_number - 1]
        stripped = line_text.strip()
        if not stripped or stripped.startswith("//"):
            continue
        if re.match(r'^"[A-Za-z0-9_./:-]+"\s*:', stripped):
            continue
        if stripped.startswith("[") or stripped.startswith("{"):
            line_start_offset = line_ranges[line_number - 1][1]
            row_start = line_start_offset + line_text.find(stripped[0])
            row_end = line_start_offset + len(line_text)
            rows.append(
                {
                    "row_index": len(rows),
                    "span": _span_dict(line_ranges, row_start, row_end),
                    "kind": "options" if stripped.startswith("{") else "row",
                }
            )
    return rows


def jbeam_decimal_places(value):
    text = str(value).strip()
    if not text or "e" in text.lower():
        return 0
    if "." not in text:
        return 0
    fractional = text.split(".", 1)[1]
    return len(fractional)


def import_jbeam_topology_subset_part(
    part_name,
    part_data,
    source_path="",
    coordinate_precision=JBEAM_TOPOLOGY_DEFAULT_PRECISION,
):
    diagnostics = []
    unknown_sections = {key: value for key, value in part_data.items() if key not in TOPOLOGY_SUBSET_PART_KEYS}
    for key in unknown_sections:
        diagnostics.append(
            JBeamImportDiagnostic(
                level="info",
                code="unsupported_preserved",
                message=f"Preserved unsupported section or field '{key}'",
                part_name=part_name,
                section=str(key),
            )
        )

    nodes = _import_topology_subset_nodes(part_name, part_data.get("nodes"), diagnostics, coordinate_precision)
    node_ids = {node.node_id for node in nodes}
    beams = _import_topology_subset_beams(part_name, part_data.get("beams"), node_ids, diagnostics)
    triangles = _import_topology_subset_triangles(part_name, part_data.get("triangles"), node_ids, diagnostics)

    information = part_data.get("information", {})
    if information is not None and not isinstance(information, dict):
        diagnostics.append(
            JBeamImportDiagnostic(
                level="warning",
                code="information_not_object",
                message="Part information is not an object and was not imported as metadata",
                part_name=part_name,
                section="information",
            )
        )
        information = {}

    slots = []
    for section_name in ("slots2", "slots"):
        section = part_data.get(section_name)
        if section is None:
            continue
        if isinstance(section, list):
            slots.append({"section": section_name, "rows": section})
        else:
            diagnostics.append(
                JBeamImportDiagnostic(
                    level="warning",
                    code="slots_not_list",
                    message=f"{section_name} is not a list and was preserved but not imported",
                    part_name=part_name,
                    section=section_name,
                )
            )

    return ImportedJBeamPart(
        part_name=part_name,
        part_guid=_new_internal_guid(),
        source_path=str(source_path or ""),
        information=jbeam_option_metadata(information),
        slot_type=part_data.get("slotType", ""),
        slots=slots,
        nodes=nodes,
        beams=beams,
        triangles=triangles,
        unknown_preserved_sections=unknown_sections,
        diagnostics=diagnostics,
    )


def _import_topology_subset_nodes(part_name, rows, diagnostics, coordinate_precision=JBEAM_TOPOLOGY_DEFAULT_PRECISION):
    nodes = []
    if rows is None:
        diagnostics.append(
            JBeamImportDiagnostic(
                level="warning",
                code="nodes_missing",
                message="Part has no nodes section",
                part_name=part_name,
                section="nodes",
            )
        )
        return nodes
    if not isinstance(rows, list):
        diagnostics.append(
            JBeamImportDiagnostic(
                level="error",
                code="nodes_not_list",
                message="Nodes section is not a list; topology editing is blocked for this part",
                part_name=part_name,
                section="nodes",
            )
        )
        return nodes

    current_options = {}
    for row_index, row in enumerate(rows):
        if isinstance(row, dict):
            current_options = merge_options(current_options, row)
            continue
        if not isinstance(row, list):
            diagnostics.append(_unsupported_row_diagnostic(part_name, "nodes", row_index, "node_row_not_list"))
            continue
        if row and str(row[0]).lower() == "id":
            continue
        if len(row) < 4:
            diagnostics.append(_unsupported_row_diagnostic(part_name, "nodes", row_index, "node_row_too_short"))
            continue
        inline_options = row[4] if len(row) > 4 and isinstance(row[4], dict) else {}
        options = merge_options(current_options, inline_options)
        original_position = (
            coerce_number(row[1], 0.0),
            coerce_number(row[2], 0.0),
            coerce_number(row[3], 0.0),
        )
        source_precision = max(jbeam_decimal_places(row[1]), jbeam_decimal_places(row[2]), jbeam_decimal_places(row[3]))
        if source_precision > int(coordinate_precision):
            diagnostics.append(
                JBeamImportDiagnostic(
                    level="warning",
                    code="node_precision_exceeds_project",
                    message=(
                        f"Node '{row[0]}' uses {source_precision} decimal place(s), "
                        f"above project precision {coordinate_precision}; coordinates preserved on import"
                    ),
                    part_name=part_name,
                    section="nodes",
                )
            )
        nodes.append(
            ImportedJBeamNode(
                node_id=str(row[0]),
                position=original_position,
                original_position=original_position,
                topology_guid=_new_internal_guid(),
                options=jbeam_option_metadata(options),
                row_index=row_index,
            )
        )
    return nodes


def rounded_jbeam_import_position(position, precision=JBEAM_TOPOLOGY_DEFAULT_PRECISION):
    return tuple(round(float(value), int(precision)) for value in position[:3])


def formatted_jbeam_import_position(position, precision=JBEAM_TOPOLOGY_DEFAULT_PRECISION):
    return [formatted_jbeam_import_number(value, precision) for value in position[:3]]


def formatted_jbeam_import_number(value, precision=JBEAM_TOPOLOGY_DEFAULT_PRECISION):
    rounded = round(float(value), int(precision))
    if rounded == 0:
        rounded = 0
    return f"{rounded:.{int(precision)}f}".rstrip("0").rstrip(".") or "0"


def _import_topology_subset_beams(part_name, rows, node_ids, diagnostics):
    beams = []
    if rows is None:
        return beams
    if not isinstance(rows, list):
        diagnostics.append(
            JBeamImportDiagnostic(
                level="error",
                code="beams_not_list",
                message="Beams section is not a list; beam editing is blocked for this part",
                part_name=part_name,
                section="beams",
            )
        )
        return beams

    current_options = {}
    for row_index, row in enumerate(rows):
        if isinstance(row, dict):
            current_options = merge_options(current_options, row)
            continue
        if not isinstance(row, list):
            diagnostics.append(_unsupported_row_diagnostic(part_name, "beams", row_index, "beam_row_not_list"))
            continue
        if row and str(row[0]).lower().startswith("id"):
            continue
        if len(row) < 2:
            diagnostics.append(_unsupported_row_diagnostic(part_name, "beams", row_index, "beam_row_too_short"))
            continue
        inline_options = row[2] if len(row) > 2 and isinstance(row[2], dict) else {}
        options = merge_options(current_options, inline_options)
        id1 = str(row[0])
        id2 = str(row[1])
        missing = tuple(node_id for node_id in (id1, id2) if node_id not in node_ids)
        if missing:
            diagnostics.append(
                JBeamImportDiagnostic(
                    level="warning",
                    code="beam_missing_local_node",
                    message=f"Beam references node(s) not owned by this imported part: {', '.join(missing)}",
                    part_name=part_name,
                    section="beams",
                )
            )
        beams.append(
            ImportedJBeamBeam(
                id1=id1,
                id2=id2,
                topology_guid=_new_internal_guid(),
                options=jbeam_option_metadata(options),
                row_index=row_index,
                missing_nodes=missing,
            )
        )
    return beams


def _import_topology_subset_triangles(part_name, rows, node_ids, diagnostics):
    triangles = []
    if rows is None:
        return triangles
    if not isinstance(rows, list):
        diagnostics.append(
            JBeamImportDiagnostic(
                level="error",
                code="triangles_not_list",
                message="Triangles section is not a list; triangle editing is blocked for this part",
                part_name=part_name,
                section="triangles",
            )
        )
        return triangles

    current_options = {}
    for row_index, row in enumerate(rows):
        if isinstance(row, dict):
            current_options = merge_options(current_options, row)
            continue
        if not isinstance(row, list):
            diagnostics.append(_unsupported_row_diagnostic(part_name, "triangles", row_index, "triangle_row_not_list"))
            continue
        if row and str(row[0]).lower().startswith("id"):
            continue
        if len(row) < 3:
            diagnostics.append(_unsupported_row_diagnostic(part_name, "triangles", row_index, "triangle_row_too_short"))
            continue
        inline_options = row[3] if len(row) > 3 and isinstance(row[3], dict) else {}
        options = merge_options(current_options, inline_options)
        ids = (str(row[0]), str(row[1]), str(row[2]))
        missing = tuple(node_id for node_id in ids if node_id not in node_ids)
        if missing:
            diagnostics.append(
                JBeamImportDiagnostic(
                    level="warning",
                    code="triangle_missing_local_node",
                    message=f"Triangle references node(s) not owned by this imported part: {', '.join(missing)}",
                    part_name=part_name,
                    section="triangles",
                )
            )
        triangles.append(
            ImportedJBeamTriangle(
                id1=ids[0],
                id2=ids[1],
                id3=ids[2],
                topology_guid=_new_internal_guid(),
                options=jbeam_option_metadata(options),
                row_index=row_index,
                missing_nodes=missing,
            )
        )
    return triangles


def _unsupported_row_diagnostic(part_name, section, row_index, code):
    return JBeamImportDiagnostic(
        level="warning",
        code=code,
        message=f"Skipped unsupported {section} row at index {row_index}",
        part_name=part_name,
        section=section,
    )


def _new_internal_guid():
    return str(uuid.uuid4())


def build_jbeam_topology_import_identity_map(parts, source_map):
    identity = {
        "schema_version": 1,
        "parts": {},
        "topology": {},
    }
    source_parts = source_map.get("parts", {}) if isinstance(source_map, dict) else {}
    for part in parts:
        part_source_map = source_parts.get(part.part_name, {})
        identity["parts"][part.part_guid] = {
            "part_name": part.part_name,
            "source_path": part.source_path,
            "source_span": part_source_map.get("span", {}),
            "evidence": {
                "type": "part_name",
                "value": part.part_name,
            },
        }
        sections = part_source_map.get("sections", {})
        _add_node_identity(identity, part, sections.get("nodes", {}).get("rows", []))
        _add_beam_identity(identity, part, sections.get("beams", {}).get("rows", []))
        _add_triangle_identity(identity, part, sections.get("triangles", {}).get("rows", []))
    return identity


def _data_row_spans(rows):
    return [row.get("span", {}) for row in rows if row.get("kind") == "row"]


def _add_node_identity(identity, part, rows):
    row_spans = _data_row_spans(rows)
    for index, node in enumerate(part.nodes):
        identity["topology"][node.topology_guid] = {
            "kind": "node",
            "part_guid": part.part_guid,
            "part_name": part.part_name,
            "external_id": node.node_id,
            "source_path": part.source_path,
            "source_row_index": node.row_index,
            "source_span": row_spans[index] if index < len(row_spans) else {},
            "evidence": {
                "type": "node_id",
                "value": node.node_id,
            },
        }


def _add_beam_identity(identity, part, rows):
    row_spans = _data_row_spans(rows)
    for index, beam in enumerate(part.beams):
        identity["topology"][beam.topology_guid] = {
            "kind": "beam",
            "part_guid": part.part_guid,
            "part_name": part.part_name,
            "external_id": [beam.id1, beam.id2],
            "source_path": part.source_path,
            "source_row_index": beam.row_index,
            "source_span": row_spans[index] if index < len(row_spans) else {},
            "evidence": {
                "type": "beam_endpoints",
                "value": [beam.id1, beam.id2],
            },
        }


def _add_triangle_identity(identity, part, rows):
    row_spans = _data_row_spans(rows)
    for index, triangle in enumerate(part.triangles):
        identity["topology"][triangle.topology_guid] = {
            "kind": "triangle",
            "part_guid": part.part_guid,
            "part_name": part.part_name,
            "external_id": [triangle.id1, triangle.id2, triangle.id3],
            "source_path": part.source_path,
            "source_row_index": triangle.row_index,
            "source_span": row_spans[index] if index < len(row_spans) else {},
            "evidence": {
                "type": "triangle_nodes",
                "value": [triangle.id1, triangle.id2, triangle.id3],
            },
        }


def parse_slots(part_data):
    slots = []
    rows = part_data.get("slots2", [])
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, list) or len(row) < 4:
                continue
            if row and row[0] == "name":
                continue
            options = row[5] if len(row) > 5 and isinstance(row[5], dict) else {}
            allow_types = row[1] if isinstance(row[1], list) else []
            deny_types = row[2] if len(row) > 2 and isinstance(row[2], list) else []
            slots.append(
                {
                    "name": row[0],
                    "allow_types": allow_types,
                    "deny_types": deny_types,
                    "default": row[3] or "",
                    "options": options,
                    "core_slot": bool(options.get("coreSlot")),
                }
            )

    legacy_rows = part_data.get("slots", [])
    if isinstance(legacy_rows, list):
        for row in legacy_rows:
            if not isinstance(row, list) or len(row) < 2:
                continue
            if row and row[0] == "type":
                continue
            options = row[3] if len(row) > 3 and isinstance(row[3], dict) else {}
            slot_name = row[0]
            default_part = row[1] or ""
            if not slot_name:
                continue
            slots.append(
                {
                    "name": slot_name,
                    "allow_types": [slot_name],
                    "deny_types": [],
                    "default": default_part,
                    "options": options,
                    "core_slot": bool(options.get("coreSlot")),
                }
            )
    return slots


def parse_flexbodies(part_def: PartDefinition, base_transform: Matrix, component_context=None, resolved_part_id=-1):
    results = []
    rows = part_def.data.get("flexbodies", [])
    if not isinstance(rows, list):
        return results

    current_options = {}
    for row in rows:
        if isinstance(row, dict):
            current_options = merge_options(current_options, row)
            continue
        if not isinstance(row, list) or len(row) < 2:
            continue
        if row and row[0] == "mesh":
            continue
        inline_options = row[3] if len(row) > 3 and isinstance(row[3], dict) else {}
        options = merge_options(current_options, inline_options)
        if is_disabled(options, component_context):
            continue
        mesh_name = evaluate_mesh_name(row[0], component_context)
        if not mesh_name or mesh_name.startswith("no"):
            continue
        local_pos = vector_from_dict(options.get("pos"), (0.0, 0.0, 0.0), component_context)
        rot = vector_from_dict(options.get("rot"), (0.0, 0.0, 0.0), component_context)
        scale = vector_from_dict(options.get("scale"), (1.0, 1.0, 1.0), component_context)
        local_transform = matrix_from_trs(local_pos, rot, scale)
        final_transform = base_transform @ local_transform
        results.append(
            FlexbodySpec(
                mesh=mesh_name,
                part_name=part_def.name,
                jbeam_path=part_def.source_path,
                transform_matrix=final_transform,
                pos=final_transform.to_translation(),
                rot=rot,
                scale=scale,
                resolved_part_id=resolved_part_id,
            )
        )
    return results


def parse_nodes(part_def: PartDefinition, base_transform: Matrix, component_context=None):
    nodes = {}
    rows = part_def.data.get("nodes", [])
    if not isinstance(rows, list):
        return nodes

    current_options = {}
    for row in rows:
        if isinstance(row, dict):
            current_options = merge_options(current_options, row)
            continue
        if not isinstance(row, list) or len(row) < 4:
            continue
        if row and row[0] == "id":
            continue

        inline_options = row[4] if len(row) > 4 and isinstance(row[4], dict) else {}
        options = merge_options(current_options, inline_options)
        if is_disabled(options, component_context):
            continue

        pos = Vector(
            (
                coerce_number(row[1], 0.0, component_context),
                coerce_number(row[2], 0.0, component_context),
                coerce_number(row[3], 0.0, component_context),
            )
        )
        nodes[str(row[0])] = base_transform @ pos
    return nodes


def jbeam_option_metadata(options):
    try:
        return json.loads(json.dumps(options or {}, default=str))
    except (TypeError, ValueError):
        return {str(key): str(value) for key, value in (options or {}).items()}


def parse_node_options(part_def: PartDefinition, component_context=None):
    node_options = {}
    rows = part_def.data.get("nodes", [])
    if not isinstance(rows, list):
        return node_options

    current_options = {}
    for row in rows:
        if isinstance(row, dict):
            current_options = merge_options(current_options, row)
            continue
        if not isinstance(row, list) or len(row) < 4:
            continue
        if row and row[0] == "id":
            continue

        inline_options = row[4] if len(row) > 4 and isinstance(row[4], dict) else {}
        options = merge_options(current_options, inline_options)
        if is_disabled(options, component_context):
            continue
        node_options[str(row[0])] = jbeam_option_metadata(options)
    return node_options


def parse_beams(
    part_def: PartDefinition,
    local_node_positions=None,
    global_node_positions=None,
    component_context=None,
    resolved_part_id=-1,
):
    beams = []
    rows = part_def.data.get("beams", [])
    if not isinstance(rows, list):
        return beams
    local_node_positions = local_node_positions or {}
    global_node_positions = global_node_positions or {}

    def lookup_node(name):
        key = str(name)
        if key in local_node_positions:
            return local_node_positions[key]
        return global_node_positions.get(key)

    current_options = {}
    for row in rows:
        if isinstance(row, dict):
            current_options = merge_options(current_options, row)
            continue
        if not isinstance(row, list) or len(row) < 2:
            continue
        if row and str(row[0]).lower().startswith("id"):
            continue

        inline_options = row[2] if len(row) > 2 and isinstance(row[2], dict) else {}
        options = merge_options(current_options, inline_options)
        if is_disabled(options, component_context):
            continue

        id1 = str(row[0])
        id2 = str(row[1])
        start = lookup_node(id1)
        end = lookup_node(id2)
        if start is None or end is None:
            continue

        beams.append(
            JBeamBeamSpec(
                id1=id1,
                id2=id2,
                start=start,
                end=end,
                part_name=part_def.name,
                resolved_part_id=resolved_part_id,
                options=jbeam_option_metadata(options),
            )
        )
    return beams


def parse_triangles(
    part_def: PartDefinition,
    local_node_positions=None,
    global_node_positions=None,
    component_context=None,
    resolved_part_id=-1,
):
    triangles = []
    rows = part_def.data.get("triangles", [])
    if not isinstance(rows, list):
        return triangles
    local_node_positions = local_node_positions or {}
    global_node_positions = global_node_positions or {}

    def lookup_node(name):
        key = str(name)
        if key in local_node_positions:
            return local_node_positions[key]
        return global_node_positions.get(key)

    current_options = {}
    for row in rows:
        if isinstance(row, dict):
            current_options = merge_options(current_options, row)
            continue
        if not isinstance(row, list) or len(row) < 3:
            continue
        if row and str(row[0]).lower().startswith("id"):
            continue

        inline_options = row[3] if len(row) > 3 and isinstance(row[3], dict) else {}
        options = merge_options(current_options, inline_options)
        if is_disabled(options, component_context):
            continue

        id1 = str(row[0])
        id2 = str(row[1])
        id3 = str(row[2])
        p1 = lookup_node(id1)
        p2 = lookup_node(id2)
        p3 = lookup_node(id3)
        if p1 is None or p2 is None or p3 is None:
            continue

        triangles.append(
            JBeamTriangleSpec(
                id1=id1,
                id2=id2,
                id3=id3,
                p1=p1,
                p2=p2,
                p3=p3,
                part_name=part_def.name,
                resolved_part_id=resolved_part_id,
                options=jbeam_option_metadata(options),
            )
        )
    return triangles


def parse_hydros(
    part_def: PartDefinition,
    local_node_positions=None,
    global_node_positions=None,
    component_context=None,
    resolved_part_id=-1,
):
    hydros = []
    rows = part_def.data.get("hydros", [])
    if not isinstance(rows, list):
        return hydros
    local_node_positions = local_node_positions or {}
    global_node_positions = global_node_positions or {}

    def lookup_node(name):
        key = str(name)
        if key in local_node_positions:
            return local_node_positions[key]
        return global_node_positions.get(key)

    current_options = {}
    for row in rows:
        if isinstance(row, dict):
            current_options = merge_options(current_options, row)
            continue
        if not isinstance(row, list) or len(row) < 2:
            continue
        if row and str(row[0]).lower().startswith("id"):
            continue

        inline_options = row[2] if len(row) > 2 and isinstance(row[2], dict) else {}
        options = merge_options(current_options, inline_options)
        if is_disabled(options, component_context):
            continue

        id1 = str(row[0])
        id2 = str(row[1])
        start = lookup_node(id1)
        end = lookup_node(id2)
        if start is None or end is None:
            continue

        hydros.append(
            JBeamHydroSpec(
                id1=id1,
                id2=id2,
                start=start,
                end=end,
                part_name=part_def.name,
                resolved_part_id=resolved_part_id,
                input_source=str(options.get("inputSource", "")),
                factor=str(options.get("factor", options.get("inputFactor", ""))),
            )
        )
    return hydros


def parse_rails_and_slidenodes(
    part_def: PartDefinition,
    local_node_positions=None,
    global_node_positions=None,
    component_context=None,
    resolved_part_id=-1,
):
    rails = []
    slidenodes = []
    rails_by_name = {}
    local_node_positions = local_node_positions or {}
    global_node_positions = global_node_positions or {}

    def lookup_node(name):
        key = str(name)
        if key in local_node_positions:
            return local_node_positions[key]
        return global_node_positions.get(key)

    def add_rail(name, links, data=None):
        if not name or not isinstance(links, list) or len(links) < 2:
            return
        node_ids = tuple(str(value) for value in links)
        points = tuple(lookup_node(value) for value in node_ids)
        if any(point is None for point in points):
            return
        data = data or {}
        rail = JBeamRailSpec(
            name=str(name),
            node_ids=node_ids,
            points=points,
            part_name=part_def.name,
            resolved_part_id=resolved_part_id,
            capped=str(data.get("capped", "")),
            looped=str(data.get("looped", "")),
        )
        rails.append(rail)
        rails_by_name[rail.name] = rail

    raw_rails = part_def.data.get("rails", {})
    if isinstance(raw_rails, dict):
        for name, data in raw_rails.items():
            if not isinstance(data, dict):
                continue
            add_rail(name, data.get("links:", data.get("links", [])), data)

    raw_rails2 = part_def.data.get("rails2", [])
    if isinstance(raw_rails2, list):
        for row in raw_rails2:
            if isinstance(row, dict):
                continue
            if not isinstance(row, list) or len(row) < 2:
                continue
            if row and str(row[0]).lower().startswith("id"):
                continue
            data = {}
            if len(row) > 3:
                data["looped"] = row[3]
            if len(row) > 4:
                data["capped"] = row[4]
            add_rail(str(row[0]), row[1], data)

    rows = part_def.data.get("slidenodes", [])
    if not isinstance(rows, list):
        return rails, slidenodes

    current_options = {}
    for row in rows:
        if isinstance(row, dict):
            current_options = merge_options(current_options, row)
            continue
        if not isinstance(row, list) or len(row) < 2:
            continue
        if row and str(row[0]).lower().startswith("id"):
            continue

        inline_options = row[-1] if row and isinstance(row[-1], dict) else {}
        options = merge_options(current_options, inline_options)
        if is_disabled(options, component_context):
            continue

        node_id = str(row[0])
        rail_name = str(row[1])
        position = lookup_node(node_id)
        rail = rails_by_name.get(rail_name)
        if position is None or rail is None:
            continue

        slidenodes.append(
            JBeamSlidenodeSpec(
                node_id=node_id,
                rail_name=rail_name,
                position=position,
                rail_points=rail.points,
                part_name=part_def.name,
                resolved_part_id=resolved_part_id,
                attached=str(row[2]) if len(row) > 2 else "",
                fix_to_rail=str(row[3]) if len(row) > 3 else "",
            )
        )
    return rails, slidenodes


def get_prop_anchor(row, local_node_positions, global_node_positions):
    if len(row) < 5:
        return None, None, {"missing": tuple()}

    local_node_positions = local_node_positions or {}
    global_node_positions = global_node_positions or {}

    def lookup_node(name):
        key = str(name)
        if key in local_node_positions:
            return local_node_positions[key]
        return global_node_positions.get(key)

    origin = lookup_node(row[2])
    x_ref = lookup_node(row[3])
    y_ref = lookup_node(row[4])
    if origin is None or x_ref is None or y_ref is None:
        missing = []
        for name, value in ((row[2], origin), (row[3], x_ref), (row[4], y_ref)):
            if value is None:
                missing.append(str(name))
        return None, None, {"missing": tuple(missing)}

    rotation = matrix_from_prop_axes(origin, x_ref - origin, y_ref - origin)
    rotation.translation = Vector((0.0, 0.0, 0.0))
    rotation_3x3 = rotation.to_3x3()
    debug = {
        "origin": origin,
        "x_ref": x_ref,
        "y_ref": y_ref,
        "x_axis": rotation_3x3.col[0],
        "y_axis": rotation_3x3.col[1],
        "z_axis": rotation_3x3.col[2],
        "determinant": rotation_3x3.determinant(),
    }
    return origin, rotation, debug


def prop_row_dict(row, index):
    return row[index] if len(row) > index and isinstance(row[index], dict) else {}


def prop_row_number(row, index, fallback, component_context=None):
    return coerce_number(row[index], fallback, component_context) if len(row) > index else float(fallback)


def first_vector_option(options, names, default=(0.0, 0.0, 0.0), component_context=None):
    for name in names:
        value = options.get(name)
        if isinstance(value, dict):
            return vector_from_dict(value, default, component_context), name
    return Vector(default), ""


def prop_static_function_factor(row, component_context=None):
    # BeamNG evaluates prop animation as clamp(input * multiplier, min, max) + offset.
    # For static import previews we use input=0, so min/max still matter and offset is
    # not multiplied.
    min_value = prop_row_number(row, 8, 0.0, component_context)
    max_value = prop_row_number(row, 9, 100.0, component_context)
    offset = prop_row_number(row, 10, 0.0, component_context)
    multiplier = prop_row_number(row, 11, 1.0, component_context)
    animated = 0.0 * multiplier
    return min(max(animated, min_value), max_value) + offset


def parse_props(
    part_def: PartDefinition,
    base_transform: Matrix,
    component_context=None,
    global_node_positions=None,
    local_node_positions=None,
    resolved_part_id=-1,
):
    results = []
    rows = part_def.data.get("props", [])
    if not isinstance(rows, list):
        return results
    global_node_positions = global_node_positions or {}
    local_node_positions = local_node_positions or {}

    current_options = {}
    for row in rows:
        if isinstance(row, dict):
            current_options = merge_options(current_options, row)
            continue
        if not isinstance(row, list) or len(row) < 2:
            continue
        if row and row[0] == "func":
            continue

        inline_options = row[12] if len(row) > 12 and isinstance(row[12], dict) else {}
        options = merge_options(current_options, inline_options)
        if is_disabled(options, component_context):
            continue

        mesh_name = evaluate_mesh_name(row[1], component_context)
        if not mesh_name or mesh_name.startswith("no") or mesh_name.upper().endswith("LIGHT"):
            continue
        mesh_key = mesh_name.lower()
        keep_template_translation = False

        base_rotation = prop_row_dict(row, 5)
        row_rotation = prop_row_dict(row, 6)
        row_translation = prop_row_dict(row, 7)
        row_anim_factor = prop_static_function_factor(row, component_context)
        row_translation_vec = vector_from_dict(row_translation, (0.0, 0.0, 0.0), component_context)
        row_rotation_vec = vector_from_dict(row_rotation, (0.0, 0.0, 0.0), component_context)
        prop_anim_translation = row_translation_vec * row_anim_factor
        prop_base_translation = vector_from_dict(options.get("baseTranslation"), (0.0, 0.0, 0.0), component_context)
        prop_global_translation, prop_global_translation_source = first_vector_option(
            options,
            ("baseTranslationGlobal", "baseTranslationGlobalElastic", "baseTranslationGlobalRigid"),
            (0.0, 0.0, 0.0),
            component_context,
        )
        prop_local_translation = prop_anim_translation + prop_base_translation
        has_global_translation = bool(prop_global_translation_source)
        base_rotation_vec = vector_from_dict(base_rotation, (0.0, 0.0, 0.0), component_context)
        anim_rotation_vec = row_rotation_vec * row_anim_factor
        global_rotation_vec = vector_from_dict(
            options.get("baseRotationGlobal"), (0.0, 0.0, 0.0), component_context
        )
        has_global_rotation = isinstance(options.get("baseRotationGlobal"), dict)
        prop_rot = base_rotation_vec + anim_rotation_vec + global_rotation_vec
        local_prop_rotation = prop_base_rotation_matrix(base_rotation_vec) @ prop_anim_rotation_matrix(anim_rotation_vec)
        global_prop_rotation = prop_global_rotation_matrix(global_rotation_vec)
        anchor_origin, anchor_rotation, anchor_debug = get_prop_anchor(row, local_node_positions, global_node_positions)
        prop_world_translation_offset = Vector((0.0, 0.0, 0.0))
        prop_rest_matrix = Matrix.Identity(4)
        if anchor_origin is not None and anchor_rotation is not None:
            if has_global_translation:
                prop_world_translation_offset = anchor_rotation.to_3x3() @ prop_anim_translation
                final_origin = (base_transform @ prop_global_translation) + prop_world_translation_offset
            else:
                prop_world_translation_offset = anchor_rotation.to_3x3() @ prop_local_translation
                final_origin = anchor_origin + prop_world_translation_offset
            rest_rotation = global_prop_rotation @ prop_base_rotation_matrix(base_rotation_vec) if has_global_rotation else anchor_rotation @ prop_base_rotation_matrix(base_rotation_vec)
            final_rotation = global_prop_rotation @ local_prop_rotation if has_global_rotation else anchor_rotation @ local_prop_rotation
            final_transform = Matrix.Translation(final_origin) @ final_rotation
            prop_rest_matrix = Matrix.Translation(final_origin) @ rest_rotation
        else:
            if len(row) >= 5:
                print(
                    "[BeamNG Importer] Skipping prop "
                    f"'{mesh_name}' from '{part_def.name}' because anchor nodes are missing: "
                    f"{anchor_debug.get('missing', tuple())}"
                )
                continue
            if has_global_translation:
                fallback_translation = prop_global_translation + prop_anim_translation
            else:
                fallback_translation = prop_local_translation
            fallback_rotation = global_prop_rotation @ local_prop_rotation if has_global_rotation else local_prop_rotation
            final_transform = base_transform @ Matrix.Translation(fallback_translation) @ fallback_rotation
            rest_rotation = global_prop_rotation @ prop_base_rotation_matrix(base_rotation_vec) if has_global_rotation else prop_base_rotation_matrix(base_rotation_vec)
            prop_rest_matrix = base_transform @ Matrix.Translation(fallback_translation) @ rest_rotation
        results.append(
            FlexbodySpec(
                mesh=mesh_name,
                part_name=part_def.name,
                jbeam_path=part_def.source_path,
                transform_matrix=final_transform,
                prop_rest_matrix=prop_rest_matrix,
                pos=final_transform.to_translation(),
                rot=prop_rot,
                scale=Vector((1.0, 1.0, 1.0)),
                source_type="prop",
                use_template_transform=False,
                keep_template_translation=keep_template_translation,
                resolved_part_id=resolved_part_id,
                debug_anchor_nodes=tuple(str(value) for value in row[2:5]) if len(row) >= 5 else tuple(),
                debug_anchor_origin=tuple(round(value, 6) for value in anchor_debug.get("origin", ())),
                debug_anchor_x=tuple(round(value, 6) for value in anchor_debug.get("x_ref", ())),
                debug_anchor_y=tuple(round(value, 6) for value in anchor_debug.get("y_ref", ())),
                debug_missing_anchor_nodes=tuple(anchor_debug.get("missing", tuple())),
                debug_prop_base_translation=tuple(round(value, 6) for value in prop_base_translation),
                debug_prop_anim_translation=tuple(round(value, 6) for value in prop_anim_translation),
                debug_prop_local_translation=tuple(round(value, 6) for value in prop_local_translation),
                debug_prop_world_translation_offset=tuple(round(value, 6) for value in prop_world_translation_offset),
                debug_prop_global_translation=tuple(round(value, 6) for value in prop_global_translation),
                debug_prop_base_rotation=tuple(
                    round(value, 6)
                    for value in base_rotation_vec
                ),
                debug_prop_row_rotation=tuple(
                    round(value, 6)
                    for value in row_rotation_vec
                ),
                debug_prop_anim_factor=round(row_anim_factor, 6),
                debug_prop_anchor_x_axis=tuple(round(value, 6) for value in anchor_debug.get("x_axis", ())),
                debug_prop_anchor_y_axis=tuple(round(value, 6) for value in anchor_debug.get("y_axis", ())),
                debug_prop_anchor_z_axis=tuple(round(value, 6) for value in anchor_debug.get("z_axis", ())),
                debug_prop_anchor_determinant=round(float(anchor_debug.get("determinant", 0.0)), 6),
            )
        )
    return results


def spec_node_ids(spec):
    ids = []
    for attr in ("id1", "id2", "id3", "node_id"):
        value = getattr(spec, attr, None)
        if value:
            ids.append(str(value))
    return tuple(ids)


def resolved_part_ancestor_ids(part_id, parent_ids_by_part):
    ancestors = set()
    seen = set()
    current = parent_ids_by_part.get(part_id, -1)
    while current is not None and current >= 0 and current not in seen:
        ancestors.add(current)
        seen.add(current)
        current = parent_ids_by_part.get(current, -1)
    return ancestors


def resolved_part_descendant_ids(part_id, child_ids_by_part):
    descendants = set()
    pending = list(child_ids_by_part.get(part_id, ()))
    while pending:
        child_id = pending.pop()
        if child_id in descendants:
            continue
        descendants.add(child_id)
        pending.extend(child_ids_by_part.get(child_id, ()))
    return descendants


def build_resolved_vehicle_model(
    pc_path,
    pc_data,
    source_description,
    main_part,
    resolved_parts,
    flexbodies,
    visual_nodes,
    visual_beams,
    visual_triangles,
    visual_hydros,
    visual_rails,
    visual_slidenodes,
):
    nodes_by_part = defaultdict(list)
    node_ids_by_part = defaultdict(set)
    node_owner_part_ids = defaultdict(set)
    for node in visual_nodes:
        nodes_by_part[node.resolved_part_id].append(node)
        node_id = str(node.name)
        node_ids_by_part[node.resolved_part_id].add(node_id)
        node_owner_part_ids[node_id].add(node.resolved_part_id)

    beams_by_part = defaultdict(list)
    for beam in visual_beams:
        beams_by_part[beam.resolved_part_id].append(beam)

    triangles_by_part = defaultdict(list)
    for triangle in visual_triangles:
        triangles_by_part[triangle.resolved_part_id].append(triangle)

    hydros_by_part = defaultdict(list)
    for hydro in visual_hydros:
        hydros_by_part[hydro.resolved_part_id].append(hydro)

    rails_by_part = defaultdict(list)
    for rail in visual_rails:
        rails_by_part[rail.resolved_part_id].append(rail)

    slidenodes_by_part = defaultdict(list)
    for slidenode in visual_slidenodes:
        slidenodes_by_part[slidenode.resolved_part_id].append(slidenode)

    flexbodies_by_part = defaultdict(list)
    props_by_part = defaultdict(list)
    for spec in flexbodies:
        if spec.source_type == "prop":
            props_by_part[spec.resolved_part_id].append(spec)
        else:
            flexbodies_by_part[spec.resolved_part_id].append(spec)

    source_files = sorted({str(part.part_def.source_path) for part in resolved_parts})
    parent_ids_by_part = {part.id: part.parent_id for part in resolved_parts}
    child_ids_by_part = defaultdict(list)
    for part in resolved_parts:
        if part.parent_id >= 0:
            child_ids_by_part[part.parent_id].append(part.id)
    part_models = []
    external_ref_total = 0
    ancestor_ref_total = 0
    descendant_ref_total = 0
    cross_branch_ref_total = 0
    unresolved_ref_total = 0

    for part in resolved_parts:
        local_node_ids = node_ids_by_part[part.id]
        ancestor_ids = resolved_part_ancestor_ids(part.id, parent_ids_by_part)
        descendant_ids = resolved_part_descendant_ids(part.id, child_ids_by_part)
        external_refs = []
        ancestor_refs = []
        descendant_refs = []
        cross_branch_refs = []
        unresolved_refs = []
        for spec in (
            beams_by_part[part.id]
            + triangles_by_part[part.id]
            + hydros_by_part[part.id]
            + slidenodes_by_part[part.id]
        ):
            for node_id in spec_node_ids(spec):
                if node_id not in local_node_ids:
                    external_refs.append(node_id)
                    owner_ids = node_owner_part_ids.get(node_id, set())
                    if not owner_ids:
                        unresolved_refs.append(node_id)
                    elif owner_ids & ancestor_ids:
                        ancestor_refs.append(node_id)
                    elif owner_ids & descendant_ids:
                        descendant_refs.append(node_id)
                    else:
                        cross_branch_refs.append(node_id)

        external_refs = tuple(sorted(set(external_refs)))
        ancestor_refs = tuple(sorted(set(ancestor_refs)))
        descendant_refs = tuple(sorted(set(descendant_refs)))
        cross_branch_refs = tuple(sorted(set(cross_branch_refs)))
        unresolved_refs = tuple(sorted(set(unresolved_refs)))
        external_ref_total += len(external_refs)
        ancestor_ref_total += len(ancestor_refs)
        descendant_ref_total += len(descendant_refs)
        cross_branch_ref_total += len(cross_branch_refs)
        unresolved_ref_total += len(unresolved_refs)
        part_models.append(
            ResolvedVehiclePartModel(
                resolved_part_id=part.id,
                name=part.part_def.name,
                source_path=str(part.part_def.source_path),
                parent_id=part.parent_id,
                slot_name=part.slot_name,
                node_ids=tuple(sorted(local_node_ids)),
                beam_count=len(beams_by_part[part.id]),
                triangle_count=len(triangles_by_part[part.id]),
                hydro_count=len(hydros_by_part[part.id]),
                rail_count=len(rails_by_part[part.id]),
                slidenode_count=len(slidenodes_by_part[part.id]),
                flexbody_count=len(flexbodies_by_part[part.id]),
                prop_count=len(props_by_part[part.id]),
                external_node_refs=external_refs,
                ancestor_node_refs=ancestor_refs,
                descendant_node_refs=descendant_refs,
                cross_branch_node_refs=cross_branch_refs,
                unresolved_node_refs=unresolved_refs,
            )
        )

    return ResolvedVehicleModel(
        pc_path=str(pc_path),
        source_description=str(source_description or ""),
        vehicle_model=str(pc_data.get("model", "")) if isinstance(pc_data, dict) else "",
        main_part=str(main_part or ""),
        parts=part_models,
        node_count=len(visual_nodes),
        beam_count=len(visual_beams),
        triangle_count=len(visual_triangles),
        hydro_count=len(visual_hydros),
        rail_count=len(visual_rails),
        slidenode_count=len(visual_slidenodes),
        flexbody_count=sum(1 for spec in flexbodies if spec.source_type != "prop"),
        prop_count=sum(1 for spec in flexbodies if spec.source_type == "prop"),
        source_files=tuple(source_files),
        external_node_ref_count=external_ref_total,
        ancestor_node_ref_count=ancestor_ref_total,
        descendant_node_ref_count=descendant_ref_total,
        cross_branch_node_ref_count=cross_branch_ref_total,
        unresolved_node_ref_count=unresolved_ref_total,
        node_owner_part_ids={node_id: tuple(sorted(owner_ids)) for node_id, owner_ids in sorted(node_owner_part_ids.items())},
        part_external_node_refs={
            str(part_model.resolved_part_id): tuple(part_model.external_node_refs)
            for part_model in part_models
            if part_model.external_node_refs
        },
    )


def resolved_vehicle_model_report_lines(model: ResolvedVehicleModel):
    lines = [
        "[BeamNG Importer] Resolved vehicle model",
        f"PC path: {model.pc_path}",
    ]
    if model.source_description:
        lines.append(f"Selected source: {model.source_description}")
    lines.extend(
        [
            f"Vehicle model: {model.vehicle_model}",
            f"Main part: {model.main_part}",
            f"Resolved parts: {len(model.parts)}",
            f"Source JBeam files: {len(model.source_files)}",
            "",
            "Totals:",
            f"  nodes={model.node_count}",
            f"  beams={model.beam_count}",
            f"  triangles={model.triangle_count}",
            f"  hydros={model.hydro_count}",
            f"  rails={model.rail_count}",
            f"  slidenodes={model.slidenode_count}",
            f"  flexbodies={model.flexbody_count}",
            f"  props={model.prop_count}",
            f"  external_node_refs={model.external_node_ref_count}",
            f"  ancestor_node_refs={model.ancestor_node_ref_count}",
            f"  descendant_node_refs={model.descendant_node_ref_count}",
            f"  cross_branch_node_refs={model.cross_branch_node_ref_count}",
            f"  unresolved_node_refs={model.unresolved_node_ref_count}",
            "",
            "Reference categories:",
            "  ancestor_node_refs: child/descendant part references a parent/ancestor node.",
            "  descendant_node_refs: parent/ancestor part references a child/descendant node.",
            "  cross_branch_node_refs: reference is outside the direct slot ancestry and needs graph review.",
            "",
            "Parts:",
        ]
    )
    for part in model.parts:
        parent = "root" if part.parent_id < 0 else str(part.parent_id)
        slot = part.slot_name or "(root)"
        lines.extend(
            [
                f"- [{part.resolved_part_id:03d}] {part.name}",
                f"  slot={slot} parent={parent}",
                f"  source={part.source_path}",
                (
                    "  counts="
                    f"nodes:{len(part.node_ids)} "
                    f"beams:{part.beam_count} "
                    f"triangles:{part.triangle_count} "
                    f"hydros:{part.hydro_count} "
                    f"rails:{part.rail_count} "
                    f"slidenodes:{part.slidenode_count} "
                    f"flexbodies:{part.flexbody_count} "
                    f"props:{part.prop_count}"
                ),
            ]
        )
        if part.external_node_refs:
            preview = ", ".join(part.external_node_refs[:12])
            if len(part.external_node_refs) > 12:
                preview += f", ... (+{len(part.external_node_refs) - 12})"
            lines.append(f"  external_node_refs={preview}")
        if part.ancestor_node_refs:
            preview = ", ".join(part.ancestor_node_refs[:12])
            if len(part.ancestor_node_refs) > 12:
                preview += f", ... (+{len(part.ancestor_node_refs) - 12})"
            lines.append(f"  ancestor_node_refs={preview}")
        if part.descendant_node_refs:
            preview = ", ".join(part.descendant_node_refs[:12])
            if len(part.descendant_node_refs) > 12:
                preview += f", ... (+{len(part.descendant_node_refs) - 12})"
            lines.append(f"  descendant_node_refs={preview}")
        if part.cross_branch_node_refs:
            preview = ", ".join(part.cross_branch_node_refs[:12])
            if len(part.cross_branch_node_refs) > 12:
                preview += f", ... (+{len(part.cross_branch_node_refs) - 12})"
            lines.append(f"  cross_branch_node_refs={preview}")
        if part.unresolved_node_refs:
            preview = ", ".join(part.unresolved_node_refs[:12])
            if len(part.unresolved_node_refs) > 12:
                preview += f", ... (+{len(part.unresolved_node_refs) - 12})"
            lines.append(f"  unresolved_node_refs={preview}")
    return lines


def infer_main_part_name(pc_data, part_index):
    main_part = pc_data.get("mainPartName")
    if main_part:
        return main_part, "mainPartName"

    model_name = pc_data.get("model")
    if model_name in part_index:
        part_data = part_index[model_name].data
        if str(part_data.get("slotType", "")).lower() == "main":
            return model_name, "model main slot"

    main_slot_parts = [
        part_name
        for part_name, part_def in part_index.items()
        if str(part_def.data.get("slotType", "")).lower() == "main"
    ]
    if len(main_slot_parts) == 1:
        return main_slot_parts[0], "single main slot"
    if model_name in part_index:
        return model_name, "model fallback"
    return "", ""


def compatible_parts_for_slot(slot, part_index):
    allow_types = slot.get("allow_types") or [slot.get("name", "")]
    allow_types = {str(item) for item in allow_types if item}
    deny_types = {str(item) for item in slot.get("deny_types", []) if item}
    if not allow_types:
        allow_types = {str(slot.get("name", ""))}

    def part_slot_types(part_def):
        slot_type = part_def.data.get("slotType", "")
        if isinstance(slot_type, list):
            return {str(item) for item in slot_type if item}
        if slot_type:
            return {str(slot_type)}
        return set()

    return sorted(
        part_name
        for part_name, part_def in part_index.items()
        if (part_slot_types(part_def) & allow_types)
        and not (part_slot_types(part_def) & deny_types)
    )


def slot_option_items_for_storage(slot, selected_part, part_index):
    items = []
    if not slot.get("core_slot"):
        items.append({"identifier": "__EMPTY__", "name": "<Empty>", "description": "Leave this slot empty"})

    compatible_parts = compatible_parts_for_slot(slot, part_index)
    if selected_part and selected_part not in compatible_parts and selected_part in part_index:
        compatible_parts.append(selected_part)
        compatible_parts.sort()

    for part_name in compatible_parts:
        info = part_index[part_name].data.get("information", {})
        label = part_name
        if isinstance(info, dict) and info.get("name"):
            label = f"{part_name} - {info.get('name')}"
        items.append({"identifier": part_name, "name": label, "description": part_name})

    items.append({"identifier": "__NEW__", "name": "New....", "description": "Placeholder for creating a new part"})
    return items


def slot_choice_items(self, _context):
    try:
        items = json.loads(self.options_json) if self.options_json else []
    except Exception:
        items = []
    if not items:
        items = [{"identifier": "__NEW__", "name": "New....", "description": "Placeholder for creating a new part"}]
    return [
        (item.get("identifier", "__NEW__"), item.get("name", "New...."), item.get("description", ""))
        for item in items
    ]
