from pathlib import Path
from xml.etree import ElementTree as ET

try:
    from .core import *
except ImportError:
    from core import *

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
