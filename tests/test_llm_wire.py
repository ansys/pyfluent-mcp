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
import sys
from types import SimpleNamespace

import pytest

from ansys.fluent.mcp.common import llm_wire
from ansys.fluent.mcp.common.llm_wire import (
    CACHE_ANTHROPIC,
    CACHE_GEMINI,
    CACHE_NONE,
    CACHE_OPENAI_AUTO,
    TRANSPORT_COMPAT,
    TRANSPORT_LITELLM,
    AaliChatModel,
    CacheSpec,
    LLMProfile,
    LLMTransportError,
    RetrySpec,
    apply_anthropic_cache_control,
    auth_headers,
    build_chat_body,
    build_litellm_kwargs,
    default_cache_mechanism,
    detect_provider,
    env_flag,
    first_model_token,
    max_tokens_param_for,
    normalise_endpoint,
    normalize_usage,
    parse_json_object,
    prompt_cache_key,
    resolve_litellm_route,
    resolve_model_config,
    resolve_profile,
    send_temperature_for,
)


def _profile(**overrides):
    """Create an LLM profile fixture for the test.

    Parameters
    ----------
    overrides : Any
        Overrides to supply to the function.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    values = {
        "provider": "compat",
        "model": "local",
        "route": "local",
        "endpoint": "https://local.example/v1",
        "transport": TRANSPORT_COMPAT,
        "tool_mode": "native",
        "supports_streaming": True,
        "supports_json_mode": True,
        "max_tokens_param": "max_tokens",
        "send_temperature": True,
        "cache": CacheSpec(mechanism=CACHE_OPENAI_AUTO, send_key=True),
        "retry": RetrySpec(max_attempts=1, timeout_s=2.0, backoff_base=0.0),
        "auth_style": "bearer",
    }
    values.update(overrides)
    return LLMProfile(**values)


def test_first_model_token_and_endpoint_normalisation():
    """Verify that first model token and endpoint normalisation.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    assert first_model_token("gpt-4o-mini gpt-4o") == "gpt-4o-mini"
    assert first_model_token("  ") is None
    assert (
        normalise_endpoint("https://example.test/v1") == "https://example.test/v1/chat/completions"
    )
    assert (
        normalise_endpoint("https://example.test/v1/chat/completions")
        == "https://example.test/v1/chat/completions"
    )


def test_resolve_model_config_precedence_and_aali_fallback():
    """Verify that resolve model config precedence and aali fallback.

    Returns
    -------
    None
        The function completes through its side effects.
    """

    def fallback():
        """Return a fallback value for the test path.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        return AaliChatModel(
            endpoint="https://aali.example/v1/chat/completions",
            api_key="aali-key",
            model="aali-model",
            model_type="openai",
            auth_style="bearer",
            source=Path("models.yaml"),
        )

    from_env = resolve_model_config(
        env={"LLM_ENDPOINT": "https://env.example/v1", "LLM_MODEL": "env-model second"},
        aali_fallback=fallback,
    )
    from_fallback = resolve_model_config(env={}, aali_fallback=fallback)
    explicit = resolve_model_config(
        endpoint="https://explicit.example/v1", model="explicit-model", env={}
    )

    assert from_env.endpoint == "https://env.example/v1"
    assert from_env.model == "env-model"
    assert from_fallback.endpoint == "https://aali.example/v1/chat/completions"
    assert from_fallback.api_key == "aali-key"
    assert explicit.endpoint == "https://explicit.example/v1"
    assert explicit.model == "explicit-model"


def test_build_chat_body_honors_reasoning_model_quirks(monkeypatch):
    """Verify that build chat body honors reasoning model quirks.

    Parameters
    ----------
    monkeypatch : Any
        Pytest fixture used to patch environment variables or dependencies.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    monkeypatch.delenv("LLM_MAX_TOKENS_PARAM", raising=False)
    monkeypatch.delenv("LLM_SEND_TEMPERATURE", raising=False)
    monkeypatch.delenv("LLM_SEND_CACHE_KEY", raising=False)

    body = build_chat_body(
        model="gpt-5-preview",
        messages=[
            {"role": "system", "content": "steady prompt"},
            {"role": "user", "content": "hi"},
        ],
        max_tokens=99,
        temperature=0.2,
    )

    assert body["max_completion_tokens"] == 99
    assert "max_tokens" not in body
    assert "temperature" not in body
    assert body["prompt_cache_key"] == prompt_cache_key("gpt-5-preview", body["messages"])


