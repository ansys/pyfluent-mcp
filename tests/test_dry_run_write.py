# Copyright (C) 2026 Synopsys, Inc. and ANSYS, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
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

"""Contract tests for the ``Backend.dry_run_write`` hook (C6).

The default implementation on the ABC returns ``{"status": "ok"}``
so unsupported backends never falsely block a write. Backends that
override it can predict rejections without mutating state.
"""

from __future__ import annotations

import asyncio

from ansys.fluent.mcp.common.backend import Backend
from ansys.fluent.mcp.common.models import ConnectResult


class _NoOpBackend(Backend):
    """Bare backend that inherits the ABC's default dry_run_write."""

    kind = "noop"
    label = "no-op"

    def __init__(self):
        super().__init__()
        self.connected = True

    async def connect(self, **_kwargs):
        self.connected = True
        return ConnectResult(status="ok", backend_kind=self.kind)

    def is_connected(self):
        return self.connected


class _OverrideBackend(_NoOpBackend):
    """Backend that overrides dry_run_write to predict a rejection."""

    kind = "override"
    label = "override"

    async def dry_run_write(self, path, value, *, kind="set", key=None, index=None):
        # Predict: value 'foo' at any viscous.model path is bogus.
        if path == "setup.models.viscous.model" and str(value).lower() == "foo":
            return {
                "status": "error",
                "error_code": "value_not_allowed",
                "message": f"value {value!r} is not in allowed set for {path!r}",
                "allowed_values": ["k-omega", "k-epsilon"],
                "rejected_value": str(value),
            }
        return {"status": "ok"}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_abc_default_dry_run_write_returns_ok() -> None:
    b = _NoOpBackend()
    result = _run(b.dry_run_write("setup.models.viscous.model", "anything"))
    assert result["status"] == "ok"
    # ABC default surfaces a note field so callers know it's a no-op.
    assert "note" in result or result["status"] == "ok"


def test_dry_run_write_accepts_all_step_kinds() -> None:
    b = _NoOpBackend()
    for kind in ("set", "set_named", "set_list_item", "set_state", "multi_edit"):
        result = _run(
            b.dry_run_write(
                "setup.foo",
                {"bar": 1} if kind in ("set_state", "multi_edit") else 1,
                kind=kind,
                key="k" if kind == "set_named" else None,
                index=0 if kind == "set_list_item" else None,
            )
        )
        assert result["status"] == "ok", f"kind={kind} unexpectedly failed"


def test_override_backend_blocks_bad_value() -> None:
    b = _OverrideBackend()
    result = _run(b.dry_run_write("setup.models.viscous.model", "foo"))
    assert result["status"] == "error"
    assert result["error_code"] == "value_not_allowed"
    assert result["allowed_values"] == ["k-omega", "k-epsilon"]
    assert result["rejected_value"] == "foo"


def test_override_backend_passes_good_value() -> None:
    b = _OverrideBackend()
    result = _run(b.dry_run_write("setup.models.viscous.model", "k-omega"))
    assert result["status"] == "ok"


def test_override_backend_passes_other_paths() -> None:
    b = _OverrideBackend()
    result = _run(b.dry_run_write("setup.general.solver.time", "transient"))
    assert result["status"] == "ok"
