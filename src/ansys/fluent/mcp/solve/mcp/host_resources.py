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

"""MCP resources and prompts for the solve leaf.

Resources are read-only live-context snapshots a user or host can attach to a
chat without a tool call. Prompts are workflow playbooks that hosts surface as
slash commands. Prompts only name tools the solve leaf publishes.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from ansys.fluent.mcp.common.errors import FluidsMCPError
from ansys.fluent.mcp.common.host.guidance import error_guidance

_JSON = "application/json"

_RULES = (
    "Rules: never use .tui or Scheme; check every write with `validate_code` and run it with "
    "`run_code` only after the user approves; there is no undo on this server, so show the code "
    "first; follow the 'next_action' field on errors; after a timeout call `session_status` "
    "before retrying."
)

PROMPTS: dict[str, dict[str, str]] = {
    "fluent_setup_change": {
        "description": "Make a validated settings change on the connected Fluent case.",
        "template": (
            "Goal: {goal}\n\n"
            "1. Call `session_status`; if not connected, call `connect`.\n"
            "2. Call `summarize_setup` to read the current models, materials and boundary "
            "conditions.\n"
            "3. Find the settings paths with `find_api` and check them with `describe_path` "
            "(exists, active, allowed values).\n"
            "4. Write PyFluent settings-API code for the change and check it with "
            "`validate_code`.\n"
            "5. Show the code, run it with `run_code` after approval, then verify with "
            "`get_state`.\n\n" + _RULES
        ),
    },
    "fluent_diagnose_divergence": {
        "description": "Diagnose a diverging or stalled Fluent run and propose fixes.",
        "template": (
            "Symptom: {symptom}\n\n"
            "1. Call `solver_status` for iterations and residuals.\n"
            "2. Call `mesh_quality` and `summarize_setup` to check mesh and numerics.\n"
            "3. Explain the most likely cause in two sentences.\n"
            "4. Propose at most three fixes (for example lower under-relaxation, better "
            "initialization, first-order start), check each with `validate_code`, and apply only "
            "what the user approves with `run_code`.\n\n" + _RULES
        ),
    },
}
"""Prompt name -> description and template."""


def render_prompt(name: str, **arguments: str) -> str:
    """Render one of :data:`PROMPTS`.

    Parameters
    ----------
    name : str
        Prompt name.
    arguments : str
        Values for the template placeholders.

    Returns
    -------
    str
        The rendered prompt text.
    """
    return PROMPTS[name]["template"].format(**arguments)


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


def register_solve_prompts(server: Any) -> None:
    """Register the solve leaf's workflow prompts.

    Parameters
    ----------
    server : Any
        Solve leaf server.
    """

    @server.prompt(
        name="fluent_setup_change",
        description=PROMPTS["fluent_setup_change"]["description"],
    )
    def fluent_setup_change(goal: str) -> str:
        return render_prompt("fluent_setup_change", goal=goal)

    @server.prompt(
        name="fluent_diagnose_divergence",
        description=PROMPTS["fluent_diagnose_divergence"]["description"],
    )
    def fluent_diagnose_divergence(symptom: str = "residuals are rising") -> str:
        return render_prompt("fluent_diagnose_divergence", symptom=symptom)


__all__ = ["PROMPTS", "register_solve_prompts", "register_solve_resources", "render_prompt"]
