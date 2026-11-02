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

"""Ask the user for a missing tool input through MCP elicitation.

Contract: a tool that cannot proceed without a user decision returns its
normal result plus::

    "input_request": {
        "argument": "<name of the tool argument to fill>",
        "message": "<question for the user>",
        "options": ["a", "b"],  # optional closed choice
        "kind": "string" | "number" | "boolean",  # used when no options
    }

When the connected host supports elicitation, :class:`ElicitationMiddleware`
asks the user once and re-runs the same tool with the answer filled in.
Otherwise, or when the user declines, the original result is returned and the
host model asks in chat instead.

Never use this for secrets: the MCP specification forbids requesting
sensitive information through form elicitation. Credentials belong in the
server configuration.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp.server.middleware import Middleware

logger = logging.getLogger("ansys.fluent.mcp.host.elicitation")

_KINDS: dict[str, type] = {"string": str, "number": float, "boolean": bool}
_SECRET_HINTS = ("password", "token", "secret", "api_key", "apikey", "credential")


def input_request_of(result: Any) -> dict[str, Any] | None:
    """Return a well-formed ``input_request`` from a tool result.

    Parameters
    ----------
    result : Any
        Tool result.

    Returns
    -------
    dict[str, Any] | None
        The request, or ``None`` when absent, malformed or secret-looking.
    """
    structured = getattr(result, "structured_content", None)
    if (
        isinstance(structured, dict)
        and set(structured) == {"result"}
        and isinstance(structured["result"], dict)
    ):
        structured = structured["result"]
    if not isinstance(structured, dict):
        return None
    request = structured.get("input_request")
    if not isinstance(request, dict):
        return None
    argument = request.get("argument")
    message = request.get("message")
    if not isinstance(argument, str) or not argument:
        return None
    if not isinstance(message, str) or not message:
        return None
    if any(hint in argument.lower() for hint in _SECRET_HINTS):
        logger.warning("refusing to elicit secret-looking argument %r", argument)
        return None
    options = request.get("options")
    if options is not None and not (
        isinstance(options, list) and options and all(isinstance(o, str) for o in options)
    ):
        return None
    if options is None and request.get("kind", "string") not in _KINDS:
        return None
    return request


def client_supports_elicitation(ctx: Any) -> bool:
    """Return whether the connected client advertised the elicitation capability.

    Parameters
    ----------
    ctx : Any
        FastMCP request context.

    Returns
    -------
    bool
        ``True`` when the client can answer elicitation requests.
    """
    session = getattr(ctx, "session", None)
    params = getattr(session, "client_params", None)
    capabilities = getattr(params, "capabilities", None)
    return getattr(capabilities, "elicitation", None) is not None


async def ask(ctx: Any, request: dict[str, Any]) -> Any | None:
    """Elicit one value for ``request``.

    Parameters
    ----------
    ctx : Any
        FastMCP request context.
    request : dict[str, Any]
        Validated ``input_request``.

    Returns
    -------
    Any | None
        The answer, or ``None`` when declined, cancelled or not an allowed option.
    """
    options = request.get("options")
    response_type: Any = list(options) if options else _KINDS[request.get("kind", "string")]
    answer = await ctx.elicit(request["message"], response_type=response_type)
    if getattr(answer, "action", None) != "accept":
        return None
    value = getattr(answer, "data", None)
    if options and value not in options:
        return None
    return value


class ElicitationMiddleware(Middleware):
    """Fill a tool's ``input_request`` via MCP elicitation and re-run it once."""

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        """Run the tool; ask the user and re-run when it requests input.

        Parameters
        ----------
        context : Any
            FastMCP middleware context.
        call_next : Any
            Next handler in the middleware chain.

        Returns
        -------
        Any
            The re-run result, or the original one.
        """
        result = await call_next(context)
        request = input_request_of(result)
        ctx = getattr(context, "fastmcp_context", None)
        if request is None or ctx is None or not client_supports_elicitation(ctx):
            return result
        value = await ask(ctx, request)
        if value is None:
            return result
        arguments = dict(getattr(context.message, "arguments", None) or {})
        arguments[request["argument"]] = value
        message = context.message.model_copy(update={"arguments": arguments})
        return await call_next(context.copy(message=message))


__all__ = ["ElicitationMiddleware", "ask", "client_supports_elicitation", "input_request_of"]
