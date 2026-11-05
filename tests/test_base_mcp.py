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
from pathlib import Path

import pytest

from ansys.fluent.mcp.common import base
from ansys.fluent.mcp.common.base import FluidsLeafMCP, select_named_objects_from_mapping
from ansys.fluent.mcp.common.domain_tools import DomainTool, DomainToolSpec
from ansys.fluent.mcp.common.errors import NotConnectedError
from ansys.fluent.mcp.common.models import ConnectResult, RunCodeResult, SessionStatus


class BaseFakeBackend:
    kind = "pyfluent"
    label = "base fake"
    endpoint = "local://base"

    def __init__(self, *, connected=True, fail_run=False, summary_mode="text"):
        """Initialize the BaseFakeBackend instance.

        Parameters
        ----------
        connected : Any
            Whether the fake or test backend should report an active connection.
        fail_run : Any
            Whether the fake backend should fail run-code requests.
        summary_mode : Any
            Summary mode returned by the fake backend.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        self.connected = connected
        self.fail_run = fail_run
        self.summary_mode = summary_mode
        self.invalidated = 0
        self.lifecycle = []
        self.run_code_snippets = []

    async def connect(self, **_kwargs):
        """Connect to the configured backend or service.

        Parameters
        ----------
        _kwargs : Any
            Keyword arguments forwarded to the wrapped call.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        self.connected = True
        return ConnectResult(status="ok", backend_kind=self.kind, endpoint=self.endpoint)

    async def disconnect(self):
        """Close resources for the BaseFakeBackend object.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        self.connected = False

    def is_connected(self):
        """Return whether connected.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        return self.connected

    def status(self, leaf):
        """Return backend status information.

        Parameters
        ----------
        leaf : Any
            Leaf MCP server instance under test.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        return SessionStatus(
            leaf=leaf,
            connected=self.connected,
            backend=self.label,
            backend_kind=self.kind,
            endpoint=self.endpoint,
        )

    def invalidate_live_caches(self):
        """Clear cached live backend state.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        self.invalidated += 1

    async def list_named_objects(self):
        """List named objects entries.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        return {
            "setup/boundary-conditions/wall": ["hot-wall", "cold-wall", "cold-wall-shadow"],
            "setup.boundary_conditions.velocity_inlet": ["inlet-1", "inlet-2"],
        }

    async def get_state(self, paths=None):
        """Return the state.

        Parameters
        ----------
        paths : Any
            Fluent object paths supplied to the operation.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        return {"paths": paths}

    async def run_code(self, code):
        """Execute Python code through the backend runtime.

        Parameters
        ----------
        code : Any
            Python code or command text to execute or validate.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        self.run_code_snippets.append(code)
        if self.fail_run:
            raise RuntimeError("backend exploded")
        if "summary" in code:
            if self.summary_mode == "error":
                return RunCodeResult(status="error", stderr="summary failed")
            if self.summary_mode == "text":
                path = code.split("file_name='")[1].split("'", 1)[0]
                with Path(path).open("w", encoding="utf-8") as output:
                    output.write("setup summary")
            return RunCodeResult(status="ok")
        if "list_simulation_reports" in code:
            return RunCodeResult(status="ok", stdout="listed", return_value=["report-a"])
        if "export_simulation_report" in code or "generate_simulation_report" in code:
            return RunCodeResult(status="ok", stdout="done")
        return RunCodeResult(status="ok", stdout="executed", return_value=code)

    async def validate_code(self, code):
        """Validate code.

        Parameters
        ----------
        code : Any
            Python code or command text to execute or validate.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        return RunCodeResult(status="ok", stdout=code)

    async def activate_component(self):
        """Exercise the activate component test helper.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        self.lifecycle.append("activate")
        return {"action": "activate"}

    async def deactivate_component(self):
        """Exercise the deactivate component test helper.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        self.lifecycle.append("deactivate")
        return {"action": "deactivate"}

    async def update_component(self):
        """Update component.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        self.lifecycle.append("update")
        return {"action": "update"}

    async def refresh_component(self):
        """Exercise the refresh component test helper.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        self.lifecycle.append("refresh")
        return {"action": "refresh"}


class BareLeaf(FluidsLeafMCP):
    leaf_name = "bare"
    component_label = "component"
    default_backend_kind = "pyfluent"