def test_auth_headers_returns_fresh_auth_style_headers():
    """Verify that auth headers returns fresh auth style headers.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    base = {"X-Trace": "1"}

    bearer = auth_headers("secret", base=base)
    azure = auth_headers("secret", auth_style="azure-api-key")

    assert bearer == {"X-Trace": "1", "Authorization": "Bearer secret"}
    assert azure == {"Content-Type": "application/json", "api-key": "secret"}
    assert base == {"X-Trace": "1"}


def test_parse_json_object_tolerates_fences_and_prose():
    """Verify that parse json object tolerates fences and prose.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    assert parse_json_object('```json\n{"ok": true}\n```') == {"ok": True}
    assert parse_json_object('before {"value": 3} after') == {"value": 3}
    assert parse_json_object("[1, 2, 3]") is None
    assert parse_json_object("no json here") is None


def test_env_and_model_quirk_helpers(monkeypatch):
    """Verify that env and model quirk helpers.

    Parameters
    ----------
    monkeypatch : Any
        Pytest fixture used to patch environment variables or dependencies.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    monkeypatch.setenv("FLAG", "off")
    assert env_flag("FLAG", default=True) is False
    monkeypatch.setenv("FLAG", "yes")
    assert env_flag("FLAG", default=False) is True

    monkeypatch.delenv("LLM_MAX_TOKENS_PARAM", raising=False)
    monkeypatch.delenv("LLM_SEND_TEMPERATURE", raising=False)
    assert max_tokens_param_for("o3-mini") == "max_completion_tokens"
    assert send_temperature_for("o3-mini") is False

    monkeypatch.setenv("LLM_MAX_TOKENS_PARAM", "limit")
    monkeypatch.setenv("LLM_SEND_TEMPERATURE", "1")
    assert max_tokens_param_for("o3-mini") == "limit"
    assert send_temperature_for("o3-mini") is True


def test_provider_detection_profile_and_routes():
    """Verify that provider detection profile and routes.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    assert detect_provider("claude-3", None, env={}) == "anthropic"
    assert detect_provider("gemini-pro", None, env={}) == "gemini"
    assert detect_provider("openai/gpt-4o", None, env={}) == "openai"
    assert detect_provider("m", "https://x.openai.azure.com/openai/v1", env={}) == "azure"
    assert detect_provider("m", "https://local.example/v1", env={}) == "compat"
    assert detect_provider("m", None, env={"LLM_PROVIDER": "gemini"}) == "gemini"

    assert resolve_litellm_route("anthropic", "claude-3") == "anthropic/claude-3"
    assert resolve_litellm_route("azure", "deployment") == "azure/deployment"
    assert resolve_litellm_route("openai", "openai/gpt-4o") == "openai/gpt-4o"
    assert default_cache_mechanism("anthropic") == CACHE_ANTHROPIC
    assert default_cache_mechanism("gemini") == CACHE_GEMINI
    assert default_cache_mechanism("compat") == CACHE_NONE

    profile = resolve_profile(
        model="claude-3",
        env={"LLM_TRANSPORT": "auto", "LLM_MAX_RETRIES": "bad", "LLM_TIMEOUT_SECONDS": "bad"},
    )
    assert profile.provider == "anthropic"
    assert profile.transport == TRANSPORT_LITELLM
    assert profile.route == "anthropic/claude-3"
    assert profile.retry.max_attempts == 3
    assert profile.retry.timeout_s == 60.0

    compat = resolve_profile(endpoint="https://local.example/v1", model="local", env={})
    assert compat.transport == TRANSPORT_COMPAT


