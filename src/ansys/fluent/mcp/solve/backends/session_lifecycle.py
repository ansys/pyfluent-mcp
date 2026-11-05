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

"""Thread coordination for solver operations and synchronous shutdown."""

import asyncio
import inspect
import logging

from ansys.fluent.mcp.common.errors import NotConnectedError

logger = logging.getLogger(__name__)


async def run_locked(backend, function, *args, **kwargs):
    """Serialize solver work with shutdown and wait for canceled workers."""

    def work():
        with backend._thread_lock:
            if backend._shutdown:
                raise NotConnectedError("The Fluent backend has been shut down.")
            return function(*args, **kwargs)

    task = asyncio.create_task(asyncio.to_thread(work))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancellation:
        # Retain the async operation lock until the worker has really stopped.
        try:
            await wait_for_completion(task)
        finally:
            raise cancellation


async def wait_for_completion(task):
    """Wait through repeated caller cancellation without canceling a worker."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise


def session_endpoint(solver):
    """Describe the actual connection without including its credentials."""
    connection = getattr(solver, "_fluent_connection", None)
    properties = getattr(connection, "connection_properties", None)
    ip, port = getattr(properties, "ip", None), getattr(properties, "port", None)
    return f"pyfluent://{ip}:{port}" if ip and port else None


def cleanup_sessions(backend, active):
    """Retain failed cleanup handles so a new launch cannot orphan them."""
    with backend._thread_lock:
        sessions = [*backend._retired_solvers]
        if active is not None and not any(active is old for old in sessions):
            sessions.append(active)
        failed = []
        for solver in sessions:
            if id(solver) in backend._borrowed:
                try:
                    _release_borrowed(solver, backend._borrowed[id(solver)])
                except Exception:
                    logger.exception("Releasing an adopted Fluent session failed; retaining it")
                    failed.append(solver)
                else:
                    del backend._borrowed[id(solver)]
                continue
            connection = getattr(solver, "_fluent_connection", None)
            if connection is not None:
                backend._cleanup_connections[id(solver)] = connection
            try:
                _close_session(
                    solver,
                    owned=backend._owns_process,
                    connection=backend._cleanup_connections.get(id(solver)),
                )
            except Exception:
                logger.exception("Fluent session cleanup failed; retaining the session handle")
                failed.append(solver)
        backend._retired_solvers = failed
        backend._cleanup_connections = {
            id(solver): backend._cleanup_connections[id(solver)]
            for solver in failed
            if id(solver) in backend._cleanup_connections
        }


def _release_borrowed(solver, release):
    """Hand an adopted session back to its owner; never exit it here."""
    if callable(release):
        release(solver)


def _close_session(solver, *, owned, connection):
    close = getattr(solver, "exit", None) or getattr(solver, "close", None)
    if connection is not None and getattr(solver, "_fluent_connection", None) is None:
        # A prior exit may have detached the session before verification failed.
        close = connection.exit
    if not callable(close):
        return
    options = {}
    if owned:
        parameters = inspect.signature(close).parameters
        accepts_options = any(
            item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values()
        )
        options = {
            key: value
            for key, value in {"timeout": 5.0, "wait": 5.0}.items()
            if accepts_options or key in parameters
        }
    wait = getattr(connection, "wait_process_finished", None)
    try:
        close(**options)
    except Exception:
        if owned and callable(wait) and wait(wait=5.0) is True:
            logger.warning("Exit reported an error, but the owned process is verified stopped")
            return
        raise
    if owned and callable(wait) and wait(wait=5.0) is not True:
        raise RuntimeError("The owned Fluent process has not finished; retain its cleanup handle.")
