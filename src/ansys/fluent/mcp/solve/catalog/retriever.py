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

The following three retriever implementations are provided. They are selected
via environment variables. (See :func:`get_default_api_retriever`.)

* :class:`HttpApiRetriever`: Forwards to a remote HTTP retriever
  endpoint that wraps the ``GetEncodedElements`` workflow.
  Opt-in via ``FLUIDS_MCP_API_RETRIEVER_URL``. Dormant by default.

* :class:`QdrantApiRetriever`: Talks to Qdrant directly using
  ``qdrant-client``. Embeddings are produced by an injected callable
  so no model is pinned here. Opt-in via ``FLUIDS_MCP_QDRANT_URL``.
  Dormant by default.

* :class:`LexicalApiRetriever`: The **default and only** retriever
  in a stock install. A thin async adapter over
  :class:`~ansys.fluent.mcp.common.api_index.ApiIndex`, which scores the
  bundled ``api_objects.json`` plus PyFluent class docstrings using
  BM25 ( at no extra cost versus the previous token-overlap heuristic).
  All ranking lives in :mod:`ansys.fluent.mcp.common.api_index`. This
  class only marshals between the ``ApiRetriever`` async protocol
  and the synchronous ``ApiIndex.search`` call.

The HTTP/Qdrant classes are kept around as option-value (~150 LOC,
tested) for the day a partner wants to plug in a hosted index, but
they are not on the hot path and should not receive new investment.
Improve the lexical path instead.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import logging
import os
import threading
from typing import Any, Awaitable, Callable, Optional, Sequence

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


EmbedFn = Callable[[str], Awaitable[list[float]]]


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
# Egress guard (shared by the opt-in network retrievers)
# ---------------------------------------------------------------------------


def _egress_allowed(url: str | None) -> bool:
    """Whether an outbound retrieval call to ``url`` is permitted.

    Honors the suite-wide egress controls so the opt-in network
    retrievers cannot bypass them:

    * ``FLUIDS_AGENT_OFFLINE`` truthy -> block all outbound retrieval.
    * ``FLUIDS_AGENT_ALLOWED_LLM_HOSTS`` (comma-separated) -> block any
      host not on the allowlist.

    Returns ``True`` when the call may proceed. Logs and returns ``False``
    when blocked, so callers degrade to an empty result set.

    Parameters
    ----------
    url : str | None
        Url to supply to the function.

    Returns
    -------
    bool
        Boolean result produced by the function.
    """
    from ansys.fluent.mcp.common.llm_wire import env_flag

    if env_flag("FLUIDS_AGENT_OFFLINE", default=False):
        logger.warning("FLUIDS_AGENT_OFFLINE is set; skipping network retrieval to %s", url)
        return False
    raw_allow = os.environ.get("FLUIDS_AGENT_ALLOWED_LLM_HOSTS", "")
    allowed = {h.strip().lower() for h in raw_allow.split(",") if h.strip()}
    if allowed and url:
        from urllib.parse import urlparse

        host = (urlparse(url).hostname or "").lower()
        if host and host not in allowed:
            logger.warning(
                "Retrieval host %r not in FLUIDS_AGENT_ALLOWED_LLM_HOSTS %r; skipping network retrieval.",  # noqa: E501
                host,
                sorted(allowed),
            )
            return False
    return True


# ---------------------------------------------------------------------------
# HTTP forwarder (preferred when Fluids One service is reachable)
# ---------------------------------------------------------------------------