def test_anthropic_cache_and_litellm_kwargs():
    """Verify that anthropic cache and litellm kwargs.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    messages = [{"role": "system", "content": "stable"}, {"role": "user", "content": "hi"}]
    marked = apply_anthropic_cache_control(messages)

    assert marked[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert messages[0]["content"] == "stable"

    profile = LLMProfile(
        provider="openai",
        model="gpt-4o",
        route="openai/gpt-4o",
        endpoint=None,
        transport=TRANSPORT_LITELLM,
        tool_mode="native",
        supports_streaming=True,
        supports_json_mode=True,
        max_tokens_param="max_tokens",
        send_temperature=True,
        cache=CacheSpec(mechanism=CACHE_OPENAI_AUTO, send_key=True),
        retry=RetrySpec(),
    )
    kwargs = build_litellm_kwargs(
        profile,
        messages,
        tools=[{"type": "function"}],
        max_tokens=10,
        temperature=0.1,
        response_format={"type": "json_object"},
        api_key="key",
        api_base="https://api.example",
        api_version="2024-01-01",
    )

    assert kwargs["model"] == "openai/gpt-4o"
    assert kwargs["tools"] == [{"type": "function"}]
    assert kwargs["tool_choice"] == "auto"
    assert kwargs["max_tokens"] == 10
    assert kwargs["temperature"] == 0.1
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["api_key"] == "key"
    assert kwargs["api_base"] == "https://api.example"
    assert kwargs["api_version"] == "2024-01-01"
    assert kwargs["prompt_cache_key"] == prompt_cache_key("gpt-4o", messages)


def test_usage_normalization_and_transport_guard_errors():
    """Verify that usage normalization and transport guard errors.

    Returns
    -------
    None
        The function completes through its side effects.
    """

    class Usage:
        prompt_tokens = 5
        completion_tokens = 2
        total_tokens = 7
        cache_read_input_tokens = 3
        cache_creation_input_tokens = 4

    assert normalize_usage(Usage()) == {
        "prompt_tokens": 5,
        "completion_tokens": 2,
        "total_tokens": 7,
        "cached_prompt_tokens": 3,
        "cache_creation_tokens": 4,
    }
    assert (
        normalize_usage({"prompt_tokens_details": {"cached_tokens": 9}})["cached_prompt_tokens"]
        == 9
    )

    profile = resolve_profile(endpoint="https://blocked.example/v1", model="local", env={})
    from ansys.fluent.mcp.common import llm_wire

    try:
        llm_wire._guard_egress(
            profile, api_base=None, allowed_hosts={"allowed.example"}, offline=False
        )
    except LLMTransportError as exc:
        assert "refusing to call hosts" in str(exc)
    else:
        raise AssertionError("expected guard error")

    try:
        llm_wire._guard_egress(profile, api_base=None, allowed_hosts=None, offline=True)
    except LLMTransportError as exc:
        assert "offline mode" in str(exc)
    else:
        raise AssertionError("expected offline error")


def test_tls_aali_and_native_provider_helpers(monkeypatch, tmp_path):
    """Verify that tls aali and native provider helpers.

    Parameters
    ----------
    monkeypatch : Any
        Pytest fixture used to patch environment variables or dependencies.
    tmp_path : Any
        Path for the tmp.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    monkeypatch.setenv("LLM_TLS_INSECURE", "1")
    assert llm_wire.resolve_tls_verify() is False
    monkeypatch.delenv("LLM_TLS_INSECURE", raising=False)
    assert llm_wire.resolve_tls_verify({"LLM_CA_BUNDLE": "ca.pem"}) == "ca.pem"
    assert llm_wire.resolve_tls_verify({}) is True

    config = tmp_path / "models.yaml"
    config.write_text("ignored")

    fake_yaml = SimpleNamespace(
        YAMLError=ValueError,
        safe_load=lambda _fh: {
            "CHAT_MODELS": [
                {
                    "URL": "https://demo.openai.azure.com/openai/v1",
                    "API_KEY": " none ",
                    "MODEL_NAME": "deployment",
                    "MODEL_TYPE": "azure_openai",
                }
            ]
        },
    )
    monkeypatch.setitem(sys.modules, "yaml", fake_yaml)
    monkeypatch.setattr(
        llm_wire, "_aali_candidate_paths", lambda: [tmp_path / "missing.yaml", config]
    )

    loaded = llm_wire.load_aali_chat_model()
    assert loaded.endpoint == "https://demo.openai.azure.com/openai/v1/chat/completions"
    assert loaded.api_key is None
    assert loaded.auth_style == "azure-api-key"
    assert loaded.source == config

    assert llm_wire.native_provider_configured(None, env={}) is False
    assert llm_wire.native_provider_configured("claude-3", env={}) is True
    assert llm_wire.native_provider_configured("plain", env={"OPENAI_API_KEY": "key"}) is True


