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
interrupts the work and starts it a second time. :class:`CallCoordinator`:

* shields the running work from client cancellation so it completes;
* lets a retry with identical arguments join the call that is still running;
* replays, once and within ``replay_ttl_s``, the result of an idempotent call
  that finished after the host gave up. Any other mutating call clears the
  replay cache, so a replay never hides a state change made in between.

Nothing is ever undone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable

from ansys.fluent.mcp.common.host.guidance import error_code_of

logger = logging.getLogger("ansys.fluent.mcp.host.operations")

REPLAY_TTL_S = 300.0
"""Seconds a finished idempotent result stays available to an identical retry."""


def call_key(tool: str, kwargs: dict[str, Any]) -> tuple[str, str]:
    """Return the identity of a call: tool name plus its JSON-serializable arguments.

    Arguments that cannot be serialized (for example an injected request
    context) are left out, so they never make identical calls look different.

    Parameters
    ----------
    tool : str
        Tool name.
    kwargs : dict[str, Any]
        Keyword arguments of the call.

    Returns
    -------
    tuple[str, str]
        Hashable call identity.
    """
    plain: dict[str, Any] = {}
    for name, value in kwargs.items():
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            continue
        plain[name] = value
    return tool, json.dumps(plain, sort_keys=True)


def _is_success(result: Any) -> bool:
    dump = getattr(result, "model_dump", None)
    payload = result if isinstance(result, dict) or not callable(dump) else dump()
    return error_code_of(payload) is None


class CallCoordinator:
    """Join, shield and replay mutating tool calls for one server.

    Parameters
    ----------
    replay_ttl_s : float, default: REPLAY_TTL_S
        Seconds a finished idempotent result can be replayed.
    clock : Callable[[], float], default: time.monotonic
        Monotonic clock, injectable for tests.
    """

    def __init__(
        self,
        *,
        replay_ttl_s: float = REPLAY_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize empty in-flight and replay caches."""
        self._replay_ttl_s = replay_ttl_s
        self._clock = clock
        self._inflight: dict[tuple[str, str], asyncio.Future[Any]] = {}
        self._completed: dict[tuple[str, str], tuple[float, Any]] = {}

    async def run(
        self,
        key: tuple[str, str],
        start: Callable[[], Awaitable[Any]],
        *,
        idempotent: bool,
    ) -> Any:
        """Run a mutating call, joining or replaying an identical one when possible.

        Parameters
        ----------
        key : tuple[str, str]
            Call identity from :func:`call_key`.
        start : Callable[[], Awaitable[Any]]
            Starts the actual tool call.
        idempotent : bool
            Whether repeating the call with the same arguments is harmless.

        Returns
        -------
        Any
            The tool result.
        """
        cached = self._completed.pop(key, None)
        if cached is not None and idempotent and cached[0] >= self._clock():
            logger.info("replaying completed %s result to an identical retry", key[0])
            return cached[1]

        task = self._inflight.get(key)
        if task is None:
            # A new mutating call may change state, so earlier replays are no longer safe.
            self._completed.clear()
            task = asyncio.ensure_future(start())
            self._inflight[key] = task
            task.add_done_callback(lambda done: self._finish(key, done, idempotent))
        else:
            logger.info("joining in-flight %s call instead of starting it again", key[0])
        return await asyncio.shield(task)

    def _finish(self, key: tuple[str, str], task: asyncio.Future[Any], idempotent: bool) -> None:
        self._inflight.pop(key, None)
        if not idempotent or task.cancelled() or task.exception() is not None:
            return
        result = task.result()
        if _is_success(result):
            self._completed[key] = (self._clock() + self._replay_ttl_s, result)


__all__ = ["REPLAY_TTL_S", "CallCoordinator", "call_key"]