class HttpApiRetriever(ApiRetriever):
    """Forward retrieval to an HTTP endpoint that wraps the legacy ``GetEncodedElements`` (Qdrant-backed) workflow.

    The endpoint is expected to accept ``POST <url>`` with JSON body::

        {
            "query": "...",
            "top_k": 10,
            "collection_name": "fluent_api_collection",
            "kinds": ["Parameter"],
            "under": "setup.boundary_conditions",
        }

    and respond with::

        {
            "hits": [
                {"path": "...", "kind": "...", "score": ..., "raw": "...", "payload": {...}},
                ...,
            ]
        }

    Any extra fields are passed through unchanged in ``payload``.
    """  # noqa: E501

    name = "http"

    def __init__(
        self,
        url: str,
        *,
        collection_name: str = "fluent_api_collection",
        timeout: float = 30.0,
        headers: Optional[dict[str, str]] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        """Initialize the HttpApiRetriever instance.

        Parameters
        ----------
        url : str
            Endpoint URL used by the client or backend.
        collection_name : str
            Collection name to supply to the function.
        timeout : float
            Maximum time to wait for the operation.
        headers : Optional[dict[str, str]]
            HTTP headers to attach to outgoing requests.
        client : Optional[httpx.AsyncClient]
            Client instance to use instead of creating a new one.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        self._url = url
        self._collection = collection_name
        self._headers = headers or {}
        self._owned_client = client is None
        from ansys.fluent.mcp.common.llm_wire import resolve_tls_verify

        self._client = client or httpx.AsyncClient(verify=resolve_tls_verify(), timeout=timeout)

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
        if not query or not query.strip():
            return []
        if not _egress_allowed(self._url):
            return []
        payload = {
            "query": query,
            "top_k": int(top_k),
            "collection_name": self._collection,
        }
        if kinds:
            payload["kinds"] = list(kinds)
        if under:
            payload["under"] = under
        try:
            resp = await self._client.post(self._url, json=payload, headers=self._headers)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("HttpApiRetriever %s failed: %s", self._url, exc)
            return []
        data = resp.json() if resp.content else {}
        raw_hits = data.get("hits") if isinstance(data, dict) else None
        if not isinstance(raw_hits, list):
            return []
        out: list[ApiHit] = []
        for item in raw_hits:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "")
            if not path:
                continue
            out.append(
                ApiHit(
                    path=path,
                    kind=str(item.get("kind") or "Parameter"),
                    score=float(item.get("score") or 0.0),
                    raw=item.get("raw"),
                    payload={
                        k: v for k, v in item.items() if k not in {"path", "kind", "score", "raw"}
                    }
                    or None,
                )
            )
        return out

    async def aclose(self) -> None:
        """Close resources for the HttpApiRetriever object.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        if self._owned_client:
            await self._client.aclose()


# ---------------------------------------------------------------------------
# Qdrant retriever (direct vector search)
# ---------------------------------------------------------------------------


class QdrantApiRetriever(ApiRetriever):
    """Direct vector search against the Fluent API Qdrant collection.

    The embedding step is deliberately injected: callers pass an
    ``embed`` coroutine that turns a string into a dense vector, so
    the choice of embedding model lives outside this module (typically
    the same model the remote retriever service uses, e.g. configured
    via ``FLUIDS_MCP_EMBEDDING_URL``). If ``embed`` is omitted, the
    retriever attempts the modern Qdrant ``query_points`` text-input
    flow, which only works when the collection has a server-side
    embedding configured.
    """

    name = "qdrant"

    def __init__(
        self,
        *,
        url: str,
        collection_name: str = "fluent_api_collection",
        api_key: Optional[str] = None,
        embed: Optional[EmbedFn] = None,
    ) -> None:
        """Initialize the QdrantApiRetriever instance.

        Parameters
        ----------
        url : str
            Endpoint URL used by the client or backend.
        collection_name : str
            Collection name to supply to the function.
        api_key : Optional[str]
            API key used to authenticate requests.
        embed : Optional[EmbedFn]
            Embedding function used to vectorize query text.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        self._url = url
        self._collection = collection_name
        self._api_key = api_key
        self._embed = embed
        self._client: Any = None
        self._lock = threading.Lock()

    def _get_client(self) -> Any:
        """Return the client.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is not None:
                return self._client
            try:
                from qdrant_client import AsyncQdrantClient  # type: ignore
            except ImportError as exc:  # pragma: no cover - exercised when qdrant absent
                raise RuntimeError(
                    "qdrant-client is required for QdrantApiRetriever; "
                    "install it or use HttpApiRetriever instead."
                ) from exc
            self._client = AsyncQdrantClient(url=self._url, api_key=self._api_key)
            return self._client

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
        if not query or not query.strip():
            return []
        if not _egress_allowed(self._url):
            return []
        client = self._get_client()
        if self._embed is None:
            try:
                response = await client.query_points(
                    collection_name=self._collection,
                    query=query,
                    limit=int(top_k),
                    with_payload=True,
                )
            except Exception as exc:  # vendor exceptions vary
                logger.warning("Qdrant query_points(text) failed: %s", exc)
                return []
            points = getattr(response, "points", None) or []
        else:
            try:
                vector = await self._embed(query)
            except Exception as exc:
                logger.warning("Embedding failed for query %r: %s", query, exc)
                return []
            try:
                response = await client.query_points(
                    collection_name=self._collection,
                    query=vector,
                    limit=int(top_k),
                    with_payload=True,
                )
            except Exception as exc:
                logger.warning("Qdrant query_points(vector) failed: %s", exc)
                return []
            points = getattr(response, "points", None) or []

        out: list[ApiHit] = []
        for p in points:
            payload = getattr(p, "payload", None) or {}
            path = str(payload.get("path") or payload.get("name") or "")
            if not path:
                continue
            if under and not path.startswith(under):
                continue
            kind = str(payload.get("kind") or payload.get("type") or "Parameter")
            if kinds and kind not in set(kinds):
                continue
            out.append(
                ApiHit(
                    path=path,
                    kind=kind,
                    score=float(getattr(p, "score", 0.0) or 0.0),
                    raw=payload.get("raw") or payload.get("text"),
                    payload={
                        k: v
                        for k, v in payload.items()
                        if k not in {"path", "name", "kind", "type", "raw", "text"}
                    }
                    or None,
                )
            )
        return out

    async def aclose(self) -> None:
        """Close resources for the QdrantApiRetriever object.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        client = self._client
        if client is not None and hasattr(client, "close"):
            try:
                await client.close()
            except Exception as exc:  # pragma: no cover
                logger.debug("Failed to close API retriever client cleanly: %s", exc)


# ---------------------------------------------------------------------------
# Lexical fallback (offline / unit-test mode)
# ---------------------------------------------------------------------------


class LexicalApiRetriever(ApiRetriever):
    """Thin async adapter over :class:`ApiIndex.search`.

    Carries no scoring logic of its own — the BM25 ranker, substring
    bonus and depth penalty all live in
    :mod:`ansys.fluent.mcp.common.api_index`. This class exists solely so
    the ``ApiRetriever`` protocol has a homogeneous async surface
    across HTTP / Qdrant / lexical implementations.

    This is the default retriever in any deployment that does not set
    ``FLUIDS_MCP_API_RETRIEVER_URL`` or ``FLUIDS_MCP_QDRANT_URL``.
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


# ---------------------------------------------------------------------------
# Default factory
# ---------------------------------------------------------------------------


_default_retriever: Optional[ApiRetriever] = None
_default_lock = threading.Lock()


def _build_default() -> ApiRetriever:
    """Choose a retriever based on environment configuration.

    Priority:

    1. ``FLUIDS_MCP_API_RETRIEVER_URL`` — HTTP forwarder (opt-in).
    2. ``FLUIDS_MCP_QDRANT_URL`` — direct Qdrant (opt-in). Optional
       ``FLUIDS_MCP_QDRANT_API_KEY`` and
       ``FLUIDS_MCP_QDRANT_COLLECTION`` (default
       ``fluent_api_collection``).
    3. **Default:** :class:`LexicalApiRetriever` over the bundled
       ``api_objects.json`` + indexed PyFluent class docstrings.

    Returns
    -------
    ApiRetriever
        Result produced by the function.
    """
    http_url = os.getenv("FLUIDS_MCP_API_RETRIEVER_URL")
    if http_url:
        logger.info("Using HttpApiRetriever via %s", http_url)
        return HttpApiRetriever(
            http_url,
            collection_name=os.getenv(
                "FLUIDS_MCP_API_RETRIEVER_COLLECTION", "fluent_api_collection"
            ),
        )
    qdrant_url = os.getenv("FLUIDS_MCP_QDRANT_URL")
    if qdrant_url:
        logger.info("Using QdrantApiRetriever via %s", qdrant_url)
        return QdrantApiRetriever(
            url=qdrant_url,
            api_key=os.getenv("FLUIDS_MCP_QDRANT_API_KEY"),
            collection_name=os.getenv("FLUIDS_MCP_QDRANT_COLLECTION", "fluent_api_collection"),
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
    "EmbedFn",
    "HttpApiRetriever",
    "QdrantApiRetriever",
    "LexicalApiRetriever",
    "get_default_api_retriever",
    "set_default_api_retriever",
]
