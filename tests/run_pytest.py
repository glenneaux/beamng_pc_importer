"""Launches pytest inside Blender's bundled Python.

Usage::

    blender -b --factory-startup --python tests/run_pytest.py
    blender -b --factory-startup --python tests/run_pytest.py -- -k import_pc

Arguments after ``--`` are passed verbatim to pytest. With no extra args the
entire ``tests/`` suite runs.

Prerequisite (one-time per Blender installation)::

    <blender-install>/<version>/python/bin/python -m pip install pytest syrupy

On Windows the path is typically
``<blender-install>\\<version>\\python\\bin\\python.exe``.
"""

from __future__ import annotations

import os
import site
import sys
from pathlib import Path

THIS_FILE = Path(__file__).resolve()
REPO_ROOT = THIS_FILE.parent.parent
REPO_PARENT = REPO_ROOT.parent

# Blender disables user-site by default, so `pip install --user pytest`
# (the default on Windows when site-packages is non-writable) leaves the
# package invisible. Append the per-user site dir to sys.path manually.
site.ENABLE_USER_SITE = True
user_site = site.getusersitepackages()
if user_site and Path(user_site).is_dir() and user_site not in sys.path:
    sys.path.append(user_site)

# Make the add-on importable both as a package (parent on path so
# `import beamng_pc_importer` works) and as flat modules (repo root on path so
# `import core` / `import resolved_model` work for boundary tests).
for entry in (str(REPO_PARENT), str(REPO_ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

if "--" in sys.argv:
    pytest_args = sys.argv[sys.argv.index("--") + 1 :]
else:
    pytest_args = []

if not pytest_args:
    pytest_args = [str(REPO_ROOT / "tests")]

try:
    import pytest
except ImportError as exc:  # pragma: no cover - environment guard
    sys.stderr.write(
        "pytest is not installed in Blender's bundled Python.\n"
        "Install it once with:\n"
        "    <blender>/<ver>/python/bin/python -m pip install pytest syrupy\n"
        f"Underlying error: {exc}\n"
    )
    sys.exit(2)

exit_code = pytest.main(pytest_args)

# Blender ignores Python's exit code unless we force it.
os._exit(int(exit_code))
