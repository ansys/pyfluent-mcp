# Copyright (C) 2026 Synopsys, Inc. and ANSYS, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Server-owned recovery for every registered tool's no-session outcome."""

import functools
import inspect

from pydantic import BaseModel

from ansys.fluent.mcp.common.errors import NotConnectedError


def _missing_session_payload(result):
    code = (
        result.get("error_code")
        if isinstance(result, dict)
        else getattr(result, "error_code", None)
    )
    if code != "not_connected":
        return None
    payload = result.model_dump() if isinstance(result, BaseModel) else result
    return payload if isinstance(payload, dict) else None


def require_connected_result(result):
    """Surface missing-session errors before an aggregator discards their code."""
    payload = _missing_session_payload(result)
    if payload is not None:
        raise NotConnectedError(payload.get("message") or "The backend has no active session.")
    return result


def with_connection_recovery(function, recover):
    """Normalize missing-session errors without forbidding offline operations.

    ``recover`` belongs to the registering server, which knows its actual
    connect arguments and owning product. This applies independently of the
    optional host contract and preserves already complete recovery responses.
    """
    def normalize(result):
        payload = _missing_session_payload(result)
        if payload is None:
            return result
        if all(key in payload for key in ("retryable", "next_tool", "hint")):
            return result
        return {**payload, **recover(reason=payload.get("message"))}

    if inspect.iscoroutinefunction(function):
        @functools.wraps(function)
        async def async_wrapper(*args, **kwargs):
            try:
                return normalize(await function(*args, **kwargs))
            except NotConnectedError as exc:
                return recover(reason=exc.message)
        return async_wrapper

    @functools.wraps(function)
    def sync_wrapper(*args, **kwargs):
        try:
            return normalize(function(*args, **kwargs))
        except NotConnectedError as exc:
            return recover(reason=exc.message)
    return sync_wrapper
