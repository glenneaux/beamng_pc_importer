# AGENTS.md

Provider-agnostic instructions for AI coding assistants (Claude, Codex/ChatGPT,
Cursor, etc.) working in this repository. Follow this file regardless of which
tool you are running under.

This file is the single source of truth. Do not maintain duplicate guidance in
provider-specific files (`CLAUDE.md`, `.cursorrules`, etc.).

---

## 1. What this project is

A Blender add-on (`bl_info` in `__init__.py`) that imports BeamNG `.pc` vehicle
configurations, resolves their JBeam slot tree, and presents the result for
visualisation and editing inside Blender. The long-term direction — a Blender-
based BeamNG authoring environment — is described in `ARCHITECTURE.md`,
`FUNCTIONAL_DESCRIPTION.md`, and `TODO.md`. **Read those before making
non-trivial design changes.**

## 2. Repository map

| Path | Role |
|------|------|
| `__init__.py` | Blender add-on entry point: `bl_info`, `register()` / `unregister()`, ~60 `Operator`/`Panel`/`PropertyGroup` classes, slot editor, JSONC PC-file rewriter, experimental JBeam mesh edit operators |
| `core.py` | JBeam / PC parsing and resolution: JSONC normalization, node/beam/triangle/hydro/rail/slidenode/flexbody/prop/slot parsing, asset-source collection (file + zip), vehicle resolution, reports |
| `visuals.py` | Blender mesh / material / collection construction; depends on `bpy`, `mathutils`, `bmesh` |
| `resolved_model.py` | Pure-Python authoring-model dataclasses; JSON round-trip and indices. Must remain `bpy`-free. |
| `dae_assets.py` | DAE indexing & catalog (file + zip) |
| `tests/` | See §5 below |
| `ARCHITECTURE.md` | Design direction — preserve-first editing, resolved vehicle model, export rules |
| `FUNCTIONAL_DESCRIPTION.md` | Planned editor engine behaviour |
| `TODO.md` | Parking lot for ideas / bugs not ready for implementation |

## 3. Setup

The add-on runs inside Blender's bundled Python; you do not need a separate
runtime to use the add-on.

For tooling (the `ruff` formatter / linter) install the optional `dev` extra
into a regular Python:

```sh
python -m pip install -e ".[dev]"
```

To run tests, install `pytest` and `syrupy` into **Blender's** bundled Python
(one-time, per Blender installation):

```sh
# Replace <blender>/<version> with your install path
<blender>/<version>/python/bin/python -m pip install pytest syrupy
```

On Windows the python executable is typically
`<blender>\<version>\python\bin\python.exe`.

## 4. Running tests

All tests run **inside Blender's bundled Python**. The add-on imports `bpy`
at module top, and the boundary tests we keep cover real Blender behaviour
(import operator, scene state, mesh topology) — so a single Blender-driven
runner is the only path that exercises them honestly.

Plain `pytest` from a normal shell will fail to even collect tests, because
`pyproject.toml` lives next to the add-on's `__init__.py` and pytest's
rootdir-package detection tries to import that file. This is expected.

### 4.1 Launch the suite

```sh
blender -b --factory-startup --python tests/run_pytest.py
# Or filter / pass extra args to pytest after `--`:
blender -b --factory-startup --python tests/run_pytest.py -- -k import_pc
blender -b --factory-startup --python tests/run_pytest.py -- -v
```

The runner inserts the repo root onto `sys.path`, forwards args after `--`
to `pytest.main`, and forces a real process exit code (Blender otherwise
swallows it).

### 4.2 Updating snapshots

When you deliberately change the parser output or authoring-model schema,
regenerate snapshots and **review the diff** before committing:

```sh
blender -b --factory-startup --python tests/run_pytest.py -- --snapshot-update
```

Never `--snapshot-update` reflexively to make a red test go green. The point
of the snapshot is to surface unintended changes.

## 5. Test layout & philosophy

```
tests/
├── conftest.py                          # fixtures: tiny_pc_path, addon_registered
├── run_pytest.py                        # Blender-driven pytest launcher
├── data/vehicles/tiny/                  # fixture vehicle (4 nodes, 1 triangle)
├── test_resolved_model_roundtrip.py     # JSON file-format boundary
├── test_data_formats.py                 # JBeam/PC -> resolved model
└── test_import_pc.py                    # import operator -> Blender scene state
```

**The tests we keep are boundary tests:** data files in, Blender state out.
Internal helper coverage is a non-goal — those helpers are exercised
indirectly through the parser / operator paths. When adding a new test, ask
"does this pin a system boundary (a file format, a Blender API call, or an
externally-observable resolved model)?" If not, prefer adding a fixture and
extending a snapshot.

## 6. Code style

- **Formatter**: `ruff format .`
- **Linter**: `ruff check .`
- Line length: 130. Indents: 4 spaces. Strings: double quotes.
- The 9K-line `__init__.py` is **not** wholesale-reformatted. Apply
  `ruff format` per-file you actually touch.
- Type hints are encouraged on new code but not required.
- Avoid adding comments that describe **what** the code does — names should
  carry that. Comments are for **why** non-obvious behaviour exists
  (constraints, BeamNG quirks, deliberate workarounds).
- Never write a docstring or comment that references "the current PR",
  "added for X", or "used by Y" — that belongs in commit messages.

