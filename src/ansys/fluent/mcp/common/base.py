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

"""Base MCP server for a Fluids leaf (geometry/mesh/solve/post).

A leaf:

- Picks one or more `Backend` instances at construction time.
- Inherits from `PyAnsysBaseMCP` so it can be registered alongside other
  PyAnsys MCP servers at the organisation level.
- Auto-registers the standard deterministic tool surface:
    ``session_status``, ``connect``, ``disconnect``,
    ``list_named_objects``, ``get_state``, ``get_targeted_context``,
    ``run_code``, ``validate_code``, ``screenshot``, and others
    (see ALL_TOOLS).

Concrete leaves only have to declare which tools to *expose* (subset of the
above) so we keep the MCP surface lean per leaf.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Iterable, Optional

from ansys.common.mcp.server import PyAnsysBaseMCP

from ansys.fluent.mcp.common.backend import Backend
from ansys.fluent.mcp.common.connection_guard import (
    require_connected_result,
    with_connection_recovery,
)
from ansys.fluent.mcp.common.errors import (
    BackendUnavailableError,
    InvalidArgumentsError,
    NotConnectedError,
    not_connected_payload,
    typed_guard,
)
from ansys.fluent.mcp.common.file_handlers import normalize_path_for_fluent
from ansys.fluent.mcp.common.host import (
    HostContract,
    annotations_for,
    host_contract_enabled,
    set_resolved_annotations,
)
from ansys.fluent.mcp.common.models import ConnectResult, SessionStatus

logger = logging.getLogger("ansys.fluent.mcp.base")


# Module-level registry of factories that contribute default ``run_code``
# observers to every :class:`FluidsLeafMCP` constructed in this process.
#
# Each factory is a zero-arg callable returning either an observer (any
# callable accepting ``code=``/``result=``/``error=`` kwargs) or ``None``
# to skip. Factories are invoked once per leaf during ``__init__`` and any
# returned observer is registered via ``register_run_code_observer``.
#
# This is the inversion that lets an optional higher-level agent layer
# attach its learning observer to MCP leaves without the leaves importing
# any agent code. That agent layer (when installed) registers a factory
# at import time. Standalone MCP installs leave the list empty and the
# observer is simply never attached — exactly the desired behavior. The
# dependency direction is strictly one-way: this package never imports
# or names the agent layer.
_OBSERVER_FACTORIES: list[Callable[[], Optional[Callable[..., Any]]]] = []


def register_run_code_observer_factory(
    factory: Callable[[], Optional[Callable[..., Any]]],
) -> None:
    """Register a factory that produces a default ``run_code`` observer.

    The factory is called when each :class:`FluidsLeafMCP` is
    constructed. A returned observer is registered via
    ``register_run_code_observer``; a ``None`` return is a no-op. Used
    by the agent layer to wire its learning observer into MCP leaves
    without coupling the MCP layer to agent imports.

    Parameters
    ----------
    factory : Callable[[], Optional[Callable[..., Any]]]
        Factory that creates an observer for a run-code request.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    if not callable(factory):
        raise TypeError("factory must be callable")
    _OBSERVER_FACTORIES.append(factory)


# Default tool catalog — leaves cherry-pick the ones they want to expose.
ALL_TOOLS = (
    "session_status",
    "connect",
    "disconnect",
    "list_named_objects",
    "find_named_object",
    "select_named_objects",
    "find_api",
    "get_state",
    "get_targeted_context",
    "get_help",
    "solver_status",
    "run_code",
    "validate_code",
    "screenshot",
    "manage_component",
    "summarize_setup",
    "simulation_report",
)

#: Leaf names that correspond to a single product.
_PRODUCT_LEAVES = frozenset({"solve", "geometry", "mesh", "post"})


