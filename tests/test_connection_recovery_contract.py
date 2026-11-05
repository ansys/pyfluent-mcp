# Copyright (C) 2026 Synopsys, Inc. and ANSYS, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Every generic tool uses the server's recovery contract, with host features optional."""

import inspect

import pytest

from ansys.fluent.mcp.common.connection_guard import with_connection_recovery
from ansys.fluent.mcp.common.errors import NotConnectedError
from ansys.fluent.mcp.common.models import TypedError
from tests.test_base_mcp import BareLeaf, _structured

CASES = [
    ("get_state", {}),
    ("find_api", {"query": "energy"}),
    ("list_named_objects", {}),
    ("find_named_object", {"name": "inlet"}),
    ("select_named_objects", {"collection": "setup.boundary_conditions.wall"}),
    ("get_targeted_context", {"paths_to_check": ["setup.models.energy.enabled"]}),
    ("get_help", {"path": "setup.models.energy"}),
    ("solver_status", {}),
    ("screenshot", {}),
    ("summarize_setup", {}),
    ("simulation_report", {}),
    ("validate_code", {"code": "1 + 1"}),
    ("run_code", {"code": "1 + 1"}),
    ("manage_component", {"action": "refresh"}),
]


class DisconnectedBackend:
    kind = "pyfluent"
    label = "disconnected"
    endpoint = None

    def is_connected(self):
        return False

    def __getattr__(self, name):
        async def missing(*args, **kwargs):
            raise NotConnectedError("The backend has no session.")
        return missing


@pytest.mark.asyncio
@pytest.mark.parametrize("host_contract", ["0", "1"])
@pytest.mark.parametrize("name,arguments", CASES)
async def test_all_generic_tools_share_canonical_recovery(
    monkeypatch, host_contract, name, arguments
):
    """Typed guards must not strip required server-specific recovery fields."""
    monkeypatch.setenv("FLUIDS_MCP_HOST_CONTRACT", host_contract)
    server = BareLeaf(backends={"pyfluent": DisconnectedBackend()}, expose_tools=("connect", name))
    payload = _structured(await server.call_tool(name, arguments))
    assert payload["error_code"] == "not_connected"
    assert payload["retryable"] is True
    assert payload["next_tool"] == "connect"
    assert payload["next_tool_args"] == {"backend_kind": "pyfluent"}
    assert payload["tool"] == name
    assert "connect" in payload["hint"]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_wrapper_preserves_signatures_and_existing_recovery(asynchronous):
    """Existing owning-product recovery stays intact and no parameters are added."""
    payload = {"error_code": "not_connected", "retryable": True,
               "next_tool": "custom_connect", "hint": "custom owning product"}

    def sync(value: str = "x"):
        return payload

    async def async_function(value: str = "x"):
        return payload

    source = async_function if asynchronous else sync
    wrapped = with_connection_recovery(source, lambda **kwargs: pytest.fail("already complete"))
    assert inspect.signature(wrapped) == inspect.signature(source)
    result = await wrapped() if asynchronous else wrapped()
    assert result is payload


@pytest.mark.asyncio
async def test_offline_success_and_other_typed_errors_are_preserved():
    """A disconnected backend can still serve offline catalog results."""
    def recover(**kwargs):
        pytest.fail("no missing-session error")

    async def offline():
        return [{"path": "file.read_case"}]

    async def invalid():
        return TypedError(error_code="invalid_arguments", message="bad input")

    assert await with_connection_recovery(offline, recover)() == [{"path": "file.read_case"}]
    assert (await with_connection_recovery(invalid, recover)()).error_code == "invalid_arguments"
