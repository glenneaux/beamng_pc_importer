bl_info = {
    "name": "BeamNG .pc Importer",
    "author": "Glenn Campigli",
    "version": (1, 0, 0),
    "blender": (3, 6, 0),
    "location": "File > Import > BeamNG Config (.pc)",
    "description": "Import a BeamNG .pc vehicle config with only the selected meshes visible",
    "category": "Import-Export",
}

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
from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy.types import AddonPreferences, Operator, Panel
from bpy_extras.io_utils import ImportHelper
from mathutils import Euler, Matrix, Vector


COLLADA_NS = {"c": "http://www.collada.org/2005/11/COLLADASchema"}

MESH_EDITING_ENABLED = "mesh_editing_enabled"
MESH_VEHICLE_MODEL = "vehicle_model"
MESH_JBEAM_PART = "jbeam_part"
MESH_JBEAM_FILE_PATH = "jbeam_file_path"


@dataclass
class FlexbodySpec:
    mesh: str
    part_name: str
    jbeam_path: Path
    transform_matrix: Matrix = field(default_factory=lambda: Matrix.Identity(4))
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
    debug_prop_local_translation: tuple = field(default_factory=tuple)
    debug_prop_global_translation: tuple = field(default_factory=tuple)
    debug_prop_base_rotation: tuple = field(default_factory=tuple)
    debug_prop_row_rotation: tuple = field(default_factory=tuple)
    debug_prop_anim_factor: float = 0.0


@dataclass
class PartDefinition:
    name: str
    data: dict
    source_path: Path


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


@dataclass
class JBeamBeamSpec:
    id1: str
    id2: str
    start: Vector
    end: Vector
    part_name: str
    resolved_part_id: int = -1


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
    cleaned = strip_trailing_commas(insert_missing_commas(strip_json_comments(text)))
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Failed to parse JSONC file {path}: {describe_json_error(cleaned, exc)}") from exc


def load_jsonc_text(text: str):
    cleaned = strip_trailing_commas(insert_missing_commas(strip_json_comments(text)))
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


def prop_base_rotation_matrix(rot: Vector):
    # BeamNG docs: props baseRotation uses intrinsic Euler -X -Z +Y.
    return Euler(
        (math.radians(-rot.x), math.radians(rot.y), math.radians(-rot.z)),
        "XZY",
    ).to_matrix().to_4x4()


def prop_anim_rotation_matrix(rot: Vector):
    # BeamNG docs: props animated rotation uses intrinsic Euler -X -Z -Y.
    return Euler(
        (math.radians(-rot.x), math.radians(-rot.y), math.radians(-rot.z)),
        "XZY",
    ).to_matrix().to_4x4()


def prop_global_rotation_matrix(rot: Vector):
    # BeamNG docs: props baseRotationGlobal uses intrinsic Euler +Y +Z +X.
    return Euler(
        (math.radians(rot.x), math.radians(rot.y), math.radians(rot.z)),
        "YZX",
    ).to_matrix().to_4x4()


def matrix_from_axes(origin, x_axis, y_axis):
    x_axis = x_axis.normalized() if x_axis.length > 0.000001 else Vector((1.0, 0.0, 0.0))
    y_axis = y_axis - x_axis * y_axis.dot(x_axis)
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


def _pc_vehicle_from_virtual_path(virtual_path: str):
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
    for path in sorted(root.rglob("*.pc")):
        if not path.is_file():
            continue
        virtual_path = normalize_virtual_path(path.relative_to(root))
        if not virtual_path.lower().startswith("vehicles/"):
            virtual_path = normalize_virtual_path(Path("vehicles") / virtual_path)
        vehicle_name = _pc_vehicle_from_virtual_path(virtual_path)
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
        vehicle_name = _pc_vehicle_from_virtual_path(virtual_path)
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


