# BeamNG PC Importer TODO

Use this as a parking place for ideas, concepts, bugs, and things to revisit.

## Next Review

- Date:
- Focus:

## Ideas

- 

## Bugs / Fixes

- PC export is experimental. Future work needs to verify that all complete .pc information survives export, including paint specs/colors and any other non-slot configuration data used by complex vehicles.
- Continue investigating BeamNG paint data support. Future work should map how paint details are split across .pc `paints` data, JBeam paint/skin/material slots, `globalSkin`, dynamic decal materials, and `.materials.json`/`skin.materials.json` files so export/editing preserves the full visual configuration.

## Design Questions

- Investigate robust synchronized 3D View navigation for independent BeamNG views. Blender Quad View supports synced zoom/pan, but it does not fit the independent Flex/Props/JBeam viewport filtering workflow because Quad View shares display settings. Current custom sync is intentionally one-way: the 3D View where sync is enabled drives all other 3D Views. Future work should consider a dedicated ViewSync-style implementation if secondary/third view manipulation should eventually drive the others too.

## Later

- 
