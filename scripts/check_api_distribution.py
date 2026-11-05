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

"""Audit the reviewed native API file surface in a checkout or release archive."""

import argparse
import ast
import json
from pathlib import Path
import sys
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "ansys/fluent/mcp/"
ALLOWED_IMPORTS = sys.stdlib_module_names | {"ansys", "h5py", "pydantic", "httpx"}


def inspect_python(name, data):
    """Reject production dependencies outside the native API dependency set."""
    problems = []
    tree = ast.parse(data, filename=name)
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            targets = [node.module]
        elif isinstance(node, ast.Call) and node.args:
            fn = node.func
            target = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
            if target in {"import_module", "__import__"}:
                first = node.args[0]
                if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
                    problems.append(f"{name}: computed dependency import")
                else:
                    targets = [first.value]
        for target in targets:
            if target.split(".")[0] not in ALLOWED_IMPORTS:
                problems.append(f"{name}: undeclared application import {target}")
    return problems


def inspect_entries(entries):
    """Require exactly the package files explicitly reviewed in the manifest."""
    reviewed = set(json.loads((ROOT / "scripts/public-api-files.json").read_text()))
    found = set()
    problems = []
    for name, data in entries:
        if PACKAGE not in name:
            continue
        relative = name.split(PACKAGE, 1)[1]
        found.add(relative)
        if relative not in reviewed:
            problems.append(f"Unreviewed package payload: {name}")
        if name.endswith(".py"):
            problems.extend(inspect_python(name, data))
    problems.extend(f"Missing reviewed API file: {name}" for name in sorted(reviewed - found))
    return problems


def inspect_archive(path):
    """Read distributions without extracting or executing their contents."""
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return inspect_entries(
                (name, archive.read(name)) for name in archive.namelist() if not name.endswith("/")
            )
    with tarfile.open(path) as archive:
        return inspect_entries(
            (member.name, archive.extractfile(member).read())
            for member in archive.getmembers() if member.isfile()
        )


def main():
    """Audit source plus every wheel/source archive in the specified directories."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", type=Path, nargs="*")
    args = parser.parse_args()
    problems = inspect_entries(
        (path.relative_to(ROOT / "src").as_posix(), path.read_bytes())
        for path in (ROOT / "src/ansys/fluent/mcp").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )
    count = 0
    for location in args.artifacts:
        if location.is_file():
            archives = [location]
        else:
            archives = sorted(location.rglob("*.whl")) + sorted(location.rglob("*.tar.gz"))
        if not archives:
            problems.append(f"No release archives found: {location}")
        for archive in archives:
            problems.extend(inspect_archive(archive))
            count += 1
    if problems:
        print("\n".join(problems))
        return 1
    print(f"Reviewed API surface passed: source and {count} release archives")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
