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

"""Read-only calls ask Fluent for each group's ``active?`` once, not once per child access.

Uses real PyFluent settings classes over a proxy that counts ``get_attrs``
round trips, so the behaviour is checked against PyFluent's own
``Group.__getattribute__``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

flobject = pytest.importorskip("ansys.fluent.core.solver.flobject")

from ansys.fluent.mcp.solve.backends import introspection  # noqa: E402
from ansys.fluent.mcp.solve.backends.introspection import (  # noqa: E402
    _is_active,
    resolve_path,
    shared_active_checks,
)


class _CountingProxy:
    def __init__(self, inactive: tuple[str, ...] = ()) -> None:
        self.active_queries: list[str] = []
        self.inactive = set(inactive)

    def get_attrs(self, path, attrs, recursive=False):
        self.active_queries.append(path)
        return {"active?": path not in self.inactive}

    def get_var(self, path):
        return 1.0


_STABLE = flobject.ExposureLevel.STABLE


def _leaf(name):
    return type(
        name,
        (flobject.Real,),
        {"fluent_name": name, "_python_name": name, "exposure_level": _STABLE},
    )


def _group(name, children):
    return type(
        name,
        (flobject.Group,),
        {
            "fluent_name": name,
            "_python_name": name,
            "exposure_level": _STABLE,
            "child_names": list(children),
            "_child_classes": dict(children),
        },
    )


def _tree(inactive: tuple[str, ...] = ()):
    inner = _group("inner", {"a": _leaf("a"), "b": _leaf("b"), "c": _leaf("c")})
    mid = _group("mid", {"inner": inner})
    root_cls = _group("root", {"mid": mid})
    root_cls._version = "271"
    root = root_cls()
    proxy = _CountingProxy(inactive)
    root._setattr("_flproxy", proxy)
    return root, proxy


_PATHS = ("mid.inner.a", "mid.inner.b", "mid.inner.c")


def test_plain_resolution_repeats_group_checks():
    root, proxy = _tree()
    for path in _PATHS:
        resolve_path(root, path)
    # root, mid and inner are each asked once per path.
    assert len(proxy.active_queries) == 9


def test_shared_checks_ask_each_group_once_with_same_results():
    plain_root, _ = _tree()
    expected = [resolve_path(plain_root, path).path for path in _PATHS]

    root, proxy = _tree()
    with shared_active_checks():
        resolved = [resolve_path(root, path) for path in _PATHS]
        leaf_active = [_is_active(node) for node in resolved]
    assert [node.path for node in resolved] == expected
    assert leaf_active == [True, True, True]
    inner = "root/mid/inner"
    assert sorted(proxy.active_queries) == [
        "root",
        "root/mid",
        inner,
        f"{inner}/a",
        f"{inner}/b",
        f"{inner}/c",
    ]


def test_child_of_inactive_group_still_raises():
    root, proxy = _tree(inactive=("root/mid/inner",))
    with pytest.raises(flobject.InactiveObjectError):
        resolve_path(root, "mid.inner.a")
    with shared_active_checks():
        for path in _PATHS:
            with pytest.raises(flobject.InactiveObjectError):
                resolve_path(root, path)
        assert _is_active(root.mid.inner) is False
    assert proxy.active_queries.count("root/mid/inner") == 2


def test_answers_do_not_outlive_the_call():
    root, proxy = _tree()
    with shared_active_checks():
        resolve_path(root, "mid.inner.a")
    with shared_active_checks():
        resolve_path(root, "mid.inner.a")
    assert proxy.active_queries.count("root/mid/inner") == 2
    assert getattr(introspection._SHARED_ACTIVE, "answers", None) is None


def test_nested_scope_keeps_outer_answers():
    root, proxy = _tree()
    with shared_active_checks():
        resolve_path(root, "mid.inner.a")
        with shared_active_checks():
            resolve_path(root, "mid.inner.b")
        resolve_path(root, "mid.inner.c")
    assert proxy.active_queries.count("root/mid/inner") == 1


def test_backend_reads_share_checks_and_run_code_does_not():
    from ansys.fluent.mcp.solve.backends.pyfluent import PyFluentBackend

    backend = PyFluentBackend()
    seen = []

    def probe():
        seen.append(getattr(introspection._SHARED_ACTIVE, "answers", None) is not None)

    asyncio.run(backend._read_locked(probe))
    asyncio.run(backend.run_exclusive(probe))
    assert seen == [True, False]


class _RecursiveProxy(_CountingProxy):
    """Answers a recursive ``get_attrs`` like Fluent: inactive children are omitted."""

    def __init__(self, inactive=(), recursive_fails=False):
        super().__init__(inactive)
        self.recursive_calls: list[str] = []
        self.recursive_fails = recursive_fails

    def get_attrs(self, path, attrs, recursive=False):
        if not recursive:
            return super().get_attrs(path, attrs)
        self.recursive_calls.append(path)
        if self.recursive_fails:
            raise RuntimeError("recursive get_attrs unsupported")
        children = {
            name: {"attrs": {"active?": True}}
            for name in ("a", "b", "c")
            if f"{path}/{name}" not in self.inactive
        }
        return {"attrs": {"active?": True}, "children": children}


def _inner(proxy):
    root, _ = _tree()
    root._setattr("_flproxy", proxy)
    _ROOTS.append(root)  # PyFluent holds parents weakly; keep the tree alive.
    return root.mid.inner


_ROOTS: list = []


def test_subtree_attrs_reads_every_child_in_one_round_trip():
    proxy = _RecursiveProxy(inactive=("root/mid/inner/b",))
    inner = _inner(proxy)
    proxy.active_queries.clear()

    expected = {"a": {"active?": True}, "c": {"active?": True}}
    assert introspection.subtree_attrs(inner, ["active?"]) == expected
    assert introspection.active_child_names(inner) == ["a", "c"]
    assert proxy.recursive_calls == ["root/mid/inner", "root/mid/inner"]
    assert proxy.active_queries == []


def test_subtree_answers_are_reused_for_the_rest_of_the_call():
    proxy = _RecursiveProxy(inactive=("root/mid/inner/b",))
    inner = _inner(proxy)
    with shared_active_checks():
        introspection.subtree_attrs(inner, [])
        proxy.active_queries.clear()
        children = [introspection.child_of(inner, name) for name in ("a", "b", "c")]
        assert [_is_active(child) for child in children] == [True, False, True]
    assert proxy.active_queries == []


def test_unsupported_recursive_query_falls_back():
    proxy = _RecursiveProxy(recursive_fails=True)
    inner = _inner(proxy)
    assert introspection.subtree_attrs(inner, ["active?"]) is None
    assert introspection.active_child_names(inner) is None


def test_introspect_prefers_the_session_and_falls_back_locally():
    from types import SimpleNamespace

    from ansys.fluent.mcp.solve.backends.pyfluent import PyFluentBackend

    root, _ = _tree()
    calls = []

    def remote(name, *args, **kwargs):
        calls.append(name)
        if name == "collect_allowed_values":
            raise NotImplementedError
        return {"remote": True}

    backend = PyFluentBackend()
    backend._solver = SimpleNamespace(settings=root, introspect=remote)
    assert backend._introspect("collect_active_status", ["mid.inner.a"]) == {"remote": True}
    assert backend._introspect("collect_allowed_values", ["mid.nope"]) == {"mid.nope": []}
    assert calls == ["collect_active_status", "collect_allowed_values"]


def test_solver_files_use_the_session_hooks_when_present(tmp_path):
    from types import SimpleNamespace

    from ansys.fluent.mcp.solve.backends.pyfluent import PyFluentBackend

    backend = PyFluentBackend()
    backend._solver = SimpleNamespace()
    local = asyncio.run(backend.solver_scratch_path(".txt"))
    Path(local).write_bytes(b"local")
    assert asyncio.run(backend.read_solver_file(local)) == b"local"
    assert asyncio.run(backend.read_solver_file(local)) == b""

    backend._solver = SimpleNamespace(
        solver_scratch_path=lambda suffix: f"/host/tmp/x{suffix}",
        read_solver_file=lambda path: b"remote:" + path.encode(),
    )
    path = asyncio.run(backend.solver_scratch_path(".png"))
    assert path == "/host/tmp/x.png"
    assert asyncio.run(backend.read_solver_file(path)) == b"remote:/host/tmp/x.png"
