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
import json
from types import SimpleNamespace

from ansys.fluent.mcp.solve.catalog import index as catalog_index, retriever as catalog_retriever
from ansys.fluent.mcp.solve.catalog.index import ApiIndex, _normalise_path, _parse_line, _tokenise
from ansys.fluent.mcp.solve.catalog.retriever import (
    ApiHit,
    HttpApiRetriever,
    LexicalApiRetriever,
    QdrantApiRetriever,
)


def _api_objects_file(tmp_path):
    """Create a temporary API objects file for the test.

    Parameters
    ----------
    tmp_path : Any
        Path for the tmp.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    path = tmp_path / "api_objects.json"
    path.write_text(
        json.dumps(
            {
                "api_objects": [
                    "<solver_session>.setup.boundary_conditions.velocity_inlet (Object)",
                    "<solver_session>.setup.boundary_conditions.velocity_inlet.thermal (Group)",
                    "<solver_session>.setup.boundary_conditions.velocity_inlet.thermal.t (Parameter)",  # noqa: E501
                    "<solver_session>.solution.run_calculation.iterate (Command)",
                    "<meshing_session>.workflow.task (Object)",
                    "<solver_session>.tui.file.read_case (Command)",
                    "bad line",
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def test_api_index_loads_filters_and_searches_with_help_tokens(tmp_path):
    """Verify that api index loads filters and searches with help tokens.

    Parameters
    ----------
    tmp_path : Any
        Path for the tmp.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    index = ApiIndex(
        custom_path=str(_api_objects_file(tmp_path)),
        help_map={
            "velocity_inlet": "incoming gas inlet boundary",
            "thermal": "thermal temperature settings",
            "t": "Static temperature value",
        },
    )

    hits = index.search("incoming gas temperature", kinds=["Parameter"], top_k=3)
    under_hits = index.search("iterate", under="solution")
    children = index.children_of("setup.boundary_conditions", max_results=10)

    assert index.available is True
    assert (
        index.lookup('setup.boundary_conditions.velocity_inlet["inlet-1"].thermal.t').kind
        == "Parameter"
    )
    assert hits[0].entry.path == "setup.boundary_conditions.velocity_inlet.thermal.t"
    assert under_hits[0].entry.path == "solution.run_calculation.iterate"
    assert [child.path for child in children] == ["setup.boundary_conditions.velocity_inlet"]
    assert index.lookup("tui.file.read_case") is None


def test_api_index_handles_bad_or_missing_files(tmp_path):
    """Verify that api index handles bad or missing files.

    Parameters
    ----------
    tmp_path : Any
        Path for the tmp.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    bad_shape = tmp_path / "bad_shape.json"
    bad_json = tmp_path / "bad_json.json"
    bad_shape.write_text(json.dumps({"api_objects": "nope"}), encoding="utf-8")
    bad_json.write_text("not json", encoding="utf-8")

    assert ApiIndex(custom_path=str(tmp_path / "missing.json")).available is False
    assert ApiIndex(custom_path=str(bad_shape)).available is False
    assert ApiIndex(custom_path=str(bad_json)).available is False


def test_parse_line_and_path_helpers():
    """Verify that parse line and path helpers.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    entry = _parse_line(
        "<solver_session>.results.scene['scene-1'].graphics_objects (Object)",
        help_map={"scene": "view scene", "graphics_objects": "graphics object collection"},
    )

    assert entry.path == "results.scene.graphics_objects"
    assert entry.tokens[:4] == ["results", "scene", "graphics", "objects"]
    assert "view scene" in entry.doc
    assert _parse_line("<solver_session>.solver.tui.file (Command)") is None
    assert _parse_line(123) is None
    assert _normalise_path('.a["name"].b..') == "a.b"
    assert _tokenise("Velocity-Inlet.T") == ["velocity", "inlet", "t"]


