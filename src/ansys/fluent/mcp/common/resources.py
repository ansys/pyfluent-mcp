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

"""MCP Resource registry for static package data.

Provides :class:`ResourceRegistry` — a declarative catalogue that maps
bundled package-data files (YAML knowledge packs, JSON evidence,
compressed schema dumps, SKILL.md prompts) onto MCP ``resources/list``
and ``resources/read`` endpoints via the FastMCP ``@server.resource()``
decorator.

Usage (inside a leaf's ``_register_resources`` override)::

    from ansys.fluent.mcp.common.resources import ResourceRegistry

    registry = ResourceRegistry()
    registry.add_package_dir(
        package="fluids_mcp.products.solve.agents.knowledge",
        uri_prefix="knowledge://solve/",
        description_prefix="Solve physics knowledge pack: ",
        glob="*.yaml",
        mime_type="text/yaml",
    )
    registry.register_on(self)  # self is a FastMCP instance
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.resources import files as _pkg_files
import logging
from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:
    from importlib.abc import Traversable

logger = logging.getLogger(__name__)

__all__ = ["ResourceEntry", "ResourceRegistry"]


@dataclass(frozen=True, slots=True)
class ResourceEntry:
    """Metadata for one MCP resource backed by a package-data file."""

    uri: str
    name: str
    description: str
    mime_type: str
    package: str
    filename: str


@dataclass
class ResourceRegistry:
    """Collect :class:`ResourceEntry` items and wire them onto a FastMCP server.

    The registry is purely declarative until :meth:`register_on` is called;
    it holds no references to the MCP server during construction so it can
    be built at import time without side effects.
    """

    _entries: list[ResourceEntry] = field(default_factory=list)

    @property
    def entries(self) -> Sequence[ResourceEntry]:
        """Return the current snapshot of registered entries (read-only view)."""
        return tuple(self._entries)

    def add(self, entry: ResourceEntry) -> None:
        """Append a single :class:`ResourceEntry`."""
        self._entries.append(entry)

    def add_file(
        self,
        *,
        package: str,
        filename: str,
        uri: str,
        name: str | None = None,
        description: str = "",
        mime_type: str = "application/octet-stream",
    ) -> None:
        """Register one file from *package* as a resource at *uri*."""
        resolved_name = name or filename.rsplit(".", 1)[0].replace("-", "_")
        self._entries.append(
            ResourceEntry(
                uri=uri,
                name=resolved_name,
                description=description,
                mime_type=mime_type,
                package=package,
                filename=filename,
            )
        )

    def add_package_dir(
        self,
        *,
        package: str,
        uri_prefix: str,
        description_prefix: str = "",
        glob: str = "*",
        mime_type: str = "application/octet-stream",
    ) -> None:
        """Auto-discover files matching *glob* in *package* and register each.

        Parameters
        ----------
        package:
            Dotted import path of the package whose ``__init__.py``
            neighbours the data files (must be declared in pyproject.toml
            ``package-data``).
        uri_prefix:
            Prefix for generated URIs.  Each file gets
            ``{uri_prefix}{filename}`` as its resource URI.
        description_prefix:
            Human-readable prefix prepended to the stem of each filename
            to form the resource description.
        glob:
            fnmatch-style pattern passed to ``importlib.resources.files``
            iteration (e.g. ``"*.yaml"``, ``"*.json"``).
        mime_type:
            MIME type applied uniformly to every discovered file.
        """
        try:
            root: Traversable = _pkg_files(package)
        except (ModuleNotFoundError, FileNotFoundError, NotImplementedError) as exc:
            logger.debug(
                "ResourceRegistry: package %r not resolvable — skipping: %s",
                package,
                exc,
            )
            return

        import fnmatch

        for child in sorted(root.iterdir(), key=lambda p: p.name):
            if not child.is_file():
                continue
            if not fnmatch.fnmatch(child.name, glob):
                continue
            stem = child.name.rsplit(".", 1)[0]
            uri = f"{uri_prefix}{child.name}"
            name = stem.replace("-", "_").replace(" ", "_")
            description = f"{description_prefix}{stem.replace('_', ' ')}"
            self._entries.append(
                ResourceEntry(
                    uri=uri,
                    name=name,
                    description=description,
                    mime_type=mime_type,
                    package=package,
                    filename=child.name,
                )
            )

    def register_on(self, server: Any) -> None:
        """Wire every entry as a ``@server.resource(...)`` endpoint.

        Parameters
        ----------
        server:
            A ``FastMCP`` (or subclass) instance that exposes the
            ``resource(uri, ...)`` decorator method.
        """
        for entry in self._entries:
            self._register_one(server, entry)

    @staticmethod
    def _register_one(server: Any, entry: ResourceEntry) -> None:
        """Register a single resource entry on the server."""
        pkg = entry.package
        fname = entry.filename

        @server.resource(
            entry.uri,
            name=entry.name,
            description=entry.description,
            mime_type=entry.mime_type,
        )
        def _reader() -> str:
            root = _pkg_files(pkg)
            data = (root / fname).read_text(encoding="utf-8")
            return data
