# BeamNG PC Importer

Blender add-on script for importing BeamNG `.pc` configurations and visualizing related meshes, props, nodes, and beams.

The active add-on entry point is `__init__.py`.

## Overarching Goal

The long-term goal is to grow this from a BeamNG vehicle/configuration importer into a Blender-based BeamNG authoring environment. The editor should eventually support importing a vehicle, exploring and changing its slot configuration, editing JBeam structures part-by-part, and exporting safe mod-folder overrides without modifying vanilla BeamNG files.

## Project Notes

- `ARCHITECTURE.md` captures the larger design direction for the resolved vehicle model, JBeam mesh editing, preserve-first exports, validation, undo/redo, and future authoring workflows.
- `FUNCTIONAL_DESCRIPTION.md` describes the planned editor engine behaviour: resolver pipeline, dependency graph, stable entity IDs, dirty tracking, transactional edits, export patching, validation, and performance strategy.
- `TODO.md` tracks shorter-term bugs, experiments, and future work items that are not ready for implementation yet.

## Milestone Timeline

- Import foundation: load `.pc` configurations, resolve JBeam parts, and import visible meshes/flexbodies from BeamNG assets.
- Asset resolution: support vanilla assets, user configurations, unpacked mods, zip-sourced assets, and cache/catalogue lookups.
- Visual diagnostics: add JBeam node/beam/triangle/hydro/slider visualisation, labels, filters, and selection helpers.
- Configuration editing: add the slot tree editor, apply/reload behaviour, dirty-state tracking, revert, save, and save-as workflows.
- View workflow: support independent view filters and source-driven 3D View synchronization for multi-view inspection.
- Future authoring: build a resolved vehicle model, add part-local JBeam mesh editing, preserve unknown data, validate edits, and export mod-safe JBeam/PC overrides.

## Versioning

The add-on version is stored in `bl_info["version"]`. `ADDON_BUILD` increments for each build of that exact version and resets to `1` when the add-on version changes.