def test_lexical_retriever_and_api_hit_tool_dict(tmp_path):
    """Verify that lexical retriever and api hit tool dict.

    Parameters
    ----------
    tmp_path : Any
        Path for the tmp.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    index = ApiIndex(custom_path=str(_api_objects_file(tmp_path)), help_map={})
    retriever = LexicalApiRetriever(index)

    hits = asyncio.run(retriever.retrieve("iterate", top_k=1, kinds=["Command"]))
    schema_only = ApiHit(
        "mesh.check_mesh", "Command", 0.123456, raw="raw", payload={"source": "schema"}
    )
    plain = ApiHit("setup.models.energy", "Parameter", 1.23456)

    assert hits[0].path == "solution.run_calculation.iterate"
    assert schema_only.to_tool_dict() == {
        "path": "mesh.check_mesh",
        "kind": "Command",
        "score": 0.1235,
        "raw": "raw",
        "payload": {"source": "schema"},
        "note": "Does not exist. Use solver.settings.mesh.check() instead.",
    }
    assert plain.to_tool_dict() == {
        "path": "setup.models.energy",
        "kind": "Parameter",
        "score": 1.2346,
    }


def test_default_api_index_singleton_can_be_reset(monkeypatch):
    """Verify that default api index singleton can be reset.

    Parameters
    ----------
    monkeypatch : Any
        Pytest fixture used to patch environment variables or dependencies.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    custom = ApiIndex(custom_path="missing.json")
    monkeypatch.setattr(catalog_index, "_default_index", custom)

    assert catalog_index.get_default_api_index() is custom
    catalog_index.reset_default_api_index()
    assert catalog_index._default_index is None


class _FakeResponse:
    def __init__(self, data, *, content=b"x", fail=False):
        """Initialize the _FakeResponse instance.

        Parameters
        ----------
        data : Any
            Input data to validate, convert, or return.
        content : Any
            Content returned by the mocked response.
        fail : Any
            Whether the test double should simulate a failure.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        self._data = data
        self.content = content
        self._fail = fail

    def raise_for_status(self):
        """Raise the configured fake HTTP status error.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        if self._fail:
            import httpx

            raise httpx.HTTPStatusError("bad", request=None, response=None)

    def json(self):
        """Return the fake JSON response payload.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        return self._data


class _FakeAsyncClient:
    def __init__(self, response):
        """Initialize the _FakeAsyncClient instance.

        Parameters
        ----------
        response : Any
            Response payload or object being handled.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        self.response = response
        self.closed = False
        self.posts = []

    async def post(self, url, *, json, headers):
        """Record a fake POST request and return its response.

        Parameters
        ----------
        url : Any
            Endpoint URL used by the client or backend.
        json : Any
            JSON payload returned by the mocked response.
        headers : Any
            HTTP headers to attach to outgoing requests.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        self.posts.append((url, json, headers))
        return self.response

    async def aclose(self):
        """Close resources for the _FakeAsyncClient object.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        self.closed = True


