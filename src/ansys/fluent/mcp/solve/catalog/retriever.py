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

"""API retrieval (RAG over the Fluent API catalog).

Only the lexical retriever is supported.

:class:`LexicalApiRetriever` is a thin async adapter over
:class:`~ansys.fluent.mcp.common.api_index.ApiIndex`, which scores the
bundled ``api_objects.json`` plus PyFluent class docstrings using BM25.
All ranking lives in :mod:`ansys.fluent.mcp.common.api_index`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import logging
import os
import threading
from typing import Any, Optional, Sequence

import httpx

from ansys.fluent.mcp.solve.catalog.index import ApiIndex, get_default_api_index

logger = logging.getLogger("ansys.fluent.mcp.api_retriever")


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class ApiHit:
    """One ranked candidate returned by a retriever."""

    path: str  # dotted Fluent settings path (no instance keys)
    kind: str  # Parameter | Command | Object | Group | ...
    score: float  # higher is better; scale is retriever-specific
    raw: Optional[str] = None  # original payload line or document, when known
    payload: Optional[dict[str, Any]] = None  # extra metadata when available

    def to_tool_dict(self) -> dict[str, Any]:
        """Convert the object to a tool response dictionary.

        Returns
        -------
        dict[str, Any]
            Mapping containing the operation result.
        """
        out: dict[str, Any] = {
            "path": self.path,
            "kind": self.kind,
            "score": round(float(self.score), 4),
        }
        if self.raw:
            out["raw"] = self.raw
        if self.payload:
            out["payload"] = self.payload
        note = _SCHEMA_ONLY_COMMANDS.get(self.path)
        if note:
            out["note"] = note
        return out


# Paths that appear in the schema but are not callable at runtime on
# solver instances.  Added as a ``note`` field in to_tool_dict() so
# every consumer (agent loop and MCP) sees the warning without any
# extra tokens unless the broken path actually surfaces in results.
_SCHEMA_ONLY_COMMANDS: dict[str, str] = {
    "results.scene.add_to_graphics": (
        "SCHEMA-ONLY: not callable on scene instances at runtime. "
        "Use results.scene['<name>'].graphics_objects.set_state("
        "{'<obj-name>': {'name': '<obj-name>', 'transparency': 0-100}}) instead."
    ),
    "results.graphics.views.save_hardcopy": (
        "Does not exist at runtime. "
        "Use results.graphics.picture.save_picture(file_name='<path.png>') instead."
    ),
    "results.field_functions": (
        "Does not exist. Use results.custom_field_functions.create(name=..., definition=...) instead."  # noqa: E501
    ),
    "mesh.check_mesh": ("Does not exist. Use solver.settings.mesh.check() instead."),
    "results.report.surface_integrals": (
        "Commands under results.report.* print to the Fluent console and return None — "
        "NOT for programmatic data extraction. "
        "For data use solution.report_definitions.<type>.create(...) then compute()."
    ),
}


class ApiRetriever(ABC):
    """ABC for an API retriever. Async by convention."""

    name: str = "abstract"

    @abstractmethod
    async def retrieve(
        self,
        query: str,
        *,
        top_k: int = 10,
        kinds: Optional[Sequence[str]] = None,
        under: Optional[str] = None,
    ) -> list[ApiHit]:
        """Retrieve API hits that match the query and filters.

        Parameters
        ----------
        query : str
            Search text or user request to evaluate.
        top_k : int
            Maximum number of results to return.
        kinds : Optional[Sequence[str]]
            Optional result kinds used to narrow the operation.
        under : Optional[str]
            Optional path prefix used to scope the operation.

        Returns
        -------
        list[ApiHit]
            List of results produced by the operation.
        """
        ...

    async def aclose(self) -> None:
        """Close resources for the ApiRetriever object.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        return None


# ---------------------------------------------------------------------------
# Lexical retriever
# ---------------------------------------------------------------------------


