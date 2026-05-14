# BeamNG Native Blender Topology Authoring Architecture
## Native Blender JBeam Editing System

# Overview

This document describes the architecture and implementation plan for evolving the BeamNG PC Importer into a native Blender-based JBeam authoring environment.

The core philosophy is:

> Blender topology editing becomes JBeam topology editing.

Instead of relying primarily on custom BeamNG creation operators, the system interprets standard Blender mesh editing operations semantically.

This allows Blender's native modeling tools to become BeamNG authoring tools automatically.

---

# Core Mapping

| Blender | JBeam |
|---|---|
| Vertex | Node |
| Edge | Beam |
| Triangle Face | Triangle |

This creates a natural editing workflow where:

| Blender Operation | JBeam Meaning |
|---|---|
| Extrude vertex | Create node + beam |
| Create edge | Create beam |
| Delete edge | Delete beam |
| Delete vertex | Delete node |
| Create triangle face | Create triangle |
| Merge vertices | Merge nodes |
| Dissolve edge | Remove beam |
| Knife cut | Split beams/triangles |

---

# Major Architectural Shift

## OLD MODEL

```text
Custom BeamNG Operator
    ↓
Create BeamNG Entity
    ↓
Create Blender Geometry
```

## NEW MODEL

```text
User Edits Blender Topology
    ↓
Topology Synchronization Layer
    ↓
ResolvedVehicleAuthoringModel
    ↓
Validation
    ↓
Export
```

This is the critical architectural change.

---

# Blender Mesh Becomes Authoritative

The editable Blender mesh becomes the authoritative interactive topology representation.

The authoring model becomes:
- semantic
- ownership-aware
- metadata-aware
- export-oriented
- validation-oriented

But NOT responsible for interactive topology editing itself.

---

# Native Blender Tool Support

The following Blender tools should become first-class JBeam authoring tools automatically:

- Extrude
- Merge
- Knife
- Dissolve
- Duplicate
- Bridge
- Fill
- Triangulate
- Snap
- Proportional Editing
- Mirror
- Symmetrize
- X-Mirror Editing

No Blender source modification is required.

Everything can be implemented entirely in the Blender Python addon system.

---

# Persistent Topology Identity System

## Critical Requirement

Blender indices are unstable.

These cannot be trusted:
- vertex index
- edge index
- face index

because topology operations constantly reindex geometry.

A persistent identity system is required.

---

# BMesh UID Layers

Use Blender BMesh custom data layers.

## Vertex UID Layer

```python
bm.verts.layers.int["beamng_uid"]
```

## Edge UID Layer

```python
bm.edges.layers.int["beamng_uid"]
```

## Face UID Layer

```python
bm.faces.layers.int["beamng_uid"]
```

---

# UID Rules

Every topology element must have:
- globally unique UID
- persistent UID
- never-reused UID
- save/load persistence
- undo/redo persistence

---

# UID Allocation

Store a scene-level counter:

```python
scene["beamng_next_topology_uid"]
```

Example allocator:

```python
def allocate_uid(scene):
    uid = scene["beamng_next_topology_uid"]
    scene["beamng_next_topology_uid"] += 1
    return uid
```

---

# Topology Synchronization Layer

This becomes the heart of the system.

Responsibilities:
- detect created verts
- detect deleted verts
- detect created edges
- detect deleted edges
- detect created faces
- detect deleted faces

Then synchronize:
- authoring model
- semantic metadata
- ownership
- export operations

---

# IMPORTANT: Do NOT Override Blender Tools

Avoid:
- custom extrude systems
- custom delete systems
- custom merge systems

Instead:
- allow Blender to operate normally
- observe topology changes afterward
- interpret them semantically

This is vastly more robust.

---

# Topology Snapshot System

Maintain previous topology state:

```python
{
    "verts": {},
    "edges": {},
    "faces": {},
}
```

Each synchronization cycle:
1. rebuild current snapshot
2. compare against previous snapshot
3. detect delta

---

# New Vertex Handling

If a new vertex is detected:
- assign UID
- create semantic node
- assign default ownership
- assign node ID
- create operation log entry

---

# New Edge Handling

If a new edge is detected:
- assign UID
- create semantic beam
- connect endpoint node IDs
- validate duplicates

---

# New Face Handling

If a new face is detected:
- validate vertex count
- if triangle:
  - create triangle entity
- otherwise:
  - mark invalid topology

---

# Triangle Rules

BeamNG triangles must always be triangles only.

Allowed:
- 3 verts

Disallowed:
- quads
- ngons

