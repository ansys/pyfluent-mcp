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

"""Live introspection helpers shared by backends.

All functions take a *root* (the ``Settings`` root of a connected solver)
and operate on **caller-provided paths**. There are no hardcoded collection lists.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import logging
import os
import threading
from typing import Any, Iterable, Iterator, Optional

logger = logging.getLogger("ansys.fluent.mcp.introspection")


# Global Fluent settings paths that are ALWAYS fetched when no explicit
# `paths` are supplied to `get_state`.
FLUENT_GLOBAL_STATE_PATHS: tuple[str, ...] = (
    "setup/general/solver",
    "setup/models/energy",
    "setup/models/viscous",
    "setup/models/radiation",
    "setup/models/multiphase",
    "setup/models/discrete-phase/general-settings/interaction",
    "solution/methods/p-v-coupling",
    "solution/run-calculation/parameters",
    "setup/general/operating-conditions/gravity",
    "setup/general/operating-conditions/operating-pressure",
)

# Bound discovery responses to keep native introspection inexpensive.
_MAX_AV_ENTRIES = 100
_MAX_AV_VALUES = 25
_MAX_AV_DEPTH = 2


def _env_int(name: str, default: int) -> int:
    """Read an integer value from the environment.

    Parameters
    ----------
    name : str
        Name of the object, module, or setting being processed.
    default : int
        Default value used by the caller when no explicit value is available.

    Returns
    -------
    int
        Configured integer limit used by the helper.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


_MAX_STATE_BYTES = _env_int("FLUIDS_MCP_MAX_STATE_BYTES", 5000)
_MAX_INSTANCE_STATE_BYTES = 8000


# ---------------------------------------------------------------------------
# Path normalization
# ---------------------------------------------------------------------------


def normalise_attr_path(path: str) -> list[str]:
    """Split a Fluent path into its attribute components.

    Accepts both slash/dash format (``setup/boundary-conditions/velocity-inlet``)
    and dot/underscore format (``setup.boundary_conditions.velocity_inlet``)
    plus indexed instances like ``...velocity_inlet[cold-in]``.

    Dash-to-underscore conversion is applied only to **attribute name**
    segments, never to text inside ``[...]`` (which is the instance key
    and may legitimately contain dashes).

    Parameters
    ----------
    path : str
        Fluent object path or file-system path to inspect.

    Returns
    -------
    list[str]
        Collection containing the operation results.
    """
    parts: list[str] = []
    for raw in _split_outside_brackets(path, "/."):
        if not raw:
            continue
        if "[" in raw and raw.endswith("]"):
            attr, _, key = raw[:-1].partition("[")
            attr = attr.replace("-", "_")
            parts.append(f"{attr}[{key}]")
        else:
            parts.append(raw.replace("-", "_"))
    if parts and parts[0] == "settings":
        parts = parts[1:]
    return parts


def _split_outside_brackets(text: str, separators: str) -> list[str]:
    """Split ``text`` on any character in ``separators``.

    It ignores those that appear inside ``[...]``.

    Parameters
    ----------
    text : str
        Text value to parse, normalize, or write.
    separators : str
        Separators to supply to the function.

    Returns
    -------
    list[str]
        Collection containing the operation results.
    """
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in text:
        if ch == "[":
            depth += 1
            buf.append(ch)
        elif ch == "]":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif depth == 0 and ch in separators:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return out


_SHARED_ACTIVE = threading.local()


@contextmanager
def shared_active_checks() -> Iterator[None]:
    """Reuse each settings object's ``active?`` answer for the rest of one read-only call.

    PyFluent asks Fluent for a group's ``active?`` (one network round trip)
    every time one of its children is accessed, so reads whose paths share
    prefixes ask the same question many times, which is expensive when Fluent
    runs on another machine. The backend lock is held for the whole call, so
    the answer cannot change and the first one is reused. Behaviour is
    otherwise unchanged: a child of an inactive group still raises
    ``InactiveObjectError``. Must not wrap code that changes settings.
    """
    outer = getattr(_SHARED_ACTIVE, "answers", None)
    if outer is None:
        _SHARED_ACTIVE.answers = {}
    try:
        yield
    finally:
        if outer is None:
            _SHARED_ACTIVE.answers = None


def _shared_is_active(answers: dict[int, tuple[Any, Any]], node: Any) -> Any:
    # The node is kept in the entry so its id cannot be reused by another
    # object while the call runs.
    entry = answers.get(id(node))
    if entry is None or entry[0] is not node:
        entry = (node, node.is_active())
        answers[id(node)] = entry
    return entry[1]


def _settings_group_type() -> Any:
    try:
        from ansys.fluent.core.solver import flobject
    except ImportError:
        return None
    return getattr(flobject, "Group", None)


