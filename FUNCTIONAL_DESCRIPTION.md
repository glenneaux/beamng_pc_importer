# BeamNG PC Importer Functional Description

This document describes the intended functional behaviour for the next major stage of the BeamNG PC Importer: moving from an importer and visual diagnostic tool toward a deterministic BeamNG configuration and JBeam editing engine inside Blender.

## Core Principle

Blender should become the view and editing surface for an authoritative internal vehicle model. Blender objects, meshes, collections, overlays, and labels are cache/view representations that can be rebuilt from the internal model. They should not become the only source of truth.

The editor should maintain a canonical internal schema with stable internal UUIDs for every editable entity. These UUIDs are independent of BeamNG node IDs, part names, row order, file ordering, or Blender object names.

Editable entities should include:

- Vehicle configurations.
- Source files.
- JBeam parts.
- Slots and slot selections.
- Nodes.
- Beams.
- Triangles.
- Hydros.
- Rails and sliders.
- Props.
- Flexbodies.
- Mesh/material references.
- Future unsupported or plugin-provided JBeam sections.

BeamNG IDs and names remain important user-facing/export-facing data, but they are not sufficient as internal identity.

## Deterministic Resolver Pipeline

Import and rebuild should follow explicit phases so behaviour is predictable and easier to validate.

1. `load`: read PC, JBeam, DAE, zip, unpacked mod, user, and vanilla sources.
2. `slot resolve`: determine selected parts from the PC file and slot tree.
3. `inheritance/default merge`: apply default slot selections, inherited options, shared/common sections, variables, slot overrides, and late-bound values.
4. `cross-part reference resolve`: map references between parts, files, nodes, beams, props, flexbodies, meshes, and materials.
5. `validation`: detect broken references, duplicate IDs, invalid slot selections, ownership problems, unsupported data, and export blockers.
6. `visual build`: create or update Blender collections, meshes, overlays, ghost/reference nodes, labels, and view filters from the resolved internal model.

Each phase should produce diagnostic output that can be inspected during development and testing.

## Dependency And Reference Graph

The editor should maintain an explicit dependency/reference graph for fast reverse-lookups, validation, property panels, and partial rebuilds.

The graph should answer questions such as:

- What references this node?
- Which beams, triangles, hydros, rails, or sliders depend on this node?
- Which props or flexbodies depend on this mesh?
- Which parts are defined by this file?
- Which files contribute to this vehicle configuration?
- Which parts reference external nodes owned by another part?
- Which entities must be rebuilt if this part changes?

Reference indexes should include:

- Node to beams, triangles, hydros, rails, sliders, props, and flexbodies.
- Beam to endpoint nodes and owning part.
- Triangle to referenced nodes and owning part.
- Part to owned nodes, beams, triangles, hydros, rails, sliders, props, flexbodies, slots, and assets.
- File to parts and sections.
- Slot to selected part, default part, available parts, and child slots.
- External references between parts.

## Ownership Semantics

The editor should formally distinguish ownership from reference.

- `owned`: real exportable data defined by the active part/file.
- `referenced`: real data owned elsewhere but used by the active entity.
- `proxy/reference-only`: Blender-visible helper representation of external data, such as ghost nodes.
- `generated/transient`: helper geometry, overlays, labels, diagnostics, caches, and temporary edit aids that must never be exported as BeamNG data.

Cross-part beams remain owned by the part/file where the beam is defined, even when they reference nodes owned by another part.

External nodes should appear as locked ghost/reference nodes when editing a part that references them. The editor should not silently duplicate or import parent nodes into child parts.

## Dirty-State Granularity

Dirty state should be tracked at several levels so the editor can avoid unnecessary full rebuilds and exports.

Dirty scopes should include:

- Vehicle/session.
- PC configuration file.
- JBeam source file.
- Part.
- Section.
- Row/entity.
- Field/property.
- Dependency graph entry.
- Blender view/cache representation.