## 7. Codebase conventions (important — read these)

These are project-specific rules. Violating them is a likely revert.

### 7.1 Module layering

- `resolved_model.py` must **not** import `bpy` or `mathutils`. It is the
  pure-Python authoring-model spine — keeping it dependency-free lets the
  JSON file-format boundary be tested cheaply.
- `core.py` and `dae_assets.py` may import `bpy` for cache directories and
  `mathutils.Matrix` / `Vector` for math. They are the parser layer.
- `visuals.py` and `__init__.py` are Blender-only.
- Star imports (`from .core import *`) are load-bearing across these files.
  When adding a new public symbol intended for `__init__.py`, define it in
  `core.py` and rely on the existing `*` re-export.

### 7.2 Preserve-first editing (per ARCHITECTURE.md)

When writing JBeam or PC files:

- **Preserve original structure, ordering, comments, and unknown fields**
  wherever practical.
- **Patch only the values/sections the editor understands.**
- Treat unknown parameters as valid data, not as errors.
- For new files (no source to preserve), generate clean structured output.

The JSONC tokenizer in `__init__.py` (`skip_jsonc_ws_comments`,
`scan_jsonc_string`, `find_matching_jsonc_brace`, `find_jsonc_object_for_key`)
exists specifically to enable this. Do not bypass it by parsing JSONC, mutating
the dict, and re-serializing — that destroys comments and field order.

### 7.3 Vanilla BeamNG files are read-only

- Never write into the BeamNG install folder.
- JBeam / DAE exports go to `current/mods/unpacked/<mod name>/vehicles/...`.
- `.pc` configuration exports go to `current/vehicles/<vehicle>/...`.
- Export operators must show a changed-file checklist and refuse vanilla
  target paths.

### 7.4 Topology identity is UID-first

Persistent Blender topology data uses per-vertex / per-edge / per-face integer
UIDs as the durable identity layer. JBeam IDs, source params, committed
params, and export mirrors live in JSON maps keyed by those UIDs. When adding
mesh-edit features:

- Assign stable provisional UIDs to new geometry **before** export scanning.
- Trim stale provisional metadata when geometry is deleted, so phantom JBeam
  nodes don't accumulate.
- New edges are **not** automatically beams. Edges that are only the boundary
  of a newly created triangle face are triangle topology unless the operator
  explicitly marks them as beams.

### 7.5 Position precision

Node and object positions are treated at **three decimal places** of
precision. Scanning, comparison, display, change records, and export should
all avoid noisy sub-millimetre churn unless a specific BeamNG field is
proved to need more precision.

### 7.6 Version + ADDON_BUILD

- `bl_info["version"]` in `__init__.py` is the user-visible add-on version.
- `ADDON_BUILD` increments for **every** build of the current `bl_info`
  version and **resets to 1** when `bl_info["version"]` changes.

### 7.7 Security / `eval`

`evaluate_jbeam_expression` uses `eval` with `__builtins__: {}` and a regex
filter to evaluate `$=` expressions from JBeam data. **Do not relax the
regex guard** without adding negative tests covering `__import__`, dunder
access, `**`, and `//`.

## 8. Pre-flight checklist (run before declaring work finished)

1. Format any files you touched: `ruff format <files>`.
2. Lint: `ruff check <files>`.
3. Run the test suite inside Blender:
   `blender -b --factory-startup --python tests/run_pytest.py`.
   This is the single test entry point; there is no faster "unit-only" tier.
4. If a snapshot test fails, **inspect the diff** before considering
   `--snapshot-update`. Snapshots exist to catch unintended regressions.
5. If you changed `bl_info["version"]`, reset `ADDON_BUILD` to 1.
   Otherwise, increment `ADDON_BUILD` if you produced a release-worthy
   build.
6. If you cannot run Blender from your environment, say so explicitly in
   your final message. Do not claim "tests pass" when they were not run.

## 9. Doing risky things

The harness rules apply to AI assistants too. Confirm with the user before:

- Deleting files, branches, or BeamNG asset folders.
- `git reset --hard`, force-push, amending published commits.
- Touching anything under the user's BeamNG install path during a test.
- Writing to vanilla BeamNG folders (this should be impossible by design —
  if you find yourself doing it, the design is wrong).

## 10. Things to avoid

- **Don't bypass the JSONC tokenizer** to "tidy up" a `.pc` or `.jbeam`
  file. Preserve-first editing is a hard rule, not a preference.
- **Don't add `bpy` imports to `resolved_model.py`.**
- **Don't add wholesale reformatting of `__init__.py`** as part of an
  unrelated change. The diff will be unreviewable.
- **Don't add tests for individual helper functions** when the same code
  path is covered by a boundary test on real fixture data. Helpers can be
  refactored freely if the boundary tests still pass.
- **Don't `--snapshot-update` to make a test pass.** Read the diff first.
- **Don't fabricate JBeam fields you have not seen in real data.** When in
  doubt, preserve.

## 11. When in doubt

- For design questions: re-read `ARCHITECTURE.md` and `FUNCTIONAL_DESCRIPTION.md`.
- For shorter-term experiments and known issues: `TODO.md`.
- For module responsibilities and the layering rule: §7.1 of this file.
- For test scope: §5 of this file ("boundary tests").
- Ask the user. Pausing to confirm is cheaper than re-doing a wrong change.