def _structured(result):
    """Create a structured test response.

    Parameters
    ----------
    result : Any
        Result object or payload to process.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    payload = getattr(result, "structured_content", None)
    if payload is None:
        payload = getattr(result, "structuredContent", None)
    if isinstance(payload, dict) and set(payload) == {"result"}:
        return payload["result"]
    return payload


def _is_error(result):
    """Return whether error.

    Parameters
    ----------
    result : Any
        Result object or payload to process.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    if getattr(result, "is_error", False) or getattr(result, "isError", False):
        return True
    payload = _structured(result)
    return isinstance(payload, dict) and payload.get("status") == "error"


def _server(backend, *, expose_tools=("run_code",)):
    """Create a test MCP server instance.

    Parameters
    ----------
    backend : Any
        Backend instance used to perform the operation.
    expose_tools : Any
        Whether MCP tools should be registered on the server.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    return BareLeaf(backends={"pyfluent": backend}, expose_tools=expose_tools)


def test_observer_factories_attach_skip_and_swallow(monkeypatch):
    """Verify that observer factories attach skip and swallow.

    Parameters
    ----------
    monkeypatch : Any
        Pytest fixture used to patch environment variables or dependencies.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    calls = []

    def observer(**kwargs):
        """Record an observer notification for the test.

        Parameters
        ----------
        kwargs : Any
            Keyword arguments forwarded to the callable.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        calls.append(kwargs)

    def bad_factory():
        """Exercise the bad factory test helper.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        raise RuntimeError("factory failed")

    def bad_observer_factory():
        """Exercise the bad observer factory test helper.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        return object()

    monkeypatch.setattr(
        base,
        "_OBSERVER_FACTORIES",
        [lambda: observer, lambda: None, bad_factory, bad_observer_factory],
    )
    backend = BaseFakeBackend()

    async def scenario():
        """Run the test scenario helper.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        server = _server(backend)
        assert len(server._run_code_observers) == 1
        assert (
            _structured(await server.call_tool("run_code", {"code": "x = 1"}))["stdout"]
            == "executed"
        )

    asyncio.run(scenario())
    assert calls[0]["code"] == "x = 1"
    assert calls[0]["error"] is None
    assert backend.invalidated == 1


