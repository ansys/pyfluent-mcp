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

"""Solve leaf MCP surface conformance for generic hosts (offline)."""

from __future__ import annotations

import json
import re
import sys
import textwrap

from fastmcp import Client
import pytest

from ansys.fluent.mcp.solve import SolveMCP
from ansys.fluent.mcp.solve.mcp.host_resources import PROMPTS, render_prompt

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TYPED_KEYS = ("type", "anyOf", "oneOf", "allOf", "enum", "const")


@pytest.fixture(scope="module")
def tools():
    import asyncio

    async def _list():
        async with Client(SolveMCP()) as client:
            return await client.list_tools()

    return asyncio.run(_list())


def test_every_tool_has_behavior_hints(tools):
    assert [t.name for t in tools if t.annotations is None] == []


def test_names_descriptions_and_schemas_suit_strict_clients(tools):
    problems = []
    for tool in tools:
        if not NAME_RE.match(tool.name):
            problems.append(f"{tool.name}: bad name")
        if not (tool.description or "").strip() or len(tool.description) > 2048:
            problems.append(f"{tool.name}: description missing or longer than 2048")
        # MCP SDK v2 renamed ``inputSchema`` to ``input_schema``; support both.
        schema = getattr(tool, "input_schema", None) or tool.inputSchema
        if schema.get("type") != "object" or "$ref" in json.dumps(schema):
            problems.append(f"{tool.name}: schema is not a flat object")
        for key, prop in (schema.get("properties") or {}).items():
            if not any(k in prop for k in _TYPED_KEYS):
                problems.append(f"{tool.name}.{key}: untyped parameter")
    assert problems == []


def test_server_can_turn_off_host_contract(monkeypatch):
    class _OwnContract(SolveMCP):
        host_contract = False

    assert _OwnContract()._host_contract is None
    monkeypatch.setenv("FLUIDS_MCP_HOST_CONTRACT", "0")
    assert SolveMCP()._host_contract is None


async def test_offline_error_contract_and_trace_id():
    async with Client(SolveMCP()) as client:
        result = await client.call_tool("get_state", {}, raise_on_error=False)
    payload = result.structured_content
    assert payload["error_code"] == "not_connected"
    assert payload["retryable"] is True
    assert "connect" in payload["next_action"]
    assert len(payload["trace_id"]) == 12


async def test_resources_and_prompts_offline():
    async with Client(SolveMCP()) as client:
        uris = {str(r.uri) for r in await client.list_resources()}
        prompts = {p.name for p in await client.list_prompts()}
        status = json.loads((await client.read_resource("fluent://session/status"))[0].text)
        solver = json.loads((await client.read_resource("fluent://solver/status"))[0].text)
        prompt = await client.get_prompt("fluent_setup_change", {"goal": "enable energy"})
    assert {
        "fluent://session/status",
        "fluent://solver/status",
        "fluent://case/named-objects",
    } <= uris
    assert set(PROMPTS) <= prompts
    assert status["connected"] is False
    assert solver["error_code"] == "not_connected" and solver["retryable"] is True
    assert "enable energy" in prompt.messages[0].content.text


def test_prompts_only_name_published_tools(tools):
    published = {t.name for t in tools}
    referenced = set()
    for name in PROMPTS:
        referenced |= set(re.findall(r"`([a-z_]+)`", render_prompt(name, goal="g", symptom="s")))
    assert referenced and sorted(referenced - published) == []


async def test_stdio_round_trip(tmp_path):
    from fastmcp.client.transports import PythonStdioTransport

    script = tmp_path / "serve_solve.py"
    script.write_text(
        textwrap.dedent(
            """
            import logging
            logging.disable(logging.CRITICAL)
            from ansys.fluent.mcp.solve import SolveMCP
            SolveMCP().run(transport="stdio")
            """
        ),
        encoding="utf-8",
    )
    transport = PythonStdioTransport(script_path=str(script), python_cmd=sys.executable)
    async with Client(transport, timeout=120) as client:
        names = {t.name for t in await client.list_tools()}
        result = await client.call_tool("get_state", {}, raise_on_error=False)
    assert {"session_status", "run_code", "describe_path"} <= names
    assert result.structured_content["retryable"] is True
