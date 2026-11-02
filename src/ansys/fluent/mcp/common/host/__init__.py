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

"""Host-facing contract for generic MCP hosts.

:func:`install_host_middleware` gives a FastMCP server the behavior generic
hosts (VS Code Copilot, Claude Desktop, Cursor) rely on, outermost first:

1. :class:`~ansys.fluent.mcp.common.host.trace.TraceMiddleware`: per-call trace id.
2. :class:`~ansys.fluent.mcp.common.host.operations.LongOperationMiddleware`:
   timeout-safe long operations.
3. :class:`~ansys.fluent.mcp.common.host.guidance.ErrorGuidanceMiddleware`:
   ``retryable`` / ``next_action`` on errors.
4. :class:`~ansys.fluent.mcp.common.host.elicitation.ElicitationMiddleware`:
   asks the user for a tool's missing input (innermost, so its single re-run
   goes straight to the tool).

Tool annotations come from :mod:`ansys.fluent.mcp.common.host.annotations`.
Set ``FLUIDS_MCP_HOST_MIDDLEWARE=0`` to disable the middleware.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from ansys.fluent.mcp.common.host.annotations import annotations_for
from ansys.fluent.mcp.common.host.elicitation import ElicitationMiddleware
from ansys.fluent.mcp.common.host.guidance import ErrorGuidanceMiddleware, error_guidance
from ansys.fluent.mcp.common.host.operations import LongOperationMiddleware
from ansys.fluent.mcp.common.host.trace import TraceMiddleware, current_trace_id

HOST_MIDDLEWARE_ENV = "FLUIDS_MCP_HOST_MIDDLEWARE"
"""Environment variable that disables the host middleware when set to ``0``."""


def host_middleware_enabled() -> bool:
    """Return whether the host middleware should be installed.

    Returns
    -------
    bool
        ``False`` only when ``FLUIDS_MCP_HOST_MIDDLEWARE`` is a falsy value.
    """
    raw = os.environ.get(HOST_MIDDLEWARE_ENV, "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def install_host_middleware(
    server: Any,
    *,
    hint_lookup: Callable[[str], dict[str, bool] | None] = annotations_for,
) -> None:
    """Install the host middleware stack on a FastMCP server.

    Parameters
    ----------
    server : Any
        FastMCP server exposing ``add_middleware``.
    hint_lookup : Callable[[str], dict[str, bool] | None], default: annotations_for
        Returns a tool's behavior hints for the long-operation middleware.
    """
    server.add_middleware(TraceMiddleware())
    server.add_middleware(LongOperationMiddleware(hint_lookup=hint_lookup))
    server.add_middleware(ErrorGuidanceMiddleware())
    server.add_middleware(ElicitationMiddleware())


__all__ = [
    "HOST_MIDDLEWARE_ENV",
    "annotations_for",
    "current_trace_id",
    "error_guidance",
    "host_middleware_enabled",
    "install_host_middleware",
]
