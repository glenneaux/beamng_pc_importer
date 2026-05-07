# BeamNG PC Importer Architecture Notes

This document captures the current direction for turning the importer into a broader BeamNG configuration and JBeam editor. It is intentionally design-focused rather than implementation-complete.

## Core Direction

The editor should build an internal resolved vehicle model and use that as the working source of truth while Blender is open. Original BeamNG files remain linked as preserved source data so exports can patch only what the editor understands.

Future workflows should include:

- Import an existing `.pc` configuration.
- Create a new `.pc` by choosing a vehicle, resolving its root/main JBeam, and recursively applying default slot selections.
- Edit the vehicle configuration through the slot tree.
- Edit JBeam structure part-by-part inside Blender.
- Export changes into the BeamNG user/mod folder structure without modifying vanilla files.

## Source And Export Rules

Vanilla BeamNG files are read-only. The editor can inspect and resolve data from vanilla folders and archives, but must not write to them.

Exports should target the BeamNG user/mod structure so BeamNG's normal override system handles modified data. If importing from mods or unpacked mod folders, overwriting mod-owned files may be allowed later with explicit confirmation.

Import should support a global switch:

- Use mods and overrides.
- Ignore mods and load vanilla-only data.

## Resolved Vehicle Model

The resolved vehicle model should contain both configuration state and JBeam structure state:

- Selected parts and slots.
- Slot defaults and available options.
- JBeam files, parts, and sections.
- Nodes, beams, triangles, hydros, sliders, props, and flexbodies.
- Part ownership and cross-part references.
- File ownership and export target paths.
- Unknown fields and parameters preserved from source files.

The working model can be mutable for interactive editing, but each meaningful edit must also be tracked explicitly so export can patch preserved source structures.

## Preserve-First Editing

Assume BeamNG file parameters can change over time. The editor should preserve anything it does not understand.

For existing files:

- Preserve original structure, ordering, comments, and unknown fields where practical.
- Patch only the values/sections the editor understands.
- Treat unknown parameters as valid data, not errors.

For new files:

- Generate clean structured output.
- Include tool metadata comments only where useful and safe.

A future diagnostic scanner should parse available JBeam files and catalogue unknown sections/parameters for review. The report should include file, part, section, field/key, example values, and frequency.

## Configuration Editing

The current configuration editor is a useful foundation and should be maintained.

Important future behaviour:

- A "Create New PC" workflow asks for the vehicle, resolves the root JBeam, and builds a default slot tree.
- Slot changes refresh children and default child selections.
- Save/export preserves non-slot `.pc` data such as paint specs and complex vehicle parameters.
- Multiple dirty parts/config files can exist in one session.

## JBeam Editing Model

JBeam editing should be part-local by default.

Object Mode:

- A JBeam part behaves as an editable object/unit.
- Moving a part moves its owned JBeam structure and attached visual elements.
- Flexbodies configured by that part must remain attached to the part, even if hidden, translucent, or non-selectable.
- Beams that cross to other parts still belong to the part/file where they are defined.
- Until the object-level part transform model is implemented, experimental editable JBeam mesh object transforms should be locked. Moving a whole mesh object also moves proxy/reference vertices, which makes cross-part references appear disconnected from their real owner nodes.

Edit Mode:

- Edit only the active JBeam part's owned nodes, beams, triangles, hydros, sliders, and properties.
- Other parts may be shown as locked/reference context.
- Cross-part references should be visible without silently changing ownership.

## Blender Representation

The current sphere/cylinder visual representation is useful for inspection/debugging but is probably too heavy and indirect for authoring.

The future editing representation should use Blender mesh principles:

- Nodes as vertices.
- Beams as edges.
- Collision triangles as faces.
- Hydros, sliders, rails, special links, and external references as overlays or auxiliary reference data.
- Rail-locked or beam/rail-constrained nodes must be represented as positional constraints along their rail/beam, not as freely editable vertices.
- Triangle winding order is meaningful because BeamNG collision triangles only collide from one side. The editor must preserve triangle node order, make face normals/direction visible, and warn before any operation flips or normalizes winding.
- Mesh-native edge visibility should be preferred over separate legacy beam geometry. Duplicate Skin/Wireframe preview meshes were visually promising, but the live synchronization cost was too high during Edit Mode node movement and the approach is parked for now.
- Experimental editable JBeam meshes should depth-test normally against flexbodies by default. X-ray/always-on-top display can be useful as an explicit inspection mode, but should not be the default authoring view because translucent collision faces appear to float over body geometry.