def test_http_api_retriever_posts_payload_and_filters_bad_hits(monkeypatch):
    """Verify that http api retriever posts payload and filters bad hits.

    Parameters
    ----------
    monkeypatch : Any
        Pytest fixture used to patch environment variables or dependencies.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    monkeypatch.delenv("FLUIDS_AGENT_OFFLINE", raising=False)
    monkeypatch.delenv("FLUIDS_AGENT_ALLOWED_LLM_HOSTS", raising=False)
    client = _FakeAsyncClient(
        _FakeResponse(
            {
                "hits": [
                    {
                        "path": "setup.models.energy",
                        "kind": "Parameter",
                        "score": 2.5,
                        "raw": "raw",
                        "extra": 7,
                    },
                    {"path": "", "kind": "Parameter"},
                    "bad",
                ]
            }
        )
    )
    retriever = HttpApiRetriever(
        "https://retriever.example/search",
        collection_name="collection",
        headers={"x-test": "1"},
        client=client,
    )

    hits = asyncio.run(retriever.retrieve("energy", top_k=3, kinds=["Parameter"], under="setup"))
    asyncio.run(retriever.aclose())

    assert hits == [
        ApiHit("setup.models.energy", "Parameter", 2.5, raw="raw", payload={"extra": 7})
    ]
    assert client.posts == [
        (
            "https://retriever.example/search",
            {
                "query": "energy",
                "top_k": 3,
                "collection_name": "collection",
                "kinds": ["Parameter"],
                "under": "setup",
            },
            {"x-test": "1"},
        )
    ]
    assert client.closed is False


def test_http_api_retriever_blocks_or_handles_bad_responses(monkeypatch):
    """Verify that http api retriever blocks or handles bad responses.

    Parameters
    ----------
    monkeypatch : Any
        Pytest fixture used to patch environment variables or dependencies.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    monkeypatch.setenv("FLUIDS_AGENT_OFFLINE", "1")
    blocked = HttpApiRetriever(
        "https://retriever.example/search", client=_FakeAsyncClient(_FakeResponse({}))
    )
    assert asyncio.run(blocked.retrieve("energy")) == []

    monkeypatch.setenv("FLUIDS_AGENT_OFFLINE", "0")
    monkeypatch.setenv("FLUIDS_AGENT_ALLOWED_LLM_HOSTS", "allowed.example")
    assert asyncio.run(blocked.retrieve("energy")) == []

    monkeypatch.setenv("FLUIDS_AGENT_ALLOWED_LLM_HOSTS", "retriever.example")
    failing = HttpApiRetriever(
        "https://retriever.example/search",
        client=_FakeAsyncClient(_FakeResponse({}, fail=True)),
    )
    wrong_shape = HttpApiRetriever(
        "https://retriever.example/search",
        client=_FakeAsyncClient(_FakeResponse({"hits": "nope"})),
    )

    assert asyncio.run(failing.retrieve("energy")) == []
    assert asyncio.run(wrong_shape.retrieve("energy")) == []
    assert asyncio.run(wrong_shape.retrieve("   ")) == []