This allows targeted validation, partial visual rebuilds, and precise export patching.

## Transactional Editing

Structural and property edits should be transactional.

The intended edit flow is:

1. Begin edit.
2. Apply the change to the authoritative internal model.
3. Validate affected entities and dependencies.
4. Update dirty state.
5. Rebuild affected Blender view/cache objects.
6. Commit the edit if valid, or roll back on failure.

This protects the project from Blender undo/desync corruption. Blender's undo system should still be used through undo-aware operators where possible, but the editor should not rely on arbitrary Blender object state as authoritative JBeam data.

## Transform Spaces And Propagation

The editor should explicitly model transform spaces.

- `local space`: raw node or mesh coordinates as defined by the owning JBeam context.
- `part space`: coordinates relative to the JBeam part object in Blender.
- `resolved/world space`: final assembled vehicle position after slot resolution, parent offsets, part transforms, mirroring, and overrides.
- `proxy space`: display-only positions for external references, ghost nodes, and helper overlays.

Object Mode should allow moving an entire JBeam part as a unit. Owned nodes, beams, triangles, hydros, sliders, props, and attached flexbodies should follow that part. Cross-part references should update visually based on the resolved positions of referenced nodes without silently changing ownership.

Future transform handling should also account for mirrored/symmetry operations and parent-child offsets.

## BeamNG Inheritance, Variables, And Late Binding

The resolver must handle BeamNG data that is not simply static rows.

Important cases include:

- `$=` expressions.
- Variables and expression-derived values.
- Shared/common sections.
- Slot-based overrides.
- Default slot selections.
- Late-bound references.
- Mod overrides and vanilla fallback behaviour.

The editor should preserve unresolved or unsupported expressions rather than flattening them destructively unless the user explicitly chooses to bake values.

## Export Patch Strategy

Exports should preserve source data wherever practical and patch only what the editor understands.

Preferred export strategy:

- Parse source data into a preservation-aware AST or structured document model.
- Preserve unknown fields, ordering, row structure, and comments where practical.
- Patch known sections/entities/fields using stable internal UUID mappings and source locations.
- Avoid whole-file regeneration for existing files unless necessary.

Text patching may be used only when AST-style patching is not possible.

Export rules should define:

- Ordering guarantees.
- Comment preservation expectations.
- Formatting normalization rules.
- How unknown fields are preserved.
- How soft-deleted rows are represented.
- How generated metadata comments are added, if used.
- How mod/user override paths are selected safely.

Vanilla BeamNG files must remain read-only.

## Canonical Schema And Versioning

The internal data model should have its own schema version. This allows future BeamNG format changes or editor model changes to be migrated safely.

The schema should support:

- Versioned entity/component definitions.
- Migration of saved editor metadata.
- Compatibility checks when reopening older project data.
- Safe handling of unknown future BeamNG sections.

## Performance Strategy

Performance should be considered early because full BeamNG vehicles can become large.

The editor should support:

- Lazy loading of assets and sections.
- Cached zip catalogues and asset indexes.
- Cached resolved structures where safe.
- Partial rebuilds based on dirty state and dependency invalidation.
- Lightweight mesh/overlay rendering for editing.
- Avoiding heavy cylinder/sphere geometry for authoring workflows.
- Large vehicle scalability through indexed storage rather than tightly coupled object trees.

An ECS-style internal data store should be considered for scalability. Entities can be stored by UUID with typed component/index maps for transforms, ownership, source locations, references, validation state, Blender view handles, and dirty flags.

## Event System

The internal model should emit events so systems remain decoupled.

Useful events include:

- Entity changed.
- Entity created.
- Entity deleted or soft-deleted.
- Dependency invalidated.
- Rebuild requested.
- Validation updated.
- Source file dirty state changed.
- Export target changed.
- Blender view/cache object rebuilt.