This lets the editor use Blender's native selection and editing concepts while the internal resolved model remains authoritative.

Recommended approach:

- Keep current visual/debug mode intact.
- Add an experimental JBeam Mesh Edit Mode rather than replacing the current visualisation immediately.
- Avoid forking the project unless a future rewrite becomes too disruptive.

## Cross-Part References

External nodes should appear as locked ghost/reference nodes when editing a part that references them.

Ownership rules:

- A child part may reference parent-owned nodes.
- The editor should not silently import or duplicate parent nodes into child parts.
- If a child edit requires a new parent attachment/reference node, provide an explicit action to create that node in the parent part.
- Cross-part references should remain clear and validated.

Optional metadata comments may be useful for tool-only relationship hints, but they must be designed carefully so BeamNG ignores them and the importer can recognise them later.

## Property Panels

Selecting an editable JBeam element should show a structured property panel with logical sections. This will require careful modelling because JBeam parameters vary widely.

Node panel sections should include:

- Identity: node name/id, owning part, source file.
- Position: coordinates, local/world/resolved position where useful.
- Physics: mass and other known node physics parameters.
- References: beams, triangles, hydros, sliders, props, or flexbodies that depend on the node.
- Validation: duplicate id, missing references, cross-part status.
- Other: unknown/unmodelled parameters preserved from source.

Beam panel sections should include:

- Identity: editor row id where needed, owning part, source file.
- Nodes: endpoint node ids, endpoint ownership, endpoint positions.
- Physics: spring, damp, deform, strength, precompression, and other known beam parameters.
- Groups/options: beam groups, break groups, bounded settings, disable flags where applicable.
- Validation: missing endpoints, cross-part references, deleted/disabled endpoint warnings.
- Other: unknown/unmodelled parameters preserved from source.

Triangle, hydro, slider, prop, and flexbody panels should follow the same pattern:

- Identity and ownership.
- Referenced nodes/parts/assets.
- Known parameters grouped by meaning.
- Triangle winding/node order and derived collision normal direction for collision faces.
- Rail/beam constraint data where nodes are locked to a position along a rail or beam.
- Validation warnings.
- Other unknown/unmodelled parameters.

Unknown parameters should remain visible and inspectable even before the editor understands them.

## Change Tracking And Undo

Use a mutable working model plus explicit change tracking.

Each meaningful edit should record:

- File.
- Part.
- Section.
- Row/id.
- Field.
- Old value.
- New value.
- Operation type: update, insert, soft delete, hard delete, rename, move.

Do not override Blender's global undo system. Instead:

- Use Blender undo-aware operators where possible.
- Route structural/property edits through editor operators.
- Monitor or control transforms so the internal model and Blender objects stay in sync.
- Keep an internal operation history for export safety and model consistency.

Deletion should support both soft and hard delete:

- Soft delete/comment/disable by default.
- Hard delete as an explicit advanced action with dependency warnings.

## Validation

Validation should run at two levels.

Live validation:

- Missing node references.
- Duplicate node ids.
- Beams/triangles/hydros/sliders referencing missing nodes.
- Triangle winding changes that may flip one-sided collision direction.
- Rail/beam-locked nodes whose stored position no longer lies on the referenced rail/beam.
- Cross-part references highlighted clearly.
- Invalid slot selections.

Export validation:

- Dependency checks across dirty parts/files.
- Deleted nodes still referenced elsewhere.
- Collision triangle winding/order preserved or explicitly acknowledged when changed.
- Required target paths are safe mod/user paths, not vanilla.
- External references and tool metadata comments are coherent.
- A report is generated for any questionable output.

## View And Display Modes

The current visual modes remain useful:

- Body/flexbody view.
- Props view.
- JBeam visual/debug view.
- Translucent body plus selectable JBeam authoring view.
- One-way 3D View sync from the source viewport where sync was enabled.

Future display work should allow independent view filters per 3D View without relying on heavy geometry where a lightweight mesh/overlay would work better.
