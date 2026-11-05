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

Generic MCP hosts (VS Code Copilot, Claude Desktop, Cursor) rely on:

* behavior hints on every tool (:mod:`ansys.fluent.mcp.common.host.annotations`);
* ``retryable`` / ``next_action`` guidance on error results
  (:mod:`ansys.fluent.mcp.common.host.guidance`);
* long-running mutating calls that survive host timeouts
  (:mod:`ansys.fluent.mcp.common.host.operations`);
* per-call trace ids in the server log (:mod:`ansys.fluent.mcp.common.host.trace`).

:class:`HostContract` applies all of this by wrapping each tool function at
registration time. It uses only the standard library, so this package does not
depend on the internals of the MCP server framework. Set
``FLUIDS_MCP_HOST_CONTRACT=0`` to turn it off.
"""

from __future__ import annotations

import functools
import inspect
import os
import time
from typing import Any, Callable

from ansys.fluent.mcp.common.host.annotations import annotations_for
from ansys.fluent.mcp.common.host.guidance import enrich_result, error_code_of, error_guidance
from ansys.fluent.mcp.common.host.operations import CallCoordinator, call_key
from ansys.fluent.mcp.common.host.trace import (
    bind_trace_id,
    current_trace_id,
    log_call,
    new_trace_id,
    reset_trace_id,
)

HOST_CONTRACT_ENV = "FLUIDS_MCP_HOST_CONTRACT"
"""Environment variable that turns the host contract off when set to ``0``."""


def host_contract_enabled() -> bool:
    """Return whether the host contract should wrap tools.

    Returns
    -------
    bool
        ``False`` only when ``FLUIDS_MCP_HOST_CONTRACT`` is a falsy value.
    """
    raw = os.environ.get(HOST_CONTRACT_ENV, "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _outcome(result: Any) -> str:
    dump = getattr(result, "model_dump", None)
    payload = result if isinstance(result, dict) or not callable(dump) else dump()
    code = error_code_of(payload)
    return f"error:{code}" if code else "ok"


def set_resolved_annotations(fn: Callable[..., Any], annotations: dict[str, Any]) -> None:
    """Give ``fn`` resolved annotations that survive Python 3.14's ``__annotate__``.

    From Python 3.14, ``functools.wraps`` copies the lazy ``__annotate__``
    slot (PEP 649) instead of ``__annotations__`` (see
    https://github.com/pydantic/pydantic-ai/issues/3685). Functions whose
    ``__annotations__`` were assigned by hand (to match a synthesized
    ``__signature__``) would then expose their *original* annotations to
    callers that resolve type hints via ``__annotate__``, which breaks
    input-schema generation. Setting both attributes keeps schema generation
    correct on Python 3.12, 3.13, and 3.14+.
    """
    resolved = dict(annotations)
    fn.__annotations__ = resolved

    def _annotate(format: Any, _resolved: dict[str, Any] = resolved) -> dict[str, Any]:
        return dict(_resolved)

    fn.__annotate__ = _annotate  # type: ignore[attr-defined]


def _copy_annotations(source: Callable[..., Any], wrapper: Callable[..., Any]) -> None:
    """Give ``wrapper`` the same resolved annotations as ``source``."""
    try:
        annotations = dict(source.__annotations__)
    except (AttributeError, NameError, TypeError):
        return
    set_resolved_annotations(wrapper, annotations)


class HostContract:
    """Wrap tool functions with trace ids, timeout-safe execution and error guidance.

    Parameters
    ----------
    hint_lookup : Callable[[str], dict[str, bool] | None], default: annotations_for
        Returns a tool's behavior hints.
    coordinator : CallCoordinator, optional
        Joins and replays mutating calls; one per server.
    """

    def __init__(
        self,
        hint_lookup: Callable[[str], dict[str, bool] | None] = annotations_for,
        *,
        coordinator: CallCoordinator | None = None,
    ) -> None:
        """Store the hint lookup and the per-server call coordinator."""
        self._hint_lookup = hint_lookup
        self._coordinator = coordinator or CallCoordinator()

    def wrap(self, tool: str, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Return ``fn`` wrapped with the host contract.

        The wrapper keeps ``fn``'s signature (``functools.wraps``), so the MCP
        framework derives the same input schema.

        Parameters
        ----------
        tool : str
            Registered tool name.
        fn : Callable[..., Any]
            Tool function, sync or async.

        Returns
        -------
        Callable[..., Any]
            The wrapped tool function.
        """
        hints = self._hint_lookup(tool)
        mutating = hints is not None and not hints.get("readOnlyHint")
        idempotent = bool(hints and hints.get("idempotentHint"))
        coordinator = self._coordinator

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                trace_id = new_trace_id()
                token = bind_trace_id(trace_id)
                started = time.monotonic()
                outcome = "raised"
                try:
                    if mutating:
                        result = await coordinator.run(
                            call_key(tool, kwargs),
                            lambda: fn(*args, **kwargs),
                            idempotent=idempotent,
                        )
                    else:
                        result = await fn(*args, **kwargs)
                    result = enrich_result(result, trace_id=trace_id)
                    outcome = _outcome(result)
                    return result
                finally:
                    reset_trace_id(token)
                    log_call(tool, trace_id, int((time.monotonic() - started) * 1000), outcome)

            _copy_annotations(fn, async_wrapper)
            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            trace_id = new_trace_id()
            token = bind_trace_id(trace_id)
            started = time.monotonic()
            outcome = "raised"
            try:
                result = enrich_result(fn(*args, **kwargs), trace_id=trace_id)
                outcome = _outcome(result)
                return result
            finally:
                reset_trace_id(token)
                log_call(tool, trace_id, int((time.monotonic() - started) * 1000), outcome)

        _copy_annotations(fn, sync_wrapper)
        return sync_wrapper


__all__ = [
    "HOST_CONTRACT_ENV",
    "HostContract",
    "annotations_for",
    "current_trace_id",
    "error_guidance",
    "host_contract_enabled",
]
