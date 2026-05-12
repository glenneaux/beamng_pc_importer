import json
from dataclasses import asdict, dataclass, field
from datetime import datetime


def _vector_tuple(value):
    try:
        return tuple(round(float(component), 6) for component in value[:3])
    except Exception:
        return (0.0, 0.0, 0.0)


@dataclass
class ResolvedNode:
    id: str
    position: tuple
    part_name: str
    resolved_part_id: int = -1
    source_file: str = ""
    kind: str = "owned"
    options: dict = field(default_factory=dict)


@dataclass
class ResolvedBeam:
    id1: str
    id2: str
    part_name: str
    resolved_part_id: int = -1
    source_file: str = ""
    options: dict = field(default_factory=dict)


@dataclass
class ResolvedTriangle:
    id1: str
    id2: str
    id3: str
    part_name: str
    resolved_part_id: int = -1
    source_file: str = ""
    normal: tuple = field(default_factory=tuple)
    options: dict = field(default_factory=dict)


@dataclass
class ResolvedPartDetail:
    name: str
    resolved_part_id: int
    source_file: str = ""
    parent_id: int = -1
    slot_name: str = ""
    node_ids: list = field(default_factory=list)
    beam_count: int = 0
    triangle_count: int = 0
    external_node_refs: list = field(default_factory=list)


@dataclass
class EditOperation:
    operation: str
    file: str
    part: str
    section: str
    row: str = ""
    field: str = ""
    old: object = None
    new: object = None
    status: str = "pending"
    created_at: str = ""


@dataclass
class ResolvedJBeamFile:
    source_file: str
    virtual_path: str = ""
    part_names: list = field(default_factory=list)
    node_count: int = 0
    beam_count: int = 0
    triangle_count: int = 0


@dataclass
class ResolvedVehicleAuthoringModel:
    schema_version: int = 1
    generated_at: str = ""
    pc_path: str = ""
    source_description: str = ""
    vehicle_model: str = ""
    main_part: str = ""
    files: list = field(default_factory=list)
    parts: list = field(default_factory=list)
    nodes: list = field(default_factory=list)
    beams: list = field(default_factory=list)
    triangles: list = field(default_factory=list)
    operations: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)

    def to_json(self):
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text):
        data = json.loads(text) if text else {}
        return cls(**data)


