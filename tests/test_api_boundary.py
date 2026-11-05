# Copyright (C) 2026 Synopsys, Inc. and ANSYS, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Protect the standalone API from implicit application dependencies."""

import ast
from pathlib import Path
import runpy
import subprocess
import sys


def test_reviewed_distribution_surface_is_enforced():
    """Fail closed for unreviewed files and computed application imports."""
    root = Path(__file__).resolve().parents[1]
    audit = runpy.run_path(str(root / "scripts/check_api_distribution.py"))
    result = subprocess.run(
        [sys.executable, str(root / "scripts/check_api_distribution.py")],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    findings = audit["inspect_entries"]([("ansys/fluent/mcp/hidden_policy.py", b"")])
    assert any("Unreviewed package payload" in finding for finding in findings)
    findings = audit["inspect_python"]("module.py", b"importlib.import_module(name)")
    assert any("computed dependency import" in finding for finding in findings)


def test_production_imports_use_only_native_api_and_declared_dependencies():
    """Reject application imports, including literal dynamic import targets."""
    allowed = {"ansys", "pydantic", "h5py", "httpx"} | sys.stdlib_module_names
    for path in (Path(__file__).resolve().parents[1] / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf8"))
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                targets = [node.module]
            elif isinstance(node, ast.Call) and node.args:
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
                if name in {"import_module", "__import__"}:
                    assert isinstance(node.args[0], ast.Constant), path
                    targets = [node.args[0].value]
            for target in targets:
                assert target.split(".")[0] in allowed, (path, target)


def test_standalone_server_imports_without_application_packages():
    """Start a fresh process that permits only native and declared dependencies."""
    code = '''
import importlib.abc
import sys
class NativeOnly(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith("fluids" + "_mcp"):
            raise AssertionError("Unexpected application import: " + fullname)
sys.meta_path.insert(0, NativeOnly())
from ansys.fluent.mcp.solve import SolveMCP
server = SolveMCP()
assert set(server._backends) == {"pyfluent"}
'''
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
