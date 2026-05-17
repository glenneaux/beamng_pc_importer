# ADVICE.md

# Ideal Blender Semantic Topology Platform — Execution Roadmap

Target:
Blender 4.2 LTS

Goal:

Create a stable, event-driven semantic topology platform for BeamNG/JBeam authoring using:

- minimal Blender native modifications
- stable topology identity
- topology lifecycle callbacks
- Python semantic graph engine
- maintainable upstream-compatible architecture

---

# PHASE 0 — ENVIRONMENT FOUNDATION

Goal:

Create stable development environment before writing infrastructure code.

---

# Step 0.1 — Prepare Workstation

Recommended:

- Windows host
- WSL2 Ubuntu
- VSCode or CLion
- Git
- Ninja
- CMake
- ccache

Deliverable:

Stable native Blender build environment.

---

# Step 0.2 — Fork Blender

Fork official Blender repository.

Create:

```text
blender-jbeam
```

Deliverable:

Own maintained Blender fork.

---

# Step 0.3 — Clone Blender 4.2 LTS

Track:

```text
blender-v4.2-release
```

NOT main/master.

Deliverable:

Stable upstream base.

---

# Step 0.4 — Configure Git Workflow

Create:

```text
upstream
origin
```

remotes.

Create integration branch:

```text
jb-4.2
```

Deliverable:

Long-term maintainable git structure.

---

# Step 0.5 — Compile Vanilla Blender

Compile unmodified Blender successfully.

Deliverable:

Known-good build pipeline.

Critical before modifying anything.

---

# PHASE 1 — BLENDER INTERNALS FAMILIARIZATION

Goal:

Understand lifecycle before patching.

---

# Step 1.1 — Study BMesh

Focus on:

```text
source/blender/bmesh/
```

Especially:

```text
bmesh_class.hh
bmesh_core.cc
```

Deliverable:

Understanding of editable topology lifecycle.

---

# Step 1.2 — Study CustomData

Focus on:

```text
BKE_customdata.hh
customdata.cc
```

Deliverable:

Understanding metadata persistence pipeline.

---

# Step 1.3 — Study RNA

Focus on:

```text
makesrna/
rna_mesh.cc
```

Deliverable:

Understanding Python exposure system.

---

# Step 1.4 — Study Undo

Focus on:

```text
editors/undo/
```

Deliverable:

Understanding topology reconstruction lifecycle.

---

# PHASE 2 — RESTRUCTURE CURRENT PYTHON ADDON

Goal:

Prepare addon architecture BEFORE native integration.

Critical phase.

---

# Step 2.1 — Remove Blender Index Dependence

Eliminate reliance on:

- vert.index
- edge.index
- face.index

Deliverable:

Index-independent architecture.

---

# Step 2.2 — Create Semantic Graph Layer

Implement:

```python
Node
Beam
Rail
Hydro
Constraint
```

Deliverable:

Explicit semantic topology model.

---

# Step 2.3 — Create TopologyBridge

Centralize ALL Blender topology access.

Deliverable:

Abstracted mesh interaction layer.

---

# Step 2.4 — Add Temporary UUID Emulation

Simulate persistent IDs in Python.

Deliverable:

Stable semantic references before native UID system exists.

---

# Step 2.5 — Convert Polling Into Synthetic Events

Current topology scan emits:

```text
EDGE_CREATED
EDGE_DELETED
EDGE_SPLIT
```

synthetically.

Deliverable:

Event-driven semantic architecture BEFORE native hooks.

Critical migration step.

---

# PHASE 3 — CREATE NATIVE EXTENSION INFRASTRUCTURE

Goal:

Create isolated Blender instrumentation subsystem.

---

# Step 3.1 — Create Native Extension Directory

Recommended:

```text
intern/jbeam_ext/
```

Deliverable:

Centralized extension subsystem.

---

# Step 3.2 — Add Build Integration

Modify:

```text
CMakeLists.txt
```

Deliverable:

Native extension compiles with Blender.

---

# Step 3.3 — Add Logging System

Implement:

```c
JB_LOG(...)
```

Deliverable:

Native event tracing infrastructure.

Critical for debugging.

---

# Step 3.4 — Add Core Extension API

Create:

```c
JB_UID
JB_Event
JB_Transaction
```

base systems.

Deliverable:

Foundational infrastructure layer.

---

# PHASE 4 — IMPLEMENT PERSISTENT UID SYSTEM

MOST IMPORTANT PHASE.

---

# Step 4.1 — Create CustomData UID Layer

Add:

```c
CD_JB_UID
```

Deliverable:

Per-element persistent identity storage.

---

# Step 4.2 — Attach UID Layer To:

- verts
- edges
- faces

Deliverable:

Universal topology identity.

---

# Step 4.3 — Create UID Allocator

Implement:

```c
JB_uid_allocate()
```

Deliverable:

Stable unique ID generation.

---

# Step 4.4 — Expose UID Through RNA

Expose:

```python
vert.jb_uid
edge.jb_uid
face.jb_uid
```

Deliverable:

Python-visible stable identity.

---

# Step 4.5 — Validate Lifecycle Stability

Verify:

- edit mode
- duplicate
- split
- extrude
- dissolve
- save/load
- undo/redo

Deliverable:

Reliable persistent topology identity.

This is first major milestone.

---

# PHASE 5 — TOPOLOGY EVENT SYSTEM

Goal:

Eliminate topology polling.

---

# Step 5.1 — Create Event Dispatcher

Implement:

```c
JB_event_dispatch(...)
```

Deliverable:

Centralized topology event system.

---

# Step 5.2 — Hook Creation Events

Hook:

```c
BM_vert_create
BM_edge_create
BM_face_create
```

Deliverable:

Direct topology creation events.

---

# Step 5.3 — Hook Deletion Events

Hook:

```c
BM_vert_kill
BM_edge_kill
BM_face_kill
```

Deliverable:

Direct topology destruction events.

---

# Step 5.4 — Create Python Subscription API

Implement:

```python
bpy.app.jbeam.subscribe(...)
```

Deliverable:

Python-accessible event stream.

---

# Step 5.5 — Replace Synthetic Events

Remove Python topology polling.

Semantic graph now consumes native events.

Deliverable:

True event-driven architecture.

Major milestone.

---

# PHASE 6 — TRANSACTION SYSTEM

Goal:

Create deterministic topology mutation batches.

---

# Step 6.1 — Add Transaction Boundaries

Implement:

```text
BEGIN_TRANSACTION
END_TRANSACTION
```

around operators.

Deliverable:

Deterministic event batching.

---

# Step 6.2 — Connect Operators

Integrate with:

- extrude
- duplicate
- subdivide
- dissolve
- split

Deliverable:

Operator-scoped semantic transactions.

---

# Step 6.3 — Add Transaction Replay

Support undo replay.

Deliverable:

Deterministic semantic rollback.

---

# PHASE 7 — SEMANTIC GRAPH REWRITE

Goal:

Transition fully to event-driven semantic model.

---

# Step 7.1 — Rewrite NodeManager

Consume native events.

Deliverable:

Incremental node graph updates.

---

# Step 7.2 — Rewrite BeamManager

Track beam lineage.

Deliverable:

Stable semantic beam topology.

---

# Step 7.3 — Implement RailManager

Support:

- proxy edges
- split propagation
- semantic continuity

Deliverable:

Deterministic rail behavior.

---

# Step 7.4 — Implement HydroManager

Support:

- actuator relationships
- propagation
- constraints

Deliverable:

Stable hydro semantics.

---

# Step 7.5 — Remove Legacy Sync Logic

Delete:

- topology scans
- repair passes
- heuristic reconciliation

Deliverable:

Pure event-driven semantic engine.

Huge milestone.

---

# PHASE 8 — UNDO/REDO STABILIZATION

Goal:

Achieve deterministic editing lifecycle.

---

# Step 8.1 — Verify UID Stability Through Undo

Deliverable:

Persistent identity survives reconstruction.

---

# Step 8.2 — Verify Event Replay

Deliverable:

Consistent semantic graph restoration.

---

# Step 8.3 — Implement Semantic Rollback

Deliverable:

Undo-safe semantic graph engine.

---

# Step 8.4 — Stress Test Undo

Test:

- extrude chains
- dissolves
- splits
- rail edits
- hydro edits

Deliverable:

Production-grade stability.

---

# PHASE 9 — SEMANTIC AUTHORING PLATFORM

Goal:

Blender becomes semantic engineering editor.

---

# Step 9.1 — Semantic Edge Creation

New edge automatically becomes beam.

Deliverable:

Native semantic authoring.

---

# Step 9.2 — Semantic Extrusion

Extrusion propagates:

- beam lineage
- rails
- hydros
- metadata

Deliverable:

Topology-aware semantic editing.

---

# Step 9.3 — Semantic Splits

Split operations preserve:

- rail continuity
- beam semantics
- hydro relationships

Deliverable:

Deterministic semantic topology editing.

---

# Step 9.4 — Add Semantic UI

Inspectors:

- node IDs
- beam IDs
- rail references
- topology lineage

Deliverable:

Debuggable semantic topology platform.

---

# PHASE 10 — EXPORT PIPELINE STABILIZATION

Goal:

Scene becomes authoritative semantic source.

---

# Step 10.1 — JBeam Generation

Generate semantic graph → JBeam.

Deliverable:

Stable export pipeline.

---

# Step 10.2 — DAE Export Integration

Use Blender DAE exporter.

Deliverable:

Geometry export compatibility.

---

# Step 10.3 — Metadata Serialization

Export semantic metadata.

Deliverable:

Complete asset pipeline.

---

# PHASE 11 — ADVANCED SYSTEMS

Only AFTER core stability.

---

# Step 11.1 — Live BeamNG Sync

Realtime updates.

---

# Step 11.2 — Structural Visualization

Force/load visualization.

---

# Step 11.3 — Hydraulic Graph Solver

Advanced hydraulic systems.

---

# Step 11.4 — Constraint Solvers

Advanced engineering semantics.

---

# Step 11.5 — FEM-Style Analysis

Potential future direction.

---

# IDEAL FINAL ARCHITECTURE

```text
Blender 4.2 LTS
    ↓
Topology instrumentation layer
    ↓
Stable UID system
    ↓
Event dispatcher
    ↓
Transaction system
    ↓
Python semantic engine
    ↓
BeamNG/JBeam systems
    ↓
Export pipeline
```

---

# MOST IMPORTANT SUCCESS CRITERIA

If you successfully achieve ONLY these three things:

---

# 1. Stable Persistent UID System

---

# 2. Native Topology Event Stream

---

# 3. Deterministic Undo Transactions

---

then:

- rails become tractable
- hydros become tractable
- semantic editing becomes deterministic
- topology synchronization complexity collapses
- the entire architecture becomes scalable

Those are the true foundation systems.

