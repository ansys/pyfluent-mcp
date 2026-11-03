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

Every wrapped tool call gets a short trace id. The server logs one line per
call (tool, trace id, duration, outcome); error results carry the id as
``trace_id``; code running inside the call can read :func:`current_trace_id`.
"""

from __future__ import annotations

import contextvars
import logging
import uuid

logger = logging.getLogger("ansys.fluent.mcp.trace")

_TRACE_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ansys_fluent_mcp_trace_id", default=None
)


def new_trace_id() -> str:
    """Return a new 12-character trace id.

    Returns
    -------
    str
        Random hexadecimal id.
    """
    return uuid.uuid4().hex[:12]


def current_trace_id() -> str | None:
    """Return the trace id of the tool call running in this context.

    Returns
    -------
    str | None
        The trace id, or ``None`` outside a tool call.
    """
    return _TRACE_ID.get()


def bind_trace_id(trace_id: str) -> contextvars.Token[str | None]:
    """Make ``trace_id`` the current trace id.

    Parameters
    ----------
    trace_id : str
        Trace id of the call that is starting.

    Returns
    -------
    contextvars.Token[str | None]
        Token to pass to :func:`reset_trace_id` when the call ends.
    """
    return _TRACE_ID.set(trace_id)


def reset_trace_id(token: contextvars.Token[str | None]) -> None:
    """Restore the trace id that was current before :func:`bind_trace_id`.

    Parameters
    ----------
    token : contextvars.Token[str | None]
        Token returned by :func:`bind_trace_id`.
    """
    _TRACE_ID.reset(token)


def log_call(tool: str, trace_id: str, duration_ms: int, outcome: str) -> None:
    """Log one line for a finished tool call.

    Parameters
    ----------
    tool : str
        Tool name.
    trace_id : str
        Trace id of the call.
    duration_ms : int
        Wall time in milliseconds.
    outcome : str
        ``ok``, ``error:<code>``, or ``raised``.
    """
    logger.info(
        "tool=%s trace_id=%s duration_ms=%d outcome=%s", tool, trace_id, duration_ms, outcome
    )


__all__ = ["bind_trace_id", "current_trace_id", "log_call", "new_trace_id", "reset_trace_id"]
