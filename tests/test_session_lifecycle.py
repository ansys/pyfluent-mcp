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

"""Concurrent connection, cancellation and shutdown ownership regressions."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from ansys.fluent.mcp.common.errors import BackendUnavailableError, NotConnectedError
from ansys.fluent.mcp.solve.backends.pyfluent import PyFluentBackend


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_connect", [False, True])
async def test_adoption_cannot_overtake_connection_publication(monkeypatch, cancel_connect):
    """Reserve the session until launch publication or cancellation cleanup finishes."""
    import ansys.fluent.core as core

    entered, finish_launch, adopted = threading.Event(), threading.Event(), threading.Event()
    closed, released, errors = [], [], []
    launched = SimpleNamespace(exit=lambda **kwargs: closed.append("launched"))
    borrowed = SimpleNamespace(exit=lambda **kwargs: closed.append("borrowed"))

    def launch(**kwargs):
        entered.set()
        assert finish_launch.wait(5)
        return launched

    backend = PyFluentBackend()

    def adopt():
        try:
            backend.adopt_session(borrowed, endpoint="test://borrowed", release=released.append)
        except BackendUnavailableError as exc:
            errors.append(exc)
        finally:
            adopted.set()

    monkeypatch.setattr(core, "launch_fluent", launch)
    connecting = asyncio.create_task(backend.connect(ui_mode="no_gui"))
    adopter = threading.Thread(target=adopt)
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        if cancel_connect:
            connecting.cancel()
            await asyncio.sleep(0)
        adopter.start()
        finish_launch.set()
        # Keep launch-result publication queued while the other thread adopts.
        # Awaiting here would let connect win the race and hide the regression.
        assert adopted.wait(5)
        if cancel_connect:
            with pytest.raises(asyncio.CancelledError):
                await connecting
        else:
            assert (await connecting).status == "ok"
            assert backend.require_solver() is launched
        assert len(errors) == 1
        assert "in progress" in str(errors[0])
        assert id(borrowed) not in backend._borrowed
    finally:
        finish_launch.set()
        if adopter.ident is not None:
            await asyncio.to_thread(adopter.join, 5)
        await backend.disconnect()

    assert closed == ["launched"]
    # A completed/cancelled connection must release the reservation for reuse.
    backend.adopt_session(borrowed, endpoint="test://borrowed", release=released.append)
    await backend.disconnect()
    assert released == [borrowed]
    assert backend._borrowed == {}


@pytest.mark.asyncio
async def test_failed_connect_allows_subsequent_adoption(monkeypatch):
    """An unsuccessful launch must release its connection reservation."""
    import ansys.fluent.core as core

    def launch(**kwargs):
        raise RuntimeError("injected launch failure")

    monkeypatch.setattr(core, "launch_fluent", launch)
    backend = PyFluentBackend()
    assert (await backend.connect(ui_mode="no_gui")).status == "error"
    released = []
    borrowed = SimpleNamespace()
    backend.adopt_session(borrowed, endpoint="test://borrowed", release=released.append)
    await backend.disconnect()
    assert released == [borrowed]


@pytest.mark.asyncio
async def test_concurrent_connect_does_not_replace_or_orphan_session(monkeypatch):
    """Only one of two callers may launch; the other must disconnect first."""
    import ansys.fluent.core as core

    launched, closed = [], []
    solver = SimpleNamespace(
        exit=lambda: closed.append(True),
        _fluent_connection=SimpleNamespace(
            connection_properties=SimpleNamespace(ip="127.0.0.1", port=1234)
        ),
    )

    def launch(**kwargs):
        launched.append(kwargs)
        return solver

    monkeypatch.setattr(core, "launch_fluent", launch)
    backend = PyFluentBackend()
    first, second = await asyncio.gather(backend.connect(), backend.connect())
    assert [first.status, second.error_code] == ["ok", "already_connected"]
    assert len(launched) == 1
    assert backend.endpoint == "pyfluent://127.0.0.1:1234"
    assert backend._solver is solver
    assert not closed
    await backend.disconnect()
    assert closed == [True]
    assert backend.endpoint is None


@pytest.mark.asyncio
async def test_sync_shutdown_waits_for_solver_worker():
    """Synchronous framework cleanup must never exit a busy solver."""
    backend = PyFluentBackend()
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    backend._solver = SimpleNamespace(exit=exited.set)

    def work():
        entered.set()
        assert release.wait(timeout=5)

    operation = asyncio.create_task(backend._run_thread(work))
    await asyncio.to_thread(entered.wait, 5)
    shutdown = asyncio.create_task(asyncio.to_thread(backend.close_sync))
    await asyncio.sleep(0.03)
    assert not exited.is_set()
    release.set()
    await asyncio.gather(operation, shutdown)
    assert exited.is_set()
    assert backend._solver is None
    with pytest.raises(NotConnectedError):
        await backend._run_thread(lambda: None)


@pytest.mark.asyncio
async def test_dead_channel_retains_session_until_cleanup_before_reconnect(monkeypatch):
    """A failed RPC cannot make the previous process unreachable to cleanup."""
    import ansys.fluent.core as core

    closed = []
    old = SimpleNamespace(exit=lambda: closed.append("old"))
    new = SimpleNamespace(exit=lambda: closed.append("new"))
    monkeypatch.setattr(core, "launch_fluent", lambda **kwargs: new)
    backend = PyFluentBackend()
    backend._solver = old
    backend._mark_solver_disconnected()
    assert not backend.is_connected()
    assert backend._retired_solvers == [old]
    assert (await backend.connect()).error_code == "session_cleanup_required"
    assert not closed
    await backend.disconnect()
    assert closed == ["old"]
    assert not backend._retired_solvers
    assert (await backend.connect()).status == "ok"
    await backend.disconnect()
    assert closed == ["old", "new"]


@pytest.mark.asyncio
async def test_failed_dead_session_cleanup_preserves_handle_and_blocks_launch(monkeypatch):
    """Cleanup failure stays actionable instead of permitting another launch."""
    import ansys.fluent.core as core

    attempts = []

    def close():
        attempts.append(True)
        raise RuntimeError("injected cleanup failure")

    monkeypatch.setattr(core, "launch_fluent", lambda **kwargs: pytest.fail("must not launch"))
    backend = PyFluentBackend()
    old = SimpleNamespace(exit=close)
    backend._solver = old
    backend._mark_solver_disconnected()
    with pytest.raises(BackendUnavailableError, match="cleanup failed"):
        await backend.disconnect()
    assert backend._retired_solvers == [old]
    assert (await backend.connect()).error_code == "session_cleanup_required"
    old.exit = lambda: attempts.append("retry")
    await backend.disconnect()
    assert attempts == [True, "retry"]
    assert not backend._retired_solvers


def test_sync_shutdown_closes_retired_sessions():
    """Product shutdown also owns handles retired by dead-channel detection."""
    closed = []
    backend = PyFluentBackend()
    backend._solver = SimpleNamespace(exit=lambda: closed.append(True))
    backend._mark_solver_disconnected()
    backend.close_sync()
    assert closed == [True]
    assert not backend._retired_solvers


@pytest.mark.asyncio
async def test_cleanup_verification_failure_retains_native_connection_for_retry():
    """A silent exit failure must remain blocked even if exit detached the session."""
    calls = []
    connection = SimpleNamespace(
        exit=lambda **kwargs: calls.append(kwargs),
        wait_process_finished=lambda **kwargs: False,
    )
    solver = SimpleNamespace(_fluent_connection=connection)

    def detach(**kwargs):
        calls.append(kwargs)
        solver._fluent_connection = None

    solver.exit = detach
    backend = PyFluentBackend()
    backend._owns_process = True
    backend._solver = solver
    backend._mark_solver_disconnected()
    with pytest.raises(BackendUnavailableError):
        await backend.disconnect()
    assert backend._cleanup_connections[id(solver)] is connection
    assert backend._retired_solvers == [solver]
    connection.wait_process_finished = lambda **kwargs: True
    await backend.disconnect()
    assert not backend._retired_solvers
    assert not backend._cleanup_connections
    assert calls == [{"timeout": 5.0, "wait": 5.0}] * 2


@pytest.mark.asyncio
async def test_verified_process_exit_overrides_native_cleanup_command_error():
    """Native cleanup may return an error after its process has already exited."""
    def close(**kwargs):
        raise RuntimeError("cleanup command returned 1")

    backend = PyFluentBackend()
    backend._owns_process = True
    backend._solver = SimpleNamespace(
        exit=close, _fluent_connection=SimpleNamespace(wait_process_finished=lambda **kwargs: True),
    )
    backend._mark_solver_disconnected()
    await backend.disconnect()
    assert not backend._retired_solvers


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup", [True, False])
async def test_cleanup_ownership_respects_explicit_launch_opt_out(monkeypatch, cleanup):
    """Only owned launches receive force-capable exit options and verification."""
    import ansys.fluent.core as core

    exits, waits = [], []
    connection = SimpleNamespace(
        wait_process_finished=lambda **kwargs: waits.append(kwargs) or True
    )
    solver = SimpleNamespace(
        exit=lambda **kwargs: exits.append(kwargs), _fluent_connection=connection
    )
    monkeypatch.setattr(core, "launch_fluent", lambda **kwargs: solver)
    backend = PyFluentBackend()
    assert (await backend.connect(cleanup_on_exit=cleanup)).status == "ok"
    backend._mark_solver_disconnected()
    await backend.disconnect()
    assert exits == ([{"timeout": 5.0, "wait": 5.0}] if cleanup else [{}])
    assert bool(waits) is cleanup


@pytest.mark.asyncio
async def test_cancelled_worker_retains_operation_lock_until_finished():
    """Cancellation cannot let disconnect overtake a still-running RPC."""
    backend = PyFluentBackend()
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    backend._solver = SimpleNamespace(exit=exited.set)

    def work():
        entered.set()
        assert release.wait(timeout=5)

    async def operation():
        async with backend._lock:
            await backend._run_thread(work)

    running = asyncio.create_task(operation())
    await asyncio.to_thread(entered.wait, 5)
    running.cancel()
    disconnect = asyncio.create_task(backend.disconnect())
    await asyncio.sleep(0.03)
    assert not running.done()
    assert not exited.is_set()
    running.cancel()
    await asyncio.sleep(0)
    assert not running.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await running
    await disconnect
    assert exited.is_set()


@pytest.mark.parametrize("healthy", [True, False])
def test_connection_health_is_observed_even_with_solver_reference(healthy):
    """An existing Python session object is insufficient evidence of liveness."""
    backend = PyFluentBackend()
    backend._solver = SimpleNamespace(is_active=lambda: healthy)
    assert backend.is_connected() is healthy


def test_failed_health_probe_reports_disconnected():
    """Transport failure must never be reported as a healthy connection."""
    class Solver:
        @property
        def is_active(self):
            raise ConnectionError("closed channel")

    backend = PyFluentBackend()
    backend._solver = Solver()
    assert not backend.is_connected()


@pytest.mark.asyncio
async def test_shutdown_during_launch_closes_the_late_session(monkeypatch):
    """Shutdown cannot miss a session published after the launcher returns."""
    import ansys.fluent.core as core

    entered, release = threading.Event(), threading.Event()
    closed = []
    solver = SimpleNamespace(exit=lambda: closed.append(True))

    def launch(**kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return solver

    monkeypatch.setattr(core, "launch_fluent", launch)
    backend = PyFluentBackend()
    connecting = asyncio.create_task(backend.connect())
    await asyncio.to_thread(entered.wait, 5)
    shutdown = asyncio.create_task(asyncio.to_thread(backend.close_sync))
    await asyncio.sleep(0.03)
    release.set()
    result, _ = await asyncio.gather(connecting, shutdown)
    assert result.status == "error"
    assert closed == [True]
    assert backend._solver is None
