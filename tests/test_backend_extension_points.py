"""Tests for the documented ``PyFluentBackend`` extension points."""

import asyncio
from types import SimpleNamespace

from ansys.fluent.mcp.common.models import RunCodeResult
from ansys.fluent.mcp.solve.backends.composite import SolveCompositeBackend
from ansys.fluent.mcp.solve.backends.pyfluent import PyFluentBackend


def _solver(**settings):
    return SimpleNamespace(settings=SimpleNamespace(**settings), is_active=lambda: True)


class _RefusingBackend(PyFluentBackend):
    def prepare_execution_tree(self, tree, solver, namespace):
        return RunCodeResult(status="error", error_code="policy_blocked", message="refused")

    def validate_write_target(self, path, value):
        return {"status": "error", "error_code": "policy_blocked", "message": path}


def test_prepare_execution_tree_can_refuse_execution():
    backend = _RefusingBackend()
    backend.adopt_session(_solver(), endpoint="pyfluent://test")

    result = asyncio.run(backend.run_code("x = 1"))

    assert result.error_code == "policy_blocked"


def test_validate_write_target_rejects_dry_run_write():
    backend = _RefusingBackend()
    backend.adopt_session(_solver(), endpoint="pyfluent://test")

    result = asyncio.run(backend.dry_run_write("setup.models.energy.enabled", True))

    assert result == {
        "status": "error",
        "error_code": "policy_blocked",
        "message": "setup.models.energy.enabled",
    }


def test_adopt_session_and_mark_disconnected():
    backend = PyFluentBackend()
    solver = _solver(marker=1)

    backend.adopt_session(solver, endpoint="pyfluent://host:1")

    assert backend.is_connected()
    assert backend.endpoint == "pyfluent://host:1"
    assert backend.require_solver() is solver
    assert backend.settings_root().marker == 1
    assert backend.session_kind == "solver_session"

    backend.mark_disconnected()

    assert backend.endpoint is None


def test_run_exclusive_runs_function_on_worker_thread():
    backend = PyFluentBackend()

    assert asyncio.run(backend.run_exclusive(lambda a, b: a + b, 2, b=3)) == 5


def _borrowed_solver(events, name):
    return SimpleNamespace(
        settings=SimpleNamespace(),
        is_active=lambda: True,
        exit=lambda **_kwargs: events.append(f"exit {name}"),
        _fluent_connection=SimpleNamespace(exit=lambda **_kwargs: events.append(f"conn {name}")),
    )


def test_adopted_session_is_released_to_its_owner_not_exited():
    events = []
    backend = PyFluentBackend()
    solver = _borrowed_solver(events, "a")

    backend.adopt_session(solver, endpoint="pyfluent://a", release=lambda s: events.append(s))
    asyncio.run(backend.disconnect())

    assert events == [solver]
    assert not backend.is_connected()
    assert backend._borrowed == {}


def test_adopted_session_without_release_is_left_running():
    events = []
    backend = PyFluentBackend()
    backend.adopt_session(_borrowed_solver(events, "a"), endpoint="pyfluent://a")

    backend.close_sync()

    assert events == []


def test_adopt_refuses_to_replace_a_held_session():
    import pytest

    from ansys.fluent.mcp.common.errors import BackendUnavailableError

    backend = PyFluentBackend()
    first = _solver()
    backend.adopt_session(first, endpoint="pyfluent://a")

    with pytest.raises(BackendUnavailableError):
        backend.adopt_session(_solver(), endpoint="pyfluent://b")

    assert backend.require_solver() is first


def test_replaced_adopted_session_is_retained_and_released():
    released = []
    backend = PyFluentBackend()
    first, second = _solver(), _solver()
    backend.adopt_session(first, endpoint="pyfluent://a", release=released.append)
    backend.mark_disconnected()
    backend.adopt_session(second, endpoint="pyfluent://b", release=released.append)

    asyncio.run(backend.disconnect())

    assert released == [first, second]
    assert backend._retired_solvers == []


def test_failed_release_retains_the_adopted_session():
    def fail(_solver):
        raise RuntimeError("owner could not close")

    backend = PyFluentBackend()
    solver = _solver()
    backend.adopt_session(solver, endpoint="pyfluent://a", release=fail)

    backend.close_sync()

    assert backend._retired_solvers == [solver]
    assert id(solver) in backend._borrowed


def test_composite_exposes_its_execution_backend():
    inner = PyFluentBackend()

    assert SolveCompositeBackend(pyfluent_backend=inner).pyfluent_backend is inner


def test_default_solve_surface_omits_unsupported_component_lifecycle():
    from ansys.fluent.mcp.solve.mcp import SolveMCP

    assert "manage_component" not in SolveMCP()._exposed
