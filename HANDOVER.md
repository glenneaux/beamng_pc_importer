# Handover: Blender 4.2 Mesh Topology Callback Patch

## Repositories and Locations

There are two separate repositories involved:

- Blender source repo:
  - `I:\blender4.2_SRC\blender\`
  - Use this for Blender 4.2 C/C++/Python API patch work.

- BeamNG Blender add-on repo:
  - `\\nas\Downloads\beamng modding\beamng_pc_importer\`
  - IDE/mapped path may appear as `z:\beamng modding\beamng_pc_importer\`
  - Use this for the Python add-on/plugin changes and handover docs.

The broader BeamNG modding workspace is:

- `\\nas\Downloads\beamng modding\`
- IDE/mapped path may appear as `z:\beamng modding\`

## Current Add-on Context

The Python add-on already runs on Blender 4.2. The desired Blender patch is not a compatibility patch.

The goal is to improve Blender as a host for BeamNG/JBeam topology authoring by:

- improving Blender's handling of custom data on mesh geometry
- adding callbacks so the add-on does not have to scan/debounce broad mesh changes

The add-on currently stores stable topology identifiers as mesh attributes:

- `beamng_node_uid` on the `POINT` domain
- `beamng_edge_uid` on the `EDGE` domain
- `beamng_face_uid` on the `FACE` domain

The add-on currently has to track topology changes indirectly via depsgraph updates, timers, signatures, and repair scans. This is expensive and imprecise because Blender mostly exposes "mesh changed", not "this topology/domain/custom-data changed".

Relevant add-on code areas in `__init__.py`:

- Auto-scan/debounce state starts around `JBEAM_AUTO_SCAN_DEBOUNCE_SECONDS`.
- `experimental_jbeam_mesh_depsgraph_update_post(scene, depsgraph)` tags scans when relevant objects change.
- `poll_experimental_jbeam_mesh_auto_scan(scene)` compares signatures and schedules scans.
- `tag_experimental_jbeam_auto_scan()` schedules the timer.
- UID attribute constants:
  - `JBEAM_NODE_UID_ATTR = "beamng_node_uid"`
  - `JBEAM_EDGE_UID_ATTR = "beamng_edge_uid"`
  - `JBEAM_FACE_UID_ATTR = "beamng_face_uid"`
- `ensure_experimental_topology_uids(obj, allow_write=True)` maintains per-element topology UIDs.

## Desired Blender Behavior

The add-on would benefit from Python callbacks that report meaningful mesh topology/custom-data changes after edit operations.

Ideal callback shape:

```python
def on_mesh_topology_changed(mesh, event):
    event.domain        # POINT, EDGE, FACE
    event.operation     # CREATE, DELETE, SPLIT, MERGE, ATTRIBUTE_CHANGE
    event.created_ids
    event.deleted_ids
    event.source_ids
    event.attribute_names
```

A smaller first patch is also useful:

```python
bpy.app.handlers.mesh_topology_update_post
```

with enough information to identify the changed mesh and possibly dirty domains:

```python
@persistent
def beamng_mesh_topology_update(mesh):
    if "beamng_node_uid" in mesh.attributes:
        handle_beamng_mesh_change(mesh)