def child_of(node: Any, name: str) -> Any:
    """Return ``getattr(node, name)``, sharing group ``active?`` checks.

    Inside :func:`shared_active_checks` this mirrors PyFluent's
    ``Group.__getattribute__`` for child access (inactive
    check, then exposure-level check), with the inactive check answered once
    per group for the call. Anything else uses plain ``getattr``.

    Parameters
    ----------
    node : Any
        Settings object whose attribute is read.
    name : str
        Attribute (child) name.

    Returns
    -------
    Any
        The child object.
    """
    answers = getattr(_SHARED_ACTIVE, "answers", None)
    group = _settings_group_type()
    if answers is None or group is None or not isinstance(node, group):
        return getattr(node, name)
    if name not in object.__getattribute__(node, "child_names"):
        return getattr(node, name)
    if _shared_is_active(answers, node) is False:
        from ansys.fluent.core.solver.flobject import InactiveObjectError

        raise InactiveObjectError(node.python_path)
    from ansys.fluent.core.solver import flobject

    hidden = getattr(flobject, "_raise_if_exposure_hidden", None)
    if hidden is not None:
        hidden(name, object.__getattribute__(node, "_child_classes"), node)
    try:
        return object.__getattribute__(node, name)
    except AttributeError:
        return getattr(node, name)


def _fluent_child_names(node: Any) -> dict[str, str]:
    """Map each child's Fluent name (as ``get_attrs`` reports it) to its Python name."""
    try:
        classes = object.__getattribute__(node, "_child_classes")
    except AttributeError:
        return {}
    return {getattr(cls, "fluent_name", name) or name: name for name, cls in classes.items()}


def subtree_attrs(node: Any, attrs: Iterable[str]) -> Optional[dict[str, dict[str, Any]]]:
    """Return ``attrs`` of every active child of ``node`` in one solver round trip.

    Fluent answers a recursive ``get_attrs`` with only the active children,
    so a child missing from the result is inactive. Keys are Python child
    names. Returns ``None`` when the query is not supported, so callers can
    fall back to per-child queries. Inside :func:`shared_active_checks` the
    answers are also recorded for the rest of the call.
    """
    fn = getattr(node, "get_attrs", None)
    if not callable(fn):
        return None
    wanted = list(dict.fromkeys(["active?", *attrs]))
    try:
        raw = fn(wanted, recursive=True)
    except Exception as exc:
        logger.debug("Recursive get_attrs failed for %r: %s", node, exc)
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("children", {}), dict):
        return None
    _remember_subtree(node, raw)
    names = _fluent_child_names(node)
    return {
        names.get(fluent, fluent): dict(entry.get("attrs") or {})
        for fluent, entry in (raw.get("children") or {}).items()
        if isinstance(entry, dict)
    }


def _remember_subtree(node: Any, raw: dict[str, Any]) -> None:
    """Record ``active?`` answers from a recursive ``get_attrs`` reply for this call."""
    answers = getattr(_SHARED_ACTIVE, "answers", None)
    group = _settings_group_type()
    if answers is None or group is None or not isinstance(node, group):
        return
    attrs = raw.get("attrs") or {}
    if "active?" in attrs:
        answers[id(node)] = (node, bool(attrs["active?"]))
    present = raw.get("children") or {}
    for fluent, name in _fluent_child_names(node).items():
        try:
            child = object.__getattribute__(node, name)
        except AttributeError:
            continue
        entry = present.get(fluent)
        if not isinstance(entry, dict):
            answers[id(child)] = (child, False)
        elif isinstance(child, group):
            _remember_subtree(child, entry)
        else:
            answers[id(child)] = (child, bool((entry.get("attrs") or {}).get("active?", True)))


def active_child_names(node: Any) -> Optional[list[str]]:
    """Python names of ``node``'s active, visible, non-deprecated children (one round trip).

    Same result as PyFluent's ``get_active_child_names`` without one query per
    child. Returns ``None`` when the batched query is unavailable, and for
    named-object collections, whose subtree can be large.
    """
    group = _settings_group_type()
    if group is None or not isinstance(node, group):
        return None
    active = subtree_attrs(node, ["active?"])
    if active is None:
        return None
    try:
        from ansys.fluent.core.solver import flobject
    except ImportError:
        flobject = None
    hidden = getattr(flobject, "_is_hidden_by_exposure_level", None)
    deprecated = getattr(flobject, "_is_deprecated", None)
    classes = object.__getattribute__(node, "_child_classes")
    names = []
    for name in object.__getattribute__(node, "child_names"):
        if name not in active or active[name].get("active?") is False:
            continue
        if hidden is not None and name in classes and hidden(classes[name], node):
            continue
        if deprecated is not None:
            try:
                if deprecated(object.__getattribute__(node, name)):
                    continue
            except AttributeError:
                pass
        names.append(name)
    return names


