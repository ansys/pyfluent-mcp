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

"""Host modules re-exported by allowed packages must not leak into run_code.

``from <allowed package> import <name>`` binds whatever the package holds at
module scope. ``ansys.fluent.core`` submodules import ``subprocess``, ``os``
and ``pathlib.Path``, so checking only the imported module root let those
objects through both the AST validator and the runtime ``__import__`` guard.
"""

from __future__ import annotations

import asyncio
import pathlib
import shutil
import subprocess
import sys
import types

import pytest

from ansys.fluent.mcp.common.validation import (
    is_allowed_sandbox_import,
    validate_python_source,
)
from ansys.fluent.mcp.solve.backends import pyfluent

_BYPASS = "from ansys.fluent.core._get_build_details import subprocess as sp\nsp.Popen([1])\n"


@pytest.mark.parametrize("strict", [True, False])
@pytest.mark.parametrize(
    "code",
    [
        _BYPASS,
        "from ansys.fluent.core.codegen.datamodelgen import Path\nPath('x').unlink()\n",
        "from ansys.fluent.core import *\n",
        "from ansys.fluent.core.utils import subprocess\n",
        "from ansys.fluent.core.utils import shutil as s\ns.rmtree('x')\n",
        "from ansys.fluent.core.launcher import os\n",
        "from ansys.fluent.core import _private_helper\n",
        "from ansys.fluent.core._internal import thing\n",
        "import ansys.fluent.core._get_build_details as d\n",
        "from math import *\n",
        "from json import PurePath\n",
    ],
)
def test_import_bypasses_are_rejected(code: str, strict: bool) -> None:
    result = validate_python_source(code, strict=strict)
    assert result.status == "error", result
    assert result.error_code in {"forbidden_import", "forbidden_call"}
    assert validate_python_source(code.splitlines()[0], strict=strict).error_code == (
        "forbidden_import"
    )


@pytest.mark.parametrize(
    "code",
    [
        "import ansys.fluent.core as pf\npf.codegen.datamodelgen.Path('x')\n",
        "import ansys.fluent.core as pf\npf.utils.shutil.rmtree('x')\n",
        "import ansys.fluent.core as pf\ngetattr(pf, 'pathlib')\n",
    ],
)
def test_module_attribute_escapes_are_rejected(code: str) -> None:
    result = validate_python_source(code, strict=True)
    assert result.status == "error"
    assert result.error_code == "forbidden_call"


@pytest.mark.parametrize(
    "code",
    [
        "import ansys.example.plugin.mod as c\n",
        "from ansys.example.plugin import mod\n",
        "import ansys\n",
    ],
)
def test_strict_mode_limits_ansys_to_public_fluent_packages(code: str) -> None:
    result = validate_python_source(code, strict=True)
    assert (result.status, result.error_code) == ("error", "forbidden_import")


@pytest.mark.parametrize(
    "code",
    [
        "import math\nprint(math.pi)\n",
        "import ansys.fluent.core as pyfluent\nprint(pyfluent.__version__)\n",
        "from ansys.fluent.core import solver as s\n",
        "from ansys.fluent import core\n",
        "from ansys.units import Quantity\n",
        "from collections import OrderedDict\nfrom typing import Any\n",
        "from dataclasses import dataclass as dc\n",
        "import json\nprint(json.dumps({'a': 1}))\n",
    ],
)
def test_legitimate_imports_still_validate(code: str) -> None:
    assert validate_python_source(code, strict=True).status == "ok"
    assert validate_python_source(code).status == "ok"


def test_relaxed_mode_keeps_aliased_public_ansys_imports() -> None:
    code = "from ansys.example.plugin.mod import app as _pa\n_pa\n"
    assert validate_python_source(code).status == "ok"


def test_is_allowed_sandbox_import() -> None:
    assert is_allowed_sandbox_import("ansys.fluent.core.solver")
    assert is_allowed_sandbox_import("math")
    assert not is_allowed_sandbox_import("ansys")
    assert not is_allowed_sandbox_import("ansys.fluent.core._get_build_details")
    assert not is_allowed_sandbox_import("ansys.fluentx")
    assert not is_allowed_sandbox_import("subprocess")


@pytest.fixture
def reexporting_module(monkeypatch: pytest.MonkeyPatch) -> str:
    """Register a public ``ansys.fluent.core`` submodule that re-exports host objects."""
    name = "ansys.fluent.core.zz_reexport_probe"
    module = types.ModuleType(name)
    module.subprocess = subprocess
    module.sp_alias = subprocess
    module.shutil_rm = shutil.rmtree
    module.PathAlias = pathlib.Path
    module.popen_alias = subprocess.Popen
    module.value = 3
    module.math_mod = sys.modules["math"]
    monkeypatch.setitem(sys.modules, name, module)
    return name


@pytest.mark.parametrize(
    "item", ["subprocess", "sp_alias", "shutil_rm", "PathAlias", "popen_alias", "_x", "*"]
)
def test_runtime_import_rejects_reexported_host_objects(reexporting_module: str, item: str) -> None:
    safe_import = pyfluent._make_safe_import()
    with pytest.raises(ImportError):
        safe_import(reexporting_module, fromlist=(item,))


def test_runtime_import_allows_plain_values_and_allowed_modules(reexporting_module: str) -> None:
    safe_import = pyfluent._make_safe_import()
    module = safe_import(reexporting_module, fromlist=("value", "math_mod"))
    assert module.value == 3


