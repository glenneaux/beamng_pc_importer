# Revised Semantic Topology Plan - 2026-05-14

This branch is for moving the BeamNG PC Importer toward a revised native Blender topology authoring model.

## Core Direction

Blender mesh topology should become the interactive authoring surface, while the add-on maintains a separate BeamNG semantic model for ownership, validation, export, source mapping, and cross-part references.

Important distinction:

- A Blender vertex is node topology, not automatically a complete exported node.
- A Blender edge is a topological relationship, not automatically a BeamNG beam.
- A Blender face is face topology, but only triangle faces can become BeamNG triangles.

The semantic layer decides whether topology represents nodes, beams, hydros, rails, triangle boundaries, proxy links, generated helpers, or reference-only structures.

## Unified Milestone Plan

### 1. Identity And Snapshot Core

- Add stable UID layers for vertices, edges, and faces.
- Add a scene-level UID allocator.
- Add topology revision tracking.
- Add topology snapshots and diff detection.
- Rebuild cleanly after undo, save/load, and mode switches.

### 2. Semantic Graph Spine

- Separate topology entities from semantic entities.
- Add semantic states such as valid, invalid, proxy, generated, orphaned, reference-only, and pending-delete.
- Add ownership modes such as owned, proxy, generated, external-reference, and reference-only.
- Store semantic mappings keyed by topology UID.
- Add a selected-element inspector for UID, semantic type, owner, source file, and dirty state.

### 3. Native Node And Relationship Authoring

- New vertices create provisional node semantics.
- Vertex extrusion creates a provisional node plus a topological relationship.
- Edges become beams only when rules classify them as beams.
- Triangle boundary edges must not automatically become beams.
- Prevent duplicate beams with canonical endpoint keys.
- Detect and clean phantom nodes.

### 4. Proxy And Cross-Part System

- Proxy nodes are reference-only semantic nodes tied to real source nodes.
- Proxy positions are synchronized copies, not authoritative positions.
- Moving an owned node should refresh matching proxies after sync.
- Deleting a proxy removes only local dependent relationships.
- Deleting an owned source node removes matching proxies and their dependent local relationships in other editable JBeam meshes.

### 5. Triangle Authoring

- Triangle faces become triangle semantics.
- Quads and ngons become invalid topology and must not export.
- Track triangle winding separately from canonical triangle identity.
- Add winding validation and flip support.

### 6. Model-Backed Export

- Sync the semantic graph into `ResolvedVehicleAuthoringModel`.
- Export from the model rather than raw Blender mesh properties.
- Track changed files from semantic dirty state.
- Preserve source text where safe.
- Use compact clean JSON fallback for new/generated files.
- Block export for invalid topology, missing non-proxy references, duplicate IDs, and ownership violations.

### 7. Recovery And Repair

- Rebuild semantic graph from topology UIDs.
- Regenerate missing metadata.
- Clean orphan topology.
- Repair ownership where possible.
- Add topology inspector and repair tools.

### 8. Authoring UI Rebuild

Suggested 3D View tabs:

- Import
- Author
- Properties
- Validate
- Export
- Inspector

The UI should make the active part, target mod, selected topology, validation state, and export impact obvious.

### 9. New Part, File, And Slot Authoring

- Create virtual JBeam files in memory only.
- Create virtual parts.
- Edit slot type, defaults, and options.
- Write only on export to the selected mod folder.
- Never write vehicle assets to the mod folder during authoring.

### 10. Rails And Hydros

- Hydros can reuse edge topology with hydro semantic metadata.
- Rails should be ordered node chains, not simple beams.
- Rails need continuity validation and may later use curve-style display.

### 11. Symmetry And Mirror

- Prefer single-sided authoring first.
- Use Mirror Modifier as visual aid only at first.
- Generate mirrored topology at export time.
- Add name resolver for `_L`/`_R`, `FL`/`FR`, and `RL`/`RR`.
- Reverse triangle winding when mirrored.
- Detect centerline nodes and duplicate mirrored topology.

## First Big Implementation Slice

The first major implementation target should combine:

- UID topology layer.
- Topology snapshot/diff.
- Thin semantic graph spine.
- Native vertex/extrude/edge interpretation.

This gives the revised architecture a working foundation before attempting rails, mirror workflows, full UI rebuild, or deep export restructuring.

## Main Risk

The semantic model must stay practical. Build a thin working slice first:

```text
UID vertex -> NodeSemantic
UID edge -> RelationshipSemantic -> optional BeamSemantic
UID face -> TriangleSemantic
```

Then expand outward into proxies, rails, hydros, export, and mirror support.

## Implementation Status

Updated during the `revised-data-structure` branch work:

- Build 110 added UID-backed semantic topology snapshots for experimental JBeam meshes.
- Vertex, edge, and face UIDs are now treated as one global topology identity space per mesh rather than separate per-domain pools.
- Meshes now store `beamng_topology_revision`, `beamng_topology_signature_json`, `beamng_semantic_topology_json`, and a previous-snapshot/delta pair.
- Edge semantics can be explicitly set to beam, triangle boundary, or relationship from the JBeam UI.
- Triangle boundary edges are no longer treated as BeamNG beams during scan/export unless explicitly marked as beams.
- The semantic snapshot writer exposes the active mesh's UID/semantic graph in a Blender text block for debugging.
- The topology diff engine now records created/deleted/changed vertices, edges, and faces in `beamng_semantic_topology_delta_json`.
- Scan now uses the semantic topology delta for newly created edge/face candidates, while semantic edge type decides whether an edge can become a BeamNG beam.
- Export validation and health checks now count duplicate/missing beam references from semantic beam edges only, not every mesh edge.
- Deleting a proxy/reference node now creates local dependent beam/triangle delete operations instead of trying to delete the source node.
- Deleting an owned source node now scans other editable JBeam meshes for matching proxies and creates dependent local beam/triangle delete operations there.
- Orphan provisional nodes are detected in validation/health and can be removed with the explicit Clear Orphans tool.
- Build 114 advances semantic topology operation generation:
  - topology signatures and deltas now track triangle winding changes, not just face identity;
  - scan treats the same three triangle nodes with changed order as a `triangles.nodes` update instead of a delete plus insert;
  - triangle parameter lookup now uses canonical triangle identity while preserving explicit winding order for export;
  - export patching can apply triangle winding/node-order updates by replacing the matched triangle row.
- Build 114 also adds semantic topology recovery/repair:
  - `Repair Semantic` rebuilds active scene semantic snapshots and prunes stale UID-keyed node/edge/face metadata maps;
  - the repair path is exposed beside `Repair UIDs` in the JBeam authoring controls.

Current next target:

- Move node insert/delete generation fully onto semantic vertex UIDs instead of legacy node-id array comparisons.
- Push accepted operations into the `ResolvedVehicleAuthoringModel` as first-class semantic operations before export planning.
- Expand topology repair into validation-guided one-click fixes for stale edge semantics, duplicate generated node IDs, and invalid face topology.