def test_run_code_observer_errors_and_backend_errors_are_swallowed_for_notifications():
    """Verify that run code observer errors and backend errors are swallowed for notifications.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    backend = BaseFakeBackend(fail_run=True)
    seen = []

    def failing_observer(**kwargs):
        """Exercise the failing observer test helper.

        Parameters
        ----------
        kwargs : Any
            Keyword arguments forwarded to the callable.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        seen.append(kwargs)
        raise RuntimeError("observer failed")

    async def scenario():
        """Run the test scenario helper.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        server = _server(backend)
        server.register_run_code_observer(failing_observer)
        result = await server.call_tool("run_code", {"code": "raise RuntimeError()"})
        # Observers still fire (recording the backend error) and an observer
        # failure is swallowed, but the BACKEND error must reach the caller
        # as a typed error. Returning an empty "success" hid dead backends.
        assert _is_error(result)
        assert _structured(result)["error_code"] == "internal_error"
        assert "backend exploded" not in _structured(result)["message"]

    asyncio.run(scenario())
    assert seen[0]["error"].args == ("backend exploded",)
    assert backend.invalidated == 1


def test_run_code_on_disconnected_backend_returns_actionable_not_connected():
    """A dead backend must yield a retryable not_connected error, never an empty ok."""
    backend = BaseFakeBackend(connected=False)

    async def scenario():
        server = _server(backend, expose_tools=("run_code", "connect"))
        result = await server.call_tool("run_code", {"code": "__return__ = 1"})
        payload = _structured(result)
        assert payload["error_code"] == "not_connected"
        assert payload["retryable"] is True
        assert payload["next_tool"] == "connect"
        assert backend.run_code_snippets == []

    asyncio.run(scenario())


def test_run_code_attaches_once_when_backend_declares_passive_attach():
    """A backend that DECLARES a passive attach is attached before failing."""
    backend = BaseFakeBackend(connected=False)
    attached = []

    async def attach_if_running():
        attached.append(True)
        backend.connected = True

    backend.attach_if_running = attach_if_running
    backend.attach_if_running_is_passive = True

    async def scenario():
        server = _server(backend)
        result = await server.call_tool("run_code", {"code": "__return__ = 1"})
        assert not _is_error(result)

    asyncio.run(scenario())
    assert attached == [True]
    assert backend.run_code_snippets == ["__return__ = 1"]


@pytest.mark.parametrize("passive_flag", [None, False, "yes", 1])
def test_undeclared_attach_if_running_is_never_called_implicitly(passive_flag):
    """``attach_if_running`` may mutate (activate/launch); only an explicit
    ``attach_if_running_is_passive = True`` lets a tool call it implicitly."""
    backend = BaseFakeBackend(connected=False)
    attached = []

    async def attach_if_running():
        attached.append(True)  # stands in for "activated a component / launched an app"
        backend.connected = True

    backend.attach_if_running = attach_if_running
    if passive_flag is not None:
        backend.attach_if_running_is_passive = passive_flag

    async def impl(backend, *, name: str) -> dict:
        return {"status": "ok", "name": name}

    tool = DomainTool(
        spec=DomainToolSpec(name="inspect_thing", description="test"),
        handler=impl,
        requires_live_session=True,
    )

    async def scenario():
        server = _server(backend)
        server._register_domain_tools([tool])
        run = _structured(await server.call_tool("run_code", {"code": "__return__ = 1"}))
        inspect = _structured(await server.call_tool("inspect_thing", {"name": "x"}))
        assert run["error_code"] == "not_connected"
        assert inspect["error_code"] == "not_connected"

    asyncio.run(scenario())
    assert attached == []
    assert backend.run_code_snippets == []


def test_standalone_recovery_matches_standalone_connect_signature():
    """Recovery args must be accepted by THIS server's ``connect`` tool."""
    backend = BaseFakeBackend(connected=False)

    async def scenario():
        server = _server(backend, expose_tools=("run_code", "connect", "manage_component"))
        payload = _structured(await server.call_tool("run_code", {"code": "__return__ = 1"}))
        assert payload["next_tool"] == "connect"
        # Standalone ``connect`` takes backend_kind / connect_kwargs, not product.
        assert payload["next_tool_args"] == {"backend_kind": "pyfluent"}
        assert "product" not in payload["next_tool_args"]
        assert "connect(backend_kind='pyfluent')" in payload["hint"]
        assert "manage_component(action='activate')" in payload["hint"]
        # Following the instruction verbatim must succeed.
        reconnect = await server.call_tool(payload["next_tool"], payload["next_tool_args"])
        assert not _is_error(reconnect)
        assert backend.connected is True

    asyncio.run(scenario())


def test_standalone_recovery_lists_choices_when_backend_is_ambiguous():
    """With several backends and no default, recovery offers valid choices only."""

    class MultiLeaf(BareLeaf):
        default_backend_kind = None

    first = BaseFakeBackend(connected=False)
    second = BaseFakeBackend(connected=False)

    async def scenario():
        server = MultiLeaf(
            backends={"pyfluent": first, "remote": second},
            expose_tools=("run_code", "connect"),
        )
        payload = _structured(await server.call_tool("run_code", {"code": "__return__ = 1"}))
        assert payload["error_code"] == "not_connected"
        assert payload["next_tool"] == "connect"
        assert payload["next_tool_args"] is None
        assert payload["next_tool_choices"] == [
            {"backend_kind": "pyfluent"},
            {"backend_kind": "remote"},
        ]
        for args in payload["next_tool_choices"]:
            assert not _is_error(await server.call_tool("connect", args))
            await server.backend.disconnect()

    asyncio.run(scenario())


def test_recovery_omits_connect_when_server_does_not_expose_it():
    """Never recommend a tool the server does not register."""
    backend = BaseFakeBackend(connected=False)

    async def scenario():
        server = _server(backend, expose_tools=("run_code",))
        payload = _structured(await server.call_tool("run_code", {"code": "__return__ = 1"}))
        assert payload["error_code"] == "not_connected"
        assert payload["next_tool"] is None
        assert payload["next_tool_args"] is None
        assert "connect(" not in payload["hint"]

    asyncio.run(scenario())