---

# Recommended Triangle Workflow

Triangles should usually be created intentionally.

Recommended workflow:
- extrude vertices for beams
- create faces intentionally using:
  - F
  - triangulate
  - bridge

This aligns naturally with JBeam semantics.

---

# Vertex Extrusion Workflow

This becomes the primary node/beam creation workflow.

## User Flow

1. Select node vertex
2. Press E
3. Move

Blender creates:
- new vertex
- connecting edge

Addon interprets:
- new node
- new beam

No custom tool required.

---

# Edge Extrusion Policy

Recommended initial strategy:
- allow edge extrusion
- automatically delete generated faces

Example:

```python
bmesh.ops.delete(
    bm,
    geom=new_faces,
    context='FACES'
)
```

Result:
- beam structures preserved
- polygon surfaces removed

---

# Editing Modes

## Node/Beam Authoring Mode

Primary operations:
- vertex editing
- edge editing
- extrusion
- snapping
- transforms

## Triangle Editing Mode

Primary operations:
- triangle creation
- winding correction
- triangulation
- normal fixing

---

# Authoring Model Role

ResolvedVehicleAuthoringModel becomes:
- semantic layer
- ownership layer
- metadata layer
- export layer

NOT primary topology editor.

---

# Ownership Semantics

Every entity must track:
- owning part
- source file
- resolved part ID
- external reference state

Ownership remains critical even though topology editing becomes native Blender editing.

---

# External Reference Nodes

External nodes should remain:
- ghosted
- locked
- reference-only

They should never silently duplicate into child parts.

---

# Collection Structure

Recommended hierarchy:

```text
Vehicle Root
 ├── JBeam Authoring Mesh
 ├── Visual Meshes (ghosted)
 ├── Props
 ├── Helpers
 ├── Diagnostics
```

---

# Authoring Mesh Separation

The editable JBeam mesh should become authoritative.

Flexbody/render meshes become:
- translucent
- locked
- reference visuals only

---

# Synchronization Timing

Recommended:
- depsgraph handlers
- edit-mode polling

because Blender edit updates are not always deterministic.

---

# Undo/Redo Handling

Undo invalidates:
- BMesh references
- cached topology
- runtime pointers

Required strategy:
- rebuild synchronization state after undo
- reconstruct UID maps
- rebuild topology snapshots

---

# Export Pipeline

Export should NEVER trust Blender topology directly.

Instead:

```text
Blender Mesh
    ↓
Semantic Synchronization
    ↓
Authoring Model
    ↓
Validation
    ↓
Export
```

---

# Validation Layer

Must validate:
- non-triangle faces
- duplicate beams
- orphan nodes
- invalid references
- missing ownership
- invalid cross-part refs
- duplicate node IDs
- invalid winding

---

# Overlay System

Recommended overlays:
- node IDs
- ownership colors
- external reference ghosts
- invalid topology highlights
- triangle winding indicators
- beam diagnostics

---

# Performance Strategy

Avoid:
- full rebuilds every frame

Prefer:
- incremental topology diffing
- dirty-region rebuilds
- cached UID maps

---

# Mirror and Symmetry Support

## Core Principle

Mirror workflows must preserve valid BeamNG semantics automatically.

The system must support:
- Mirror Modifier
- Symmetrize
- X-Mirror Editing
- Duplicate + Mirror

without generating:
- duplicate node IDs
- duplicate beams
- invalid triangles
- broken ownership
- malformed exports

---

# Recommended Symmetry Philosophy

## Authoring Should Be Single-Sided

The authoritative JBeam topology should normally exist only on:
- left side
OR
- right side

The opposite side should be:
- generated
- synchronized
- derived

This avoids:
- duplicate beams
- asymmetry drift
- node-name conflicts

---

# Recommended Mirror Workflow

## Preferred Workflow

Use:
- Mirror Modifier
- single-sided authoring

The mirror modifier becomes:
- viewport visualization
- symmetry editing aid

The export system generates real mirrored JBeam topology later.

---

# Mirror Semantic Metadata

Topology entities need:

```python
symmetry_group
symmetry_role
mirror_source_uid
mirror_axis
```

---

# Symmetry Roles

Recommended roles:
- source
- mirrored
- centerline
- independent

---

# Node Naming System

Duplicate node IDs must never occur.

Recommended naming:
- _L / _R
- FL / FR
- RL / RR

Examples:
- frame_FL
- frame_FR
- shock_L
- shock_R

---

# Mirror Name Resolver

Implement:

