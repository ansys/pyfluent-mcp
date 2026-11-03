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

"""MCP tool annotations (behavior hints) for the leaf tool surface.

MCP hosts (VS Code Copilot, Claude Desktop, Cursor) read ``readOnlyHint``,
``destructiveHint`` and ``idempotentHint`` to decide which calls can run
without confirmation and which ones must not be retried blindly. Each tool is
classified by its worst-case action. Lifecycle tools named ``manage_<label>``
can deactivate a component, so they are destructive. Unclassified tools get
no annotations and the host falls back to the protocol default (not
read-only, possibly destructive).
"""

from __future__ import annotations

READ_ONLY: dict[str, bool] = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
}
"""Reads state or builds code without changing anything."""

ADDITIVE: dict[str, bool] = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
}
"""Adds or attaches state without overwriting anything; safe to repeat."""

OVERWRITE: dict[str, bool] = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": True,
}
"""Overwrites values or lifecycle state; repeating with the same arguments is harmless."""

DESTRUCTIVE: dict[str, bool] = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
}
"""Arbitrary or irreversible mutation; confirm first and never retry blindly."""

LEAF_TOOL_ANNOTATIONS: dict[str, dict[str, bool]] = {
    "compare_files": READ_ONLY,
    "describe_named_object_template": READ_ONLY,
    "describe_path": READ_ONLY,
    "find_api": READ_ONLY,
    "find_named_object": READ_ONLY,
    "get_active_status": READ_ONLY,
    "get_allowed_values": READ_ONLY,
    "get_help": READ_ONLY,
    "get_state": READ_ONLY,
    "get_targeted_context": READ_ONLY,
    "list_fields": READ_ONLY,
    "list_named_objects": READ_ONLY,
    "mesh_quality": READ_ONLY,
    "probe_path": READ_ONLY,
    "screenshot": READ_ONLY,
    "select_named_objects": READ_ONLY,
    "session_status": READ_ONLY,
    "solver_status": READ_ONLY,
    "summarize_setup": READ_ONLY,
    "validate_code": READ_ONLY,
    "connect": ADDITIVE,
    "simulation_report": ADDITIVE,
    "disconnect": OVERWRITE,
    "run_code": DESTRUCTIVE,
}
"""Hints for every tool the shared leaf base and the solve leaf register."""


def annotations_for(tool_name: str | None) -> dict[str, bool] | None:
    """Return the behavior hints for a tool.

    Parameters
    ----------
    tool_name : str | None
        Registered tool name.

    Returns
    -------
    dict[str, bool] | None
        A copy of the hint profile, or ``None`` when the tool is unclassified.
    """
    if not tool_name:
        return None
    profile = LEAF_TOOL_ANNOTATIONS.get(tool_name)
    if profile is None and tool_name.startswith("manage_"):
        profile = OVERWRITE
    return dict(profile) if profile is not None else None


__all__ = [
    "ADDITIVE",
    "DESTRUCTIVE",
    "LEAF_TOOL_ANNOTATIONS",
    "OVERWRITE",
    "READ_ONLY",
    "annotations_for",
]
