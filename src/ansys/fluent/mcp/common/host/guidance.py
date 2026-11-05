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
(or ``{"ok": False, "error": "<code>"}``, or a typed error model). A host LLM
still has to guess whether to retry and what to do next. :func:`enrich_result`
adds:

* ``retryable``: ``True`` only when the same call can succeed after waiting or
  after the named recovery step.
* ``next_action``: one concrete recovery step, when the error class has one.

Values a tool already set are never overwritten, and successful results are
returned unchanged. Error codes are classified by ordered name patterns, so new
codes that follow the existing naming are covered automatically.
"""

from __future__ import annotations

import re
from typing import Any

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
        r"^(syntax|sequence)_error$|^(tui_not_allowed|validation_failed)$",
        False,
        _REWRITE,
    ),
    (r"^forbidden_|_blocked$|_in_read_mode$|^read_only_leaf$", False, _REWRITE),
    (
        r"^(not_connected|no_session|no_backend|solver_disconnected)$|_not_activated$",
        True,
        _RECONNECT,
    ),
    (r"_not_connected$|_session_required$|_backend_required$", True, _RECONNECT),
    (r"^(timeout|backend_unavailable|upstream_error)$", True, _WAIT_AND_CHECK),
    (
        r"_timeout$|_hung$|_transport_error$|empty_response$",
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


def error_code_of(payload: Any) -> str | None:
    """Return the error code of a result payload, or ``None`` for a success.

    Parameters
    ----------
    payload : Any
        Tool result as a mapping.

    Returns
    -------
    str | None
        The ``error_code`` (or ``error`` when ``ok`` is ``False``), else ``None``.
    """
    if not isinstance(payload, dict):
        return None
    code = payload.get("error_code")
    if not code and payload.get("ok") is False:
        code = payload.get("error")
    if not isinstance(code, str) or not code or payload.get("status") == "ok":
        return None
    return code


def enrich_error_payload(payload: dict[str, Any]) -> bool:
    """Add ``retryable`` and ``next_action`` to an error payload in place.

    A payload carrying ``input_request`` (a missing user decision with an
    ``argument`` name and optional ``options``) is told to ask the user and call
    again.

    Parameters
    ----------
    payload : dict[str, Any]
        Structured tool result.

    Returns
    -------
    bool
        Whether the payload was changed.
    """
    code = error_code_of(payload)
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


def enrich_result(result: Any, *, trace_id: str | None = None) -> Any:
    """Return a tool result with error guidance added; successes are returned unchanged.

    Mappings are copied before they are changed. Pydantic error models (for
    example ``TypedError``) are converted to a mapping so the guidance fields
    can be added.

    Parameters
    ----------
    result : Any
        Value returned by a tool function.
    trace_id : str | None, default: None
        Trace id of the call, added to error results as ``trace_id``.

    Returns
    -------
    Any
        The original result, or an enriched mapping.
    """
    payload = result
    dump = getattr(result, "model_dump", None)
    if not isinstance(result, dict) and callable(dump):
        payload = dump()
    if error_code_of(payload) is None:
        return result
    payload = dict(payload)
    enrich_error_payload(payload)
    if trace_id and "trace_id" not in payload:
        payload["trace_id"] = trace_id
    return payload


__all__ = ["enrich_error_payload", "enrich_result", "error_code_of", "error_guidance"]