Consumers can include validation, property panels, viewport overlays, dependency graphs, export preparation, and diagnostics.

## Soft Delete Semantics

Deletion should be explicit and reversible by default.

Soft-delete states may include:

- `disabled`: entity remains in data but is marked inactive where BeamNG supports it.
- `commented`: entity is preserved as a comment or tool-preserved inactive row.
- `hidden`: entity remains valid/exportable but is hidden in the Blender view.
- `orphaned`: entity remains in source but has broken or removed dependencies.
- `pending hard delete`: entity is marked for removal but export validation has not confirmed it is safe.

Hard delete should be an advanced action with dependency warnings.

## Validation Severity

Validation should use clear severity levels.

- `info`: useful diagnostic detail, not a problem.
- `warning`: suspicious or incomplete data, but export may continue.
- `error`: invalid editor/model state that should be fixed.
- `export-blocking error`: cannot safely export until resolved.

Validation should run both live during editing and again before export.

## Fault Tolerance And Recovery

The editor should degrade gracefully where possible.

It should generate useful diagnostics for:

- Corrupted JBeam files.
- Invalid PC files.
- Unresolved references.
- Missing meshes or materials.
- Cyclic slot relationships.
- Broken mod overrides.
- Unsupported sections.
- Conflicting part definitions.
- Zip/catalogue read failures.

Partial import should remain possible where safe, with unavailable data clearly marked.

## Plugin And Extensibility API

Future unsupported JBeam sections and BeamNG structures should be extensible without forcing the core editor to understand everything immediately.

An extension API should allow plugins or future modules to:

- Register new section parsers.
- Register validators.
- Register property panel layouts.
- Register visual overlays.
- Register export patch handlers.
- Provide migration logic for custom metadata.

Unknown parameters should remain inspectable even before a plugin understands them.

## Blender Scene Representation

Blender collections should be treated as display/filter convenience, not as authoritative ownership.

The project should clarify whether collections mirror:

- Source files.
- JBeam parts.
- Ownership groups.
- Visual categories.
- Slot hierarchy.
- View/debug modes.

Recommended approach:

- Top-level collection represents the imported vehicle/session.
- Internal model stores authoritative file, part, slot, and ownership relationships.
- Blender collections group objects for display and filtering only.
- Generated helper geometry and ghost/reference nodes are isolated from exportable real data.

## Multiple Views And Edit Contexts

The editor should define rules for multiple Blender 3D Views and edit contexts.

Current supported behaviour:

- One-way 3D View synchronization from the source viewport where sync was enabled.

Future behaviour should consider:

- Independent view filters per 3D View.
- JBeam selectable view with translucent non-selectable bodies.
- Flexbody-only, prop-only, and JBeam-only views.
- Multiple edit contexts without corrupting the authoritative internal model.

## Testing Requirements

Automated tests should eventually cover round-trip safety.

Important tests include:

- Import, export, and reimport equivalence.
- Unknown field preservation.
- No unintended diffs on unedited files.
- Correct dependency graph references.
- Correct dirty-state granularity.
- Correct soft-delete behaviour.
- Correct handling of missing meshes and broken references.
- Export path safety so vanilla files are never modified.
- Mod override precedence and conflict handling.

## Future Structure Types

The model should remain open to non-vehicle BeamNG structures if useful later.

Possible future targets include:

- Props.
- Trailers.
- Machines.
- Map objects.
- Procedural assemblies.

This should not distract from vehicle editing now, but the internal model should avoid assumptions that make future support impossible.

## Immediate Testing Guidance

For the current scaffold, import one simple vehicle and one complex vehicle, then open the `BeamNG Resolved Vehicle Model` text block.

Check whether:

- Per-part counts look believable.
- External node references make sense.
- Source files match the expected resolved JBeam files.
- Obvious parent/child ownership mistakes appear in the report.

This diagnostic report is the first practical checkpoint before deeper editor work begins.
