# BeamNG PC Importer TODO

Use this as a parking place for ideas, concepts, bugs, and things to revisit.

## Next Review

- Date:
- Focus:

## Ideas

- Parked experimental JBeam beam preview: duplicating the JBeam mesh and using a Skin or Wireframe modifier produced excellent beam visibility while preserving translucent triangle faces, but live coupling/synchronization was too slow during Edit Mode node movement. Revisit later with a lighter overlay strategy, GPU draw handler, or deferred/topology-aware sync that does not rebuild modifier-backed mesh data while dragging.

## Bugs / Fixes

- PC export is experimental. Future work needs to verify that all complete .pc information survives export, including paint specs/colors and any other non-slot configuration data used by complex vehicles.
- Continue investigating BeamNG paint data support. Future work should map how paint details are split across .pc `paints` data, JBeam paint/skin/material slots, `globalSkin`, dynamic decal materials, and `.materials.json`/`skin.materials.json` files so export/editing preserves the full visual configuration.
- Continue prop import validation. A first documentation-aligned transform pass now handles inherited prop options, static function factor evaluation, local/global translation separation, and documented prop Euler orders. Next work should compare several vanilla steering wheels, needles, pedals, lights, and driveshafts against BeamNG in-game placement.
- Harden identity-first topology editing. The current pass now uses persistent integer topology UIDs on Blender vertices, edges, and faces with JSON maps keyed by UID. A later pass should stress-test delete/merge/separate/extrude workflows and decide whether more params should move from JSON maps into native Blender attributes.
- Stress-test export preflight with messy topology edits. Current export blocks non-triangle faces and missing non-proxy references, shows selected node/beam/triangle counts, and commits mesh baselines after successful export; future work should add richer face-normal/winding UI before allowing triangle-heavy editing to feel final.
- Stress-test explicit topology operators: Add Node, Beam 2, Triangle 3, Delete JBeam, Triangulate Faces, and Flip Triangles across edit-mode selection, undo, export, and reimport.
- Stress-test cross-part proxy deletion. Deleting a proxy/reference node from one part should reliably delete local dependent beams/triangles only, while deleting an owned source node should remove matching proxy nodes and their local dependent beams/triangles from every other editable JBeam mesh.
- Continue model-spine migration. `resolved_model.py` now stores files/parts/nodes/beams/triangles/edit operations as an authoring-model snapshot; next passes should make mesh creation, property panels, validation, and export read/write this model directly instead of treating Blender mesh JSON as primary state.

## Design Questions

- Investigate robust synchronized 3D View navigation for independent BeamNG views. Blender Quad View supports synced zoom/pan, but it does not fit the independent Flex/Props/JBeam viewport filtering workflow because Quad View shares display settings. Current custom sync is intentionally one-way: the 3D View where sync is enabled drives all other 3D Views. Future work should consider a dedicated ViewSync-style implementation if secondary/third view manipulation should eventually drive the others too.
- Revisit import source-loading strategy. For now, loading a vanilla configuration should warn only when user mod assets would affect it, and the importer should provide an obvious "vanilla data only / no mods or overrides" option. Longer term, source layers should be clearer: vanilla, unpacked mods, mod zips, selected external files, and user `.pc` configurations should be visible as explicit resolution layers.
- Future importer workflow cleanup: make `.pc from BeamNG assets` the primary/default config import path and keep direct `.pc file` import as an advanced/manual fallback. Both `.pc` paths should share the same two-mode UI: `Visual model only` for flexbodies/props/config slots without JBeam unless explicitly requested, and `Authoring mode` for editable JBeam parts, assembly state, slots, proxies, and crossbeam tools.
- Future mod-resolution warning cleanup: warn whenever the selected `.pc` could be affected by active user mods/overrides, whether the `.pc` came from vanilla assets, user files, or a direct file pick. Offer `Load clean / ignore mods` and `Load with active mods`, with clean/no-mod resolution as the default. Add-on cache/materialized files must remain excluded from normal source discovery except as temporary internal files for selected zip assets.
- Future raw JBeam import cleanup: keep raw `.jbeam` import separate as `Editable JBeam Part/File Import`, offering `All parts` or a specific part from the file. Warn that importing multiple parts with shared node, beam, or triangle names can create identity/name conflicts until namespacing or conflict resolution is implemented.
- Revisit working-mod selection. The current global working-mod folder is acceptable for now, but later the editor may need to list active/available mods and let the user select the target mod per session or export. JBeam/DAE exports should always identify or ask for the active mod folder before writing, with `.pc` output staying under `current/vehicles` and vehicle assets staying under `current/mods/unpacked/<mod name>/vehicles`.

## Later

- 