def test_recovery_uses_tool_owning_product_on_multi_product_server():
    """A multi-product server's override decides the product, not a default."""

    class TwoProductLeaf(BareLeaf):
        leaf_name = "unified"

        def _owning_product(self, tool_name):
            return "geometry" if tool_name == "get_selection" else "solve"

        def _connect_recovery(self, product):
            return {"next_tool": "connect", "next_tool_args": {"product": product}}

    backend = BaseFakeBackend(connected=False)

    async def impl(backend) -> dict:
        return {"status": "ok"}

    tool = DomainTool(
        spec=DomainToolSpec(name="get_selection", description="test"),
        handler=impl,
        requires_live_session=True,
    )

    async def scenario():
        server = TwoProductLeaf(backends={"pyfluent": backend}, expose_tools=())
        server._register_domain_tools([tool])
        payload = _structured(await server.call_tool("get_selection", {}))
        assert payload["product"] == "geometry"
        assert payload["next_tool_args"] == {"product": "geometry"}

    asyncio.run(scenario())


def test_run_code_not_connected_error_mid_call_is_typed():
    """NotConnectedError raised by the backend maps to the canonical payload."""
    backend = BaseFakeBackend()

    async def boom(code):
        raise NotConnectedError("grpc channel closed")

    backend.run_code = boom

    async def scenario():
        server = _server(backend)
        result = await server.call_tool("run_code", {"code": "__return__ = 1"})
        payload = _structured(result)
        assert payload["error_code"] == "not_connected"
        assert "grpc channel closed" in payload["message"]

    asyncio.run(scenario())


def test_domain_tool_session_guard_returns_canonical_not_connected():
    """requires_live_session tools report the same actionable not_connected shape."""
    backend = BaseFakeBackend(connected=False)

    async def impl(backend, *, name: str) -> dict:
        return {"status": "ok", "name": name}

    tool = DomainTool(
        spec=DomainToolSpec(name="needs_session", description="test"),
        handler=impl,
        requires_live_session=True,
    )

    async def scenario():
        server = _server(backend, expose_tools=())
        server._register_domain_tools([tool])
        result = await server.call_tool("needs_session", {"name": "x"})
        payload = _structured(result)
        assert payload["error_code"] == "not_connected"
        assert payload["tool"] == "needs_session"
        assert payload["retryable"] is True

    asyncio.run(scenario())


def test_base_tool_error_branches_and_report_actions(tmp_path):
    """Verify that base tool error branches and report actions.

    Parameters
    ----------
    tmp_path : Any
        Path for the tmp.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    backend = BaseFakeBackend(summary_mode="empty")

    async def scenario():
        """Run the test scenario helper.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        server = _server(
            backend,
            expose_tools=(
                "list_named_objects",
                "get_state",
                "run_code",
                "validate_code",
                "manage_component",
                "summarize_setup",
                "simulation_report",
            ),
        )

        assert (
            _structured(await server.call_tool("list_named_objects", {}))[
                "setup/boundary-conditions/wall"
            ][0]
            == "hot-wall"
        )
        assert _is_error(await server.call_tool("list_named_objects", {"limit": 0}))
        assert _is_error(await server.call_tool("list_named_objects", {"offset": -1}))
        assert _is_error(await server.call_tool("get_state", {"paths": ["a", "b"], "key": "name"}))
        assert _is_error(
            await server.call_tool("get_state", {"paths": ["setup.wall[name]"], "key": "name"})
        )
        assert _is_error(
            await server.call_tool("get_state", {"paths": ["setup.wall"], "key": "bad]name"})
        )
        assert _is_error(await server.call_tool("run_code", {"code": "  "}))
        assert _is_error(await server.call_tool("validate_code", {"code": ""}))
        assert _is_error(await server.call_tool("manage_component", {"action": "restart"}))

        assert _structured(
            await server.call_tool("manage_component", {"action": "deactivate"})
        ) == {"action": "deactivate"}
        assert _structured(await server.call_tool("manage_component", {"action": "Update"})) == {
            "action": "update"
        }
        assert backend.lifecycle == ["deactivate", "update"]

        assert _structured(await server.call_tool("summarize_setup", {})) == {
            "summary": "(no output)"
        }
        assert (
            _structured(
                await server.call_tool(
                    "simulation_report", {"action": "generate", "report_name": "r"}
                )
            )["action"]
            == "generate"
        )
        html = _structured(
            await server.call_tool(
                "simulation_report",
                {"action": "export_html", "report_name": "r", "output_path": str(tmp_path)},
            )
        )
        assert html["action"] == "export_html"
        assert html["output_path"].replace("\\", "/").endswith(str(tmp_path).replace("\\", "/"))
        assert (
            _structured(await server.call_tool("simulation_report", {"action": "export_pdf"}))[
                "action"
            ]
            == "export_pdf"
        )
        assert (
            _structured(await server.call_tool("simulation_report", {"action": "export_pptx"}))[
                "action"
            ]
            == "export_pptx"
        )

    asyncio.run(scenario())


