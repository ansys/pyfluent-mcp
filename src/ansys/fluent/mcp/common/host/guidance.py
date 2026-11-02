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

"""Uniform ``retryable`` / ``next_action`` guidance on tool error results.

Leaf tools report failures as ``{"status": "error", "error_code", "message"}``
(or ``{"ok": False, "error": "<code>"}``). A host LLM still has to guess
whether to retry and what to do next. :class:`ErrorGuidanceMiddleware` adds:

* ``retryable``: ``True`` only when the same call can succeed after waiting or
  after the named recovery step.
* ``next_action``: one concrete recovery step, when the error class has one.

Values a tool already set are never overwritten. Error codes are classified by
ordered name patterns, so new codes that follow the existing naming are
covered automatically.
"""

from __future__ import annotations

import json
import re
from typing import Any

from fastmcp.server.middleware import Middleware
from fastmcp.tools.tool import ToolResult

from mcp.types import TextContent

_RECONNECT = (
    "Call session_status, then connect (and activate the component with its manage_* tool) "
    "before retrying the same call."
)
_WAIT_AND_CHECK = (
    "The operation may still be running. Call session_status before retrying, and do not "
    "start a second instance."
)
_AUTH = (
    "Authentication failed. Configure the credentials in the MCP server environment "
    "(not in chat), restart the server, then retry."
)
_FIX_ARGUMENTS = (
    "Correct the arguments (use describe_path, find_api or list_named_objects to find valid "
    "values) and call again; do not resend the same arguments."
)
_REWRITE = "Rewrite the code or step as the message describes; do not resend it unchanged."
_INSTALL = (
    "A required component is not installed or configured on the server; report it to the user."
)

_RULE_TABLE: tuple[tuple[str, bool, str], ...] = (
    (
        r"^(risk_blocked|sequence_error|syntax_error|tui_not_allowed|validation_failed)$",
        False,
        _REWRITE,
    ),
    (r"^forbidden_|_blocked$|_in_read_mode$|^read_only_leaf$", False, _REWRITE),
    (
        r"^(not_connected|no_session|no_backend|solver_disconnected|fluent_not_activated)$",
        True,
        _RECONNECT,
    ),
    (r"_not_connected$|_session_required$|_backend_required$", True, _RECONNECT),
    (r"^(timeout|backend_unavailable|upstream_error)$", True, _WAIT_AND_CHECK),
    (
        r"_timeout$|_hung$|_transport_error$|^(empty_response|llm_empty_response)$",
        True,
        _WAIT_AND_CHECK,
    ),
    (r"^(unauthorized|auth_failed|invalid_token|invalid_password)$", False, _AUTH),
    (
        r"^(invalid|missing|unknown|unsupported)_|_not_found$|^not_found$|^value_not_allowed$",
        False,
        _FIX_ARGUMENTS,
    ),
    (r"_not_installed$|_not_configured$", False, _INSTALL),
    (r"_required$", False, _FIX_ARGUMENTS),
    (
        r"_(activate|refresh|connect|start|launch|attach)_failed$|^connect_failed$",
        True,
        _WAIT_AND_CHECK,
    ),
)
_RULES = tuple(
    (re.compile(pattern), retryable, action) for pattern, retryable, action in _RULE_TABLE
)
_AUTH_MESSAGE = re.compile(
    r"\b(401|unauthori[sz]ed|invalid password|invalid token|authentication)\b", re.IGNORECASE
)


def error_guidance(error_code: str, message: str = "") -> tuple[bool, str | None]:
    """Classify an error code.

    Parameters
    ----------
    error_code : str
        Machine-readable error code reported by the tool.
    message : str, default: ""
        Human-readable error message; authentication failures are detected here.

    Returns
    -------
    tuple[bool, str | None]
        ``(retryable, next_action)``. ``next_action`` is ``None`` for unknown codes.
    """
    if message and _AUTH_MESSAGE.search(message):
        return False, _AUTH
    for pattern, retryable, action in _RULES:
        if pattern.search(error_code):
            return retryable, action
    return False, None


def _error_code(payload: dict[str, Any]) -> str | None:
    code = payload.get("error_code")
    if not code and payload.get("ok") is False:
        code = payload.get("error")
    if not isinstance(code, str) or not code or payload.get("status") == "ok":
        return None
    return code


def _error_payload(structured: Any) -> dict[str, Any] | None:
    if not isinstance(structured, dict):
        return None
    payload = structured
    if set(structured) == {"result"} and isinstance(structured["result"], dict):
        payload = structured["result"]
    return payload if _error_code(payload) else None


def enrich_error_payload(payload: dict[str, Any]) -> bool:
    """Add ``retryable`` and ``next_action`` to an error payload in place.

    A payload carrying ``input_request`` (a missing user decision) is told to
    ask the user and call again.

    Parameters
    ----------
    payload : dict[str, Any]
        Structured tool result.

    Returns
    -------
    bool
        Whether the payload was changed.
    """
    code = _error_code(payload)
    if code is None:
        return False
    retryable, action = error_guidance(code, str(payload.get("message") or ""))
    request = payload.get("input_request")
    if isinstance(request, dict) and request.get("argument"):
        options = request.get("options")
        choice = (
            f" (one of {', '.join(map(str, options))})"
            if isinstance(options, list) and options
            else ""
        )
        retryable = True
        action = (
            f"Ask the user for `{request['argument']}`{choice}, then call the same tool again "
            "with that argument set."
        )
    changed = False
    if "retryable" not in payload:
        payload["retryable"] = retryable
        changed = True
    if action and not payload.get("next_action"):
        payload["next_action"] = action
        changed = True
    return changed


def enrich_tool_result(result: ToolResult) -> ToolResult:
    """Return a tool result with error guidance in its structured and text content.

    Parameters
    ----------
    result : ToolResult
        Result produced by the tool.

    Returns
    -------
    ToolResult
        The same object when nothing changed, otherwise an enriched copy.
    """
    original = result.structured_content
    if _error_payload(original) is None:
        return result
    structured = json.loads(json.dumps(original, default=str))
    payload = _error_payload(structured)
    if payload is None or not enrich_error_payload(payload):
        return result
    content = []
    for block in result.content or []:
        text = getattr(block, "text", None)
        if isinstance(block, TextContent) and isinstance(text, str):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if parsed == original:
                block = TextContent(type="text", text=json.dumps(structured, default=str))
            elif isinstance(original, dict) and parsed == original.get("result"):
                block = TextContent(type="text", text=json.dumps(payload, default=str))
        content.append(block)
    return ToolResult(content=content, structured_content=structured, meta=result.meta)


class ErrorGuidanceMiddleware(Middleware):
    """Apply :func:`enrich_tool_result` to every tool call."""

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        """Run the tool and enrich an error result.

        Parameters
        ----------
        context : Any
            FastMCP middleware context.
        call_next : Any
            Next handler in the middleware chain.

        Returns
        -------
        Any
            The (possibly enriched) tool result.
        """
        return enrich_tool_result(await call_next(context))


__all__ = [
    "ErrorGuidanceMiddleware",
    "enrich_error_payload",
    "enrich_tool_result",
    "error_guidance",
]