class _FakeQdrantClient:
    def __init__(self, points=None, fail=False):
        """Initialize the _FakeQdrantClient instance.

        Parameters
        ----------
        points : Any
            Points returned by the mocked vector-search response.
        fail : Any
            Whether the test double should simulate a failure.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        self.points = points or []
        self.fail = fail
        self.queries = []
        self.closed = False

    async def query_points(self, **kwargs):
        """Return fake vector-search query results.

        Parameters
        ----------
        kwargs : Any
            Keyword arguments forwarded to the callable.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        self.queries.append(kwargs)
        if self.fail:
            raise RuntimeError("qdrant down")
        return SimpleNamespace(points=self.points)

    async def close(self):
        """Close resources for the _FakeQdrantClient object.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        self.closed = True


def test_qdrant_retriever_text_and_vector_paths(monkeypatch):
    """Verify that qdrant retriever text and vector paths.

    Parameters
    ----------
    monkeypatch : Any
        Pytest fixture used to patch environment variables or dependencies.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    monkeypatch.delenv("FLUIDS_AGENT_OFFLINE", raising=False)
    monkeypatch.delenv("FLUIDS_AGENT_ALLOWED_LLM_HOSTS", raising=False)
    point = SimpleNamespace(
        score=0.9,
        payload={"path": "setup.models.energy", "kind": "Parameter", "raw": "raw", "extra": "kept"},
    )
    text_client = _FakeQdrantClient(points=[point, SimpleNamespace(score=1, payload={})])
    text_retriever = QdrantApiRetriever(url="https://qdrant.example")
    monkeypatch.setattr(text_retriever, "_get_client", lambda: text_client)

    text_hits = asyncio.run(
        text_retriever.retrieve("energy", top_k=2, kinds=["Parameter"], under="setup")
    )

    async def embed(query):
        """Return fake embedding vectors for the test input.

        Parameters
        ----------
        query : Any
            Search text or user request to evaluate.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        return [1.0, 2.0, 3.0]

    vector_client = _FakeQdrantClient(
        points=[
            SimpleNamespace(
                score=0.8, payload={"name": "solution.run", "type": "Command", "text": "run"}
            )
        ]
    )
    vector_retriever = QdrantApiRetriever(url="https://qdrant.example", embed=embed)
    monkeypatch.setattr(vector_retriever, "_get_client", lambda: vector_client)
    vector_hits = asyncio.run(vector_retriever.retrieve("run"))
    asyncio.run(vector_retriever.aclose())

    assert text_hits == [
        ApiHit("setup.models.energy", "Parameter", 0.9, raw="raw", payload={"extra": "kept"})
    ]
    assert text_client.queries[0]["query"] == "energy"
    assert vector_hits == [ApiHit("solution.run", "Command", 0.8, raw="run")]
    assert vector_client.queries[0]["query"] == [1.0, 2.0, 3.0]
    assert vector_client.closed is False


def test_qdrant_retriever_error_paths(monkeypatch):
    """Verify that qdrant retriever error paths.

    Parameters
    ----------
    monkeypatch : Any
        Pytest fixture used to patch environment variables or dependencies.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    monkeypatch.setenv("FLUIDS_AGENT_OFFLINE", "0")
    monkeypatch.delenv("FLUIDS_AGENT_ALLOWED_LLM_HOSTS", raising=False)
    failing_query = QdrantApiRetriever(url="https://qdrant.example")
    monkeypatch.setattr(failing_query, "_get_client", lambda: _FakeQdrantClient(fail=True))

    async def failing_embed(query):
        """Raise the configured embedding failure.

        Parameters
        ----------
        query : Any
            Search text or user request to evaluate.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        raise RuntimeError("embed failed")

    failing_vector = QdrantApiRetriever(url="https://qdrant.example", embed=failing_embed)
    monkeypatch.setattr(failing_vector, "_get_client", lambda: _FakeQdrantClient())

    assert asyncio.run(failing_query.retrieve("energy")) == []
    assert asyncio.run(failing_vector.retrieve("energy")) == []
    assert asyncio.run(failing_query.retrieve("")) == []


def test_default_retriever_factory_and_override(monkeypatch):
    """Verify that default retriever factory and override.

    Parameters
    ----------
    monkeypatch : Any
        Pytest fixture used to patch environment variables or dependencies.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    catalog_retriever.set_default_api_retriever(None)
    monkeypatch.setenv("FLUIDS_MCP_API_RETRIEVER_URL", "https://retriever.example/search")
    monkeypatch.setenv("FLUIDS_MCP_API_RETRIEVER_COLLECTION", "custom")
    http = catalog_retriever._build_default()
    assert isinstance(http, HttpApiRetriever)
    assert http._collection == "custom"

    monkeypatch.delenv("FLUIDS_MCP_API_RETRIEVER_URL", raising=False)
    monkeypatch.setenv("FLUIDS_MCP_QDRANT_URL", "https://qdrant.example")
    monkeypatch.setenv("FLUIDS_MCP_QDRANT_API_KEY", "key")
    monkeypatch.setenv("FLUIDS_MCP_QDRANT_COLLECTION", "qcoll")
    qdrant = catalog_retriever._build_default()
    assert isinstance(qdrant, QdrantApiRetriever)
    assert qdrant._collection == "qcoll"
    assert qdrant._api_key == "key"

    monkeypatch.delenv("FLUIDS_MCP_QDRANT_URL", raising=False)
    lexical = catalog_retriever._build_default()
    assert isinstance(lexical, LexicalApiRetriever)

    catalog_retriever.set_default_api_retriever(http)
    assert catalog_retriever.get_default_api_retriever() is http
    catalog_retriever.set_default_api_retriever(None)
