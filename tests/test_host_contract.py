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

"""Host contract: annotations, error guidance, timeout-safe calls, trace ids."""

from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path

from pydantic import BaseModel
import pytest

from ansys.fluent.mcp.common.host import HostContract
from ansys.fluent.mcp.common.host.annotations import annotations_for
from ansys.fluent.mcp.common.host.guidance import enrich_result, error_guidance
from ansys.fluent.mcp.common.host.operations import CallCoordinator, call_key
from ansys.fluent.mcp.common.host.trace import current_trace_id

SRC = Path(__file__).resolve().parents[1] / "src"

# Framework independence -----------------------------------------------------


def test_src_never_imports_the_mcp_framework_directly():
    offenders = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            for name in names:
                if name.split(".")[0] in {"fastmcp", "mcp"}:
                    offenders.append(f"{path.relative_to(SRC)}: {name}")
    assert offenders == []


# Annotations ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "read_only", "destructive", "idempotent"),
    [
        ("get_state", True, False, True),
        ("connect", False, False, True),
        ("disconnect", False, True, True),
        ("manage_fluent", False, True, True),
        ("run_code", False, True, False),
    ],
)
def test_annotation_profiles(name, read_only, destructive, idempotent):
    hints = annotations_for(name)
    assert (hints["readOnlyHint"], hints["destructiveHint"], hints["idempotentHint"]) == (
        read_only,
        destructive,
        idempotent,
    )


def test_unknown_tool_has_no_annotations():
    assert annotations_for("not_a_tool") is None
    assert annotations_for(None) is None


# Error guidance ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "message", "retryable", "fragment"),
    [
        ("not_connected", "", True, "connect"),
        ("solver_disconnected", "", True, "connect"),
        ("application_start_timeout", "", True, "still be running"),
        ("fluent_activate_failed", "HTTP 401: Invalid password", False, "credentials"),
        ("invalid_arguments", "", False, "Correct the arguments"),
        ("risk_blocked", "", False, "Rewrite"),
        ("forbidden_import", "", False, "Rewrite"),
        ("pyfluent_not_installed", "", False, "not installed"),
    ],
)
def test_error_guidance(code, message, retryable, fragment):
    got_retryable, action = error_guidance(code, message)
    assert got_retryable is retryable
    assert fragment in action


def test_unknown_error_code_has_no_action():
    assert error_guidance("something_odd") == (False, None)


def test_enrich_result_on_mappings():
    error = {"status": "error", "error_code": "not_connected", "message": "x"}
    enriched = enrich_result(error, trace_id="abc")
    assert enriched["retryable"] is True and "connect" in enriched["next_action"]
    assert enriched["trace_id"] == "abc"
    assert "retryable" not in error, "the tool's own mapping is not mutated"
    ok = {"status": "ok", "value": 1}
    assert enrich_result(ok) is ok


def test_enrich_result_keeps_tool_guidance_and_handles_ok_false():
    custom = {"status": "error", "error_code": "not_connected", "next_action": "custom"}
    assert enrich_result(custom)["next_action"] == "custom"
    asked = enrich_result(
        {
            "ok": False,
            "error": "target_required",
            "input_request": {"argument": "target", "message": "?", "options": ["a", "b"]},
        }
    )
    assert asked["retryable"] is True and "a, b" in asked["next_action"]


def test_enrich_result_on_models():
    class TypedError(BaseModel):
        status: str = "error"
        error_code: str
        message: str

    class Ok(BaseModel):
        status: str = "ok"

    enriched = enrich_result(TypedError(error_code="timeout", message="slow"))
    assert enriched["error_code"] == "timeout" and enriched["retryable"] is True
    ok = Ok()
    assert enrich_result(ok) is ok


# Call coordination --------------------------------------------------------------


class _Calls:
    def __init__(self, *, delay=0.0, result=None):
        self.count = 0
        self.delay = delay
        self.result = result

    async def __call__(self):
        self.count += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.result if self.result is not None else {"status": "ok", "n": self.count}


def test_call_key_ignores_unserializable_arguments():
    assert call_key("connect", {"ip": "a", "ctx": object()}) == call_key("connect", {"ip": "a"})
    assert call_key("connect", {"ip": "a"}) != call_key("connect", {"ip": "b"})


