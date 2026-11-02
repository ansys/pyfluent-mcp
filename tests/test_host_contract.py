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

"""Host contract middleware: guidance, long operations, trace ids, elicitation."""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Optional

from fastmcp import Client, FastMCP
from fastmcp.client.elicitation import ElicitResult
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent
import pytest

from ansys.fluent.mcp.common.host import install_host_middleware
from ansys.fluent.mcp.common.host.annotations import annotations_for
from ansys.fluent.mcp.common.host.elicitation import input_request_of
from ansys.fluent.mcp.common.host.guidance import enrich_tool_result, error_guidance
from ansys.fluent.mcp.common.host.operations import LongOperationMiddleware
from ansys.fluent.mcp.common.host.trace import DURATION_KEY, TRACE_ID_KEY, current_trace_id

# Annotations --------------------------------------------------------------


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


# Error guidance -------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "message", "retryable", "fragment"),
    [
        ("not_connected", "", True, "connect"),
        ("solver_disconnected", "", True, "connect"),
        ("fluids_one_start_timeout", "", True, "still be running"),
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


def _result(payload):
    return ToolResult(
        content=[TextContent(type="text", text=json.dumps(payload))], structured_content=payload
    )


def test_enrich_updates_structured_and_text_content():
    enriched = enrich_tool_result(
        _result({"status": "error", "error_code": "not_connected", "message": "x"})
    )
    assert enriched.structured_content["retryable"] is True
    assert json.loads(enriched.content[0].text) == enriched.structured_content


def test_enrich_keeps_tool_provided_guidance_and_ignores_success():
    payload = {
        "status": "error",
        "error_code": "not_connected",
        "retryable": False,
        "next_action": "custom",
    }
    enriched = enrich_tool_result(_result(payload))
    assert enriched.structured_content["next_action"] == "custom"
    assert enriched.structured_content["retryable"] is False
    ok = _result({"status": "ok", "value": 1})
    assert enrich_tool_result(ok) is ok


def test_enrich_handles_ok_false_and_input_request():
    enriched = enrich_tool_result(
        _result(
            {
                "ok": False,
                "error": "target_required",
                "input_request": {"argument": "target", "message": "?", "options": ["a", "b"]},
            }
        )
    )
    assert enriched.structured_content["retryable"] is True
    assert "a, b" in enriched.structured_content["next_action"]


# Long operations ------------------------------------------------------------


class _Backend:
    def __init__(self, *, delay=0.0, result=None):
        self.calls = 0
        self.delay = delay
        self.result = result

    async def __call__(self, context):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.result is not None:
            return self.result
        return SimpleNamespace(is_error=False, structured_content={"status": "ok", "n": self.calls})


def _ctx(name, arguments=None, progress=None):
    return SimpleNamespace(
        message=SimpleNamespace(name=name, arguments=arguments or {}), fastmcp_context=progress
    )


async def test_read_only_calls_pass_through():
    middleware, backend = LongOperationMiddleware(), _Backend()
    await middleware.on_call_tool(_ctx("get_state"), backend)
    await middleware.on_call_tool(_ctx("get_state"), backend)
    assert backend.calls == 2


async def test_identical_concurrent_calls_join():
    middleware, backend = LongOperationMiddleware(heartbeat_s=0.01), _Backend(delay=0.05)
    first, second = await asyncio.gather(
        middleware.on_call_tool(_ctx("run_code", {"code": "x"}), backend),
        middleware.on_call_tool(_ctx("run_code", {"code": "x"}), backend),
    )
    assert backend.calls == 1 and first is second


async def test_idempotent_result_replayed_once_and_cleared_by_other_mutation():
    middleware, backend = LongOperationMiddleware(), _Backend()
    await middleware.on_call_tool(_ctx("connect"), backend)
    await middleware.on_call_tool(_ctx("connect"), backend)
    assert backend.calls == 1
    await middleware.on_call_tool(_ctx("connect"), backend)
    assert backend.calls == 2
    await middleware.on_call_tool(_ctx("disconnect"), backend)
    await middleware.on_call_tool(_ctx("connect"), backend)
    assert backend.calls == 4


async def test_non_idempotent_and_error_results_are_not_replayed():
    middleware, backend = LongOperationMiddleware(), _Backend()
    await middleware.on_call_tool(_ctx("run_code", {"code": "iterate"}), backend)
    await middleware.on_call_tool(_ctx("run_code", {"code": "iterate"}), backend)
    assert backend.calls == 2
    failing = _Backend(
        result=SimpleNamespace(is_error=False, structured_content={"ok": False, "error": "timeout"})
    )
    await middleware.on_call_tool(_ctx("connect"), failing)
    await middleware.on_call_tool(_ctx("connect"), failing)
    assert failing.calls == 2


async def test_host_cancellation_does_not_interrupt_work():
    middleware, backend = LongOperationMiddleware(heartbeat_s=0.01), _Backend(delay=0.05)
    waiter = asyncio.ensure_future(middleware.on_call_tool(_ctx("connect"), backend))
    await asyncio.sleep(0.01)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    retry = await middleware.on_call_tool(_ctx("connect"), backend)
    assert backend.calls == 1 and retry.structured_content["n"] == 1


async def test_heartbeat_progress_while_running():
    reports = []

    class _Progress:
        async def report_progress(self, progress, total=None, message=None):
            reports.append(message)

    middleware = LongOperationMiddleware(heartbeat_s=0.01)
    await middleware.on_call_tool(_ctx("run_code", progress=_Progress()), _Backend(delay=0.05))
    assert reports and all("run_code still running" in m for m in reports)


async def test_custom_hint_lookup():
    middleware = LongOperationMiddleware(
        hint_lookup=lambda name: {"readOnlyHint": False, "idempotentHint": True}
    )
    backend = _Backend()
    await middleware.on_call_tool(_ctx("custom_tool"), backend)
    await middleware.on_call_tool(_ctx("custom_tool"), backend)
    assert backend.calls == 1


# End to end on a small server -------------------------------------------------


def _server():
    server = FastMCP(name="host-contract-test")
    install_host_middleware(server)
    seen: list = []

    @server.tool(name="echo")
    async def echo(value: str) -> dict:
        seen.append(current_trace_id())
        return {"status": "ok", "value": value}

    @server.tool(name="explode")
    async def explode() -> dict:
        raise RuntimeError("boom")

    @server.tool(name="choose_target")
    async def choose_target(target: Optional[str] = None) -> dict:
        if target is None:
            return {
                "ok": False,
                "error": "target_required",
                "message": "Which target?",
                "input_request": {
                    "argument": "target",
                    "message": "Which target?",
                    "options": ["a", "b"],
                },
            }
        return {"status": "ok", "target": target}

    return server, seen


async def test_trace_ids_in_meta_log_and_context(caplog):
    server, seen = _server()
    caplog.set_level(logging.INFO, logger="ansys.fluent.mcp.trace")
    async with Client(server) as client:
        first = await client.call_tool("echo", {"value": "a"})
        second = await client.call_tool("echo", {"value": "b"})
        boom = await client.call_tool("explode", {}, raise_on_error=False)
    ids = [first.meta[TRACE_ID_KEY], second.meta[TRACE_ID_KEY]]
    assert ids[0] != ids[1] and seen == ids
    assert isinstance(first.meta[DURATION_KEY], int)
    assert boom.is_error and "outcome=raised" in caplog.text


async def test_elicitation_accept_decline_and_unsupported():
    async def accept(message, response_type, params, context):
        return "b"

    async def decline(message, response_type, params, context):
        return ElicitResult(action="decline")

    server, _ = _server()
    async with Client(server, elicitation_handler=accept) as client:
        accepted = await client.call_tool("choose_target", {}, raise_on_error=False)
    async with Client(server, elicitation_handler=decline) as client:
        declined = await client.call_tool("choose_target", {}, raise_on_error=False)
    async with Client(server) as client:
        unsupported = await client.call_tool("choose_target", {}, raise_on_error=False)
    assert accepted.structured_content == {"status": "ok", "target": "b"}
    for result in (declined, unsupported):
        assert result.structured_content["error"] == "target_required"
        assert "Ask the user" in result.structured_content["next_action"]


@pytest.mark.parametrize(
    ("request_payload", "valid"),
    [
        ({"argument": "target", "message": "?", "options": ["a"]}, True),
        ({"argument": "count", "message": "?", "kind": "number"}, True),
        ({"argument": "target", "message": "?", "options": []}, False),
        ({"argument": "x", "message": "?", "kind": "matrix"}, False),
        ({"argument": "password", "message": "?"}, False),
        ({"argument": "api_token", "message": "?"}, False),
    ],
)
def test_input_request_validation(request_payload, valid):
    result = SimpleNamespace(structured_content={"ok": False, "input_request": request_payload})
    assert (input_request_of(result) is not None) is valid