def test_profile_resolution_and_request_building_branches(monkeypatch):
    """Verify that profile resolution and request building branches.

    Parameters
    ----------
    monkeypatch : Any
        Pytest fixture used to patch environment variables or dependencies.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    env = {
        "LLM_PROVIDER": "openai",
        "LLM_TRANSPORT": "bad",
        "LLM_CACHE_MECHANISM": "bad",
        "LLM_CACHE_TTL_SECONDS": "bad",
        "LLM_TOOL_MODE": "bad",
        "LLM_SUPPORTS_STREAMING": "0",
        "LLM_SUPPORTS_JSON_MODE": "false",
        "LLM_SEND_CACHE_KEY": "0",
    }
    profile = resolve_profile(model="gpt-4o", env=env)

    assert profile.transport == TRANSPORT_LITELLM
    assert profile.cache.mechanism == CACHE_OPENAI_AUTO
    assert profile.cache.ttl_seconds == 300
    assert profile.cache.send_key is False
    assert profile.tool_mode == "native"
    assert profile.supports_streaming is False
    assert profile.supports_json_mode is False

    compat = _profile(auth_style="azure-api-key")
    url, body, headers = llm_wire._build_compat_request(
        compat,
        [{"role": "system", "content": "stable"}, {"role": "user", "content": "hi"}],
        tools=[{"type": "function"}],
        max_tokens=5,
        temperature=0.4,
        response_format={"type": "json_object"},
        api_key="key",
    )

    assert url == "https://local.example/v1/chat/completions"
    assert body["tools"] == [{"type": "function"}]
    assert body["tool_choice"] == "auto"
    assert body["max_tokens"] == 5
    assert body["temperature"] == 0.4
    assert body["response_format"] == {"type": "json_object"}
    assert headers["api-key"] == "key"

    with pytest.raises(LLMTransportError, match="no endpoint"):
        llm_wire._build_compat_request(
            _profile(endpoint=None),
            [],
            tools=None,
            max_tokens=None,
            temperature=None,
            response_format=None,
            api_key=None,
        )


def test_litellm_call_stream_and_warm_cache(monkeypatch):
    """Verify that litellm call stream and warm cache.

    Parameters
    ----------
    monkeypatch : Any
        Pytest fixture used to patch environment variables or dependencies.

    Returns
    -------
    None
        The function completes through its side effects.
    """

    class Response(dict):
        def model_dump(self):
            """Return a model dump payload for the fake response.

            Returns
            -------
            None
                The function completes through its side effects.
            """
            return {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 2}}

    class Chunk(dict):
        def model_dump(self):
            """Return a model dump payload for the fake response.

            Returns
            -------
            None
                The function completes through its side effects.
            """
            return {"choices": [{"delta": {"content": "piece"}}]}

    async def stream():
        """Yield fake streaming chunks for the test.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        yield Chunk()

    class FakeLiteLLM:
        telemetry = True

        async def acompletion(self, **kwargs):
            """Return a fake asynchronous completion response.

            Parameters
            ----------
            kwargs : Any
                Keyword arguments forwarded to the callable.

            Returns
            -------
            None
                The function completes through its side effects.
            """
            self.last_async = kwargs
            if kwargs.get("stream"):
                return stream()
            return Response()

        def completion(self, **kwargs):
            """Return a fake completion response.

            Parameters
            ----------
            kwargs : Any
                Keyword arguments forwarded to the callable.

            Returns
            -------
            None
                The function completes through its side effects.
            """
            self.last_sync = kwargs
            return Response()

    fake_litellm = FakeLiteLLM()
    monkeypatch.setattr(llm_wire, "_import_litellm", lambda: fake_litellm)
    profile = _profile(transport=TRANSPORT_LITELLM, route="openai/gpt-4o", provider="openai")
    messages = [{"role": "system", "content": "stable"}, {"role": "user", "content": "hi"}]

    assert llm_wire.call(profile, messages, max_tokens=2)["usage"] == {"prompt_tokens": 2}
    assert (
        asyncio.run(llm_wire.acall(profile, messages, max_tokens=2))["choices"][0]["message"][
            "content"
        ]
        == "ok"
    )

    async def collect_stream():
        """Collect streaming chunks into a response.

        Returns
        -------
        None
            The function completes through its side effects.
        """
        return [chunk async for chunk in llm_wire.astream(profile, messages, max_tokens=2)]

    assert asyncio.run(collect_stream()) == [{"choices": [{"delta": {"content": "piece"}}]}]
    assert asyncio.run(llm_wire.warm_cache(profile, "stable", api_key="key")) == {
        "prompt_tokens": 2,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    assert asyncio.run(llm_wire.warm_cache(_profile(cache=CacheSpec()), "stable")) is None
    assert asyncio.run(llm_wire.warm_cache(profile, "", offline=False)) is None

    with pytest.raises(LLMTransportError, match="astream is only supported"):

        async def fail_stream():
            """Raise a fake streaming failure.

            Returns
            -------
            None
                The function completes through its side effects.
            """
            async for _chunk in llm_wire.astream(_profile(), messages):
                pass

        asyncio.run(fail_stream())


def test_httpx_transports_are_called_with_built_requests(monkeypatch):
    """Verify that httpx transports are called with built requests.

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

    class Response:
        def raise_for_status(self):
            """Raise the configured fake HTTP status error.

            Returns
            -------
            None
                The function completes through its side effects.
            """
            calls.append(("raise", None))

        def json(self):
            """Return the fake JSON response payload.

            Returns
            -------
            None
                The function completes through its side effects.
            """
            return {"choices": [{"message": {"content": "http"}}], "usage": {"total_tokens": 1}}

    class Client:
        def __init__(self, **kwargs):
            """Initialize the Client instance.

            Parameters
            ----------
            kwargs : Any
                Keyword arguments forwarded to the callable.

            Returns
            -------
            None
                The function completes through its side effects.
            """
            self.kwargs = kwargs

        def __enter__(self):
            """Enter the fake context manager.

            Returns
            -------
            None
                The function completes through its side effects.
            """
            calls.append(("client", self.kwargs))
            return self

        def __exit__(self, *_args):
            """Exit the fake context manager.

            Parameters
            ----------
            _args : Any
                Positional arguments forwarded to the wrapped call.

            Returns
            -------
            None
                The function completes through its side effects.
            """
            calls.append(("client_exit", None))

        def post(self, url, *, json, headers):
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
            calls.append(("post", url, json, headers))
            return Response()

    class AsyncClient(Client):
        async def __aenter__(self):
            """Enter the fake asynchronous context manager.

            Returns
            -------
            None
                The function completes through its side effects.
            """
            calls.append(("async_client", self.kwargs))
            return self

        async def __aexit__(self, *_args):
            """Exit the fake asynchronous context manager.

            Parameters
            ----------
            _args : Any
                Positional arguments forwarded to the wrapped call.

            Returns
            -------
            None
                The function completes through its side effects.
            """
            calls.append(("async_client_exit", None))

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
            calls.append(("async_post", url, json, headers))
            return Response()

    monkeypatch.setitem(
        sys.modules, "httpx", SimpleNamespace(Client=Client, AsyncClient=AsyncClient)
    )
    monkeypatch.setattr(llm_wire, "resolve_tls_verify", lambda: "ca.pem")

    profile = _profile()
    messages = [{"role": "user", "content": "hi"}]

    assert llm_wire.call(profile, messages, api_key="key")["usage"] == {"total_tokens": 1}
    assert (
        asyncio.run(llm_wire.acall(profile, messages, api_key="key"))["choices"][0]["message"][
            "content"
        ]
        == "http"
    )
    assert any(
        item[0] == "post" and item[1] == "https://local.example/v1/chat/completions"
        for item in calls
    )
    assert any(item[0] == "async_post" for item in calls)
