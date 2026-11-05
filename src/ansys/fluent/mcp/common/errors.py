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

"""Helper module to handle errors.

Typed error helpers and a guard decorator that converts unexpected
exceptions into a `TypedError` instead of crashing the MCP transport.
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Awaitable, Callable, TypeVar

from ansys.fluent.mcp.common.models import TypedError

logger = logging.getLogger("ansys.fluent.mcp.errors")

T = TypeVar("T")


class FluidsMCPError(Exception):
    """Base class for typed errors raised by leaves and backends."""

    error_code = "internal_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        """Initialize the FluidsMCPError instance.

        Parameters
        ----------
        message : str
            Message text to format, log, or return.
        details : dict[str, Any] | None
            Details to supply to the function.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_typed(self) -> TypedError:
        """Convert the error into a typed error payload.

        Returns
        -------
        TypedError
            TypedError produced by the operation.
        """
        return TypedError(error_code=self.error_code, message=self.message, details=self.details)


class BackendUnavailableError(FluidsMCPError):
    """Raised when a backend call fails because the backend is unavailable."""

    error_code = "backend_unavailable"


class NotConnectedError(FluidsMCPError):
    """Raised when a backend call fails because the backend is not connected."""

    error_code = "not_connected"


class InvalidArgumentsError(FluidsMCPError):
    """Raised when a backend call fails due to invalid arguments."""

    error_code = "invalid_arguments"


class UpstreamError(FluidsMCPError):
    """Raised when a backend call fails due to an upstream error (for example, a network issue)."""

    error_code = "upstream_error"


class DiscoveryError(FluidsMCPError):
    """Raised when a backend discovery operation fails."""

    error_code = "discovery_error"


def typed_guard(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T | TypedError]]:
    """Wrap an async tool handler so unexpected exceptions become `TypedError`.

    ``FluidsMCPError`` subclasses are converted to their typed form.
    Anything else is logged and wrapped as `internal_error`.

    Parameters
    ----------
    func : Callable[..., Awaitable[T]]
        Function supplied to the decorator.

    Returns
    -------
    Callable[..., Awaitable[T | TypedError]]
        Result produced by the function.
    """

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any):
        """Execute the nested helper for the enclosing operation.

        Parameters
        ----------
        args : Any
            Positional arguments forwarded to the callable.
        kwargs : Any
            Keyword arguments forwarded to the callable.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        try:
            return await func(*args, **kwargs)
        except FluidsMCPError as exc:
            logger.info("Typed error from %s: %s — %s", func.__name__, exc.error_code, exc.message)
            return exc.to_typed()
        except Exception:  # boundary
            logger.exception("Unhandled error in %s", func.__name__)
            return TypedError(
                error_code="internal_error",
                message="An internal error occurred. Consult the server logs for details.",
            )

    return wrapper


def _format_call(name: str, args: dict[str, Any]) -> str:
    """Render ``name(k=v, ...)`` for a human-readable recovery hint."""
    return f"{name}({', '.join(f'{key}={value!r}' for key, value in args.items())})"


def not_connected_payload(
    *,
    tool: str,
    product: str | None = None,
    next_tool: str | None = None,
    next_tool_args: dict[str, Any] | None = None,
    next_tool_choices: list[dict[str, Any]] | None = None,
    activate_tool: str | None = None,
    activate_args: dict[str, Any] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Return the canonical, actionable ``not_connected`` tool result.

    Every "no live session" outcome should use this shape so hosts can
    recognise it (``error_code``), retry it (``retryable``), and fix it
    without guessing (``next_tool`` / ``next_tool_args``).

    This helper only formats. The caller (the MCP server) owns the
    recovery arguments because only it knows its real tool interface:
    a unified server's ``connect`` takes ``product``, a standalone leaf's
    takes ``backend_kind`` / ``connect_kwargs``. Recovery guidance is
    never guessed here.

    Parameters
    ----------
    tool : str
        Name of the tool that needed a session.
    product : str | None
        Product that owns ``tool`` (``solve``, ``geometry``, ...), when known.
    next_tool : str | None
        Registered tool that restores the session, or ``None`` when the
        server exposes none.
    next_tool_args : dict[str, Any] | None
        Arguments valid for ``next_tool`` on THIS server. ``None`` when the
        caller must pick one of ``next_tool_choices``.
    next_tool_choices : list[dict[str, Any]] | None
        Valid alternative argument sets when no single one can be chosen.
    activate_tool : str | None
        Registered tool that starts the component, when one exists.
    activate_args : dict[str, Any] | None
        Arguments valid for ``activate_tool`` on THIS server.
    reason : str | None
        Optional underlying cause.

    Returns
    -------
    dict[str, Any]
        Structured error payload.
    """
    scope = f"live {product} session" if product else "live session"
    message = f"Tool {tool!r} needs a {scope}, and none is attached."
    if reason:
        message = f"{message} Cause: {reason}"

    hints: list[str] = []
    if next_tool and next_tool_args is not None:
        hints.append(f"Call {_format_call(next_tool, next_tool_args)} and retry {tool!r}.")
    elif next_tool and next_tool_choices:
        options = " or ".join(_format_call(next_tool, args) for args in next_tool_choices)
        hints.append(f"Call {options}, then retry {tool!r}.")
    else:
        hints.append(f"Attach a session to this server, then retry {tool!r}.")
    if activate_tool:
        hints.append(
            "If the component is not running yet, call "
            f"{_format_call(activate_tool, activate_args or {})} first."
        )

    payload: dict[str, Any] = {
        "ok": False,
        "status": "error",
        "error_code": "not_connected",
        "tool": tool,
        "message": message,
        "retryable": True,
        "next_tool": next_tool,
        "next_tool_args": next_tool_args,
        "hint": " ".join(hints),
    }
    if product:
        payload["product"] = product
    if next_tool_choices:
        payload["next_tool_choices"] = next_tool_choices
    return payload
