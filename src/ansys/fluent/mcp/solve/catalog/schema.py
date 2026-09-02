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

"""Static index over PyFluent's canonical ``settings.json`` schema.

The full schema (``setup``/``solution``/``results``/... consists of 77k+ nodes,
including command-argument signatures, query lists, enum-flagged
parameters, and child-aliases). It ships gzipped under
``ansys/fluent/mcp/solve/data/settings_271.json.gz``. PyFluent itself only
ships the slimmer ``api_objects.json`` (path plus kind only) The full
schema is what allows validation of command kwargs, enum strings, and
path/alias resolution **without a live solver**.

The on-disk file is ~0.8 MB gzipped (~11.7 MB uncompressed). Because parsing
the JSON is the expensive step (~150 ms on a recent laptop), the
loader is lazy and lru-cached.

Override the bundled file by setting ``FLUIDS_MCP_SETTINGS_JSON`` to
an absolute path (``.json`` or ``.json.gz``). This is useful for testing a
newer Fluent build before rolling a new vendored snapshot.

Which vendored snapshot is used can also be selected by version:
``FLUIDS_MCP_FLUENT_VERSION`` (``"241"``, ``"24.1"`` and ``"v24_1"`` are all
accepted) picks ``settings_<version>.json.gz`` from the data directory. When
the MCP server attaches to a live solver it also records the reported Fluent
version and, unless the environment variable is set, uses that as the default
so static checks line up with the connected release. If no matching snapshot
is bundled the loader returns ``None`` and static checks degrade gracefully.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import gzip
from importlib import resources
import json
import logging
import os
from pathlib import Path
import re
from typing import Any

logger = logging.getLogger(__name__)

# Newest Fluent release this package ships a vendored ``settings_<v>.json.gz``
# snapshot for. Used as the fallback when nothing else pins a version.
_BUNDLED_VERSION = "271"
_OVERRIDE_ENV = "FLUIDS_MCP_SETTINGS_JSON"
_VERSION_ENV = "FLUIDS_MCP_FLUENT_VERSION"

# Matches only strings that genuinely look like a Fluent version, so a
# stray ``repr()`` (e.g. of a mock session) can't be mistaken for one:
#   "24.1"  "24.1.0"  "v24_1"  "2024R1"  "241"
_VERSION_RE = re.compile(
    r"""^\s*
    (?:
        v?(?P<yy>\d{2})[._](?P<rr>\d)(?:[._]\d+)?   # 24.1 / v24_1 / 24.1.0
      | (?:20)?(?P<yy2>\d{2})[rR](?P<rr2>\d)        # 2024R1 / 24R1
      | (?P<compact>\d{3})                          # 241
    )
    \s*$""",
    re.VERBOSE,
)


def normalize_fluent_version(value: str | None) -> str | None:
    """Normalize a Fluent version to the compact ``"NNN"`` form.

    Accepts the forms seen in the wild: ``"24.1"`` (dotted), ``"v24_1"``
    (PyFluent ``FluentVersion`` enum), ``"24.1.0"`` (Scheme
    ``inquire-release``), ``"2024R1"`` and the already-compact ``"241"``.
    Returns ``None`` for anything that does not clearly look like a
    version.

    Parameters
    ----------
    value : str | None
        Raw version string.

    Returns
    -------
    str | None
        Compact ``"NNN"`` string, or ``None`` when unparseable.
    """
    if not value:
        return None
    match = _VERSION_RE.match(str(value))
    if match is None:
        return None
    if match.group("compact"):
        return match.group("compact")
    yy = match.group("yy") or match.group("yy2")
    rr = match.group("rr") or match.group("rr2")
    return f"{yy}{rr}"


def _env_version() -> str | None:
    """Return the version pinned by ``FLUIDS_MCP_FLUENT_VERSION``, if any."""
    return normalize_fluent_version(os.getenv(_VERSION_ENV))


# Runtime-observed Fluent version (set by the PyFluent backend on connect).
# Only consulted when the environment variable is not set.
_runtime_version: str | None = None


def set_runtime_fluent_version(value: str | None) -> None:
    """Record the Fluent version reported by a live session.

    Pass ``None`` (e.g. on disconnect) to clear it. A non-empty value
    that is not version-shaped is ignored and the current value kept.

    Parameters
    ----------
    value : str | None
        Version string in any form :func:`normalize_fluent_version` accepts,
        or ``None`` to clear.
    """
    global _runtime_version
    if value is None:
        normalized: str | None = None
    else:
        normalized = normalize_fluent_version(value)
        if normalized is None:
            logger.debug("ignoring unrecognized runtime Fluent version: %r", value)
            return
    if normalized != _runtime_version:
        _runtime_version = normalized
        # A different target version invalidates any cached parse.
        load_settings_schema.cache_clear()


def default_schema_version() -> str:
    """Resolve which schema version the offline checks should use.

    Precedence: ``FLUIDS_MCP_FLUENT_VERSION`` env var, then the version
    reported by the connected solver, then the newest bundled snapshot.

    Returns
    -------
    str
        Compact ``"NNN"`` version string.
    """
    return _env_version() or _runtime_version or _BUNDLED_VERSION


# Backwards-compatible alias: some call sites / tests import this name.
_DEFAULT_VERSION = _BUNDLED_VERSION


@dataclass(frozen=True)
class CommandArg:
    """One argument of a command or query."""

    name: str  # canonical (kebab-case as in settings.json)
    py_name: str  # snake_case form (what PyFluent accepts in Python)
    type_hint: str  # "string" | "real" | "integer" | "boolean" | "file" | "real-list" | ...
    help: str
    file_purpose: str | None = None  # "input" or "output" for file args


@dataclass(frozen=True)
class CommandSpec:
    """Static signature for a command or query."""

    name: str  # canonical
    py_name: str  # snake_case
    help: str
    arguments: tuple[CommandArg, ...]
    is_query: bool = False

    def arg_names(self) -> list[str]:
        """Return both canonical and snake_case names of every arg.

        Returns
        -------
        list[str]
            Collection containing the operation results.
        """
        out: list[str] = []
        for a in self.arguments:
            out.append(a.py_name)
            if a.name != a.py_name:
                out.append(a.name)
        return out


@dataclass(frozen=True)
class SettingsNode:
    """One node in the settings tree.

    Path uses snake_case dotted notation (e.g.
    ``setup.boundary_conditions.wall``). NamedObject members are
    addressed as ``setup.boundary_conditions.wall["<name>"]`` by the
    caller; the lookup helpers below transparently descend through
    the schema's ``object-type`` envelope.
    """

    path: str  # snake_case dotted
    kind: str  # group | named-object | list-object | string | real | integer | boolean | file | ...
    help: str
    child_names: tuple[str, ...]  # snake_case
    commands: dict[str, CommandSpec]  # keyed by snake_case
    queries: dict[str, CommandSpec]  # keyed by snake_case
    has_allowed_values: bool
    user_creatable: bool  # only meaningful when kind == "named-object"
    aliases: dict[str, str]  # alias name (snake) -> target path (snake)


def _kebab_to_snake(s: str) -> str:
    # Fluent marks boolean parameters with a trailing ``?`` in the
    # schema (``enable?``, ``enabled?``, ``frozen_flux?``, ...).
    # PyFluent strips it for the Python attribute, so callers always
    # spell the path without ``?``. Normalise here so both forms hit
    # the same index entry.
    """Convert a kebab-case name to snake_case.

    Parameters
    ----------
    s : str
        S to supply to the function.

    Returns
    -------
    str
        String value produced by the helper.
    """
    if s.endswith("?"):
        s = s[:-1]
    return s.replace("-", "_")


def _normalise_path(path: str) -> str:
    """Normalise a Fluent object path to snake_case dotted notation.

    Strip ``solver.settings.`` prefix, convert kebab to snake, drop
    bracketed named-object keys (the static schema has no per-name
    facts; the member shape is what matters).

    Parameters
    ----------
    path : str
        Fluent object path or file-system path to inspect.

    Returns
    -------
    str
        String result produced by the function.
    """
    if not path:
        return ""
    p = path.strip()
    for prefix in ("solver.settings.", "settings."):
        if p.startswith(prefix):
            p = p[len(prefix) :]
            break
    # Drop ["..."] / ['...'] member selectors.
    out_parts: list[str] = []
    for raw in p.split("."):
        if not raw:
            continue
        # split off bracketed parts: cell_zone_conditions["fluid-1"] → cell_zone_conditions
        bracket = raw.find("[")
        if bracket >= 0:
            raw = raw[:bracket]
        out_parts.append(_kebab_to_snake(raw))
    return ".".join(out_parts)


def _locate_default_data(version: str = _BUNDLED_VERSION) -> Path | None:
    """Find a bundled gzipped schema by version, returning ``None`` if missing.

    Parameters
    ----------
    version : str
        Compact ``"NNN"`` Fluent version, e.g. ``"271"`` or ``"241"``.

    Returns
    -------
    Path | None
        Result produced by the function.
    """
    name = f"settings_{version}.json.gz"
    try:
        res = resources.files("ansys.fluent.mcp.solve.data").joinpath(name)
        # On editable installs ``res`` is a Path-like; ``is_file`` works
        # in both ``zipfile.Path`` and ``pathlib.Path`` flavours.
        if res.is_file():
            return Path(str(res))
    except (ModuleNotFoundError, AttributeError, FileNotFoundError):
        pass
    return None


def _load_raw(path: Path) -> dict[str, Any]:
    """Load raw.

    Parameters
    ----------
    path : Path
        Filesystem path or API path to process.

    Returns
    -------
    dict[str, Any]
        Mapping containing the operation result.
    """
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as fh:
        return json.load(fh)


def _build_command_spec(name: str, raw: dict[str, Any], *, is_query: bool) -> CommandSpec:
    """Build command spec.

    Parameters
    ----------
    name : str
        Name of the object, module, or setting being processed.
    raw : dict[str, Any]
        Raw string value to parse or validate.
    is_query : bool
        Whether to enable or apply is query.

    Returns
    -------
    CommandSpec
        CommandSpec produced by the operation.
    """
    args: list[CommandArg] = []
    for arg_name, arg_raw in (raw.get("arguments") or {}).items():
        if not isinstance(arg_raw, dict):
            continue
        args.append(
            CommandArg(
                name=arg_name,
                py_name=_kebab_to_snake(arg_name),
                type_hint=str(arg_raw.get("type") or ""),
                help=str(arg_raw.get("help") or ""),
                file_purpose=arg_raw.get("file-purpose"),
            )
        )
    return CommandSpec(
        name=name,
        py_name=_kebab_to_snake(name),
        help=str(raw.get("help") or ""),
        arguments=tuple(args),
        is_query=is_query,
    )


class SettingsSchema:
    """Indexed view over the static settings schema.

    Indexing is built once at construction. Resolution is
    ``O(depth)`` over the snake_case path components, with kebab and
    snake spellings accepted interchangeably.
    """

    def __init__(self, raw: dict[str, Any], *, source: str = "<bundled>") -> None:
        """Initialize the SettingsSchema instance.

        Parameters
        ----------
        raw : dict[str, Any]
            Raw string value to parse or validate.
        source : str
            Source to supply to the function.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        self._raw = raw
        self._source = source
        # Flat index by snake_case path.
        self._by_path: dict[str, SettingsNode] = {}
        # Member-of (path → snake_case path of element schema, e.g.
        # "setup.boundary_conditions.wall" → "setup.boundary_conditions.wall.<member>").
        self._member_of: dict[str, str] = {}
        self._build_index(raw, path="")

    # ----------------------------------------------------------------
    # public API
    # ----------------------------------------------------------------
    @property
    def source(self) -> str:
        """Return the source associated with the catalog node.

        Returns
        -------
        str
            String value produced by the helper.
        """
        return self._source

    @property
    def node_count(self) -> int:
        """Return the number of nodes in the catalog tree.

        Returns
        -------
        int
            Configured integer limit used by the helper.
        """
        return len(self._by_path)

    def resolve(self, path: str) -> SettingsNode | None:
        """Resolve a snake_case (or kebab-case) dotted path.

        Bracketed member selectors (``...wall["w1"]``) are stripped
        and the schema descends through the NamedObject's element
        envelope automatically — so
        ``resolve("setup.boundary_conditions.wall.thermal")`` returns
        the *element-level* node for wall thermal, not the container.

        Dot-form member keys are also supported:
        ``solution.controls.under_relaxation.pressure`` resolves to
        the ``under_relaxation`` member envelope (Fluent rejects new
        keys here at runtime, but the schema can't know which keys
        the user has created — so we accept any token after a
        NamedObject as a member-key access).

        Parameters
        ----------
        path : str
            Fluent object path or file-system path to inspect.

        Returns
        -------
        SettingsNode | None
            Collection containing the operation results.
        """
        norm = _normalise_path(path)
        if not norm:
            return self._by_path.get("")
        node = self._by_path.get(norm)
        if node is not None:
            return node
        parts = norm.split(".")
        cur = ""
        i = 0
        while i < len(parts):
            part = parts[i]
            candidate = f"{cur}.{part}" if cur else part
            if candidate in self._by_path:
                cur = candidate
                i += 1
                continue
            member = self._member_of.get(cur)
            if member is not None:
                via = f"{member}.{part}"
                if via in self._by_path:
                    cur = via
                    i += 1
                    continue
                # ``part`` is a member key (e.g. "pressure" under
                # ``under_relaxation``). Move into the member envelope
                # and consume the key; subsequent parts apply to it.
                cur = member
                i += 1
                continue
            return None
        return self._by_path.get(cur)

    def lookup_command(self, path: str) -> CommandSpec | None:
        """Resolve a command call path like ``solution.run_calculation.iterate``.

        Splits the trailing segment as the command name and looks it
        up on the parent node's ``commands`` (then ``queries`` as a
        fallback).

        Parameters
        ----------
        path : str
            Fluent object path or file-system path to inspect.

        Returns
        -------
        CommandSpec | None
            Result produced by the function.
        """
        norm = _normalise_path(path)
        if "." not in norm:
            return None
        parent, last = norm.rsplit(".", 1)
        node = self.resolve(parent)
        if node is None:
            return None
        cmd = node.commands.get(last)
        if cmd is not None:
            return cmd
        return node.queries.get(last)

    # ----------------------------------------------------------------
    # build
    # ----------------------------------------------------------------
    def _build_index(self, raw: dict[str, Any], path: str) -> None:
        """Build index.

        Parameters
        ----------
        raw : dict[str, Any]
            Raw string value to parse or validate.
        path : str
            Filesystem path or API path to process.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        kind = str(raw.get("type") or "")
        children_raw = raw.get("children") or {}
        child_names_snake: list[str] = sorted(_kebab_to_snake(k) for k in children_raw)
        commands_raw = raw.get("commands") or {}
        queries_raw = raw.get("queries") or {}
        commands: dict[str, CommandSpec] = {}
        for cname, craw in commands_raw.items():
            if not isinstance(craw, dict):
                continue
            spec = _build_command_spec(cname, craw, is_query=False)
            commands[spec.py_name] = spec
        queries: dict[str, CommandSpec] = {}
        for qname, qraw in queries_raw.items():
            if not isinstance(qraw, dict):
                continue
            spec = _build_command_spec(qname, qraw, is_query=True)
            queries[spec.py_name] = spec
        aliases_raw = raw.get("child-aliases") or {}
        aliases: dict[str, str] = {}
        for an, target in aliases_raw.items():
            if not isinstance(target, str):
                continue
            aliases[_kebab_to_snake(an)] = _kebab_to_snake(target)
        node = SettingsNode(
            path=path,
            kind=kind,
            help=str(raw.get("help") or ""),
            child_names=tuple(child_names_snake),
            commands=commands,
            queries=queries,
            has_allowed_values=bool(raw.get("has-allowed-values")),
            user_creatable=bool(raw.get("user-creatable?")),
            aliases=aliases,
        )
        self._by_path[path] = node
        # Recurse into children.
        for cname, craw in children_raw.items():
            if not isinstance(craw, dict):
                continue
            cpath = f"{path}.{_kebab_to_snake(cname)}" if path else _kebab_to_snake(cname)
            self._build_index(craw, cpath)
        # Recurse into named/list-object element schema (``object-type``).
        elem = raw.get("object-type")
        if isinstance(elem, dict):
            member_path = f"{path}.<member>" if path else "<member>"
            self._member_of[path] = member_path
            self._build_index(elem, member_path)


# ----------------------------------------------------------------------
# module-level loader
# ----------------------------------------------------------------------
@lru_cache(maxsize=4)
def load_settings_schema(version: str | None = None) -> SettingsSchema | None:
    """Load the offline settings schema for a Fluent version.

    ``version`` accepts any form :func:`normalize_fluent_version` handles
    (``"241"``, ``"24.1"``, ``"v24_1"``). When omitted it is resolved via
    :func:`default_schema_version` (env var, then connected-solver version,
    then the newest bundled snapshot).

    Honors ``FLUIDS_MCP_SETTINGS_JSON`` for ad-hoc file overrides. Returns
    ``None`` (and logs at INFO) if no schema can be located for the
    requested version -- callers must treat schema-based checks as
    best-effort. ``settings_271.json.gz`` ships with the package; other
    versions require a vendored ``settings_<v>.json.gz`` or the env
    override.

    Parameters
    ----------
    version : str | None
        Target Fluent version, or ``None`` to auto-resolve.

    Returns
    -------
    SettingsSchema | None
        Collection containing the operation results.
    """
    if version is None:
        resolved = default_schema_version()
    else:
        # An explicit request is honored as-is: a recognizable version is
        # normalized, anything else is used verbatim as the snapshot token
        # (and simply won't be found unless vendored / overridden).
        resolved = normalize_fluent_version(version) or version
    override = os.getenv(_OVERRIDE_ENV)
    src_path: Path | None = None
    if override:
        cand = Path(override)
        if cand.is_file():
            src_path = cand
        else:
            logger.warning("%s set but file missing: %s", _OVERRIDE_ENV, override)
    if src_path is None:
        src_path = _locate_default_data(resolved)
    if src_path is None and resolved != _BUNDLED_VERSION:
        logger.info(
            "no bundled settings schema for Fluent %s; "
            "set %s to an exported settings_%s.json[.gz] for offline checks",
            resolved,
            _OVERRIDE_ENV,
            resolved,
        )
    if src_path is None:
        logger.info("settings schema not located for version %s; static checks disabled", resolved)
        return None
    try:
        raw = _load_raw(src_path)
    except Exception as exc:
        logger.warning("failed to read settings schema %s: %s", src_path, exc)
        return None
    return SettingsSchema(raw, source=str(src_path))