def collect_beamng_asset_sources(pc_path: Path, user_folder: str = "", vanilla_folder: str = "", cache_enabled=True):
    vehicle_name = pc_path.parent.name
    selected_vehicle_root = pc_path.parent
    current_folder = resolve_user_current_folder(user_folder, pc_path)
    vanilla_vehicles = resolve_vanilla_vehicles_folder(vanilla_folder)
    vanilla_vehicle_root = vehicle_folder_from_vehicles_root(vanilla_vehicles, vehicle_name) if vanilla_vehicles else None

    jbeam_sources = []
    dae_sources = []

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

    if current_folder:
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

        add_vehicle_root(current_folder / "vehicles" / vehicle_name, 30)

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
            slots.append(
                {
                    "name": row[0],
                    "allow_types": allow_types,
                    "default": row[3] or "",
                    "options": options,
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
                    "default": default_part,
                    "options": options,
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

    rotation = matrix_from_axes(origin, x_ref - origin, y_ref - origin)
    rotation.translation = Vector((0.0, 0.0, 0.0))
    debug = {
        "origin": origin,
        "x_ref": x_ref,
        "y_ref": y_ref,
    }
    return origin, rotation, debug


def prop_row_dict(row, index):
    return row[index] if len(row) > index and isinstance(row[index], dict) else {}


def prop_row_number(row, index, fallback, component_context=None):
    return coerce_number(row[index], fallback, component_context) if len(row) > index else float(fallback)


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

    for row in rows:
        if not isinstance(row, list) or len(row) < 2:
            continue
        if row and row[0] == "func":
            continue

        options = row[12] if len(row) > 12 and isinstance(row[12], dict) else {}
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
        row_base_translation = prop_row_dict(row, 9)
        row_anim_factor = (
            prop_row_number(row, 10, 0.0, component_context)
            * prop_row_number(row, 11, 1.0, component_context)
        )
        row_translation_vec = vector_from_dict(row_translation, (0.0, 0.0, 0.0), component_context)
        row_rotation_vec = vector_from_dict(row_rotation, (0.0, 0.0, 0.0), component_context)
        prop_local_translation = (
            (row_translation_vec * row_anim_factor)
            + vector_from_dict(row_base_translation, (0.0, 0.0, 0.0), component_context)
            + vector_from_dict(options.get("baseTranslation"), (0.0, 0.0, 0.0), component_context)
        )
        prop_global_translation = vector_from_dict(
            options.get("baseTranslationGlobal"), (0.0, 0.0, 0.0), component_context
        )
        base_rotation_vec = vector_from_dict(base_rotation, (0.0, 0.0, 0.0), component_context)
        anim_rotation_vec = row_rotation_vec * row_anim_factor
        global_rotation_vec = vector_from_dict(
            options.get("baseRotationGlobal"), (0.0, 0.0, 0.0), component_context
        )
        prop_rot = base_rotation_vec + anim_rotation_vec + global_rotation_vec
        prop_rotation = (
            prop_base_rotation_matrix(base_rotation_vec)
            @ prop_anim_rotation_matrix(anim_rotation_vec)
            @ prop_global_rotation_matrix(global_rotation_vec)
        )
        anchor_origin, anchor_rotation, anchor_debug = get_prop_anchor(row, local_node_positions, global_node_positions)
        if anchor_origin is not None and anchor_rotation is not None:
            offset = anchor_rotation.to_3x3() @ prop_local_translation
            final_origin = anchor_origin + offset
            if prop_global_translation.length > 0.000001:
                final_origin = base_transform @ (prop_global_translation + prop_local_translation)
            axis_correction = Euler((math.radians(270.0), 0.0, 0.0), "XYZ").to_matrix().to_4x4()
            final_transform = (
                Matrix.Translation(final_origin)
                @ anchor_rotation
                @ axis_correction
                @ prop_rotation
            )
        else:
            if len(row) >= 5:
                print(
                    "[BeamNG Importer] Skipping prop "
                    f"'{mesh_name}' from '{part_def.name}' because anchor nodes are missing: "
                    f"{anchor_debug.get('missing', tuple())}"
                )
                continue
            fallback_translation = prop_local_translation
            if prop_global_translation.length > 0.000001:
                fallback_translation = prop_global_translation + prop_local_translation
            final_transform = base_transform @ Matrix.Translation(fallback_translation) @ prop_rotation
        results.append(
            FlexbodySpec(
                mesh=mesh_name,
                part_name=part_def.name,
                jbeam_path=part_def.source_path,
                transform_matrix=final_transform,
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
                debug_prop_local_translation=tuple(round(value, 6) for value in prop_local_translation),
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
            )
        )
    return results


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


def build_dae_name_index(dae_path: Path):
    names = set()
    try:
        for _event, element in ET.iterparse(str(dae_path), events=("start",)):
            tag = element.tag.rsplit("}", 1)[-1]
            if tag not in {"node", "geometry"}:
                continue
            for attr in ("id", "name"):
                value = element.get(attr)
                if value:
                    names.add(normalized_name(value))
    except Exception as exc:
        print(f"[BeamNG Importer] Failed to parse DAE {dae_path}: {exc}")
        return names
    return names


def build_dae_name_index_from_text(xml_text: str):
    names = set()
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return names

    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag not in {"node", "geometry"}:
            continue
        for attr in ("id", "name"):
            value = element.get(attr)
            if value:
                names.add(normalized_name(value))
    return names


def find_common_asset_roots(vehicle_root: Path):
    common_dirs = []
    common_zip_paths = []

    search_roots = [vehicle_root, *vehicle_root.parents]
    seen_dirs = set()
    seen_zips = set()

    for root in search_roots:
        extracted_common = root / "common" / "vehicles" / "common"
        if extracted_common.exists() and extracted_common.is_dir():
            resolved = extracted_common.resolve()
            if resolved not in seen_dirs:
                common_dirs.append(extracted_common)
                seen_dirs.add(resolved)

        common_zip = root / "common.zip"
        if common_zip.exists() and common_zip.is_file():
            resolved = common_zip.resolve()
            if resolved not in seen_zips:
                common_zip_paths.append(common_zip)
                seen_zips.add(resolved)

    return common_dirs, common_zip_paths


def dae_names_for_source(source, cache_enabled=True):
    cache_key = source_signature(source)
    if cache_enabled:
        cached = DAE_NAME_INDEX_CACHE.get(cache_key)
        if cached is not None:
            return cached

    disk_key = persistent_cache_key(source)
    if cache_enabled:
        disk_cache = load_disk_cache("dae_name_indexes")
        cached_names = disk_cache.get(disk_key)
        if isinstance(cached_names, list):
            names = set(cached_names)
            DAE_NAME_INDEX_CACHE[cache_key] = names
            return names

    if source.asset_type == "file":
        names = build_dae_name_index(Path(source.path))
    else:
        names = set()
        try:
            with zipfile.ZipFile(source.zip_path, "r") as archive:
                xml_text = archive.read(source.zip_entry).decode("utf-8", errors="ignore")
            names = build_dae_name_index_from_text(xml_text)
        except Exception as exc:
            print(f"[BeamNG Importer] Failed to index {source.virtual_path}: {exc}")

    if cache_enabled:
        DAE_NAME_INDEX_CACHE[cache_key] = names
        disk_cache = load_disk_cache("dae_name_indexes")
        disk_cache[disk_key] = sorted(names)
        mark_disk_cache_dirty("dae_name_indexes")
    return names


def build_dae_catalog(vehicle_root: Path, asset_sources=None, cache_enabled=True, required_mesh_names=None):
    if asset_sources is not None:
        required_names = {normalized_name(name) for name in (required_mesh_names or []) if name}
        cache_key = (
            tuple(source_signature(source) for source in asset_sources),
            tuple(sorted(required_names)),
        )
        cached = DAE_CATALOG_CACHE.get(cache_key)
        if cache_enabled and cached is not None:
            return cached

        dae_name_cache = {}
        dae_paths_by_dir = defaultdict(list)
        mesh_to_dae_paths = defaultdict(list)

        unresolved_names = set(required_names)
        sources_by_precedence = defaultdict(list)
        for source in asset_sources:
            sources_by_precedence[source.precedence].append(source)

        for precedence in sorted(sources_by_precedence.keys(), reverse=True):
            layer_found = set()
            for source in sorted(sources_by_precedence[precedence], key=lambda item: item.virtual_path):
                virtual_path = normalize_virtual_path(source.virtual_path)
                names = dae_names_for_source(source, cache_enabled)
                relevant_names = names
                if required_names:
                    relevant_names = names.intersection(unresolved_names or required_names)
                    if not relevant_names:
                        continue

                dae_source = DaeAssetSource(
                    asset_type=source.asset_type,
                    path=source.path or virtual_path,
                    zip_path=source.zip_path,
                    zip_entry=source.zip_entry,
                    virtual_path=virtual_path,
                    precedence=source.precedence,
                )
                dae_paths_by_dir[Path(virtual_path).parent].append(dae_source)
                dae_name_cache[dae_source] = names
                for name in relevant_names:
                    mesh_to_dae_paths[name].append(dae_source)
                layer_found.update(relevant_names)

            if required_names:
                unresolved_names.difference_update(layer_found)
                if not unresolved_names:
                    break

        result = (dae_name_cache, dae_paths_by_dir, mesh_to_dae_paths)
        if cache_enabled:
            DAE_CATALOG_CACHE[cache_key] = result
            save_dirty_disk_caches()
        return result

    dae_paths = list(vehicle_root.rglob("*.dae"))
    dae_name_cache = {}
    dae_paths_by_dir = defaultdict(list)
    mesh_to_dae_paths = defaultdict(list)

    for dae_path in dae_paths:
        source = DaeAssetSource(asset_type="file", path=str(dae_path))
        dae_paths_by_dir[dae_path.parent].append(source)
        names = build_dae_name_index(dae_path)
        dae_name_cache[source] = names
        for name in names:
            mesh_to_dae_paths[name].append(source)

    common_dirs, common_zip_paths = find_common_asset_roots(vehicle_root)

    for common_dir in common_dirs:
        for dae_path in common_dir.rglob("*.dae"):
            source = DaeAssetSource(asset_type="file", path=str(dae_path))
            dae_paths_by_dir[dae_path.parent].append(source)
            names = build_dae_name_index(dae_path)
            dae_name_cache[source] = names
            for name in names:
                mesh_to_dae_paths[name].append(source)

    for common_zip_path in common_zip_paths:
        try:
            with zipfile.ZipFile(common_zip_path, "r") as archive:
                for entry in archive.infolist():
                    if not entry.filename.lower().endswith(".dae"):
                        continue
                    try:
                        xml_text = archive.read(entry.filename).decode("utf-8", errors="ignore")
                    except Exception:
                        continue
                    source = DaeAssetSource(
                        asset_type="zip",
                        path=entry.filename,
                        zip_path=str(common_zip_path),
                        zip_entry=entry.filename,
                    )
                    names = build_dae_name_index_from_text(xml_text)
                    dae_name_cache[source] = names
                    for name in names:
                        mesh_to_dae_paths[name].append(source)
        except Exception as exc:
            print(f"[BeamNG Importer] Failed to index {common_zip_path}: {exc}")

    return dae_name_cache, dae_paths_by_dir, mesh_to_dae_paths


def choose_dae_for_mesh(mesh_name: str, jbeam_path: Path, vehicle_root: Path, dae_paths_by_dir, mesh_to_dae_paths):
    mesh_key = normalized_name(mesh_name)
    candidate_paths = mesh_to_dae_paths.get(mesh_key, [])
    if not candidate_paths:
        return None

    search_dirs = []
    current = jbeam_path.parent
    while True:
        search_dirs.append(current)
        if current == vehicle_root or current.parent == current:
            break
        current = current.parent

    for directory in search_dirs:
        for asset in reversed(dae_paths_by_dir.get(directory, [])):
            if asset in candidate_paths:
                return asset

    return max(candidate_paths, key=lambda item: item.precedence)


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
    material.show_transparent_back = True
    material.diffuse_color = (color[0], color[1], color[2], alpha)
    bsdf = material.node_tree.nodes.get("Principled BSDF") if material.node_tree else None
    if bsdf:
        if "Alpha" in bsdf.inputs:
            bsdf.inputs["Alpha"].default_value = alpha
        if "Base Color" in bsdf.inputs:
            bsdf.inputs["Base Color"].default_value = (color[0], color[1], color[2], alpha)
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


def tag_mesh_data(mesh_data, part_name: str, jbeam_path: Path, vehicle_model: str, editing_enabled: bool):
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


def prop_keeps_template_orientation(mesh_name: str) -> bool:
    key = normalized_name(mesh_name)
    return (
        "enginefan" in key
        or "enginepulley" in key
        or "slipshaft" in key
    )


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
    template_location, _template_rotation, template_scale = template_obj.matrix_world.decompose()
    instance["beamng_template_scale"] = tuple(round(value, 6) for value in template_scale)
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
    if spec.debug_prop_local_translation:
        instance["beamng_prop_local_translation"] = spec.debug_prop_local_translation
    if spec.debug_prop_global_translation:
        instance["beamng_prop_global_translation"] = spec.debug_prop_global_translation
    if spec.debug_prop_base_rotation:
        instance["beamng_prop_base_rotation_deg"] = spec.debug_prop_base_rotation
    if spec.debug_prop_row_rotation:
        instance["beamng_prop_row_rotation_deg"] = spec.debug_prop_row_rotation
    instance["beamng_prop_anim_factor"] = spec.debug_prop_anim_factor

    normalized_negative_scale = False
    if spec.use_template_transform:
        target_matrix = spec.transform_matrix @ template_obj.matrix_world
    elif spec.keep_template_translation:
        target_matrix = spec.transform_matrix @ template_obj.matrix_world
    else:
        template_transform = matrix_without_translation(template_obj.matrix_world)
        if spec.source_type == "prop":
            if prop_keeps_template_orientation(spec.mesh):
                instance["beamng_prop_template_orientation_mode"] = "template_rotation"
            else:
                template_transform = matrix_scale_only(template_obj.matrix_world)
                instance["beamng_prop_template_orientation_mode"] = "scale_only"
        target_matrix = spec.transform_matrix @ template_transform
    if spec.source_type == "prop":
        target_matrix, normalized_negative_scale = bake_negative_handedness_into_mesh(instance, target_matrix)
    instance["beamng_normalized_negative_scale"] = normalized_negative_scale

    if parent_obj is not None:
        instance.parent = parent_obj
        instance.matrix_parent_inverse = Matrix.Identity(4)
        instance.matrix_local = parent_obj.matrix_world.inverted() @ target_matrix
    else:
        instance.matrix_world = target_matrix
    return instance


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


PC_SOURCE_ENUM_ITEMS = []
PC_SOURCE_BY_KEY = {}


def refresh_pc_source_options(context):
    global PC_SOURCE_ENUM_ITEMS, PC_SOURCE_BY_KEY
    prefs = get_addon_preferences(context)
    beamng_user_folder = prefs.beamng_user_folder if prefs else ""
    vanilla_vehicles_folder = prefs.vanilla_vehicles_folder if prefs else ""
    cache_asset_catalogs = prefs.cache_asset_catalogs if prefs else True
    sources = collect_beamng_pc_sources(
        beamng_user_folder,
        vanilla_vehicles_folder,
        cache_asset_catalogs,
    )

    items = []
    by_key = {}
    for index, source in enumerate(sources):
        vehicle_name = _pc_vehicle_from_virtual_path(source.virtual_path)
        config_name = Path(source.virtual_path).name
        label_prefix = getattr(source, "label_prefix", "") or ("Zip" if source.asset_type == "zip" else "File")
        key = str(index)
        if source.asset_type == "zip":
            description = f"{source.zip_path} :: {source.zip_entry}"
        else:
            description = source.path
        label = f"{vehicle_name} | {config_name} | {label_prefix}"
        items.append((key, label, description))
        by_key[key] = source

    PC_SOURCE_ENUM_ITEMS = items
    PC_SOURCE_BY_KEY = by_key
    return items


def pc_source_enum_items(self, context):
    if not PC_SOURCE_ENUM_ITEMS:
        refresh_pc_source_options(context)
    return PC_SOURCE_ENUM_ITEMS


class IMPORT_OT_beamng_pc_from_assets(Operator):
    bl_idname = "import_scene.beamng_pc_from_assets"
    bl_label = "Import BeamNG Config From Assets"
    bl_options = {"REGISTER", "UNDO"}

    pc_source_key: EnumProperty(
        name="BeamNG Config",
        description="A .pc file discovered in the configured BeamNG user, mod, or vanilla asset folders",
        items=pc_source_enum_items,
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
        if not PC_SOURCE_ENUM_ITEMS:
            self.report(
                {"ERROR"},
                "No BeamNG .pc configs found. Set the BeamNG user folder and vanilla vehicles folder in add-on preferences.",
            )
            return {"CANCELLED"}
        self.pc_source_key = PC_SOURCE_ENUM_ITEMS[0][0]
        return context.window_manager.invoke_props_dialog(self, width=650)

    def execute(self, context):
        source = PC_SOURCE_BY_KEY.get(self.pc_source_key)
        if source is None:
            refresh_pc_source_options(context)
            source = PC_SOURCE_BY_KEY.get(self.pc_source_key)
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
    BeamNGPCImporterPreferences,
    BEAMNG_OT_set_visibility,
    BEAMNG_OT_jbeam_relationship,
    BEAMNG_OT_select_jbeam_body_structure,
    BEAMNG_OT_show_all_jbeams,
    BEAMNG_OT_hide_selected_jbeam_items,
    BEAMNG_OT_set_jbeam_visual_visibility,
    BEAMNG_OT_print_prop_transforms,
    BEAMNG_OT_toggle_relationship_lines,
    VIEW3D_PT_beamng_pc_importer,
    IMPORT_OT_beamng_pc,
    IMPORT_OT_beamng_pc_from_assets,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    bpy.types.VIEW3D_MT_object_context_menu.append(menu_func_jbeam_context)


def unregister():
    bpy.types.VIEW3D_MT_object_context_menu.remove(menu_func_jbeam_context)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
