import json
import math
import re
import tempfile
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