```

Even a mesh-level post-edit callback would allow the add-on to replace broad depsgraph scanning with targeted handling.

## Blender Source Patch Points

The local Blender 4.2 source repo is at:

```text
I:\blender4.2_SRC\blender\
```

The most important first patch point found in the source is:

```text
source/blender/editors/mesh/editmesh_utils.cc
```

Function:

```cpp
void EDBM_update(Mesh *mesh, const EDBMUpdate_Params *params)
```

Most edit-mode mesh operators call `EDBM_update()` after modifying mesh topology. Python's `bmesh.update_edit_mesh()` also funnels into:

```cpp
void EDBM_update_extern(Mesh *mesh, const bool do_tessellation, const bool is_destructive)
```

which then calls `EDBM_update()`.

This makes `EDBM_update()` the clean first attachment point for a "mesh topology/custom-data updated" notification.

Python `bmesh.update_edit_mesh()` path found in:

```text
source/blender/python/bmesh/bmesh_py_api.cc
```

It calls:

```cpp
EDBM_update_extern(mesh, do_loop_triangles, is_destructive);
```

## Python Handler Exposure Points

Blender callback enum:

```text
source/blender/blenkernel/BKE_callbacks.hh
```

Add a new event near depsgraph callbacks, for example:

```cpp
BKE_CB_EVT_MESH_TOPOLOGY_UPDATE_POST,
```

Python app handler table:

```text
source/blender/python/intern/bpy_app_handlers.cc
```

Add a field such as:

```cpp
{"mesh_topology_update_post", "on mesh topology update after edit mesh flush"},
```

Callback execution helper already exists:

```text
source/blender/blenkernel/intern/callbacks.cc
```

Existing helpers include:

```cpp
void BKE_callback_exec_id(Main *bmain, ID *id, eCbEvent evt);
```

## Design Wrinkle

`EDBM_update()` currently only receives:

```cpp
Mesh *mesh
const EDBMUpdate_Params *params
```

It does not receive `Main *bmain` or `bContext *C`.

Options:

- Use `G_MAIN` from inside/near `EDBM_update()` for a first private patch.
- Add a callback helper that tolerates a null `Main`.
- Emit the callback one layer higher where `Main` or context is already available.

For a private experimental patch, using `EDBM_update()` directly is likely the fastest useful path. For an upstreamable patch, avoid relying on global state if possible.

## Custom Data / Attribute Source Areas

If the work expands from callbacks into custom-data survival policy, inspect:

```text
source/blender/blenkernel/intern/customdata.cc
source/blender/blenkernel/intern/attribute.cc
source/blender/blenkernel/intern/attribute_access.cc
source/blender/makesrna/intern/rna_attribute.cc
source/blender/makesrna/intern/rna_mesh.cc
source/blender/bmesh/intern/bmesh_mesh_convert.cc
source/blender/bmesh/intern/bmesh_mesh_convert.hh
```

Important lower-level conversion function:

```cpp
void BM_mesh_bm_to_me(Main *bmain, BMesh *bm, Mesh *mesh, const BMeshToMeshParams *params)
```

found in:

```text
source/blender/bmesh/intern/bmesh_mesh_convert.cc
```

## Recommended Patch Strategy

Start narrow:

1. Add a new Blender callback event for post edit-mesh topology updates.
2. Fire it from `EDBM_update()` after the edit operation has tagged geometry dirty and recalculated required normals/looptris.
3. Expose it as `bpy.app.handlers.mesh_topology_update_post`.
4. Pass the changed `Mesh` as the handler argument.
5. Update the BeamNG add-on to use that handler when present, falling back to existing depsgraph/timer scanning on stock Blender.

Avoid starting by modifying every edit mesh operator. Nearly all relevant operators already funnel through `EDBM_update()`.

Also avoid making Blender understand BeamNG/JBeam semantics. Blender should provide generic mesh topology/custom-data notifications; the add-on should continue owning JBeam-specific UID and export logic.

## Patch Status - 2026-05-16

First narrow Blender callback patch has been applied in the local Blender source repo on branch `jbeam`.

Changed files:

```text
I:\blender4.2_SRC\blender\source\blender\blenkernel\BKE_callbacks.hh
I:\blender4.2_SRC\blender\source\blender\python\intern\bpy_app_handlers.cc
I:\blender4.2_SRC\blender\source\blender\editors\mesh\editmesh_utils.cc
```

Implementation:

- Added `BKE_CB_EVT_MESH_TOPOLOGY_UPDATE_POST`.
- Exposed it as `bpy.app.handlers.mesh_topology_update_post`.
- Fired it from `EDBM_update()` after edit mesh update work using:

```cpp
BKE_callback_exec_id(G_MAIN, &mesh->id, BKE_CB_EVT_MESH_TOPOLOGY_UPDATE_POST);
```

The BeamNG add-on build 115 now registers an optional handler when the patched Blender exposes it:

```python
getattr(bpy.app.handlers, "mesh_topology_update_post", None)
```

Stock Blender remains supported because the add-on falls back to the existing depsgraph/timer/signature path when this handler is unavailable.

Validation status:

- Add-on Python compile passed.
- Blender C++ compile was not completed because the existing local build folder is configured for `Visual Studio 15 2017`, which is not available on this machine:

```text
CMake Error: Visual Studio 15 2017 could not find any instance of Visual Studio.
```

Next validation step:

- Configure a current Blender build environment, preferably Ninja/MSVC or the currently installed Visual Studio generator.
- Build Blender.
- In patched Blender, verify:

```python
hasattr(bpy.app.handlers, "mesh_topology_update_post")
```

- Add a temporary Python handler that prints the received mesh name, then edit an experimental JBeam mesh and confirm the handler fires after edit mesh flush.

## Longer-Term Better API

A more complete future API could report:

- affected domains: `POINT`, `EDGE`, `FACE`, `CORNER`
- topology vs attribute-only changes
- created/deleted element indices
- split/merge source relationships
- changed custom attribute names

That could allow the add-on to update UID maps without rescanning entire meshes.

For now, a mesh-level callback is still valuable because it removes broad scene/depsgraph scanning and lets the add-on respond only to changed meshes.

## Patch Status - 2026-05-16 Proxy Lock Big Swing

The next Blender-fork pass has been staged in the local Blender source repo on branch `jbeam`.

Changed Blender files:

```text
I:\blender4.2_SRC\blender\source\blender\editors\transform\transform_convert_mesh.cc
I:\blender4.2_SRC\blender\source\blender\editors\mesh\editmesh_tools.cc
```

Implementation:

- Transform now ignores edit-mesh vertices with the attribute `beamng_node_is_proxy`, so proxy/reference nodes should not get `TransData` and therefore should not move with normal grab/transform tools. The lock accepts bool, int32, or int8 layers because the add-on currently writes this as an integer Blender attribute.
- Central mesh delete now filters selected proxy vertices before delete, and also protects selected edges/faces that touch proxy vertices for the standard delete operator.
- This is intentionally BeamNG-fork-specific. It relies on the add-on's existing persistent Blender attribute `beamng_node_is_proxy`.

Changed add-on file:

```text
beamng_pc_importer\__init__.py
```

Implementation:

- Add-on build is now `1.0.1 build 116`.
- Added a 3D View overlay that draws proxy/reference nodes as orange crosses.
- Added preference `Show Proxy Node Crosses`.
- Kept stock-Blender fallback proxy sync/scan behaviour.
- In patched Blender, the optional `mesh_topology_update_post` handler now also attempts proxy sync before tagging auto-scan.

Validation status:

- Add-on source Python compile passed.
- Installed add-on copy Python compile passed.
- `git diff --check` passed for the touched add-on and Blender files, with only existing CRLF warnings from Git.
- Blender C++ compile still needs to be run with `I:\blender4.2_SRC\blender\make.bat` or the direct incremental build command.

Residual risk:

- The delete protection currently covers the standard `MESH_OT_delete` path. Other topology-changing operators such as dissolve, merge, extrude, knife, and script-created topology may still need follow-up guards or add-on validation.
- The overlay is a lightweight draw handler; if it is noisy or slow on huge vehicles, disable `Show Proxy Node Crosses` in add-on preferences.

## Patch Status - 2026-05-17 Native Topology GUIDs

Blender source now has native GUID maintainers for both edit-mode and object-mode mesh data.

Changed Blender file:

```text
I:\blender4.2_SRC\blender\source\blender\blenkernel\BKE_mesh.hh
I:\blender4.2_SRC\blender\source\blender\blenkernel\intern\mesh.cc
I:\blender4.2_SRC\blender\source\blender\editors\mesh\editmesh_utils.cc
I:\blender4.2_SRC\blender\source\blender\makesrna\intern\rna_mesh_api.cc
```

Implementation:

- Ensures edit meshes have string geometry attributes containing RFC4122 UUID/GUID values:
  - `beamng_node_uid` on vertices.
  - `beamng_edge_uid` on edges.
  - `beamng_face_uid` on faces.
- Existing valid unique GUIDs are preserved.
- Missing, invalid, nil, or duplicate GUIDs are assigned new UUID v4 strings after edit-mesh updates.
- New topology created by Blender tools should therefore receive stable GUIDs at the end of the operator/update cycle.
- If an older experimental mesh still has integer attributes with these names, the patch removes those int layers and recreates string layers.
- Object-mode `Mesh` datablocks also get the same GUID attributes through `blender::bke::mesh_ensure_topology_guids(Mesh &mesh)`.
- Opened `.blend` meshes are materialized during mesh blend-read.
- Importers and Python-created meshes are materialized when `mesh.update()` runs.
- The values are exposed to Python through Blender's normal geometry attribute API:

```python
mesh.attributes["beamng_node_uid"]
mesh.attributes["beamng_edge_uid"]
mesh.attributes["beamng_face_uid"]
```

Current limitation:

- Direct object-mode mesh mutation paths that do not call `mesh.update()` may not materialize until a later update/load/edit path.
- Blender string attributes store `MStringProperty`, currently 255 bytes, so formatted 36-character UUID strings fit safely.

## Patch Status - 2026-05-17 View3D Item GUID Display

Blender source now displays BeamNG topology GUIDs in the View3D sidebar `Item > Transform` panel for edit-mode mesh selections.

Changed Blender file:

```text
I:\blender4.2_SRC\blender\source\blender\editors\space_view3d\view3d_buttons.cc
```

Implementation:

- Reads string attributes:
  - `beamng_node_uid` for a single selected vertex.
  - `beamng_edge_uid` for a single selected edge.
  - `beamng_face_uid` for a single selected face.
- Shows a read-only label above the existing transform/median fields:
  - `Vertex GUID: ...`
  - `Edge GUID: ...`
  - `Face GUID: ...`
- Mixed or multiple selection intentionally does not show a GUID because the transform panel is a median editor and there is no single identity to display.

Validation status:

- `git diff --check` passed for `view3d_buttons.cc`, with only Git CRLF warnings.
- Requires Blender rebuild before testing.