def resolve_path(root: Any, path: str) -> Any:
    """Resolve a Fluent settings ``path`` against ``root`` (== solver.settings).

    Supports indexed segments such as ``...velocity_inlet[cold-in]`` for
    named-object instance access. Raises ``AttributeError``/``KeyError``
    if the path does not resolve.

    Dotted segments that are not exposed as Python attributes (such as a
    NamedObject key containing a dash, like ``phase.phase-1.momentum``)
    are transparently re-tried as ``__getitem__`` lookups. This lets
    callers write the same dotted form for attribute children and
    NamedObject keys, matching how the agent expresses query predicates.

    Inside :func:`shared_active_checks` each group's ``active?`` check is
    made once for the whole call instead of once per path.

    Parameters
    ----------
    root : Any
        Root to supply to the function.
    path : str
        Fluent object path or file-system path to inspect.

    Returns
    -------
    Any
        Result produced by the function.
    """
    parts = normalise_attr_path(path)
    node = root
    for part in parts:
        if "[" in part and part.endswith("]"):
            attr, _, key = part[:-1].partition("[")
            node = child_of(node, attr)
            if key.isdecimal():
                node = node[int(key)]
            else:
                node = node[key.strip("\"'")]
            continue
        try:
            node = child_of(node, part)
        except AttributeError:
            # The part may be a NamedObject KEY rather than an
            # attribute. PyFluent only exposes children whose names
            # are valid Python identifiers as attributes; keys with
            # dashes (``phase-1``), digits-leading, etc. are only
            # reachable via ``node[key]``. ``normalise_attr_path``
            # has already converted dashes to underscores in the
            # attribute-style spelling, so try both forms.
            get_item = getattr(node, "__getitem__", None)
            if get_item is None:
                raise
            for candidate in (part, part.replace("_", "-")):
                try:
                    node = get_item(candidate)
                except Exception as exc:
                    logger.debug("Failed to get item '%s': %s", candidate, exc)
                    continue
                else:
                    break
            else:
                raise
    return node


# ---------------------------------------------------------------------------
# Discovery — generic, no hardcoded paths
# ---------------------------------------------------------------------------