def test_runtime_import_rejects_private_and_non_fluent_ansys_modules() -> None:
    safe_import = pyfluent._make_safe_import()
    with pytest.raises(ImportError):
        safe_import("ansys.fluent.core._get_build_details", fromlist=("x",))
    with pytest.raises(ImportError):
        safe_import("ansys.example.plugin.mod")


def test_runtime_exact_bypass_with_real_pyfluent() -> None:
    pytest.importorskip("ansys.fluent.core._get_build_details")
    safe_import = pyfluent._make_safe_import()
    with pytest.raises(ImportError):
        safe_import("ansys.fluent.core._get_build_details", fromlist=("subprocess",))
    datamodelgen = pytest.importorskip("ansys.fluent.core.codegen.datamodelgen")
    if hasattr(datamodelgen, "Path"):
        with pytest.raises(ImportError):
            safe_import("ansys.fluent.core.codegen.datamodelgen", fromlist=("Path",))


# Frame / generator introspection and module attributes reached through
# allowed packages. Each payload passed strict validation and ran in run_code.
_GEN_FRAME_WALK = (
    "def gen():\n"
    "    yield 1\n"
    "    yield me.gi_frame.f_back.f_back.f_globals\n"
    "me = gen()\n"
    "for glb in me:\n"
    "    pass\n"
    "__return__ = glb.get('o' + 's')\n"
)
_INTROSPECTION_ESCAPES = [
    "import ansys.fluent.core as pf\n"
    "pf.module_config.inspect.currentframe().f_back.f_globals.get('o' + 's').getcwd()\n",
    _GEN_FRAME_WALK,
    "from ansys.fluent.core import module_config as mc\nmc.inspect\n",
    "import ansys.fluent.core as pf\npf.execution.launcher.fluent_container.tempfile.mkdtemp()\n",
    "import ansys.fluent.core\nansys.fluent.mcp.solve.backends.pyfluent.tempfile\n",
    "import ansys.fluent.core\n__return__ = ansys.fluent\n",
]


def _backend() -> pyfluent.PyFluentBackend:
    backend = pyfluent.PyFluentBackend()
    backend._solver = types.SimpleNamespace(settings=types.SimpleNamespace())
    return backend


@pytest.mark.parametrize("code", _INTROSPECTION_ESCAPES)
def test_introspection_and_root_escapes_are_rejected(code: str) -> None:
    result = validate_python_source(code, strict=True)
    assert (result.status, result.error_code) in {
        ("error", "forbidden_call"),
        ("error", "forbidden_import"),
    }, result
    executed = asyncio.run(_backend().run_code(code))
    assert executed.status == "error"
    assert executed.return_value is None


@pytest.mark.parametrize("code", ["import ansys.fluent.core\n", "import ansys.units\n"])
def test_unaliased_ansys_import_names_the_accepted_form(code: str) -> None:
    result = validate_python_source(code, strict=True)
    assert (result.status, result.error_code) == ("error", "forbidden_import")
    assert " as <alias>'" in result.message
    assert "from " in result.message


@pytest.mark.parametrize(
    "code",
    [
        "def gen():\n    yield 1\ng = gen()\n__return__ = getattr(g, 'gi_' + 'frame')\n",
        "import ansys.fluent.core as pf\n__return__ = getattr(pf.module_config, 'insp' + 'ect')\n",
        "import ansys.fluent.core as pf\n"
        "__return__ = getattr(pf.execution.launcher.fluent_container, 'temp' + 'file')\n",
    ],
)
def test_runtime_guard_blocks_dynamic_introspection_names(code: str) -> None:
    assert validate_python_source(code, strict=True).status == "ok"
    result = asyncio.run(_backend().run_code(code))
    assert result.status == "error"
    assert "not permitted" in (result.stderr or result.message or "")


@pytest.mark.parametrize(
    "name",
    ["gi_frame", "cr_frame", "ag_frame", "f_back", "f_globals", "tb_frame", "co_code"]
    + ["inspect", "tempfile", "pickle", "os", "sys", "subprocess", "codecs", "types"],
)
def test_validator_and_runtime_guard_agree_on_attribute_names(name: str) -> None:
    assert pyfluent._is_forbidden_attribute_name(name)
    assert validate_python_source(f"x = 1\nx.{name}\n", strict=True).status == "error"
    assert validate_python_source(f"x = 1\ngetattr(x, {name!r})\n", strict=True).status == "error"
    safe_getattr = pyfluent._build_safe_builtins()["getattr"]
    with pytest.raises(AttributeError):
        safe_getattr(types.SimpleNamespace(**{name: 1}), name)


@pytest.mark.parametrize(
    "code",
    [
        "solver.settings.setup.models.energy.enabled = True\n",
        "solver.settings.results.report.surface_integrals.area_weighted_avg(\n"
        "    report_of='pressure', surface_names=['outlet']\n)\n",
        "inlet = solver.settings.setup.boundary_conditions.velocity_inlet['inlet']\n"
        "inlet.momentum.velocity.value = 1.5\n",
        "state = solver.settings.setup.models.viscous.get_state()\nprint(state)\n",
        "walls = solver.settings.setup.boundary_conditions.wall\n"
        "names = [n for n in walls.get_object_names() if n.startswith('w')]\n"
        "temps = {n: walls[n].thermal.get_state() for n in names}\n",
        "import ansys.fluent.core as pf\nprint(pf.__version__)\n",
        "from ansys.fluent.core import solver as s\n",
        "import collections.abc\nprint(collections.abc.Mapping)\n",
    ],
)
def test_settings_api_code_still_validates(code: str) -> None:
    assert validate_python_source(code, strict=True).status == "ok"