```python
def mirrored_node_name(name):
```

Examples:
- shock_L → shock_R
- frame_FR → frame_FL

Fallback:
- node_001 → node_001_mirror

---

# Mirror UID Semantics

UIDs remain globally unique.

Mirrored entities additionally track:

```python
mirror_source_uid
```

Example:
- UID 101 = left node
- UID 202 = right mirrored node
- mirror_source_uid = 101

---

# Mirror Modifier Rules

Mirror modifier geometry is NOT authoritative topology until applied.

Therefore:
- source topology remains authoritative
- mirrored visualization is generated only

---

# Export-Time Mirroring

During export:
1. traverse source topology
2. generate mirrored topology
3. rename nodes
4. remap beams
5. remap triangles
6. validate duplicates
7. emit final JBeam

---

# Beam Mirroring Rules

Example:

Source:
```text
nodeA_L → nodeB_L
```

Mirrored:
```text
nodeA_R → nodeB_R
```

Duplicate beams must never occur.

---

# Triangle Mirroring Rules

Mirroring reverses handedness.

Example:
```text
A B C
```

must become:
```text
A C B
```

Otherwise normals invert.

This is critical.

---

# Centerline Topology

Nodes on symmetry plane:
- should usually remain shared
- should not duplicate

Examples:
- chassis spine
- driveline
- center mounts

---

# Mirror Validation System

Must detect:
- duplicate node IDs
- duplicate beams
- duplicate triangles
- self-referencing beams
- degenerate triangles
- centerline duplication

---

# X-Mirror Editing Support

When Blender creates mirrored topology:
- assign UID immediately
- assign ownership immediately
- assign semantic metadata immediately

No topology may exist without valid semantic data.

---

# CRITICAL RULE

## No topology may exist without valid BeamNG semantic data.

Every:
- vertex
- edge
- face

must ALWAYS have:
- UID
- ownership
- semantic classification
- export eligibility

even during editing.

This is one of the most important architectural requirements.

---

# Immediate Semantic Initialization

Whenever topology is created:
- immediately assign semantic metadata
- immediately assign ownership
- immediately assign validity state

Even if placeholder/default values are temporarily used.

---

# Semantic Initialization Pipeline

Whenever topology delta is detected:

```python
assign_default_semantic_data()
```

---

# Vertex Initialization

Assign:
- UID
- node ID
- owner part
- symmetry state
- validity state

---

# Edge Initialization

Assign:
- UID
- beam endpoints
- owner part
- mirror state

---

# Face Initialization

Assign:
- UID
- triangle nodes
- winding
- mirror state

---

# Duplicate Beam Prevention

Use canonical beam keys:

```python
frozenset({id1, id2})
```

Prevents:
- A-B
- B-A

duplicate beams.

---

# Duplicate Triangle Prevention

Use canonical triangle signature:

```python
tuple(sorted([a, b, c]))
```

while separately tracking winding.

---

# Symmetrize Support

Blender Symmetrize creates real topology.

This is acceptable.

But:
- new mirrored topology must immediately receive valid semantic metadata.

---

# Recommended Implementation Phases

# PHASE 1
Implement:
- UID layers
- topology snapshots
- synchronization framework

---

# PHASE 2
Support:
- vertex/edge synchronization
- extrusion workflows

---

# PHASE 3
Support:
- triangle synchronization
- topology validation

---

# PHASE 4
Implement:
- undo/redo recovery
- persistent synchronization rebuilds

---

# PHASE 5
Implement:
- symmetry metadata
- mirror-aware naming
- centerline detection

---

# PHASE 6
Support:
- Mirror Modifier visualization
- export-time mirror generation

---

# PHASE 7
Implement:
- duplicate beam prevention
- duplicate triangle prevention

---

# PHASE 8
Support:
- live X-Mirror editing
- semantic mirrored topology initialization

---

# PHASE 9
Integrate:
- ownership semantics
- authoring model synchronization
- export pipeline

---

# Recommended Immediate Milestone

Best proof-of-concept target:

## Native Vertex Extrusion → Node + Beam Creation

Reasons:
- Blender already supports it naturally
- no face handling required
- validates architecture quickly
- massive UX improvement
- minimal complexity

This should become the first production milestone.

---

# Final Design Philosophy

The long-term goal is:

> Blender should feel like it natively supports JBeam authoring.

The addon should:
- interpret Blender topology semantically
- preserve BeamNG-specific meaning
- validate topology continuously
- guarantee valid export data
- support symmetry workflows naturally

without fighting Blender’s native modeling system.