def build_authoring_model_from_import(
    pc_path,
    pc_data,
    source_description,
    main_part,
    resolved_parts,
    visual_nodes,
    visual_beams,
    visual_triangles,
    operations=None,
):
    part_by_id = {}
    parts = []
    for resolved_part in resolved_parts:
        source_file = str(getattr(resolved_part.part_def, "source_path", ""))
        part = ResolvedPartDetail(
            name=str(getattr(resolved_part.part_def, "name", "")),
            resolved_part_id=int(getattr(resolved_part, "id", -1)),
            source_file=source_file,
            parent_id=int(getattr(resolved_part, "parent_id", -1)),
            slot_name=str(getattr(resolved_part, "slot_name", "")),
        )
        part_by_id[part.resolved_part_id] = part
        parts.append(part)

    nodes = []
    known_node_ids = set()
    for node in visual_nodes:
        resolved_part_id = int(getattr(node, "resolved_part_id", -1))
        part = part_by_id.get(resolved_part_id)
        source_file = part.source_file if part else ""
        node_id = str(getattr(node, "name", ""))
        known_node_ids.add(node_id)
        if part:
            part.node_ids.append(node_id)
        nodes.append(
            ResolvedNode(
                id=node_id,
                position=_vector_tuple(getattr(node, "position", (0.0, 0.0, 0.0))),
                part_name=str(getattr(node, "part_name", "")),
                resolved_part_id=resolved_part_id,
                source_file=source_file,
                options=dict(getattr(node, "options", {}) or {}),
            )
        )

    beams = []
    for beam in visual_beams:
        resolved_part_id = int(getattr(beam, "resolved_part_id", -1))
        part = part_by_id.get(resolved_part_id)
        source_file = part.source_file if part else ""
        id1 = str(getattr(beam, "id1", ""))
        id2 = str(getattr(beam, "id2", ""))
        if part:
            part.beam_count += 1
            for node_id in (id1, id2):
                if node_id not in known_node_ids and node_id not in part.external_node_refs:
                    part.external_node_refs.append(node_id)
        beams.append(
            ResolvedBeam(
                id1=id1,
                id2=id2,
                part_name=str(getattr(beam, "part_name", "")),
                resolved_part_id=resolved_part_id,
                source_file=source_file,
                options=dict(getattr(beam, "options", {}) or {}),
            )
        )

    triangles = []
    for triangle in visual_triangles:
        resolved_part_id = int(getattr(triangle, "resolved_part_id", -1))
        part = part_by_id.get(resolved_part_id)
        source_file = part.source_file if part else ""
        ids = [str(getattr(triangle, name, "")) for name in ("id1", "id2", "id3")]
        if part:
            part.triangle_count += 1
            for node_id in ids:
                if node_id not in known_node_ids and node_id not in part.external_node_refs:
                    part.external_node_refs.append(node_id)
        triangles.append(
            ResolvedTriangle(
                id1=ids[0],
                id2=ids[1],
                id3=ids[2],
                part_name=str(getattr(triangle, "part_name", "")),
                resolved_part_id=resolved_part_id,
                source_file=source_file,
                normal=_triangle_normal_tuple(triangle),
                options=dict(getattr(triangle, "options", {}) or {}),
            )
        )

    file_map = {}
    for part in parts:
        file_entry = file_map.setdefault(
            part.source_file,
            ResolvedJBeamFile(source_file=part.source_file, virtual_path=_virtual_vehicle_path(part.source_file)),
        )
        file_entry.part_names.append(part.name)
    for node in nodes:
        if node.source_file in file_map:
            file_map[node.source_file].node_count += 1
    for beam in beams:
        if beam.source_file in file_map:
            file_map[beam.source_file].beam_count += 1
    for triangle in triangles:
        if triangle.source_file in file_map:
            file_map[triangle.source_file].triangle_count += 1

    return ResolvedVehicleAuthoringModel(
        generated_at=datetime.now().isoformat(timespec="seconds"),
        pc_path=str(pc_path),
        source_description=str(source_description or ""),
        vehicle_model=str(pc_data.get("model", "")) if isinstance(pc_data, dict) else "",
        main_part=str(main_part or ""),
        files=list(file_map.values()),
        parts=parts,
        nodes=nodes,
        beams=beams,
        triangles=triangles,
        operations=[EditOperation(**operation) if isinstance(operation, dict) else operation for operation in (operations or [])],
    )


def _triangle_normal_tuple(triangle):
    try:
        p1 = _vector_tuple(getattr(triangle, "p1", (0.0, 0.0, 0.0)))
        p2 = _vector_tuple(getattr(triangle, "p2", (0.0, 0.0, 0.0)))
        p3 = _vector_tuple(getattr(triangle, "p3", (0.0, 0.0, 0.0)))
        ax, ay, az = (p2[index] - p1[index] for index in range(3))
        bx, by, bz = (p3[index] - p1[index] for index in range(3))
        normal = (
            ay * bz - az * by,
            az * bx - ax * bz,
            ax * by - ay * bx,
        )
        length = sum(component * component for component in normal) ** 0.5
        if length <= 0:
            return ()
        return tuple(round(component / length, 6) for component in normal)
    except Exception:
        return ()


def _virtual_vehicle_path(source_file):
    text = str(source_file).replace("\\", "/")
    lower = text.lower()
    marker = "/vehicles/"
    if marker in lower:
        return text[lower.index(marker) + 1 :]
    if lower.startswith("vehicles/"):
        return text
    return ""


def authoring_model_report_lines(model):
    return [
        "[BeamNG Authoring Model]",
        f"Generated: {model.generated_at}",
        f"PC: {model.pc_path}",
        f"Vehicle model: {model.vehicle_model or '(unknown)'}",
        f"Main part: {model.main_part or '(unknown)'}",
        f"Files: {len(model.files)}",
        f"Parts: {len(model.parts)}",
        f"Nodes: {len(model.nodes)}",
        f"Beams: {len(model.beams)}",
        f"Triangles: {len(model.triangles)}",
        f"Operations: {len(model.operations)}",
        "",
        "Top files:",
        *[
            f"- {file.virtual_path or file.source_file}: {file.node_count} nodes, {file.beam_count} beams, {file.triangle_count} triangles"
            for file in sorted(model.files, key=lambda item: item.source_file)[:20]
        ],
    ]
