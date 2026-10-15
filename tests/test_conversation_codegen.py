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

import asyncio

import pytest

from ansys.fluent.mcp.common.codegen import CodegenPipeline
from ansys.fluent.mcp.common.conversation import ConversationStore
from ansys.fluent.mcp.common.errors import InvalidArgumentsError, NotConnectedError
from ansys.fluent.mcp.common.models import Clarification, CodegenResult


class FakeBackend:
    def __init__(self, *, connected=True, result=None, clarify_result=None):
        """Initialize the FakeBackend instance.

        Parameters
        ----------
        connected : Any
            Whether the fake or test backend should report an active connection.
        result : Any
            Result object or payload to process.
        clarify_result : Any
            Clarify result to supply to the function.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        self.connected = connected
        self.result = result or CodegenResult(status="ok", code="print('ok')")
        self.clarify_result = clarify_result or CodegenResult(
            status="ok", code="print('clarified')"
        )
        self.codegen_calls = []
        self.clarify_calls = []

    def is_connected(self):
        """Return whether connected.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        return self.connected

    async def codegen(self, **kwargs):
        """Generate code from the provided prompt.

        Parameters
        ----------
        kwargs : Any
            Keyword arguments forwarded to the callable.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        self.codegen_calls.append(kwargs)
        return self.result

    async def clarify(self, **kwargs):
        """Apply a clarification answer to a pending code-generation session.

        Parameters
        ----------
        kwargs : Any
            Keyword arguments forwarded to the callable.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        self.clarify_calls.append(kwargs)
        return self.clarify_result


def test_conversation_store_lifecycle_history_and_pending(monkeypatch):
    """Verify that conversation store lifecycle history and pending.

    Parameters
    ----------
    monkeypatch : Any
        Pytest fixture used to patch environment variables or dependencies.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    now = [100.0]
    monkeypatch.setattr(ConversationStore, "_now", staticmethod(lambda: now[0]))
    store = ConversationStore(ttl_seconds=10, max_entries=2)

    entry = store.create()
    store.append_history(entry.session_id, "user", "hello")
    store.set_pending_clarification(entry.session_id, {"id": "c1", "question": "Pick inlet?"})

    assert store.get(entry.session_id) is entry
    assert entry.history[0]["role"] == "user"
    assert store.has_pending_clarification_id(entry.session_id, "c1") is True
    assert store.clarification_was_just_asked(entry.session_id, " pick inlet? ") is True

    now[0] = 111.0
    assert store.get(entry.session_id) is None


def test_conversation_store_capacity_evicts_oldest(monkeypatch):
    """Verify that conversation store capacity evicts oldest.

    Parameters
    ----------
    monkeypatch : Any
        Pytest fixture used to patch environment variables or dependencies.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    now = [1.0]
    monkeypatch.setattr(ConversationStore, "_now", staticmethod(lambda: now[0]))
    store = ConversationStore(ttl_seconds=100, max_entries=2)

    first = store.create()
    now[0] += 1
    second = store.create()
    now[0] += 1
    third = store.create()

    assert store.get(first.session_id) is None
    assert store.get(second.session_id) is second
    assert store.get(third.session_id) is third
    assert store.get_or_create(second.session_id) is second
    assert store.get_or_create("missing").session_id not in {
        first.session_id,
        second.session_id,
        third.session_id,
    }


def test_conversation_store_missing_operations_are_noops():
    """Verify that conversation store missing operations are noops.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    store = ConversationStore()

    store.touch("missing")
    store.append_history("missing", "user", "ignored")
    store.set_pending_clarification("missing", {"id": "c1"})

    assert store.get("missing") is None
    assert store.has_pending_clarification_id("missing", "c1") is False
    assert store.clarification_was_just_asked("missing", "question") is False


def test_codegen_pipeline_generates_records_history_and_pending_clarification():
    """Verify that codegen pipeline generates records history and pending clarification.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    clarification = Clarification(id="c1", question="Which inlet?", choices=["inlet-1"])
    backend = FakeBackend(
        result=CodegenResult(status="needs_clarification", clarifications=[clarification])
    )
    store = ConversationStore()
    pipeline = CodegenPipeline(store=store)

    result = asyncio.run(
        pipeline.generate(backend=backend, prompt="set inlet", context={"units": []})
    )
    entry = store.get(result.session_id)

    assert result.session_id is not None
    assert backend.codegen_calls[0]["session_id"] == result.session_id
    assert backend.codegen_calls[0]["context"] == {"units": []}
    assert [item["role"] for item in entry.history] == ["user", "assistant"]
    assert entry.pending_clarification["id"] == "c1"


def test_codegen_pipeline_clarify_records_result_and_clears_pending():
    """Verify that codegen pipeline clarify records result and clears pending.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    store = ConversationStore()
    entry = store.create()
    store.set_pending_clarification(entry.session_id, {"id": "c1", "question": "Q"})
    backend = FakeBackend(clarify_result=CodegenResult(status="ok", code="print('done')"))
    pipeline = CodegenPipeline(store=store)

    result = asyncio.run(
        pipeline.clarify(
            backend=backend,
            session_id=entry.session_id,
            clarification_id="c1",
            answer="inlet-1",
        )
    )

    assert result.session_id == entry.session_id
    assert backend.clarify_calls[0] == {
        "session_id": entry.session_id,
        "clarification_id": "c1",
        "answer": "inlet-1",
    }
    assert store.get(entry.session_id).pending_clarification is None


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda pipeline, backend: pipeline.generate(backend=backend, prompt="  "), "prompt"),
        (
            lambda pipeline, backend: pipeline.clarify(
                backend=backend, session_id="", clarification_id="c1", answer="a"
            ),
            "session_id",
        ),
        (
            lambda pipeline, backend: pipeline.clarify(
                backend=backend, session_id="s", clarification_id="", answer="a"
            ),
            "clarification_id",
        ),
        (
            lambda pipeline, backend: pipeline.clarify(
                backend=backend, session_id="missing", clarification_id="c1", answer="a"
            ),
            "Unknown",
        ),
    ],
)
def test_codegen_pipeline_rejects_invalid_arguments(call, message):
    """Verify that codegen pipeline rejects invalid arguments.

    Parameters
    ----------
    call : Any
        Call to supply to the function.
    message : Any
        Message text to format, log, or return.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    pipeline = CodegenPipeline(store=ConversationStore())
    with pytest.raises(InvalidArgumentsError, match=message):
        asyncio.run(call(pipeline, FakeBackend()))


def test_codegen_pipeline_requires_connected_backend():
    """Verify that codegen pipeline requires connected backend.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    pipeline = CodegenPipeline(store=ConversationStore())
    entry = pipeline.store.create()

    with pytest.raises(NotConnectedError):
        asyncio.run(pipeline.generate(backend=FakeBackend(connected=False), prompt="do it"))
    with pytest.raises(NotConnectedError):
        asyncio.run(
            pipeline.clarify(
                backend=FakeBackend(connected=False),
                session_id=entry.session_id,
                clarification_id="c1",
                answer="a",
            )
        )