async def test_identical_concurrent_calls_join():
    coordinator, calls = CallCoordinator(), _Calls(delay=0.05)
    key = call_key("run_code", {"code": "x"})
    first, second = await asyncio.gather(
        coordinator.run(key, calls, idempotent=False),
        coordinator.run(key, calls, idempotent=False),
    )
    assert calls.count == 1 and first is second


async def test_replay_once_and_cleared_by_other_mutation():
    coordinator, calls = CallCoordinator(), _Calls()
    connect, disconnect = call_key("connect", {}), call_key("disconnect", {})
    await coordinator.run(connect, calls, idempotent=True)
    await coordinator.run(connect, calls, idempotent=True)
    assert calls.count == 1
    await coordinator.run(connect, calls, idempotent=True)
    assert calls.count == 2
    await coordinator.run(disconnect, calls, idempotent=True)
    await coordinator.run(connect, calls, idempotent=True)
    assert calls.count == 4


async def test_errors_and_non_idempotent_calls_are_not_replayed():
    coordinator = CallCoordinator()
    calls = _Calls()
    key = call_key("run_code", {"code": "iterate"})
    await coordinator.run(key, calls, idempotent=False)
    await coordinator.run(key, calls, idempotent=False)
    assert calls.count == 2
    failing = _Calls(result={"ok": False, "error": "timeout"})
    await coordinator.run(call_key("connect", {}), failing, idempotent=True)
    await coordinator.run(call_key("connect", {}), failing, idempotent=True)
    assert failing.count == 2


async def test_replay_expires():
    now = [0.0]
    coordinator = CallCoordinator(replay_ttl_s=60, clock=lambda: now[0])
    calls = _Calls()
    await coordinator.run(call_key("connect", {}), calls, idempotent=True)
    now[0] = 61
    await coordinator.run(call_key("connect", {}), calls, idempotent=True)
    assert calls.count == 2


async def test_host_cancellation_does_not_interrupt_work():
    coordinator, calls = CallCoordinator(), _Calls(delay=0.05)
    key = call_key("connect", {})
    waiter = asyncio.ensure_future(coordinator.run(key, calls, idempotent=True))
    await asyncio.sleep(0.01)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    retry = await coordinator.run(key, calls, idempotent=True)
    assert calls.count == 1 and retry["n"] == 1


# Wrapper --------------------------------------------------------------------------


async def test_wrapper_traces_guides_and_coordinates(caplog):
    caplog.set_level(logging.INFO, logger="ansys.fluent.mcp.trace")
    seen = []

    async def connect(ip: str = "") -> dict:
        """Connect."""
        seen.append(current_trace_id())
        return {"status": "error", "error_code": "not_connected", "message": "x"}

    wrapped = HostContract().wrap("connect", connect)
    assert wrapped.__wrapped__ is connect and wrapped.__doc__ == "Connect."
    result = await wrapped(ip="h")
    assert result["retryable"] is True and result["trace_id"] == seen[0]
    assert f"trace_id={seen[0]}" in caplog.text and "outcome=error:not_connected" in caplog.text
    assert current_trace_id() is None


async def test_wrapper_logs_raised_and_supports_sync_tools(caplog):
    caplog.set_level(logging.INFO, logger="ansys.fluent.mcp.trace")

    async def explode() -> dict:
        raise RuntimeError("boom")

    def ping() -> dict:
        return {"status": "ok"}

    contract = HostContract()
    with pytest.raises(RuntimeError):
        await contract.wrap("run_code", explode)()
    assert "outcome=raised" in caplog.text
    assert contract.wrap("ping", ping)() == {"status": "ok"}


def test_wrapper_keeps_hand_set_annotations():
    import inspect

    async def guarded(**kwargs) -> dict:
        return {"status": "ok", **kwargs}

    guarded.__signature__ = inspect.Signature(
        [inspect.Parameter("include_check", inspect.Parameter.KEYWORD_ONLY, annotation=bool)]
    )
    guarded.__annotations__ = {"include_check": bool, "return": dict}
    wrapped = HostContract().wrap("mesh_quality", guarded)
    assert wrapped.__annotations__ == {"include_check": bool, "return": dict}
    assert list(inspect.signature(wrapped).parameters) == ["include_check"]