def discover_named_objects_via_walk(
    root: Any,
    *,
    max_depth: int = 4,
    max_entries: int = 200,
) -> dict[str, list[str]]:
    """Walk the settings tree and collect every named-object collection.

    A node is considered a named-object collection if it exposes
    ``get_object_names()`` (or has dictionary-like ``keys()``).

    Returns a mapping of slash-separated path → list of instance names.

    Parameters
    ----------
    root : Any
        Root to supply to the function.
    max_depth : int
        Maximum depth to supply to the function.
    max_entries : int
        Maximum entries to supply to the function.

    Returns
    -------
    dict[str, list[str]]
        Mapping containing the operation result.
    """
    out: dict[str, list[str]] = {}

    def _names(node: Any) -> Optional[list[str]]:
        """Return available names for the current object.

        Parameters
        ----------
        node : Any
            Node being inspected or registered.

        Returns
        -------
        Optional[list[str]]
            Optional value produced by the operation.
        """
        for accessor in ("get_object_names", "child_names", "_names"):
            fn = getattr(node, accessor, None)
            if fn is None:
                continue
            try:
                v = fn() if callable(fn) else fn
            except Exception as exc:
                logger.debug("Failed to get names from '%s': %s", accessor, exc)
                continue
            if isinstance(v, (list, tuple)):
                # `child_names` is also present on regular containers, so
                # only treat it as a name list when the node is indexable.
                if accessor == "child_names" and not _is_indexable(node):
                    return None
                return [str(n) for n in v]
        if hasattr(node, "keys") and _is_indexable(node):
            try:
                return [str(k) for k in node.keys()]  # type: ignore[call-arg]
            except Exception:
                return None
        return None

    def _walk(node: Any, slash_path: str, depth: int) -> None:
        """Walk the object tree and collect matching entries.

        Parameters
        ----------
        node : Any
            Node to inspect or register.
        slash_path : str
            Path for the slash.
        depth : int
            Depth to supply to the function.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        if len(out) >= max_entries or depth > max_depth:
            return
        names = _names(node)
        if names is not None:
            out[slash_path] = names
            # Don't recurse into instances; one schema per collection.
            return
        cn = getattr(node, "child_names", None)
        if not cn:
            return
        for child in cn:
            try:
                child_obj = child_of(node, child)
            except Exception as exc:
                logger.debug("Failed to get child '%s' of '%s': %s", child, slash_path, exc)
                continue
            child_label = child.replace("_", "-")
            _walk(
                child_obj, f"{slash_path}/{child_label}" if slash_path else child_label, depth + 1
            )

    _walk(root, "", 0)
    if len(out) >= max_entries:
        logger.warning(
            "discover_named_objects_via_walk truncated at max_entries=%d "
            "(max_depth=%d). Some named-object collections may be missing; "
            "increase the discovery limits or query a specific collection.",
            max_entries,
            max_depth,
        )
    return out


def _is_indexable(node: Any) -> bool:
    """Return whether indexable.

    Parameters
    ----------
    node : Any
        Node being inspected or registered.

    Returns
    -------
    bool
        Boolean result of the operation.
    """
    return hasattr(node, "__getitem__") and not isinstance(node, (str, bytes))


# ---------------------------------------------------------------------------
# Targeted context — feature parity with /fluent_get_targeted_context
# ---------------------------------------------------------------------------


def collect_targeted_context(
    root: Any,
    *,
    paths_to_check: Iterable[str],
    named_object_types: Iterable[str] = (),
    instance_state_fetch: Iterable[str] = (),
) -> dict[str, Any]:
    """Replicate the legacy targeted-context endpoint against a live solver.

    Returns a dictionary with the following keys (all optional. Missing entries simply
    don't appear.

    * ``active_status``    : path → bool
    * ``state_values``     : path → state value (or ``"<large_state_omitted>"``)
    * ``child_names``      : path → list of child setting names
    * ``named_objects``    : type_path → list of instance names
    * ``allowed_values``   : property path → list of allowed values
    * ``phase_property_map``: phase_name → list of property categories

    Parameters
    ----------
    root : Any
        Root to supply to the function.
    paths_to_check : Iterable[str]
        Paths to check to supply to the function.
    named_object_types : Iterable[str]
        Named object types to supply to the function.
    instance_state_fetch : Iterable[str]
        Instance state fetch to supply to the function.

    Returns
    -------
    dict[str, Any]
        Mapping containing the operation result.
    """
    paths_to_check = list(paths_to_check)
    named_object_types = list(named_object_types)
    instance_state_fetch = list(instance_state_fetch)

    active_status: dict[str, bool] = {}
    state_values: dict[str, Any] = {}
    child_names_map: dict[str, list[str]] = {}
    named_objects_map: dict[str, list[str]] = {}
    allowed_values_map: dict[str, list[Any]] = {}

    # ---- per-path active / state / child_names ------------------------
    for dot_path in paths_to_check:
        try:
            node = resolve_path(root, dot_path)
        except (AttributeError, KeyError, TypeError):
            active_status[dot_path] = False
            continue

        active_status[dot_path] = _is_active(node)

        cn = getattr(node, "child_names", None)
        if cn:
            child_names_map[dot_path] = list(cn)

        # Skip get_state on collection containers (their state recurses
        # over every instance and is huge).
        if active_status[dot_path] and not _looks_like_collection_container(node, cn):
            try:
                if hasattr(node, "get_state"):
                    st = node.get_state()
                    state_values[dot_path] = _bound_state(st, _MAX_STATE_BYTES)
                elif not callable(node):
                    state_values[dot_path] = node() if callable(node) else str(node)
            except Exception as exc:
                logger.debug("Failed to get state for '%s': %s", dot_path, exc)

    # ---- named-object instance lists ----------------------------------
    for type_path in named_object_types:
        try:
            node = resolve_path(root, type_path)
        except (AttributeError, KeyError, TypeError):
            named_objects_map[type_path] = []
            continue
        try:
            names = node.get_object_names()
            named_objects_map[type_path] = [str(n) for n in (names or [])]
        except Exception as exc:
            logger.debug("Failed to get object names for '%s': %s", type_path, exc)
            try:
                named_objects_map[type_path] = [str(k) for k in node.keys()]  # type: ignore[call-arg]
            except Exception as exc:
                logger.debug("Failed to get keys for '%s': %s", type_path, exc)
                named_objects_map[type_path] = []

    # ---- allowed-values walk (depth-limited, size-capped) -------------
    for dot_path in paths_to_check:
        if len(allowed_values_map) >= _MAX_AV_ENTRIES:
            break
        if not active_status.get(dot_path):
            continue
        try:
            node = resolve_path(root, dot_path)
        except Exception as exc:
            logger.debug("Failed to resolve path '%s' for allowed-values: %s", dot_path, exc)
            continue
        _collect_allowed_values(node, dot_path, allowed_values_map, depth=0)

    # Walk first instance of each named-object type for property allowed values.
    for type_path, names in named_objects_map.items():
        if len(allowed_values_map) >= _MAX_AV_ENTRIES or not names:
            continue
        try:
            coll = resolve_path(root, type_path)
            first = coll[names[0]]
            inst_prefix = type_path.replace("/", ".").replace("-", "_") + ".<instance>"
            _collect_allowed_values(first, inst_prefix, allowed_values_map, depth=0)
        except Exception as exc:
            logger.debug("Failed to collect allowed values for '%s': %s", type_path, exc)
            continue

    # ---- multiphase phase-property availability -----------------------
    phase_property_map = _probe_phase_properties(root, state_values, named_objects_map)

    # ---- explicit per-instance state fetch ----------------------------
    for fetch in instance_state_fetch:
        # Accept either "type_path/inst" or "type_path[inst]".
        if "[" in fetch and fetch.endswith("]"):
            head, _, key = fetch[:-1].partition("[")
            type_path, inst_name = head, key.strip("\"'")
        else:
            parts = fetch.rsplit("/", 1)
            if len(parts) != 2:
                continue
            type_path, inst_name = parts
        try:
            coll = resolve_path(root, type_path)
            inst = coll[inst_name]
        except Exception as exc:
            logger.debug(
                "Failed to fetch instance '%s' for type '%s': %s", inst_name, type_path, exc
            )
            continue
        # Always check active status BEFORE get_state to avoid
        # "object is inactive" exceptions from PyFluent.
        if not _is_active(inst):
            active_status[fetch] = False
            state_values[fetch] = {"inactive": True}
            continue
        active_status[fetch] = True
        try:
            if hasattr(inst, "get_state"):
                state_values[fetch] = _bound_state(inst.get_state(), _MAX_INSTANCE_STATE_BYTES)
        except Exception as exc:
            logger.debug(
                "Failed to get state for instance '%s' of type '%s': %s", inst_name, type_path, exc
            )
            continue

    return {
        "active_status": active_status,
        "state_values": state_values,
        "child_names": child_names_map,
        "named_objects": named_objects_map,
        "allowed_values": allowed_values_map,
        "phase_property_map": phase_property_map,
    }


def collect_global_state(
    root: Any,
    paths: Optional[Iterable[str]] = None,
) -> dict[str, Any]:
    """Fetch state for the requested paths, defaulting to the global set.

    Always probes ``is_active()`` before calling ``get_state()`` so that
    inactive objects (such as ``setup/models/viscous`` while the model is
    not enabled) yield a structured ``{"inactive": True}`` marker rather
    than raising the PyFluent ``object is inactive`` RuntimeError. This
    matches the gating already done in :func:`collect_targeted_context`
    and the ``active?`` semantics defined by the Fluent Settings API,
    where every setting may declare an ``active?`` predicate.

    Parameters
    ----------
    root : Any
        Root to supply to the function.
    paths : Optional[Iterable[str]]
        Fluent object paths to supply to the operation.

    Returns
    -------
    dict[str, Any]
        Mapping containing the operation result.
    """
    out: dict[str, Any] = {}
    for p in list(paths) if paths else FLUENT_GLOBAL_STATE_PATHS:
        try:
            node = resolve_path(root, p)
        except Exception as exc:
            msg = str(exc)
            if "inactive" in msg.lower():
                out[p] = {"inactive": True}
            else:
                out[p] = {"error": msg}
            continue
        # Kind-aware filtering: Commands and Queries are CALLABLES that
        # produce a side effect / value when invoked; reading state on
        # them is meaningless and on some Fluent builds raises a
        # server-side error. Detect by ``argument_names`` (set on
        # Command/Query) or by ``command_names`` (group-style) — both
        # are stable PyFluent base-class attributes.
        if _is_command_or_query(node):
            out[p] = {"skipped": "command_or_query"}
            continue
        # Skip Command-argument leaves: probing them produces a
        # server-side "attr value not defined" error in Fluent.
        if _is_command_argument(node):
            out[p] = {"skipped": "command_argument"}
            continue
        if not _is_active(node):
            out[p] = {"inactive": True}
            continue
        try:
            if hasattr(node, "get_state"):
                out[p] = _bound_state(
                    _prune_inactive_children(node, node.get_state()),
                    _MAX_STATE_BYTES,
                )
            elif not callable(node):
                out[p] = node() if callable(node) else str(node)
        except Exception as exc:
            # Defensive: a setting may flip inactive between the
            # is_active probe and get_state, or raise for other reasons.
            msg = str(exc)
            if "inactive" in msg.lower():
                out[p] = {"inactive": True}
            else:
                out[p] = {"error": msg}
    return out


def _is_command_or_query(node: Any) -> bool:
    """Return True when ``node`` itself is a PyFluent Command or Query.

    Distinct from :func:`_is_command_argument`, which detects the
    *child* arguments of a Command. Both Commands and Queries expose
    ``argument_names`` as a tuple of strings; only callables that also
    expose their own arguments are considered here. We additionally
    require the node to be callable, because some plain Group nodes
    have an empty ``argument_names`` attribute on certain builds.

    Parameters
    ----------
    node : Any
        Node to supply to the function.

    Returns
    -------
    bool
        Boolean result produced by the function.
    """
    arg_names = getattr(node, "argument_names", None)
    if arg_names is None:
        return False
    if not callable(node):
        return False
    # ``argument_names`` should be an iterable of strings on real
    # Command/Query nodes; reject anything else.
    try:
        return any(isinstance(n, str) for n in arg_names) or len(arg_names) == 0
    except TypeError:
        return False


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _is_active(node: Any) -> bool:
    """Return whether active.

    Inside :func:`shared_active_checks` the answer is reused for the rest
    of the call.

    Parameters
    ----------
    node : Any
        Node being inspected or registered.

    Returns
    -------
    bool
        Boolean result of the operation.
    """
    fn = getattr(node, "is_active", None)
    if not callable(fn):
        # No is_active method → implicitly active when the node resolves.
        return True
    try:
        answers = getattr(_SHARED_ACTIVE, "answers", None)
        if answers is not None:
            return bool(_shared_is_active(answers, node))
        return bool(fn())
    except Exception:
        return False


def _is_command_argument(node: Any) -> bool:
    """Return True when ``node`` is a PyFluent Command-argument leaf.

    Reading ``.get_state()`` / ``.value`` on such a node makes the
    Fluent solver log a server-side ``Error: attr value not defined``
    because the parent Command has not been invoked. We must skip
    these for any read-only state probe.

    Detection: PyFluent Command objects expose ``argument_names`` (a
    tuple of strings naming their argument children). A node whose
    parent has ``argument_names`` and lists this node's
    ``_python_name`` (or attribute name) in that tuple is an argument
    leaf.

    Parameters
    ----------
    node : Any
        Node to supply to the function.

    Returns
    -------
    bool
        Boolean result produced by the function.
    """
    parent = getattr(node, "_parent", None)
    if parent is None:
        return False
    arg_names = getattr(parent, "argument_names", None)
    if not arg_names:
        return False
    own_name = getattr(node, "_python_name", None) or getattr(node, "obj_name", None)
    if own_name is None:
        return False
    try:
        return str(own_name) in {str(n) for n in arg_names}
    except TypeError:
        return False


def _looks_like_collection_container(node: Any, child_names: Any) -> bool:
    """Heuristic: True if `node` is a container of named-object collections.

    Mirrors the legacy endpoint's check — peek at the first few children
    and see if any expose ``get_object_names``.

    Parameters
    ----------
    node : Any
        Node to supply to the function.
    child_names : Any
        Child names to supply to the function.

    Returns
    -------
    bool
        Boolean result produced by the function.
    """
    if not child_names:
        return False
    for child in list(child_names)[:3]:
        try:
            if hasattr(child_of(node, child), "get_object_names"):
                return True
        except Exception as exc:
            logger.debug(
                "Failed to check child '%s' of node '%s' for get_object_names: %s", child, node, exc
            )
            continue
    return False


def _bound_state(value: Any, limit_bytes: int) -> Any:
    """Return state bound to the selected object path.

    Parameters
    ----------
    value : Any
        Value to inspect, convert, or store.
    limit_bytes : int
        Limit bytes to supply to the function.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    try:
        s = json.dumps(value, default=str)
    except Exception:
        return str(value)[:limit_bytes]
    if len(s) <= limit_bytes:
        return value
    return "<large_state_omitted>"


def _prune_inactive_children(node: Any, state: Any) -> Any:
    """Drop inactive child keys from a Group's ``get_state()`` dict.

    Fluent's settings tree exposes ``get_active_child_names()`` on
    Group nodes. When present and successful, restrict the returned
    dict to active children only — inactive subtrees (e.g. species
    settings while the species model is off) carry default noise that
    bloats the state payload and confuses callers.

    Tolerates the known ``setup.models`` Scheme bug where the call
    raises ``RuntimeError``: on failure we return ``state`` unchanged.
    Non-dict states and nodes without ``get_active_child_names`` also
    pass through unchanged.

    Parameters
    ----------
    node : Any
        Node to supply to the function.
    state : Any
        State to supply to the function.

    Returns
    -------
    Any
        Result produced by the function.
    """
    if not isinstance(state, dict):
        return state
    active = active_child_names(node)
    if active is None:
        fn = getattr(node, "get_active_child_names", None)
        if not callable(fn):
            return state
        try:
            active = fn()
        except (RuntimeError, AttributeError, TypeError):
            return state
    if not active:
        return state
    active_set = {str(a) for a in active}
    pruned = {k: v for k, v in state.items() if str(k) in active_set}
    # Only return the pruned view when it actually shrunk things.
    # Otherwise keep the original (defensive: if active_set is wrong
    # we don't want to silently drop everything).
    if pruned and len(pruned) < len(state):
        return pruned
    return state


def _collect_allowed_values(
    node: Any,
    prefix: str,
    out: dict[str, list[Any]],
    *,
    depth: int,
) -> None:
    """Collect allowed values.

    Parameters
    ----------
    node : Any
        Node being inspected or registered.
    prefix : str
        Prefix to supply to the function.
    out : dict[str, list[Any]]
        Out to supply to the function.
    depth : int
        Depth to supply to the function.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    if depth > _MAX_AV_DEPTH or len(out) >= _MAX_AV_ENTRIES:
        return
    cn = getattr(node, "child_names", None)
    if not cn:
        return
    for child_name in cn:
        if len(out) >= _MAX_AV_ENTRIES:
            return
        try:
            child = child_of(node, child_name)
        except Exception as exc:
            logger.debug("Failed to get child '%s' of '%s': %s", child_name, prefix, exc)
            continue
        child_path = f"{prefix}.{child_name}"
        if hasattr(child, "allowed_values"):
            try:
                avs = child.allowed_values()
                if avs:
                    out[child_path] = list(avs)[:_MAX_AV_VALUES]
            except Exception as exc:
                logger.debug("Failed to get allowed_values for '%s': %s", child_path, exc)
        if depth < _MAX_AV_DEPTH:
            _collect_allowed_values(child, child_path, out, depth=depth + 1)


def _probe_phase_properties(
    root: Any,
    state_values: dict[str, Any],
    named_objects_map: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Probe per-phase property availability when multiphase is active.

    Returns ``{phase_name: [property_categories]}`` or an empty dict when
    multiphase is off / not applicable. Mirrors the legacy endpoint.

    Parameters
    ----------
    root : Any
        Root to supply to the function.
    state_values : dict[str, Any]
        State values to supply to the function.
    named_objects_map : dict[str, list[str]]
        Named objects map to supply to the function.

    Returns
    -------
    dict[str, list[str]]
        Mapping containing the operation result.
    """
    out: dict[str, list[str]] = {}
    mp_state = state_values.get("setup.models.multiphase") or state_values.get(
        "setup/models/multiphase"
    )
    mp_model = ""
    if isinstance(mp_state, dict):
        mp_model = str(mp_state.get("model", "")).lower()
    if not mp_model or mp_model in ("off", "none", "disabled", ""):
        return out

    phase_names = (
        named_objects_map.get("setup/models/multiphase/phases")
        or named_objects_map.get("setup.models.multiphase.phases")
        or []
    )
    if not phase_names:
        return out

    bc_type_order = ("velocity_inlet", "pressure_outlet", "pressure_inlet", "mass_flow_inlet")
    rep = None
    # UTL exposes the BC tree under ``setup.physics.boundaries``; the
    # standard root goes inactive when UTL is enabled. Try both so the
    # multiphase phase-property probe surfaces in either mode without
    # the caller having to know which one is live.
    bc_root_candidates: list[Any] = []
    try:
        bc_root_candidates.append(root.setup.boundary_conditions)
    except Exception as exc:
        logger.debug("Failed to get BC root 'setup.boundary_conditions': %s", exc)
    try:
        bc_root_candidates.append(root.setup.physics.boundaries)
    except Exception as exc:
        logger.debug("Failed to get BC root 'setup.physics.boundaries': %s", exc)
    if not bc_root_candidates:
        return out
    for bcs in bc_root_candidates:
        for bc_type in bc_type_order:
            try:
                bc_coll = getattr(bcs, bc_type)
                names = (
                    bc_coll.get_object_names()
                    if hasattr(bc_coll, "get_object_names")
                    else list(bc_coll.keys())  # type: ignore[call-arg]
                )
                if names:
                    rep = bc_coll[names[0]]
                    break
            except Exception as exc:
                logger.debug("Failed to get BC collection '%s' from '%s': %s", bc_type, bcs, exc)
                continue
        if rep is not None:
            break

    if rep is None or not hasattr(rep, "phase"):
        return out

    candidate_phases = list(phase_names)
    if "mixture" not in candidate_phases:
        candidate_phases.append("mixture")
    for phase_name in candidate_phases:
        try:
            phase_node = rep.phase[phase_name]
            pcn = getattr(phase_node, "child_names", None)
            if pcn:
                out[phase_name] = list(pcn)
        except Exception as exc:
            logger.debug("Failed to get child '%s' of '%s': %s", phase_name, rep.phase, exc)
            continue
    return out


def collect_active_status(root: Any, paths: Iterable[str]) -> dict[str, bool]:
    """Return ``{path: is_active}``; a path that does not resolve is inactive.

    Parameters
    ----------
    root : Any
        Settings root.
    paths : Iterable[str]
        Fluent object paths.

    Returns
    -------
    dict[str, bool]
        Active status per path.
    """
    out: dict[str, bool] = {}
    for path in paths:
        try:
            node = resolve_path(root, path)
        except Exception:  # broaden: bracket-subscript paths
            # ``solution.controls.under_relaxation["pressure"]`` can raise
            # non-(AttributeError/KeyError/TypeError) after a case reload
            # (settings tree rebuilt with a different schema for the new
            # pv-coupling). Treat every resolve failure as "not active" so the
            # caller still gets a complete status map.
            out[path] = False
            continue
        fn = getattr(node, "is_active", None)
        out[path] = _is_active(node) if callable(fn) else True
    return out


def collect_allowed_values(root: Any, paths: Iterable[str]) -> dict[str, list[Any]]:
    """Return ``{path: allowed values}``; unresolved paths map to ``[]``.

    Parameters
    ----------
    root : Any
        Settings root.
    paths : Iterable[str]
        Fluent object paths.

    Returns
    -------
    dict[str, list[Any]]
        Allowed values per path.
    """
    out: dict[str, list[Any]] = {}
    for path in paths:
        try:
            node = resolve_path(root, path)
        except (AttributeError, KeyError, TypeError):
            out[path] = []
            continue
        values: list[Any] = []
        for accessor in ("allowed_values", "get_allowed_values"):
            fn = getattr(node, accessor, None)
            if callable(fn):
                try:
                    values = list(fn())
                    break
                except Exception as exc:
                    logger.debug("allowed_values() failed for %s: %s", path, exc, exc_info=True)
        out[path] = values
    return out


def probe_paths(root: Any, paths: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Return ``{path: {exists, is_active, is_user_creatable, kind}}`` for each path.

    ``kind`` is one of ``"missing"``, ``"command"``, ``"query"``,
    ``"named_object"``, ``"group"``, ``"leaf"``; unusual nodes default to
    ``"leaf"``. Each path costs one batched ``get_attrs`` call.

    Parameters
    ----------
    root : Any
        Settings root.
    paths : Iterable[str]
        Fluent object paths.

    Returns
    -------
    dict[str, dict[str, Any]]
        Facts per path.
    """
    out: dict[str, dict[str, Any]] = {}
    for path in paths:
        try:
            node = resolve_path(root, path)
        except (AttributeError, KeyError, TypeError):
            out[path] = {"exists": False, "kind": "missing"}
            continue
        rec: dict[str, Any] = {
            "exists": True,
            "is_active": True,
            "is_user_creatable": False,
            "kind": "leaf",
        }
        # Kind sniff — cheap class/attr probes only.
        arg_names = getattr(node, "argument_names", None)
        if arg_names is not None and callable(node):
            rec["kind"] = "query" if "query" in type(node).__name__.lower() else "command"
        elif getattr(node, "child_object_type", None) is not None:
            rec["kind"] = "named_object"
        elif hasattr(node, "child_names") and getattr(node, "child_names", None):
            rec["kind"] = "group"
        fn = getattr(node, "get_attrs", None)
        if callable(fn):
            try:
                raw = fn(["active?", "user-creatable?"]) or {}
            except Exception:
                raw = {}
            if isinstance(raw, dict):
                if "active?" in raw:
                    rec["is_active"] = bool(raw["active?"])
                if "user-creatable?" in raw:
                    rec["is_user_creatable"] = bool(raw["user-creatable?"])
        else:
            is_active = getattr(node, "is_active", None)
            if callable(is_active):
                try:
                    rec["is_active"] = bool(is_active())
                except Exception as exc:
                    logger.debug("is_active() failed for %s: %s", path, exc, exc_info=True)
        out[path] = rec
    return out


__all__ = [
    "FLUENT_GLOBAL_STATE_PATHS",
    "normalise_attr_path",
    "resolve_path",
    "discover_named_objects_via_walk",
    "collect_targeted_context",
    "collect_global_state",
    "collect_active_status",
    "collect_allowed_values",
    "probe_paths",
]
