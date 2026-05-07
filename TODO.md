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
- Revisit prop import logic. Props are still not importing correctly, even for visual/reference purposes, so their placement/orientation/scale pipeline needs another dedicated pass.

## Design Questions

- Investigate robust synchronized 3D View navigation for independent BeamNG views. Blender Quad View supports synced zoom/pan, but it does not fit the independent Flex/Props/JBeam viewport filtering workflow because Quad View shares display settings. Current custom sync is intentionally one-way: the 3D View where sync is enabled drives all other 3D Views. Future work should consider a dedicated ViewSync-style implementation if secondary/third view manipulation should eventually drive the others too.

## Later

- 