def test_summary_and_report_failure_results():
    """Verify that summary and report failure results.

    Returns
    -------
    None
        The function completes through its side effects.
    """

    async def scenario():
        """Run the test scenario helper.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        summary_server = _server(
            BaseFakeBackend(summary_mode="error"), expose_tools=("summarize_setup",)
        )
        assert _structured(await summary_server.call_tool("summarize_setup", {})) == {
            "error": "summary failed"
        }

        class FailingReportBackend(BaseFakeBackend):
            async def run_code(self, code):
                """Execute Python code through the backend runtime.

                Parameters
                ----------
                code : Any
                    Python code or command text to execute or validate.

                Returns
                -------
                None
                    The function completes through its side effects.
                """
                self.run_code_snippets.append(code)
                return RunCodeResult(status="error", stderr="report failed")

        report_server = _server(FailingReportBackend(), expose_tools=("simulation_report",))
        assert _structured(
            await report_server.call_tool("simulation_report", {"action": "list"})
        ) == {"error": "report failed"}
        assert _structured(
            await report_server.call_tool("simulation_report", {"action": "export_pdf"})
        ) == {"error": "report failed"}

    asyncio.run(scenario())


def test_backend_selection_and_run_helpers(monkeypatch):
    """Verify that backend selection and run helpers.

    Parameters
    ----------
    monkeypatch : Any
        Pytest fixture used to patch environment variables or dependencies.

    Returns
    -------
    None
        The function completes through its side effects.
    """

    class RunnableLeaf(FluidsLeafMCP):
        leaf_name = "runnable"
        default_backend_kind = "pyfluent"

    leaf = RunnableLeaf(backends={"a": BaseFakeBackend(), "b": BaseFakeBackend()}, expose_tools=())
    with pytest.raises(NotConnectedError, match="No active backend"):
        _ = leaf.backend

    calls = []
    monkeypatch.setattr(
        base.PyAnsysBaseMCP, "run", lambda _self, **kwargs: calls.append(kwargs), raising=False
    )
    runnable = RunnableLeaf(backends={"pyfluent": BaseFakeBackend()}, expose_tools=())
    runnable.run()
    runnable.run(transport="http", host="0.0.0.0", port=9001)
    assert calls == [{"transport": "stdio"}, {"transport": "http", "host": "0.0.0.0", "port": 9001}]

    missing_runtime = BareLeaf(backends={"pyfluent": BaseFakeBackend()}, expose_tools=())
    monkeypatch.setattr(base.PyAnsysBaseMCP, "run", None, raising=False)
    with pytest.raises(RuntimeError, match="runtime is unavailable"):
        missing_runtime.run()

    with pytest.raises(TypeError, match="factory must"):
        base.register_run_code_observer_factory(object())
    with pytest.raises(TypeError, match="observer must"):
        missing_runtime.register_run_code_observer(object())


def test_domain_tool_registration_validation_and_live_session_guard():
    """Verify that domain tool registration validation and live session guard.

    Returns
    -------
    None
        The function completes through its side effects.
    """

    async def good_handler(backend, *, name: str) -> dict[str, str]:
        """Exercise the good handler test helper.

        Parameters
        ----------
        backend : Any
            Backend instance used to perform the operation.
        name : str
            Name of the object, module, or setting being processed.

        Returns
        -------
        dict[str, str]
            Mapping containing the operation result.
        """
        return {"backend": backend.label, "name": name}

    async def no_backend() -> dict[str, str]:
        """Exercise the no backend test helper.

        Returns
        -------
        dict[str, str]
            Mapping containing the operation result.
        """
        return {}

    async def varargs_handler(backend, *names: str) -> dict[str, str]:
        """Exercise the varargs handler test helper.

        Parameters
        ----------
        backend : Any
            Backend instance used to perform the operation.
        names : str
            Object names supplied to the helper.

        Returns
        -------
        dict[str, str]
            Mapping containing the operation result.
        """
        return {}

    async def missing_annotation(backend, *, name) -> dict[str, str]:
        """Exercise the missing annotation test helper.

        Parameters
        ----------
        backend : Any
            Backend instance used to perform the operation.
        name : Any
            Name of the object, module, or setting being processed.

        Returns
        -------
        dict[str, str]
            Mapping containing the operation result.
        """
        return {}

    good = DomainTool(DomainToolSpec("good_tool", "Good tool"), good_handler)
    duplicate = DomainTool(DomainToolSpec("good_tool", "Duplicate"), good_handler)
    live = DomainTool(
        DomainToolSpec("live_tool", "Live tool"), good_handler, requires_live_session=True
    )

    server = _server(BaseFakeBackend(connected=True), expose_tools=())
    with pytest.raises(TypeError, match="DomainTool instances"):
        server._register_domain_tools([object()])
    with pytest.raises(ValueError, match="duplicate"):
        server._register_domain_tools([good, duplicate])
    with pytest.raises(TypeError, match="at least"):
        server._register_domain_tools([DomainTool(DomainToolSpec("bad", "Bad"), no_backend)])
    with pytest.raises(TypeError, match="keyword-only"):
        server._register_domain_tools(
            [DomainTool(DomainToolSpec("varargs", "Bad"), varargs_handler)]
        )
    with pytest.raises(TypeError, match="missing a type annotation"):
        server._register_domain_tools(
            [DomainTool(DomainToolSpec("missing", "Bad"), missing_annotation)]
        )

    async def scenario():
        """Run the test scenario helper.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        offline = _server(BaseFakeBackend(connected=False), expose_tools=())
        offline._register_domain_tools([live])
        assert _structured(await offline.call_tool("live_tool", {"name": "x"}))["ok"] is False

        online = _server(BaseFakeBackend(connected=True), expose_tools=())
        online._register_domain_tools([good])
        assert _structured(await online.call_tool("good_tool", {"name": "x"})) == {
            "backend": "base fake",
            "name": "x",
        }

    asyncio.run(scenario())


