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

import asyncio

from ansys.fluent.mcp.solve import mcp as solve_mcp
from ansys.fluent.mcp.solve.lib.discovery_tools import list_fields_impl
from ansys.fluent.mcp.solve.lib.pattern import expand_pattern, is_pattern


class FieldsBackend:
    def __init__(self, *, connected=True, result=None, error=None):
        """Initialize the FieldsBackend instance.

        Parameters
        ----------
        connected : Any
            Whether the fake or test backend should report an active connection.
        result : Any
            Result object or payload to process.
        error : Any
            Error instance or message to convert.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        self.connected = connected
        self.result = result
        self.error = error
        self.scopes = []

    def is_connected(self):
        """Return whether connected.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        return self.connected

    async def list_fields(self, *, scope):
        """List fields entries.

        Parameters
        ----------
        scope : Any
            Scope to supply to the function.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        self.scopes.append(scope)
        if self.error:
            raise self.error
        return self.result


def test_pattern_detection_and_expansion():
    """Verify that pattern detection and expansion.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    candidates = ["wall-1", "wall-2", "inlet-a", "outlet", "wall-1"]

    assert is_pattern(None) is False
    assert is_pattern("wall-1") is False
    assert is_pattern("wall-*") is True
    assert expand_pattern("wall-1", candidates) == ["wall-1"]
    assert expand_pattern("missing", candidates) == []
    assert expand_pattern("wall-*|inlet-?", candidates) == ["wall-1", "wall-2", "inlet-a"]
    assert expand_pattern("|out*", candidates) == ["outlet"]


def test_list_fields_impl_handles_disconnected_empty_success_and_errors():
    """Verify that list fields impl handles disconnected empty success and errors.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    disconnected = asyncio.run(list_fields_impl(FieldsBackend(connected=False)))
    success_backend = FieldsBackend(result={"fields": ["pressure"], "scope": "cell"})
    success = asyncio.run(list_fields_impl(success_backend, scope=" cell "))
    empty_backend = FieldsBackend(result={})
    empty = asyncio.run(list_fields_impl(empty_backend, scope=" "))
    failed = asyncio.run(list_fields_impl(FieldsBackend(error=RuntimeError("boom"))))

    assert disconnected == {
        "connected": False,
        "fields": [],
        "note": "live session required to list solver fields",
    }
    assert success == {"connected": True, "fields": ["pressure"], "scope": "cell"}
    assert success_backend.scopes == ["cell"]
    assert empty["connected"] is True
    assert empty["scope"] == "any"
    assert "no field info available" in empty["note"]
    assert failed == {"error": "failed to list fields: boom"}


def test_backend_injection_does_not_load_entry_points(monkeypatch):
    """Embedding is explicit and never imports application plugins."""

    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected plugin discovery")

    monkeypatch.setattr("importlib.metadata.entry_points", forbidden)
    backend = solve_mcp.SolveCompositeBackend()
    server = solve_mcp.SolveMCP(backends={"custom": backend}, default_backend_kind="custom")
    assert server._backends == {"custom": backend}
