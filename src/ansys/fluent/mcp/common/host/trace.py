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

"""Per-call trace ids so host-side and server-side logs line up.

:class:`TraceMiddleware` gives every tool call a short ``trace_id``, logs one
line per call (tool, trace id, duration, outcome) and attaches
``{"fluent/trace_id", "fluent/duration_ms"}`` to the MCP result ``_meta``.
Code running inside the call can read :func:`current_trace_id`.
"""

from __future__ import annotations

import contextvars
import logging
import time
from typing import Any
import uuid

from fastmcp.server.middleware import Middleware

logger = logging.getLogger("ansys.fluent.mcp.trace")

TRACE_ID_KEY = "fluent/trace_id"
"""Result ``_meta`` key carrying the trace id."""

DURATION_KEY = "fluent/duration_ms"
"""Result ``_meta`` key carrying the call duration in milliseconds."""

_TRACE_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ansys_fluent_mcp_trace_id", default=None
)


def current_trace_id() -> str | None:
    """Return the trace id of the tool call running in this context.

    Returns
    -------
    str | None
        The trace id, or ``None`` outside a tool call.
    """
    return _TRACE_ID.get()


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _outcome(result: Any) -> str:
    if getattr(result, "is_error", False):
        return "error"
    structured = getattr(result, "structured_content", None)
    if (
        isinstance(structured, dict)
        and set(structured) == {"result"}
        and isinstance(structured["result"], dict)
    ):
        structured = structured["result"]
    if isinstance(structured, dict) and (
        structured.get("status") == "error"
        or structured.get("error_code")
        or structured.get("ok") is False
    ):
        return f"error:{structured.get('error_code') or structured.get('error') or 'unknown'}"
    return "ok"


class TraceMiddleware(Middleware):
    """Assign, log and return a trace id for every tool call."""

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        """Run the tool inside a trace.

        Parameters
        ----------
        context : Any
            FastMCP middleware context.
        call_next : Any
            Next handler in the middleware chain.

        Returns
        -------
        Any
            The tool result with trace metadata attached.
        """
        trace_id = uuid.uuid4().hex[:12]
        token = _TRACE_ID.set(trace_id)
        name = getattr(context.message, "name", "?")
        started = time.monotonic()
        result = None
        try:
            result = await call_next(context)
        finally:
            _TRACE_ID.reset(token)
            if result is None:
                logger.info(
                    "tool=%s trace_id=%s duration_ms=%d outcome=raised",
                    name,
                    trace_id,
                    _ms(started),
                )
        duration = _ms(started)
        logger.info(
            "tool=%s trace_id=%s duration_ms=%d outcome=%s",
            name,
            trace_id,
            duration,
            _outcome(result),
        )
        if hasattr(result, "meta"):
            result.meta = {**(result.meta or {}), TRACE_ID_KEY: trace_id, DURATION_KEY: duration}
        return result


__all__ = ["DURATION_KEY", "TRACE_ID_KEY", "TraceMiddleware", "current_trace_id"]
