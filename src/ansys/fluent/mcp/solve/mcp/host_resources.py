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

"""Native MCP resources for the solve leaf.

Resources are read-only live-context snapshots a user or host can attach to a
chat without a tool call. Applications own workflow prompts and advice.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from ansys.fluent.mcp.common.errors import FluidsMCPError
from ansys.fluent.mcp.common.host.guidance import error_guidance

_JSON = "application/json"


def _dump(payload: Any) -> str:
    return json.dumps(payload, indent=2, default=str)


def _error(code: str, message: str) -> str:
    retryable, action = error_guidance(code, message)
    return _dump(
        {
            "status": "error",
            "error_code": code,
            "message": message,
            "retryable": retryable,
            "next_action": action,
        }
    )


async def _read_connected(server: Any, read: Callable[[Any], Awaitable[Any]]) -> str:
    try:
        backend = server.backend
    except FluidsMCPError:
        return _error("not_connected", "No active solve backend.")
    if not backend.is_connected():
        return _error("not_connected", "Fluent is not connected.")
    try:
        return _dump(await read(backend))
    except (
        ConnectionError,
        OSError,
        RuntimeError,
        TimeoutError,
        ValueError,
        FluidsMCPError,
    ) as exc:
        return _error("upstream_error", f"{type(exc).__name__}: {exc}")


def register_solve_resources(server: Any) -> None:
    """Register the solve leaf's live-context resources.

    Parameters
    ----------
    server : Any
        Solve leaf server.
    """

    @server.resource(
        "fluent://session/status",
        name="fluent_session_status",
        description="Whether a Fluent session is connected and through which backend.",
        mime_type=_JSON,
    )
    async def session_status_resource() -> str:
        try:
            backend = server.backend
        except FluidsMCPError:
            return _dump({"connected": False, "backend_kind": None})
        return _dump(
            {
                "connected": bool(backend.is_connected()),
                "backend_kind": getattr(backend, "kind", None),
            }
        )

    @server.resource(
        "fluent://solver/status",
        name="fluent_solver_status",
        description="Solver readiness: initialized, iterations, residuals, steady/transient.",
        mime_type=_JSON,
    )
    async def solver_status_resource() -> str:
        return await _read_connected(server, lambda backend: backend.solver_status())

    @server.resource(
        "fluent://case/named-objects",
        name="fluent_named_objects",
        description="Named objects in the loaded case (zones, materials, report definitions, ...).",
        mime_type=_JSON,
    )
    async def named_objects_resource() -> str:
        return await _read_connected(server, lambda backend: backend.list_named_objects())


__all__ = ["register_solve_resources"]