class FluidsLeafMCP(PyAnsysBaseMCP):
    """Base server class for a single Fluids MCP leaf."""

    def run(
        self, transport: str = "stdio", host: str | None = None, port: int | None = None
    ) -> None:
        """Start the MCP server via the parent implementation when available.

        Parameters
        ----------
        transport : str
            Transport name used to run the MCP server.
        host : str | None
            Host interface used by the server transport.
        port : int | None
            Port used by the selected server transport.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        parent_run = getattr(super(), "run", None)
        if not callable(parent_run):
            raise RuntimeError(
                "PyAnsys MCP runtime is unavailable: missing base `run()` implementation. "
                "Install `ansys-common-mcp` (or equivalent runtime dependency) to launch this server."  # noqa: E501
            )
        if transport == "stdio":
            parent_run(transport="stdio")
            return
        parent_run(transport="http", host=host or "127.0.0.1", port=port or 8000)

    leaf_name: str = "fluids"
    default_backend_kind: Optional[str] = None
    cache_ttl_seconds: float = 30.0

    #: Short label used to name component lifecycle tools.
    #: Each leaf sets this to match its managed component:
    #: ``"prepare"`` (geometry), ``"mesh"`` (Prime), ``"fluent"`` (Fluent).
    component_label: str = ""

    #: Apply the host contract (trace ids, timeout-safe long operations,
    #: error guidance) to every registered tool. Subclasses that apply their
    #: own equivalent contract set this to ``False``.
    host_contract: bool = True

    def __init__(
        self,
        *,
        backends: dict[str, Backend],
        expose_tools: Iterable[str] = ALL_TOOLS,
        name: Optional[str] = None,
        **fastmcp_kwargs: Any,
    ) -> None:
        """Initialize the FluidsLeafMCP instance.

        Parameters
        ----------
        backends : dict[str, Backend]
            Backend instances available to the leaf server.
        expose_tools : Iterable[str]
            Whether MCP tools should be registered on the server.
        name : Optional[str]
            Name of the object, module, or setting being processed.
        fastmcp_kwargs : Any
            Keyword arguments forwarded when constructing the FastMCP server.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        if not backends:
            raise ValueError(f"{self.leaf_name}: at least one backend is required")
        # The Fluent leaf executes Python against the in-process PyFluent
        # solver session (``run_code`` -> backend), never the base framework's
        # ``PersistentPythonSession`` subprocess. Pass ``need_python=False`` so
        # that subprocess is not launched (it would otherwise start on every
        # server boot and sit idle). Callers may still override via kwargs.
        fastmcp_kwargs.setdefault("need_python", False)
        super().__init__(name=name or f"ansys-fluent-mcp-{self.leaf_name}", **fastmcp_kwargs)
        self._backends = backends
        self._active_kind: Optional[str] = None
        self._lifecycle_lock = asyncio.Lock()
        self._exposed = set(expose_tools)
        if self.default_backend_kind and self.default_backend_kind in backends:
            self._active_kind = self.default_backend_kind

        # Opt-in observers invoked after every ``run_code`` tool call.
        # External packages may register their own observers via
        # :func:`register_run_code_observer_factory`.
        self._run_code_observers: list[Callable[..., Awaitable[None] | None]] = []

        # Host contract: trace ids, timeout-safe long operations and error
        # guidance on every tool (see ``ansys.fluent.mcp.common.host``).
        self._host_contract: HostContract | None = (
            HostContract(self.tool_annotations_for)
            if self.host_contract and host_contract_enabled()
            else None
        )

        self._register_tools()
        self._register_resources()
        self._attach_default_observers()

    def tool_annotations_for(self, tool_name: str | None) -> dict[str, bool] | None:
        """Return the MCP behavior hints for one of this server's tools.

        Subclasses that register extra tools override this to add their own
        classifications and fall back to ``super()``.

        Parameters
        ----------
        tool_name : str | None
            Registered tool name.

        Returns
        -------
        dict[str, bool] | None
            ``readOnlyHint`` / ``destructiveHint`` / ``idempotentHint``, or
            ``None`` when the tool is unclassified.
        """
        return annotations_for(tool_name)

    def tool(self, name_or_fn: Any = None, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        """Register a tool with behavior hints and the host contract applied.

        Accepts every ``FastMCP.tool`` call form (direct function, positional
        or keyword name, bare decorator). Explicit ``annotations=`` win.

        Parameters
        ----------
        name_or_fn : Any
            Tool name or function, as accepted by ``FastMCP.tool``.
        args : Any
            Positional arguments forwarded to ``FastMCP.tool``.
        kwargs : Any
            Keyword arguments forwarded to ``FastMCP.tool``.

        Returns
        -------
        Any
            Whatever ``FastMCP.tool`` returns (a decorator or a tool).
        """
        parent_tool = super().tool
        explicit = kwargs.get("name") or (name_or_fn if isinstance(name_or_fn, str) else None)

        def _prepare(fn: Callable[..., Any]) -> Callable[..., Any]:
            tool_name = str(explicit or getattr(fn, "__name__", ""))
            if kwargs.get("annotations") is None:
                kwargs["annotations"] = self.tool_annotations_for(tool_name)
            fn = with_connection_recovery(
                fn, lambda **recovery: self._not_connected(tool_name, **recovery)
            )
            contract = getattr(self, "_host_contract", None)
            return contract.wrap(tool_name, fn) if contract is not None else fn

        if callable(name_or_fn):
            fn = _prepare(name_or_fn)
            return parent_tool(fn, *args, **kwargs)

        def _decorate(fn: Callable[..., Any]) -> Any:
            prepared = _prepare(fn)
            return parent_tool(name_or_fn, *args, **kwargs)(prepared)

        return _decorate

    def _attach_default_observers(self) -> None:
        """Retrieve and attach default ``run_code`` observers from registered factories.

        Invoke every factory in ``_OBSERVER_FACTORIES`` and attach the
        observers it produces. Factory failures are logged and swallowed
        so a misbehaving plug-in cannot break MCP startup.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        for factory in list(_OBSERVER_FACTORIES):
            try:
                observer = factory()
            except Exception:  # plug-in failures must never break MCP startup
                logger.debug(
                    "run_code observer factory %r failed",
                    factory,
                    exc_info=True,
                )
                continue
            if observer is None:
                continue
            try:
                self.register_run_code_observer(observer)
            except Exception:
                logger.debug(
                    "registering observer from factory %r failed",
                    factory,
                    exc_info=True,
                )

    # ------------------------------------------------------------------
    # PyAnsysBaseMCP abstract methods
    # ------------------------------------------------------------------

    def product_startup(self) -> None:  # noqa: D401
        """No persistent product session at startup. Backends connect lazily.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        logger.info("%s leaf starting (backends=%s)", self.leaf_name, list(self._backends))

    def product_cleanup(self) -> None:  # noqa: D401
        """Disconnect any active backend at shutdown.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        for backend in self._backends.values():
            try:
                # Backends own their own event loop integration; best-effort sync close.
                close = getattr(backend, "close_sync", None)
                if callable(close):
                    close()
            except Exception:
                logger.exception("Error during cleanup of %s", backend.label)

    # ------------------------------------------------------------------
    # Backend selection
    # ------------------------------------------------------------------

    @property
    def backend(self) -> Backend:
        """Return the active backend instance.

        Returns
        -------
        Backend
            Backend produced by the operation.
        """
        if self._active_kind is None:
            # If only one backend is configured, treat it as active.
            if len(self._backends) == 1:
                self._active_kind = next(iter(self._backends))
            else:
                raise NotConnectedError(
                    f"No active backend selected for {self.leaf_name}. Call `connect`."
                )
        return self._backends[self._active_kind]

    # ------------------------------------------------------------------
    # Tool registration
    # ------------------------------------------------------------------

    def _register_tools(self) -> None:
        """Register the tools exposed by this MCP leaf.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        if "session_status" in self._exposed:
            self._tool_session_status()
        if "connect" in self._exposed:
            self._tool_connect()
        if "disconnect" in self._exposed:
            self._tool_disconnect()
        if "list_named_objects" in self._exposed:
            self._tool_list_named_objects()
        if "find_named_object" in self._exposed:
            self._tool_find_named_object()
        if "select_named_objects" in self._exposed:
            self._tool_select_named_objects()
        if "find_api" in self._exposed:
            self._tool_find_api()
        if "get_state" in self._exposed:
            self._tool_get_state()
        if "get_targeted_context" in self._exposed:
            self._tool_get_targeted_context()
        if "get_help" in self._exposed:
            self._tool_get_help()
        if "solver_status" in self._exposed:
            self._tool_solver_status()
        if "run_code" in self._exposed:
            self._tool_run_code()
        if "validate_code" in self._exposed:
            self._tool_validate_code()
        if "screenshot" in self._exposed:
            self._tool_screenshot()
        if "manage_component" in self._exposed:
            self._tool_manage_component()
        if "summarize_setup" in self._exposed:
            self._tool_summarize_setup()
        if "simulation_report" in self._exposed:
            self._tool_simulation_report()

    # ------------------------------------------------------------------
    # Domain-tool registration
    # ------------------------------------------------------------------

    def _register_domain_tools(self, domain_tools: "Iterable[Any]") -> None:
        """Register a list of :class:`DomainTool` instances on this leaf.

        Each leaf's ``_register_tools()`` override calls this with the
        list returned from its ``get_<leaf>_domain_tools()`` catalog.
        Domain tools register unconditionally; a leaf that wants to ship
        a smaller surface returns a smaller list. Names must be unique
        across every call on the same leaf.

        Domain tools are intentionally outside the ``ALL_TOOLS`` /
        ``expose_tools`` filter machinery, which curates the general
        leaf surface (screenshot, manage_component, ``run_code``,
        ``validate_code``, …).

        Parameters
        ----------
        domain_tools : 'Iterable[Any]'
            Domain tool definitions to register on the MCP server.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        from ansys.fluent.mcp.common.domain_tools import DomainTool  # noqa: PLC0415

        # Track names across calls so a subclass adding tools cannot shadow a built-in one.
        seen: set[str] = self.__dict__.setdefault("_domain_tool_names", set())
        for tool in domain_tools:
            if not isinstance(tool, DomainTool):
                raise TypeError(
                    f"_register_domain_tools expects DomainTool instances, got {type(tool).__name__}"  # noqa: E501
                )
            if tool.spec.name in seen:
                raise ValueError(
                    f"duplicate domain tool name on leaf {self.leaf_name!r}: {tool.spec.name!r}"
                )
            seen.add(tool.spec.name)
            self._register_one_domain_tool(tool)

    def _resolve_domain_backend(self, tool: "Any") -> "Any":
        """Resolve domain backend.

        Parameters
        ----------
        tool : 'Any'
            Tool callable being wrapped or registered.

        Returns
        -------
        'Any'
            'Any' produced by the operation.
        """
        return self.backend

    async def _ensure_live_session(self, backend: "Any") -> bool:
        """Return True when ``backend`` is connected, attaching once if allowed.

        A tool that needs a live session should not fail just because the
        caller never issued ``connect`` while the product is already running.
        Inspection tools call this implicitly, so the attach must have NO
        side effects: it may only attach to a component that is already up,
        and must never launch an application or activate a component.

        A method name alone cannot promise that: existing
        ``attach_if_running()`` implementations activate components or
        launch processes. The backend must therefore opt in explicitly by
        setting ``attach_if_running_is_passive = True``. Without that
        declaration no attach is attempted and the tool reports
        ``not_connected``, leaving the mutating step to an explicit
        ``connect`` / ``manage_*`` call.

        Parameters
        ----------
        backend : Any
            Backend resolved for the tool.

        Returns
        -------
        bool
            Whether the backend is connected after the optional attach.
        """

        def _connected() -> bool:
            try:
                return bool(backend.is_connected())
            except Exception:  # noqa: BLE001 - any probe failure means "not connected"
                return False

        if _connected():
            return True
        attach = getattr(backend, "attach_if_running", None)
        # ``is True`` (not truthiness) so a mock or an unrelated attribute
        # can never enable an implicit, possibly mutating, attach.
        passive = getattr(backend, "attach_if_running_is_passive", False) is True
        if callable(attach) and passive:
            try:
                await attach()
            except Exception:  # noqa: BLE001 - attach is best-effort
                logger.debug("attach_if_running failed", exc_info=True)
        return _connected()

    # ---- not-connected recovery guidance ------------------------------

    def _owning_product(self, tool_name: str) -> Optional[str]:
        """Return the product that owns ``tool_name`` on this server.

        A product leaf owns all of its tools. A multi-product server
        (for example a unified server routing tools to per-product
        backends) must override this so recovery guidance names the
        tool's real product rather than a default.

        Parameters
        ----------
        tool_name : str
            Registered tool name.

        Returns
        -------
        Optional[str]
            ``solve`` / ``geometry`` / ``mesh`` / ``post``, or ``None`` when
            this server cannot tell.
        """
        del tool_name
        return self.leaf_name if self.leaf_name in _PRODUCT_LEAVES else None

    def _connect_recovery(self, product: Optional[str]) -> dict[str, Any]:
        """Return the reconnect instruction valid for THIS server's ``connect``.

        The base ``connect`` tool accepts ``backend_kind`` and
        ``connect_kwargs``; it has no ``product`` argument. Servers that
        register a different ``connect`` signature must override this.

        Parameters
        ----------
        product : Optional[str]
            Product that owns the failing tool (unused by the base leaf,
            whose ``connect`` is already product-scoped).

        Returns
        -------
        dict[str, Any]
            ``next_tool`` plus either ``next_tool_args`` or
            ``next_tool_choices``.
        """
        del product
        if "connect" not in self._exposed:
            return {"next_tool": None, "next_tool_args": None}
        kind = self._active_kind or self.default_backend_kind
        if kind is None and len(self._backends) == 1:
            kind = next(iter(self._backends))
        if kind is not None and kind in self._backends:
            return {"next_tool": "connect", "next_tool_args": {"backend_kind": kind}}
        return {
            "next_tool": "connect",
            "next_tool_args": None,
            "next_tool_choices": [{"backend_kind": k} for k in self._backends],
        }

    def _activate_recovery(self, product: Optional[str]) -> dict[str, Any]:
        """Return the component-activation instruction valid for THIS server.

        Mirrors :meth:`_tool_manage_component`, which registers
        ``manage_<label>`` with a single ``action`` argument.

        Parameters
        ----------
        product : Optional[str]
            Product that owns the failing tool (unused by the base leaf).

        Returns
        -------
        dict[str, Any]
            ``activate_tool`` / ``activate_args``, or an empty dict when the
            server exposes no lifecycle tool.
        """
        del product
        if "manage_component" not in self._exposed:
            return {}
        label = self.component_label or self.leaf_name
        return {"activate_tool": f"manage_{label}", "activate_args": {"action": "activate"}}

    def _not_connected(self, tool_name: str, *, reason: Optional[str] = None) -> dict[str, Any]:
        """Build the ``not_connected`` result with recovery valid on this server.

        Parameters
        ----------
        tool_name : str
            Tool that needed a live session.
        reason : Optional[str]
            Optional underlying cause.

        Returns
        -------
        dict[str, Any]
            Canonical ``not_connected`` payload.
        """
        product = self._owning_product(tool_name)
        return not_connected_payload(
            tool=tool_name,
            product=product,
            reason=reason,
            **self._connect_recovery(product),
            **self._activate_recovery(product),
        )

    def _register_one_domain_tool(self, tool: "Any") -> None:
        """Bind one :class:`DomainTool` to ``self.tool``.

        Synthesizes a thin wrapper whose ``inspect.Signature`` mirrors
        the domain handler minus the leading ``backend`` parameter so
        FastMCP can extract the input schema from the wrapper. Keeping
        the synthesis here means individual domain-tool authors only
        ship a typed coroutine like::

            async def my_tool_impl(
                backend: Backend,
                *,
                material: str,
                condition: str | None = None,
            ) -> dict[str, Any]: ...

        and never have to write a parallel JSON-schema or a wrapper
        function. Captured in its own method so subclasses can wrap
        (cross-product routing on the unified leaf, etc.) without
        re-implementing the loop.

        Parameters
        ----------
        tool : 'Any'
            Tool callable being wrapped or registered.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        from inspect import Parameter, Signature, signature  # noqa: PLC0415
        from typing import get_type_hints  # noqa: PLC0415

        spec = tool.spec
        handler = tool.handler
        requires_session = tool.requires_live_session

        handler_sig = signature(handler)
        handler_params = list(handler_sig.parameters.values())
        if not handler_params:
            raise TypeError(
                f"domain handler {handler!r} must accept at least a "
                f"backend parameter as its first positional argument."
            )
        # Resolve PEP 563 string annotations against the HANDLER's
        # module globals. Every domain-tool impl module ships
        # ``from __future__ import annotations``, so the raw
        # ``Parameter.annotation`` we read above is a string like
        # ``"PorousGeometry"`` — a name only defined in the impl
        # module's globals. Copying that string into the synthesized
        # wrapper would later force pydantic/FastMCP to resolve it
        # against ``common/base.py``'s globals, where the alias does
        # not exist — producing a ``NameError`` on first call and,
        # because schema derivation happens during MCP server start-up,
        # crashing standalone leaf deployment. ``get_type_hints``
        # evaluates the strings against the handler's ``__globals__``
        # so the wrapper exposes real type objects.
        try:
            resolved_hints = get_type_hints(handler, include_extras=True)
        except Exception:  # defensive fallback
            resolved_hints = {}
        public_params: list[Parameter] = []
        for p in handler_params[1:]:
            if p.kind in (Parameter.VAR_POSITIONAL, Parameter.VAR_KEYWORD):
                raise TypeError(
                    f"domain handler {handler!r}: parameter {p.name!r} "
                    f"must be keyword-only with an annotation; got "
                    f"kind={p.kind.name}."
                )
            if p.annotation is Parameter.empty:
                raise TypeError(
                    f"domain handler {handler!r}: parameter {p.name!r} "
                    f"is missing a type annotation. FastMCP needs the "
                    f"annotation to derive the input schema."
                )
            annotation = resolved_hints.get(p.name, p.annotation)
            public_params.append(p.replace(annotation=annotation))
        return_annotation = resolved_hints.get(
            "return",
            handler_sig.return_annotation,
        )
        public_sig = Signature(
            parameters=public_params,
            return_annotation=return_annotation,
        )

        async def _domain_impl(**kwargs: Any) -> dict[str, Any]:
            """Execute a domain tool against the resolved backend.

            Parameters
            ----------
            kwargs : Any
                Keyword arguments forwarded to the callable.

            Returns
            -------
            dict[str, Any]
                Mapping containing the operation result.
            """
            try:
                backend = self._resolve_domain_backend(tool)
            except NotConnectedError as exc:
                # No backend selected for the tool's product yet.
                return self._not_connected(spec.name, reason=exc.message)
            if requires_session and not await self._ensure_live_session(backend):
                return self._not_connected(spec.name)
            return await handler(backend, **kwargs)

        annotations = {p.name: p.annotation for p in public_params}
        if return_annotation is not Signature.empty:
            annotations["return"] = return_annotation

        _domain_impl.__signature__ = public_sig  # type: ignore[attr-defined]
        _domain_impl.__name__ = spec.name
        _domain_impl.__qualname__ = f"{type(self).__name__}._domain.{spec.name}"
        _domain_impl.__doc__ = spec.description
        set_resolved_annotations(_domain_impl, annotations)

        guarded = typed_guard(_domain_impl)
        # ``typed_guard`` uses ``functools.wraps`` which copies
        # ``__signature__`` and ``__annotations__`` so the registered
        # tool exposes the correct schema to FastMCP. Re-apply for
        # safety in case wraps is patched.
        guarded.__signature__ = public_sig  # type: ignore[attr-defined]
        set_resolved_annotations(guarded, annotations)
        self.tool(name=spec.name, description=spec.description)(guarded)

    # ------------------------------------------------------------------
    # Resource registration
    # ------------------------------------------------------------------

    def _register_resources(self) -> None:
        """Register MCP resources (toolsets://definition for conductor discovery).

        Returns
        -------
        None
            The function completes through its side effects.
        """
        toolsets_fn = self.build_toolsets

        @self.resource(
            "toolsets://definition",
            name="toolsets_definition",
            description="Toolset definitions for PyAnsysMCPService discovery.",
            mime_type="application/json",
        )
        def get_toolsets() -> list[dict[str, Any]]:
            """Return the toolsets.

            Returns
            -------
            list[dict[str, Any]]
                List of results produced by the operation.
            """
            return toolsets_fn()

    # ------------------------------------------------------------------
    # Toolset definitions
    # ------------------------------------------------------------------

    #: Master catalog mapping every tool to its logical toolset.
    #: Tools not listed here fall into the "general" toolset.
    _TOOLSET_CATALOGUE: dict[str, dict[str, Any]] = {
        "connection": {
            "description": ("Tools for connecting to and managing solver sessions."),
            "skill": (
                "Call session_status to check connectivity before other "
                "operations. Use connect with host/port from discovery or "
                "let it auto-launch. Call disconnect for graceful cleanup."
            ),
            "tools": ["session_status", "connect", "disconnect"],
        },
        "api-discovery": {
            "description": ("Tools for exploring and searching the settings API tree."),
            "skill": (
                "Use find_api for keyword-based semantic search. Use "
                "get_help for docstrings and child listings. Use "
                "get_targeted_context for batched disambiguation "
                "(active-status + state + allowed-values + child-names "
                "in one round-trip)."
            ),
            "tools": [
                "find_api",
                "get_help",
                "get_targeted_context",
            ],
        },
        "named-objects": {
            "description": (
                "Tools for discovering and selecting named objects in the settings tree."
            ),
            "skill": (
                "Use list_named_objects to enumerate a collection. "
                "Use find_named_object to resolve a symbolic name "
                "across all collections. Use select_named_objects "
                "for glob-based filtering."
            ),
            "tools": [
                "list_named_objects",
                "find_named_object",
                "select_named_objects",
            ],
        },
        "state-inspection": {
            "description": ("Tools for reading live solver state and status."),
            "skill": (
                "Use get_state to read current values of settings "
                "paths (confirm active first via get_targeted_context). "
                "Use solver_status for iteration count, residuals, and "
                "convergence info."
            ),
            "tools": ["get_state", "solver_status"],
        },
        "code-execution": {
            "description": ("Tools for executing Python code against the live solver session."),
            "skill": (
                "Use run_code to execute Python against the live solver "
                "session — both read-only introspection and state "
                "mutations. Use validate_code for pre-flight syntax "
                "and safety checks before executing."
            ),
            "tools": ["run_code", "validate_code"],
        },
        "visualization": {
            "description": ("Tools for capturing visual output from the solver."),
            "skill": (
                "Use screenshot to capture the current model view as a PNG image for the user."
            ),
            "tools": ["screenshot"],
        },
        "error-handling": {
            "description": ("Tools for diagnosing and remediating product-specific errors."),
            "skill": (
                "Use error_remediation when a product workflow returns a "
                "structured failure that needs a recovery recommendation."
            ),
            "tools": ["error_remediation"],
        },
        "component-lifecycle": {
            "description": (
                "Tools for activating, deactivating, updating, and "
                "refreshing solver/component instances in managed backend."
            ),
            "skill": (
                "Use manage_component with action='activate' to start "
                "a solver component. Use action='deactivate' to cleanly "
                "stop it. Use action='update' to apply pending config "
                "changes. Use action='refresh' to force a state reload."
            ),
            "tools": ["manage_component"],
        },
        "reports": {
            "description": (
                "Tools for generating simulation reports and retrieving setup summaries from the solver."  # noqa: E501
            ),
            "skill": (
                "Use summarize_setup to get the full solver setup "
                "overview (models, materials, BCs, solver settings, "
                "schemes, limits) in one call. Use simulation_report "
                "to generate or export a rich simulation report "
                "(HTML, PDF, or PPTX). Prefer summarize_setup as the "
                "first tool when the user asks 'show me my setup' or "
                "'what is configured?'."
            ),
            "tools": ["summarize_setup", "simulation_report"],
        },
    }

    def build_toolsets(self) -> list[dict[str, Any]]:
        """Build toolset definitions filtered to the tools this leaf exposes.

        Returns a list of toolset dicts conforming to the
        PyAnsysMCPService conductor contract.

        Returns
        -------
        list[dict[str, Any]]
            Mapping containing the operation result.
        """
        result: list[dict[str, Any]] = []
        for name, defn in self._TOOLSET_CATALOGUE.items():
            # Only include tools that are actually exposed by this leaf.
            active_tools = [t for t in defn["tools"] if t in self._exposed]
            if not active_tools:
                continue
            result.append(
                {
                    "name": name,
                    "description": defn["description"],
                    "skill": defn["skill"],
                    "tools": active_tools,
                }
            )
        return result

    # ---- session.status -----------------------------------------------

    def _tool_session_status(self) -> None:
        """Register the ``session_status`` MCP tool.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        leaf = self.leaf_name

        @self.tool(
            name="session_status",
            description=(
                f"Report whether the {leaf} leaf has an active backend. "
                "Safe to call before `connect`. Returns the connected endpoint, "
                "backend kind, and the list of tools available in the current state."
            ),
        )
        @typed_guard
        async def session_status() -> SessionStatus:
            """Return the current leaf session status.

            Returns
            -------
            SessionStatus
                SessionStatus produced by the operation.
            """
            if self._active_kind is None:
                return SessionStatus(leaf=leaf, connected=False, notes=["No backend connected."])
            return self._backends[self._active_kind].status(leaf)

    # ---- connect / disconnect ----------------------------------------

    def _tool_connect(self) -> None:
        """Register the ``connect`` MCP tool.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        leaf = self.leaf_name
        kinds = list(self._backends)

        @self.tool(
            name="connect",
            description=(
                f"Connect the {leaf} leaf to a backend. "
                f"Available backend kinds: {kinds}. "
                "Pass `backend_kind` to choose, or omit it to auto-select. "
                "Backend-specific options (url/token/ip/port/...) go in "
                "`connect_kwargs` as a dict and are forwarded to the backend."
            ),
        )
        @typed_guard
        async def connect(
            backend_kind: Optional[str] = None,
            connect_kwargs: Optional[dict[str, Any]] = None,
        ) -> ConnectResult:
            """Connect to the configured backend or service.

            Parameters
            ----------
            backend_kind : Optional[str]
                Backend kind to select from the configured backends.
            connect_kwargs : Optional[dict[str, Any]]
                Backend-specific connection options.

            Returns
            -------
            ConnectResult
                ConnectResult produced by the operation.
            """
            kind = backend_kind or self.default_backend_kind
            if kind is None:
                if len(self._backends) == 1:
                    kind = next(iter(self._backends))
                else:
                    raise InvalidArgumentsError(f"backend_kind is required; choose one of {kinds}")
            if kind not in self._backends:
                raise InvalidArgumentsError(f"Unknown backend_kind '{kind}'; available: {kinds}")
            async with self._lifecycle_lock:
                if self._active_kind is not None and self._active_kind != kind:
                    previous = self._backends[self._active_kind]
                    if previous.is_connected():
                        raise InvalidArgumentsError(
                            "Disconnect the active backend before switching backends."
                        )
                backend = self._backends[kind]
                result = await backend.connect(**(connect_kwargs or {}))
                if result.status == "ok":
                    self._active_kind = kind
                return result

    def _tool_disconnect(self) -> None:
        """Register the ``disconnect`` MCP tool.

        Returns
        -------
        None
            The function completes through its side effects.
        """

        @self.tool(
            name="disconnect",
            description=f"Disconnect the {self.leaf_name} leaf's active backend.",
        )
        @typed_guard
        async def disconnect() -> dict[str, Any]:
            """Close resources for the FluidsLeafMCP object.

            Returns
            -------
            dict[str, Any]
                Mapping containing the operation result.
            """
            async with self._lifecycle_lock:
                if self._active_kind is None:
                    return {"status": "ok", "message": "No active backend."}
                backend = self._backends[self._active_kind]
                await backend.disconnect()
                self._active_kind = None
            return {"status": "ok"}

    # ---- live model context -------------------------------------------

    def _tool_list_named_objects(self) -> None:
        """Register the ``list_named_objects`` MCP tool.

        Returns
        -------
        None
            The function completes through its side effects.
        """

        @self.tool(
            name="list_named_objects",
            description=(
                "Return a mapping of named-object collection paths to the names "
                "of the objects currently defined. Cached briefly to keep "
                "follow-up tool calls fast. Supports pagination: pass "
                "`limit` (>=1) and optional `offset` to slice the names "
                "of each collection; the response then includes a "
                "`_pagination` envelope with the original totals so the "
                "caller can request more if needed."
            ),
        )
        @typed_guard
        async def list_named_objects(
            limit: Optional[int] = None,
            offset: int = 0,
        ) -> dict[str, Any]:
            """List named objects entries.

            Parameters
            ----------
            limit : Optional[int]
                Maximum number of items to include in the response.
            offset : int
                Number of items to skip before returning results.

            Returns
            -------
            dict[str, Any]
                Mapping containing the operation result.
            """
            mapping = await self.backend.list_named_objects()
            if limit is None and not offset:
                return mapping
            if limit is not None and limit < 1:
                raise InvalidArgumentsError("limit must be >= 1")
            if offset < 0:
                raise InvalidArgumentsError("offset must be >= 0")
            sliced: dict[str, Any] = {}
            totals: dict[str, int] = {}
            for coll, names in (mapping or {}).items():
                lst = list(names or [])
                totals[coll] = len(lst)
                end = offset + limit if limit is not None else None
                sliced[coll] = lst[offset:end]
            sliced["_pagination"] = {
                "offset": offset,
                "limit": limit,
                "totals": totals,
                "truncated": any(
                    (limit is not None and totals[c] > offset + limit) or offset > 0 for c in totals
                ),
            }
            return sliced

    def _tool_find_named_object(self) -> None:
        """Register the ``find_named_object`` MCP tool.

        Returns
        -------
        None
            The function completes through its side effects.
        """

        @self.tool(
            name="find_named_object",
            description=(
                "Resolve a symbolic name (e.g. 'inlet-1') across every "
                "named-object collection. Returns a list of "
                "{collection_path, name, exact} matches sorted with exact "
                "matches first. Use this BEFORE generating code so you know "
                "which collection (wall, velocity_inlet, fluid_zone, ...) "
                "the user actually meant; if multiple matches exist, ask a "
                "follow-up question."
            ),
        )
        @typed_guard
        async def find_named_object(name: str) -> list[dict[str, Any]]:
            """Find named object entries.

            Parameters
            ----------
            name : str
                Name of the object, module, or setting being processed.

            Returns
            -------
            list[dict[str, Any]]
                List of results produced by the operation.
            """
            return await self.backend.find_named_object(name)

    def _tool_select_named_objects(self) -> None:
        """Register the ``select_named_objects`` MCP tool.

        Returns
        -------
        None
            The function completes through its side effects.
        """

        @self.tool(
            name="select_named_objects",
            description=(
                "Expand a glob pattern over a single named-object "
                "collection and return the matching names. Use this "
                "instead of hand-curating a long list when the user "
                "asks for 'all walls', 'all inlets', 'every fluid "
                "zone', etc. — it makes the selection reproducible "
                "and survives mesh re-numbering. "
                "Arguments: `collection` is the dotted path of the "
                "named-object family (e.g. "
                "'setup.boundary_conditions.wall'); `pattern` is a "
                "Unix-shell-style glob (default `*`); "
                "`include_shadows` defaults to `true` for thermal "
                "wall families and is silently ignored for "
                "non-wall collections; `exclude` is an optional "
                "list of glob patterns to subtract from the result."
            ),
        )
        @typed_guard
        async def select_named_objects(
            collection: str,
            pattern: str = "*",
            include_shadows: bool = True,
            exclude: Optional[list[str]] = None,
        ) -> dict[str, Any]:
            """Select named objects entries.

            Parameters
            ----------
            collection : str
                Named-object collection or API collection to search.
            pattern : str
                Glob pattern used to select matching names.
            include_shadows : bool
                Whether shadow objects should be included when applicable.
            exclude : Optional[list[str]]
                Optional glob patterns to remove from the selection.

            Returns
            -------
            dict[str, Any]
                Mapping containing the operation result.
            """
            named = await self.backend.list_named_objects()
            return select_named_objects_from_mapping(
                named,
                collection=collection,
                pattern=pattern,
                include_shadows=include_shadows,
                exclude=exclude,
            )

    def _tool_find_api(self) -> None:
        """Register the ``find_api`` MCP tool.

        Returns
        -------
        None
            The function completes through its side effects.
        """

        @self.tool(
            name="find_api",
            description=(
                "Retrieve candidate Fluent settings APIs for a query "
                "from the bundled Fluent API catalog. Returns ranked hits as "
                "{path, kind, score, ...}. Use this to locate the "
                "dotted settings path that implements a property "
                "(e.g. query 'temperature wall' returns "
                "wall.thermal.temperature variants). Optional filters: "
                "`kinds` (Parameter, Command, Object, Group) and "
                "`under` (path prefix to scope the search)."
            ),
        )
        @typed_guard
        async def find_api(
            query: str,
            top_k: int = 10,
            kinds: Optional[list[str]] = None,
            under: Optional[str] = None,
            compact: bool = False,
        ) -> list[dict[str, Any]]:
            """Find api entries.

            Parameters
            ----------
            query : str
                Search text or user request to evaluate.
            top_k : int
                Maximum number of results to return.
            kinds : Optional[list[str]]
                Optional result kinds used to narrow the operation.
            under : Optional[str]
                Optional path prefix used to scope the operation.
            compact : bool
                Whether to enable or apply compact.

            Returns
            -------
            list[dict[str, Any]]
                List of results produced by the operation.
            """
            hits = await self.backend.find_api(
                query,
                top_k=top_k,
                kinds=kinds,
                under=under,
            )
            if not compact:
                return hits
            # Slim envelope: only the fields the agent uses to pick
            # the next call. Drops schema/allowed_values/docstring
            # (~80% of the bytes per hit) so cheap discovery turns
            # don't bloat the prompt cache.
            slim: list[dict[str, Any]] = []
            for h in hits:
                desc = h.get("docstring") or h.get("description") or ""
                if isinstance(desc, str):
                    one_line = desc.strip().split("\n", 1)[0][:160]
                else:
                    one_line = ""
                slim.append(
                    {
                        "path": h.get("path"),
                        "kind": h.get("kind"),
                        "score": h.get("score"),
                        "summary": one_line,
                    }
                )
            return slim

    def _tool_get_state(self) -> None:
        """Register the ``get_state`` MCP tool.

        Returns
        -------
        None
            The function completes through its side effects.
        """

        @self.tool(
            name="get_state",
            description=(
                "Return the current state of the requested settings paths. "
                "If `paths` is omitted, returns the global Fluids state summary. "
                "FAST PATH: pass `key` together with a single collection path "
                "in `paths` (e.g. paths=['setup.boundary_conditions.wall'], "
                "key='outer-wall') to fetch JUST that one named-object slice "
                "without dumping every sibling — saves substantial prompt "
                "tokens on big cases."
            ),
        )
        @typed_guard
        async def get_state(
            paths: Optional[list[str]] = None,
            key: Optional[str] = None,
        ) -> dict[str, Any]:
            """Return the state.

            Parameters
            ----------
            paths : Optional[list[str]]
                Fluent object paths supplied to the operation.
            key : Optional[str]
                Key used to look up or store the associated value.

            Returns
            -------
            dict[str, Any]
                Mapping containing the operation result.
            """
            if key is not None:
                if not paths or len(paths) != 1:
                    raise InvalidArgumentsError(
                        "`key` requires exactly one collection path in `paths`"
                    )
                base = paths[0].rstrip(".")
                if base.endswith("]"):
                    raise InvalidArgumentsError(
                        "`paths[0]` already indexes a named object; drop `key`"
                    )
                if '"' in key or "'" in key or "]" in key or "[" in key:
                    raise InvalidArgumentsError("`key` contains invalid characters")
                paths = [f"{base}[{key}]"]
            return await self.backend.get_state(paths=paths)

    def _tool_get_targeted_context(self) -> None:
        """Register the ``get_targeted_context`` MCP tool.

        Returns
        -------
        None
            The function completes through its side effects.
        """

        @self.tool(
            name="get_targeted_context",
            description=(
                "Fetch active-status, state, named-objects, child-names, and "
                "allowed values for a focused set of paths in a single call. "
                "Use this for fast disambiguation before generating code."
            ),
        )
        @typed_guard
        async def get_targeted_context(
            paths_to_check: list[str],
            named_object_types: Optional[list[str]] = None,
            instance_state_fetch: Optional[list[str]] = None,
        ) -> dict[str, Any]:
            """Return the targeted context.

            Parameters
            ----------
            paths_to_check : list[str]
                Fluent object paths to validate or inspect.
            named_object_types : Optional[list[str]]
                Named-object families that should be considered during lookup.
            instance_state_fetch : Optional[list[str]]
                Whether named-object instance state should be fetched.

            Returns
            -------
            dict[str, Any]
                Mapping containing the operation result.
            """
            return await self.backend.get_targeted_context(
                paths_to_check=paths_to_check,
                named_object_types=named_object_types or [],
                instance_state_fetch=instance_state_fetch or [],
            )

    def _tool_get_help(self) -> None:
        """Register the ``get_help`` MCP tool.

        Returns
        -------
        None
            The function completes through its side effects.
        """

        @self.tool(
            name="get_help",
            description=(
                "Return docstring + child names + allowed values for a "
                "specific Fluent settings path. Use this to confirm "
                "semantics of an ambiguous parameter (e.g. "
                "'turbulent_intensity' vs 'intensity') before emitting code."
            ),
        )
        @typed_guard
        async def get_help(path: str) -> dict[str, Any]:
            """Return the help.

            Parameters
            ----------
            path : str
                Filesystem path or API path to process.

            Returns
            -------
            dict[str, Any]
                Mapping containing the operation result.
            """
            return await self.backend.get_help(path)

    def _tool_solver_status(self) -> None:
        """Register the ``solver_status`` MCP tool.

        Returns
        -------
        None
            The function completes through its side effects.
        """

        @self.tool(
            name="solver_status",
            description=(
                "Return solver readiness summary: initialized, iterations, "
                "residuals, solver_mode (steady|transient). Use this before "
                "deciding whether to emit run_calculation.iterate(...)."
            ),
        )
        @typed_guard
        async def solver_status() -> dict[str, Any]:
            """Return solver status information from the backend.

            Returns
            -------
            dict[str, Any]
                Mapping containing the operation result.
            """
            return await self.backend.solver_status()

    # ---- code execution ------------------------------------------------

    def _tool_run_code(self) -> None:
        """Register the ``run_code`` MCP tool.

        Returns
        -------
        None
            The function completes through its side effects.
        """

        @self.tool(
            name="run_code",
            description=(
                "Execute Python code against the connected PyFluent solver session. "
                "The code runs with `solver` (and `session` alias) pre-injected in "
                "scope — use `solver.settings.<path>` to read or mutate the model. "
                "Returns stdout, stderr, and any `__return__` value. "
                "Prefer `get_state` / `get_targeted_context` for read-only queries."
            ),
        )
        @typed_guard
        async def run_code(code: str):
            """Execute Python code through the backend runtime.

            Parameters
            ----------
            code : str
                Python code or command text to execute or validate.

            Returns
            -------
            None
                The function completes through its side effects.
            """
            if not code or not code.strip():
                raise InvalidArgumentsError("code must be a non-empty string")
            try:
                backend = self.backend
            except NotConnectedError as exc:
                return self._not_connected("run_code", reason=exc.message)
            if not await self._ensure_live_session(backend):
                return self._not_connected("run_code")
            result: Any = None
            error: BaseException | None = None
            try:
                result = await backend.run_code(code)
            except BaseException as exc:  # observers see the raw error
                error = exc
            finally:
                # Belt-and-braces: even backends that don't override
                # `invalidate_live_caches` get them dropped here so the
                # next live tool sees post-mutation state.
                backend.invalidate_live_caches()
                # Notify observers (learning, telemetry, audit) about
                # the call. Observer failures must never break the
                # tool result the caller already received.
                await self._notify_run_code_observers(
                    code=code,
                    result=result,
                    error=error,
                )
            if error is not None:
                # Re-raise AFTER observers ran. Swallowing it here returned
                # ``None``: the host saw an empty "success" for a dead
                # backend. ``typed_guard`` converts it to a typed error.
                if isinstance(error, NotConnectedError):
                    return self._not_connected("run_code", reason=error.message)
                raise error
            return result

    def register_run_code_observer(
        self,
        observer: Callable[..., Awaitable[None] | None],
    ) -> None:
        """Register a callable invoked after every ``run_code`` tool call.

        The observer is called as ``observer(code=..., result=...,
        error=...)``. ``result`` is the backend's :class:`RunCodeResult`
        on success (and ``None`` on exception); ``error`` is the raised
        exception on failure (and ``None`` on success). Observers may be
        sync or async. Exceptions raised by an observer are logged and
        swallowed so the original tool result is preserved.

        Parameters
        ----------
        observer : Callable[..., Awaitable[None] | None]
            Observer callback invoked after run-code execution.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        if not callable(observer):
            raise TypeError("observer must be callable")
        self._run_code_observers.append(observer)

    async def _notify_run_code_observers(
        self,
        *,
        code: str,
        result: Any,
        error: BaseException | None,
    ) -> None:
        """Notify observers after a run-code request completes.

        Parameters
        ----------
        code : str
            Python code or command text to execute or validate.
        result : Any
            Result object or payload to process.
        error : BaseException | None
            Error instance or message to convert.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        if not self._run_code_observers:
            return
        import inspect

        for observer in list(self._run_code_observers):
            try:
                ret = observer(code=code, result=result, error=error)
                if inspect.isawaitable(ret):
                    await ret
            except Exception:  # observers must never break run_code
                logger.debug("run_code observer failed", exc_info=True)

    def _tool_validate_code(self) -> None:
        """Register the ``validate_code`` MCP tool.

        Returns
        -------
        None
            The function completes through its side effects.
        """

        @self.tool(
            name="validate_code",
            description=(
                "Dry-run / validate the generated code without applying side "
                "effects. Returns parse / type / semantic feedback for callers."
            ),
        )
        @typed_guard
        async def validate_code(code: str):
            """Validate code.

            Parameters
            ----------
            code : str
                Python code or command text to execute or validate.

            Returns
            -------
            None
                The function completes through its side effects.
            """
            if not code or not code.strip():
                raise InvalidArgumentsError("code must be a non-empty string")
            return await self.backend.validate_code(code)

    # ---- component lifecycle -----------------------------------------

    def _tool_manage_component(self) -> None:
        """Register the ``manage_component`` MCP tool.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        label = self.component_label or self.leaf_name

        @self.tool(
            name=f"manage_{label}",
            description=(
                f"Manage the {label} component lifecycle in managed backend. "
                "Actions:\n"
                f"  • activate — start or resume the {label} component.\n"
                f"  • deactivate — cleanly stop {label} and free resources.\n"
                f"  • update — apply pending configuration changes.\n"
                f"  • refresh — force reload state from the server.\n"
                "Returns a status dict."
            ),
        )
        @typed_guard
        async def manage_component(action: str) -> dict[str, Any]:
            """Apply a component management action.

            Parameters
            ----------
            action : str
                Action name requested by the caller.

            Returns
            -------
            dict[str, Any]
                Mapping containing the operation result.
            """
            action = (action or "").strip().lower()
            if action == "activate":
                return await self.backend.activate_component()
            elif action == "deactivate":
                return await self.backend.deactivate_component()
            elif action == "update":
                return await self.backend.update_component()
            elif action == "refresh":
                return await self.backend.refresh_component()
            else:
                raise InvalidArgumentsError(
                    f"invalid action {action!r}; use 'activate', 'deactivate', 'update', or 'refresh'"  # noqa: E501
                )

    # ---- visuals ------------------------------------------------------

    def _tool_screenshot(self) -> None:
        """Register the ``screenshot`` MCP tool.

        Returns
        -------
        None
            The function completes through its side effects.
        """

        @self.tool(
            name="screenshot",
            description=(
                "Capture a PNG screenshot of the current model view. "
                "Returns `{format: 'png', data: <base64>}`."
            ),
        )
        @typed_guard
        async def screenshot(view: Optional[str] = None) -> dict[str, Any]:
            """Capture a screenshot from the backend runtime.

            Parameters
            ----------
            view : Optional[str]
                Graphics view or camera preset requested by the caller.

            Returns
            -------
            dict[str, Any]
                Mapping containing the operation result.
            """
            return await self.backend.screenshot(view=view)

    # ---- reports ------------------------------------------------------

    def _tool_summarize_setup(self) -> None:
        """Register the ``summarize_setup`` MCP tool.

        Returns
        -------
        None
            The function completes through its side effects.
        """

        @self.tool(
            name="summarize_setup",
            description=(
                "Return the full solver setup summary — models, "
                "materials, boundary conditions, solver settings, "
                "discretization schemes, and limits — in a single "
                "call. Equivalent to Fluent's Report > Summary. "
                "Use this FIRST when the user asks 'show me my "
                "setup' or 'what is configured?'. Read-only."
            ),
        )
        @typed_guard
        async def summarize_setup() -> dict[str, Any]:
            """Summarize the current setup state.

            Returns
            -------
            dict[str, Any]
                Mapping containing the operation result.
            """
            import tempfile as _tf

            tmp_file = _tf.NamedTemporaryFile(suffix=".txt", delete=False)
            tmp_file.close()
            tmp = normalize_path_for_fluent(tmp_file.name)
            snippet = (
                f"session.settings.results.report.summary(write_to_file=True, file_name={tmp!r})"
            )
            result = require_connected_result(await self.backend.run_code(snippet))
            status = getattr(result, "status", "")
            if status != "ok":
                err = (
                    getattr(result, "stderr", None)
                    or getattr(result, "message", None)
                    or "summary command failed"
                )
                return {"error": err}
            import pathlib as _pl

            fp = _pl.Path(tmp)
            content = ""
            try:
                if fp.exists():
                    content = fp.read_text(
                        encoding="utf-8",
                        errors="replace",
                    )
                    fp.unlink(missing_ok=True)
            except OSError:
                pass
            return {"summary": content or "(no output)"}

    def _tool_simulation_report(self) -> None:
        """Register the ``simulation_report`` MCP tool.

        Returns
        -------
        None
            The function completes through its side effects.
        """

        @self.tool(
            name="simulation_report",
            description=(
                "Generate or export a rich simulation report from "
                "the connected solver session. Actions:\n"
                "  • generate — create a named simulation report.\n"
                "  • export_html — export an existing report as HTML.\n"
                "  • export_pdf — export as PDF.\n"
                "  • export_pptx — export as PowerPoint.\n"
                "  • list — list previously generated reports.\n"
                "Returns the output path or report list."
            ),
        )
        @typed_guard
        async def simulation_report(
            action: str = "list",
            report_name: str = "default-report",
            output_path: Optional[str] = None,
        ) -> dict[str, Any]:
            """Create a simulation report from the backend state.

            Parameters
            ----------
            action : str
                Action name requested by the caller.
            report_name : str
                Report definition name targeted by the operation.
            output_path : Optional[str]
                Path for the output.

            Returns
            -------
            dict[str, Any]
                Mapping containing the operation result.
            """
            import tempfile as _tf

            action = (action or "list").strip().lower()
            valid = {
                "generate",
                "export_html",
                "export_pdf",
                "export_pptx",
                "list",
            }
            if action not in valid:
                return {
                    "error": f"invalid action {action!r}",
                    "valid_actions": sorted(valid),
                }

            if action == "list":
                snippet = (
                    "session.settings.results.report.simulation_reports.list_simulation_reports()"
                )
                result = require_connected_result(await self.backend.run_code(snippet))
                if getattr(result, "status", "") != "ok":
                    return {
                        "error": (
                            getattr(result, "stderr", None) or "list_simulation_reports failed"
                        ),
                    }
                return {
                    "reports": getattr(result, "return_value", None),
                    "stdout": getattr(result, "stdout", "") or None,
                }

            if action == "generate":
                snippet = (
                    "session.settings.results.report"
                    ".simulation_reports"
                    f".generate_simulation_report("
                    f"report_name={report_name!r})"
                )
            elif action == "export_html":
                out = normalize_path_for_fluent(output_path or _tf.mkdtemp(prefix="fluent_report_"))
                snippet = (
                    "session.settings.results.report"
                    ".simulation_reports"
                    ".export_simulation_report_as_html("
                    f"report_name={report_name!r}, "
                    f"output_dir={out!r})"
                )
            elif action == "export_pdf":
                if output_path is None:
                    out_file = _tf.NamedTemporaryFile(suffix=".pdf", delete=False)
                    out_file.close()
                    output_path = out_file.name
                out = normalize_path_for_fluent(output_path)
                snippet = (
                    "session.settings.results.report"
                    ".simulation_reports"
                    ".export_simulation_report_as_pdf("
                    f"report_name={report_name!r}, "
                    f"file_name={out!r})"
                )
            else:  # export_pptx
                if output_path is None:
                    out_file = _tf.NamedTemporaryFile(suffix=".pptx", delete=False)
                    out_file.close()
                    output_path = out_file.name
                out = normalize_path_for_fluent(output_path)
                snippet = (
                    "session.settings.results.report"
                    ".simulation_reports"
                    ".export_simulation_report_as_pptx("
                    f"report_name={report_name!r}, "
                    f"file_name={out!r})"
                )

            result = require_connected_result(await self.backend.run_code(snippet))
            if getattr(result, "status", "") != "ok":
                return {
                    "error": (getattr(result, "stderr", None) or f"{action} failed"),
                }
            return {
                "action": action,
                "report_name": report_name,
                "output_path": (out if action != "generate" else None),
                "stdout": getattr(result, "stdout", "") or None,
                "note": (f"Simulation report {action} completed."),
            }


__all__ = [
    "FluidsLeafMCP",
    "ALL_TOOLS",
    "BackendUnavailableError",
    "NotConnectedError",
    "select_named_objects_from_mapping",
]


def select_named_objects_from_mapping(
    named: dict[str, list[str]],
    *,
    collection: str,
    pattern: str = "*",
    include_shadows: bool = True,
    exclude: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Glob-expand ``pattern`` over ``named[collection]``.

    Pure-data helper — no backend, no I/O. Used by both the
    ``select_named_objects`` MCP tool and tests. Accepts either dotted
    (``setup.boundary_conditions.wall``) or slashed-and-hyphenated
    (``setup/boundary-conditions/wall``) collection spellings so callers
    don't have to know which naming convention the active backend
    happens to use.

    Parameters
    ----------
    named : dict[str, list[str]]
        Mapping of available named objects.
    collection : str
        Collection of named objects to search.
    pattern : str
        Selection pattern used to match object names.
    include_shadows : bool
        Whether shadow objects should be included in results.
    exclude : Optional[list[str]]
        Names or patterns excluded from the selection.

    Returns
    -------
    dict[str, Any]
        Mapping containing the operation result.
    """
    import fnmatch

    candidates = (
        named.get(collection)
        or named.get(collection.replace(".", "/").replace("_", "-"))
        or named.get(collection.replace("/", ".").replace("-", "_"))
    )
    if candidates is None:
        return {
            "collection": collection,
            "pattern": pattern,
            "names": [],
            "available_collections": sorted(named.keys()),
            "note": ("collection not found; pass one of `available_collections`."),
        }
    matched = [n for n in candidates if fnmatch.fnmatchcase(n, pattern)]
    if not include_shadows:
        matched = [n for n in matched if not n.endswith("-shadow")]
    for ex_pattern in exclude or []:
        matched = [n for n in matched if not fnmatch.fnmatchcase(n, ex_pattern)]
    return {
        "collection": collection,
        "pattern": pattern,
        "include_shadows": include_shadows,
        "exclude": list(exclude or []),
        "names": matched,
        "count": len(matched),
    }
