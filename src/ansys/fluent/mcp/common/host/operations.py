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

"""Make long-running mutating tool calls survive host timeouts.

Component activation, case loading and solver runs often outlast an MCP host's
request timeout. The host then cancels the call and usually retries, which
interrupts the work and starts it a second time. :class:`LongOperationMiddleware`:

* shields the running work from client cancellation so it completes;
* lets a retry with identical arguments join the call that is still running;
* replays, once and within ``replay_ttl_s``, the result of an idempotent call
  that finished after the host gave up (any other mutating call clears the
  replay cache, so a replay never hides a state change made in between);
* sends progress notifications every ``heartbeat_s`` seconds while a call runs.

Read-only and unclassified tools pass straight through. Nothing is ever undone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable

import anyio
from fastmcp.server.middleware import Middleware

from ansys.fluent.mcp.common.host.annotations import annotations_for

logger = logging.getLogger("ansys.fluent.mcp.host.operations")

HEARTBEAT_S = 10.0
"""Seconds between progress notifications."""

REPLAY_TTL_S = 300.0
"""Seconds a finished idempotent result stays available to an identical retry."""


def _is_success(result: Any) -> bool:
    if getattr(result, "is_error", False):
        return False
    structured = getattr(result, "structured_content", None)
    if (
        isinstance(structured, dict)
        and set(structured) == {"result"}
        and isinstance(structured["result"], dict)
    ):
        structured = structured["result"]
    if isinstance(structured, dict):
        if structured.get("ok") is False:
            return False
        return structured.get("status") != "error" and not structured.get("error_code")
    return True


class LongOperationMiddleware(Middleware):
    """Join, replay and heartbeat long-running mutating tool calls.

    Parameters
    ----------
    hint_lookup : Callable[[str], dict[str, bool] | None], optional
        Returns a tool's behavior hints; defaults to :func:`annotations_for`.
    heartbeat_s : float, default: HEARTBEAT_S
        Seconds between progress notifications.
    replay_ttl_s : float, default: REPLAY_TTL_S
        Seconds a finished idempotent result can be replayed.
    clock : Callable[[], float], default: time.monotonic
        Monotonic clock, injectable for tests.
    """

    def __init__(
        self,
        *,
        hint_lookup: Callable[[str], dict[str, bool] | None] = annotations_for,
        heartbeat_s: float = HEARTBEAT_S,
        replay_ttl_s: float = REPLAY_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize the middleware state."""
        self._hint_lookup = hint_lookup
        self._heartbeat_s = heartbeat_s
        self._replay_ttl_s = replay_ttl_s
        self._clock = clock
        self._inflight: dict[tuple[str, str], asyncio.Future[Any]] = {}
        self._completed: dict[tuple[str, str], tuple[float, Any]] = {}

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        """Run, join or replay a tool call.

        Parameters
        ----------
        context : Any
            FastMCP middleware context.
        call_next : Any
            Next handler in the middleware chain.

        Returns
        -------
        Any
            The tool result.
        """
        name = str(getattr(context.message, "name", "") or "")
        hints = self._hint_lookup(name)
        if hints is None or hints.get("readOnlyHint"):
            return await call_next(context)
        arguments = getattr(context.message, "arguments", None) or {}
        key = (name, json.dumps(arguments, sort_keys=True, default=str))
        idempotent = bool(hints.get("idempotentHint"))

        cached = self._completed.pop(key, None)
        if cached is not None and idempotent and cached[0] >= self._clock():
            logger.info("replaying completed %s result to an identical retry", name)
            return cached[1]

        task = self._inflight.get(key)
        if task is None:
            # A new mutating call may change state, so earlier replays are no longer safe.
            self._completed.clear()
            task = asyncio.ensure_future(call_next(context))
            self._inflight[key] = task
            task.add_done_callback(lambda done: self._finish(key, done, idempotent))
        else:
            logger.info("joining in-flight %s call instead of starting it again", name)
        return await self._await_with_heartbeat(task, context, name)

    def _finish(self, key: tuple[str, str], task: asyncio.Future[Any], idempotent: bool) -> None:
        self._inflight.pop(key, None)
        if not idempotent or task.cancelled() or task.exception() is not None:
            return
        result = task.result()
        if _is_success(result):
            self._completed[key] = (self._clock() + self._replay_ttl_s, result)

    async def _await_with_heartbeat(
        self, task: asyncio.Future[Any], context: Any, name: str
    ) -> Any:
        ctx = getattr(context, "fastmcp_context", None)
        started = self._clock()
        while True:
            done, _ = await asyncio.wait({asyncio.shield(task)}, timeout=self._heartbeat_s)
            if done:
                return task.result()
            if ctx is None:
                continue
            elapsed = self._clock() - started
            try:
                await ctx.report_progress(
                    progress=elapsed, message=f"{name} still running ({elapsed:.0f} s)"
                )
            except (anyio.BrokenResourceError, anyio.ClosedResourceError, RuntimeError):
                logger.debug("progress notification for %s failed", name, exc_info=True)


__all__ = ["HEARTBEAT_S", "REPLAY_TTL_S", "LongOperationMiddleware"]