def test_select_named_objects_from_mapping_handles_aliases_and_missing_collection():
    """Verify that select named objects from mapping handles aliases and missing collection.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    named = {
        "setup/boundary-conditions/wall": ["hot-wall", "cold-wall", "cold-wall-shadow", "ambient"],
        "setup.boundary_conditions.velocity_inlet": ["inlet-1"],
    }

    assert select_named_objects_from_mapping(
        named,
        collection="setup.boundary_conditions.wall",
        pattern="*wall*",
        include_shadows=False,
        exclude=["hot-*"],
    ) == {
        "collection": "setup.boundary_conditions.wall",
        "pattern": "*wall*",
        "include_shadows": False,
        "exclude": ["hot-*"],
        "names": ["cold-wall"],
        "count": 1,
    }
    missing = select_named_objects_from_mapping(named, collection="setup.unknown")
    assert missing["names"] == []
    assert missing["available_collections"] == sorted(named)


@pytest.mark.asyncio
async def test_disconnect_waits_for_connect_before_clearing_active_route():
    """A concurrent disconnect must close the session that is still connecting."""
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowBackend(BaseFakeBackend):
        async def connect(self, **kwargs):
            entered.set()
            await release.wait()
            return await super().connect(**kwargs)

    backend = SlowBackend(connected=False)
    server = _server(backend, expose_tools=("connect", "disconnect"))
    connecting = asyncio.create_task(server.call_tool("connect", {}))
    await entered.wait()
    disconnecting = asyncio.create_task(server.call_tool("disconnect", {}))
    await asyncio.sleep(0)
    assert not disconnecting.done()
    release.set()
    await asyncio.gather(connecting, disconnecting)
    assert not backend.connected
    assert server._active_kind is None
