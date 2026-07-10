"""MCP Prompt registry for reusable prompt templates.

Provides :class:`PromptRegistry` — a declarative catalogue that maps
bundled Markdown prompt templates and SKILL files onto MCP
``prompts/list`` and ``prompts/get`` endpoints via the FastMCP
``@server.prompt()`` decorator.

Usage (inside a leaf's ``_register_prompts`` override)::

    from ansys.fluent.mcp.common.prompts import PromptRegistry

    registry = PromptRegistry()
    registry.add_package_dir(
        package="fluids_mcp.products.solve.agents.skills",
        uri_prefix="prompt://solve/skill/",
        glob="SKILL.md",
        description_prefix="SKILL guide: ",
    )
    registry.register_on(self)  # self is a FastMCP instance
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from importlib.resources import files as _pkg_files
from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:
    from importlib.abc import Traversable

logger = logging.getLogger(__name__)

__all__ = ["PromptEntry", "PromptRegistry"]


@dataclass(frozen=True, slots=True)
class PromptEntry:
    """Metadata for one MCP prompt backed by a package-data file."""

    name: str
    description: str
    package: str
    filename: str


@dataclass
class PromptRegistry:
    """Collect :class:`PromptEntry` items and wire them onto a FastMCP server.

    The registry is purely declarative until :meth:`register_on` is called;
    it holds no references to the MCP server during construction so it can
    be built at import time without side effects.
    """

    _entries: list[PromptEntry] = field(default_factory=list)

    @property
    def entries(self) -> Sequence[PromptEntry]:
        """Return the current snapshot of registered entries (read-only view)."""
        return tuple(self._entries)

    def add(self, entry: PromptEntry) -> None:
        """Append a single :class:`PromptEntry`."""
        self._entries.append(entry)

    def add_file(
        self,
        *,
        package: str,
        filename: str,
        name: str,
        description: str = "",
    ) -> None:
        """Register one file from *package* as a prompt."""
        self._entries.append(
            PromptEntry(
                name=name,
                description=description,
                package=package,
                filename=filename,
            )
        )

    def add_package_dir(
        self,
        *,
        package: str,
        glob: str = "*.md",
        name_prefix: str = "",
        description_prefix: str = "",
    ) -> None:
        """Auto-discover Markdown files matching *glob* and register each as a prompt.

        Parameters
        ----------
        package:
            Dotted import path of the package whose ``__init__.py``
            neighbours the template files.
        glob:
            fnmatch-style pattern (e.g. ``"*.md"``, ``"SKILL.md"``).
        name_prefix:
            Prefix prepended to the filename stem to form the prompt name.
        description_prefix:
            Human-readable prefix prepended to the stem of each filename
            to form the prompt description.
        """
        try:
            root: Traversable = _pkg_files(package)
        except (ModuleNotFoundError, FileNotFoundError, NotImplementedError) as exc:
            logger.debug(
                "PromptRegistry: package %r not resolvable — skipping: %s",
                package,
                exc,
            )
            return

        import fnmatch as _fnmatch

        for child in sorted(root.iterdir(), key=lambda p: p.name):
            if not child.is_file():
                continue
            if not _fnmatch.fnmatch(child.name, glob):
                continue
            stem = child.name.rsplit(".", 1)[0]
            prompt_name = f"{name_prefix}{stem}".replace("-", "_").replace(" ", "_")
            description = f"{description_prefix}{stem.replace('_', ' ')}"
            self._entries.append(
                PromptEntry(
                    name=prompt_name,
                    description=description,
                    package=package,
                    filename=child.name,
                )
            )

    def register_on(self, server: Any) -> None:
        """Wire every entry as a ``@server.prompt(...)`` endpoint.

        Parameters
        ----------
        server:
            A ``FastMCP`` (or subclass) instance that exposes the
            ``prompt(name, ...)`` decorator method.
        """
        for entry in self._entries:
            self._register_one(server, entry)

    @staticmethod
    def _register_one(server: Any, entry: PromptEntry) -> None:
        """Register a single prompt entry on the server."""
        pkg = entry.package
        fname = entry.filename

        @server.prompt(name=entry.name, description=entry.description)
        def _template(_pkg: str = pkg, _fname: str = fname) -> str:
            root = _pkg_files(_pkg)
            return (root / _fname).read_text(encoding="utf-8")

