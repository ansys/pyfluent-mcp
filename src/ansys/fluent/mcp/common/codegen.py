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

"""Pluggable code generation pipeline.

The pipeline forwards `(prompt, session_id)` to a backend's `codegen`
method (which usually means calling the Fluids One
`/api/chat/propose_code` endpoint). It gives every leaf a single,
consistent place to drive code generation — including lifting the LLM
orchestration loop into the server itself, calling backend tools
(named-objects / state / allowed-values) directly.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ansys.fluent.mcp.common.backend import Backend
from ansys.fluent.mcp.common.conversation import ConversationStore
from ansys.fluent.mcp.common.errors import InvalidArgumentsError, NotConnectedError
from ansys.fluent.mcp.common.models import CodegenResult

logger = logging.getLogger("ansys.fluent.mcp.codegen")


class CodegenPipeline:
    """Default pipeline: delegate to the backend.

    Subclass and override `generate`/`clarify` to add LLM orchestration,
    retrieval, validation passes, etc.
    """

    def __init__(self, *, store: ConversationStore) -> None:
        """Initialize the CodegenPipeline instance.

        Parameters
        ----------
        store : ConversationStore
            Store to supply to the function.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        self.store = store

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    async def generate(
        self,
        *,
        backend: Backend,
        prompt: str,
        session_id: Optional[str] = None,
        context: Optional[dict[str, Any]] = None,
    ) -> CodegenResult:
        """Generate code for the requested conversation state.

        Parameters
        ----------
        backend : Backend
            Backend instance for performing the operation.
        prompt : str
            Natural-language request to process.
        session_id : Optional[str]
            Identifier for the conversation or tool session.
        context : Optional[dict[str, Any]]
            Additional context to pass to the backend or pipeline.

        Returns
        -------
        CodegenResult
            CodegenResult produced by the operation.
        """
        if not prompt or not prompt.strip():
            raise InvalidArgumentsError("prompt must be a non-empty string")
        if not backend.is_connected():
            raise NotConnectedError("Call `connect` before `codegen`.")

        entry = self.store.get_or_create(session_id)
        self.store.append_history(entry.session_id, "user", prompt)

        result = await backend.codegen(
            prompt=prompt,
            session_id=entry.session_id,
            context=context,
        )

        # Carry our internal session id through so multi-turn clarify works.
        if result.session_id is None:
            result.session_id = entry.session_id

        self._record_result(entry.session_id, result)
        return result

    async def clarify(
        self, *, backend: Backend, session_id: str, clarification_id: str, answer: str
    ) -> CodegenResult:
        """Apply a clarification answer to a pending code-generation session.

        Parameters
        ----------
        backend : Backend
            Backend instance for performing the operation.
        session_id : str
            Identifier for the conversation or tool session.
        clarification_id : str
            Identifier for the clarification.
        answer : str
            Answer text supplied for the pending clarification.

        Returns
        -------
        CodegenResult
            CodegenResult produced by the operation.
        """
        if not session_id:
            raise InvalidArgumentsError("session_id is required")
        if not clarification_id:
            raise InvalidArgumentsError("clarification_id is required")

        entry = self.store.get(session_id)
        if entry is None:
            raise InvalidArgumentsError(f"Unknown or expired session_id: {session_id}")
        if not backend.is_connected():
            raise NotConnectedError("Call `connect` before `clarify`.")

        self.store.append_history(
            session_id,
            "user",
            {"clarification_id": clarification_id, "answer": answer},
        )

        result = await backend.clarify(
            session_id=session_id,
            clarification_id=clarification_id,
            answer=answer,
        )
        if result.session_id is None:
            result.session_id = session_id

        self._record_result(session_id, result)
        return result

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _record_result(self, session_id: str, result: CodegenResult) -> None:
        """Record a tool execution result in the conversation state.

        Parameters
        ----------
        session_id : str
            Identifier for the conversation or tool session.
        result : CodegenResult
            Result object or payload to process.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        self.store.append_history(session_id, "assistant", result.model_dump())
        if result.status == "needs_clarification" and result.clarifications:
            # Store the first pending clarification as the "current" one so
            # the LLM/UI can answer without re-sending the whole list.
            self.store.set_pending_clarification(session_id, result.clarifications[0].model_dump())
        else:
            self.store.set_pending_clarification(session_id, None)
