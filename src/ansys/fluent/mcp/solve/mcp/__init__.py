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

"""Solve MCP leaf provides Fluent solver code generation and live-context tools.

This module physically hosts the :class:`SolveMCP` class. The parent
package ``ansys.fluent.mcp.solve`` re-exports the class so both
import paths (``from ansys.fluent.mcp.solve import SolveMCP`` and
``from ansys.fluent.mcp.solve.mcp import SolveMCP``) resolve to the
same class. This subpackage is the entire MCP-only surface. The
agent loop, planner, and recipes are not part of this package.

This open-source leaf ships only the PyFluent backend. It always
connects to PyFluent for code execution and live-model introspection.
``codegen``/``clarify are handled by the LLM pipeline. Additional backends
(such as the internal Fluids One managed-service ``fluids_one_solve``
backend) are contributed by other installed packages through the
``ansys.fluent.mcp.solve_backends`` entry-point group and merged into
the backend registry at construction time.

Usage::

    connect()  # launch / attach PyFluent
    connect(ip="...", port=12345)  # attach to a remote solver
    codegen("set inlet to 323 K")  # LLM pipeline
    run_code(code)  # always executed via PyFluent
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Iterable, Optional

from ansys.fluent.mcp.common.base import FluidsLeafMCP
from ansys.fluent.mcp.solve.backends.composite import SolveCompositeBackend

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ansys.fluent.mcp.common.backend import Backend

logger = logging.getLogger("ansys.fluent.mcp.solve.mcp")

_SOLVE_BACKENDS_ENTRY_POINT_GROUP = "ansys.fluent.mcp.solve_backends"


def _discover_external_solve_backends() -> "dict[str, Backend]":
    """Discover extra solve backends contributed via entry points.

    Each entry point in the ``ansys.fluent.mcp.solve_backends`` group
    must load to a zero-argument callable returning a mapping of
    ``{backend_kind: Backend}``. Failures are logged and skipped so a
    misbehaving plugin never prevents the leaf from starting with the
    built-in PyFluent backend.

    Returns
    -------
    'dict[str, Backend]'
        Mapping containing the operation result.
    """
    from importlib.metadata import entry_points

    discovered: dict[str, Backend] = {}
    eps = entry_points(group=_SOLVE_BACKENDS_ENTRY_POINT_GROUP)
    for ep in eps:
        try:
            factory = ep.load()
            result = factory()
            if result:
                discovered.update(result)
        except Exception:  # never let a plugin break startup
            logger.warning(
                "Failed to load solve backend provider %r",
                getattr(ep, "name", ep),
                exc_info=True,
            )
    return discovered


class SolveMCP(FluidsLeafMCP):
    """Solve MCP leaf: Fluent solver codegen and live-context tools."""

    leaf_name = "solve"
    default_backend_kind = "pyfluent"
    component_label = "fluent"

    def __init__(
        self,
        *,
        expose_tools: Optional[Iterable[str]] = None,
        default_backend_kind: Optional[str] = None,
        **fastmcp_kwargs: Any,
    ) -> None:
        """Initialize the SolveMCP instance.

        Parameters
        ----------
        expose_tools : Optional[Iterable[str]]
            Whether MCP tools should be registered on the server.
        default_backend_kind : Optional[str]
            Default backend kind to supply to the function.
        fastmcp_kwargs : Any
            Keyword arguments forwarded when constructing the FastMCP server.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        if default_backend_kind is not None:
            self.default_backend_kind = default_backend_kind
        backends: dict[str, Any] = {"pyfluent": SolveCompositeBackend()}
        backends.update(_discover_external_solve_backends())
        super().__init__(
            backends=backends,
            expose_tools=expose_tools
            or (
                "session_status",
                "connect",
                "disconnect",
                "codegen",
                "clarify",
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
            ),
            **fastmcp_kwargs,
        )

    def _register_resources(self) -> None:
        """Register MCP resources for the Solve leaf."""
        super()._register_resources()
        from ansys.fluent.mcp.common.resources import ResourceRegistry

        registry = ResourceRegistry()
        registry.add_file(
            package="ansys.fluent.mcp.solve.resources",
            filename="settings_271.json.gz",
            uri="resource://solve/schema/settings_271.json.gz",
            description="Fluent v27.1 offline settings schema (compressed)",
            mime_type="application/gzip",
        )
        registry.register_on(self)

    def _register_prompts(self) -> None:
        """Register MCP prompts for the Solve leaf."""
        super()._register_prompts()
        from ansys.fluent.mcp.common.prompts import PromptRegistry

        registry = PromptRegistry()
        registry.add_file(
            package="ansys.fluent.mcp.solve.skills",
            filename="SKILL.md",
            name="solve_skill",
            description="Fluent Solve MCP-leaf skill guide",
        )
        registry.register_on(self)

    def _register_tools(self) -> None:
        """Register the tools exposed by this MCP leaf.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        super()._register_tools()
        # Register the canonical solve-leaf domain tools from
        # ``ansys.fluent.mcp.solve.tools.domain_tools``. These are the
        # pure backend / catalog operations exposed on the MCP surface.
        from ansys.fluent.mcp.solve.tools.domain_tools import get_solve_domain_tools

        self._register_domain_tools(get_solve_domain_tools())

        # When an optional higher-level agent layer is installed and
        # imported in this process, its module-level factory (registered
        # on ``ansys.fluent.mcp.common.base``) has already attached its
        # run-code observer via ``FluidsLeafMCP._attach_default_observers``.
        # Standalone MCP installs leave the observer registry empty and
        # behave as plain leaves — exactly the goal of the MCP/agent
        # split. This package never imports or names that agent layer.


__all__ = ["SolveMCP"]