class LexicalApiRetriever(ApiRetriever):
    """Thin async adapter over :class:`ApiIndex.search`.

    Carries no scoring logic of its own — the BM25 ranker, substring
    bonus and depth penalty all live in
    :mod:`ansys.fluent.mcp.common.api_index`.
    """

    name = "lexical"

    def __init__(self, index: Optional[ApiIndex] = None) -> None:
        """Initialize the LexicalApiRetriever instance.

        Parameters
        ----------
        index : Optional[ApiIndex]
            Index to supply to the function.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        self._index = index or get_default_api_index()

    async def retrieve(
        self,
        query: str,
        *,
        top_k: int = 10,
        kinds: Optional[Sequence[str]] = None,
        under: Optional[str] = None,
    ) -> list[ApiHit]:
        """Retrieve API hits that match the query and filters.

        Parameters
        ----------
        query : str
            Search text or user request to evaluate.
        top_k : int
            Maximum number of results to return.
        kinds : Optional[Sequence[str]]
            Optional result kinds used to narrow the operation.
        under : Optional[str]
            Optional path prefix used to scope the operation.

        Returns
        -------
        list[ApiHit]
            List of results produced by the operation.
        """
        if not self._index.available:
            return []
        hits = self._index.search(
            query,
            top_k=top_k,
            kinds=list(kinds) if kinds else None,
            under=under,
        )
        return [
            ApiHit(path=h.entry.path, kind=h.entry.kind, score=float(h.score), raw=h.entry.raw)
            for h in hits
        ]


class HttpApiRetriever(ApiRetriever):
    """HTTP forwarder compatible with the legacy encoded-elements service."""

    name = "http"

    def __init__(
        self,
        url: str,
        *,
        client: Optional[httpx.AsyncClient] = None,
        collection_name: str | None = None,
    ) -> None:
        """Initialize the HTTP API retriever."""
        self.url = url
        self.collection_name = collection_name
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None

    async def retrieve(
        self,
        query: str,
        *,
        top_k: int = 10,
        kinds: Optional[Sequence[str]] = None,
        under: Optional[str] = None,
    ) -> list[ApiHit]:
        """Retrieve API hits from an HTTP retrieval endpoint."""
        payload: dict[str, Any] = {
            "query": query,
            "top_k": top_k,
        }
        if self.collection_name:
            payload["collection_name"] = self.collection_name
        if kinds:
            payload["kinds"] = list(kinds)
        if under:
            payload["under"] = under

        try:
            response = await self._client.post(self.url, json=payload)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("HTTP API retriever failed: %s", exc)
            return []

        raw_hits = data.get("hits", []) if isinstance(data, dict) else []
        hits: list[ApiHit] = []
        for raw_hit in raw_hits:
            if not isinstance(raw_hit, dict):
                continue
            path = raw_hit.get("path")
            kind = raw_hit.get("kind")
            if not isinstance(path, str) or not isinstance(kind, str):
                continue
            payload_extra = {
                key: value
                for key, value in raw_hit.items()
                if key not in {"path", "kind", "score", "raw"}
            }
            hits.append(ApiHit(
                path=path,
                kind=kind,
                score=float(raw_hit.get("score", 0.0)),
                raw=raw_hit.get("raw") if isinstance(raw_hit.get("raw"), str) else None,
                payload=payload_extra or None,
            ))
        return hits

    async def aclose(self) -> None:
        """Close the owned HTTP client, when applicable."""
        if self._owns_client:
            await self._client.aclose()


class QdrantApiRetriever(ApiRetriever):
    """Compatibility placeholder for legacy Qdrant retriever configuration."""

    name = "qdrant"

    def __init__(self, url: str, *, collection_name: str | None = None) -> None:
        """Initialize the Qdrant API retriever."""
        self.url = url
        self.collection_name = collection_name

    async def retrieve(
        self,
        query: str,
        *,
        top_k: int = 10,
        kinds: Optional[Sequence[str]] = None,
        under: Optional[str] = None,
    ) -> list[ApiHit]:
        """Return no hits when no Qdrant client integration is installed."""
        return []


# ---------------------------------------------------------------------------
# Default factory
# ---------------------------------------------------------------------------


_default_retriever: Optional[ApiRetriever] = None
_default_lock = threading.Lock()


def _build_default() -> ApiRetriever:
    """Build the default API retriever.

    Returns
    -------
    ApiRetriever
        Result produced by the function.
    """
    http_url = os.environ.get("FLUIDS_MCP_API_RETRIEVER_URL")
    if http_url:
        logger.info("Using HttpApiRetriever from FLUIDS_MCP_API_RETRIEVER_URL")
        return HttpApiRetriever(
            http_url,
            collection_name=os.environ.get("FLUIDS_MCP_API_RETRIEVER_COLLECTION"),
        )

    qdrant_url = os.environ.get("FLUIDS_MCP_QDRANT_URL")
    if qdrant_url:
        logger.info("Using QdrantApiRetriever from FLUIDS_MCP_QDRANT_URL")
        return QdrantApiRetriever(
            qdrant_url,
            collection_name=os.environ.get("FLUIDS_MCP_QDRANT_COLLECTION"),
        )

    logger.info("Using LexicalApiRetriever (default, BM25 over api_objects.json + docstrings)")
    return LexicalApiRetriever()


def get_default_api_retriever() -> ApiRetriever:
    """Return the default API retriever.

    Returns
    -------
    ApiRetriever
        API retriever produced by the operation.
    """
    global _default_retriever
    if _default_retriever is None:
        with _default_lock:
            if _default_retriever is None:
                _default_retriever = _build_default()
    return _default_retriever


def set_default_api_retriever(retriever: Optional[ApiRetriever]) -> None:
    """Test/integration hook to override the default.

    Parameters
    ----------
    retriever : Optional[ApiRetriever]
        Retriever to supply to the function.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    global _default_retriever
    with _default_lock:
        _default_retriever = retriever


__all__ = [
    "ApiHit",
    "ApiRetriever",
    "HttpApiRetriever",
    "LexicalApiRetriever",
    "QdrantApiRetriever",
    "_build_default",
    "get_default_api_retriever",
    "set_default_api_retriever",
]
