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

"""Model-agnostic wire-format core for OpenAI-compatible LLM endpoints.

This is the single place that knows how to talk to an OpenAI-style
``POST /chat/completions`` endpoint *regardless of which model sits
behind it*. Every LLM call path in the suite—the agent provider, the
solve codegen pipeline, and the tier-2 assist helper—builds its
request body, resolves its model/endpoint/key triplet, and parses
replies through the helpers here, so "model agnostic" is a property of
one tested module instead of three drifting copies.

Design rules:

* **Transport is OpenAI-compatible only.** Other vendors (Anthropic,
  Gemini, local models) are reached through an OpenAI-compatible gateway
  (LiteLLM/OpenRouter/vLLM/Ollama). The only vendor-specific
  surface is the small :data:`_MODEL_QUIRKS` table.
* **No model name is hard-coded outside this module.** Callers fall back
  to :data:`DEFAULT_MODEL`.
* **httpx is the only third-party dependency**, and it is imported by the
  caller. This module never makes the HTTP call itself. It only shapes
  the request and reads the response.

The module is intentionally dependency-free with respect to the rest of
the package so that both this package and any optional higher-level layer
that consumes it can import it. The dependency direction is one-way.
Consumers import this module. This module never imports a consumer.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Callable, Optional, Sequence

logger = logging.getLogger("ansys.fluent.mcp.common.llm_wire")

#: The single fallback model name used everywhere when nothing is
#: configured via constructor args, env vars, or the AALI configuration file.
DEFAULT_MODEL = "gpt-4o-mini"


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


def env_flag(name: str, *, default: bool) -> bool:
    """Parse a boolean env var with a permissive truthy/falsy grammar.

    Parameters
    ----------
    name : str
        Name of the object, module, or setting being processed.
    default : bool
        Fallback value used when no explicit value is supplied.

    Returns
    -------
    bool
        Boolean result produced by the function.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


_TLS_INSECURE_WARNED = False


def resolve_tls_verify(env=os.environ) -> bool | str:
    """Resolve the value to pass to ``httpx``'s ``verify=`` for LLM calls.

    Secure by default. ``httpx`` does NOT honor ``SSL_CERT_FILE``/
    ``REQUESTS_CA_BUNDLE`` automatically (unlike ``requests``), so a
    corporate/self-signed CA must be passed explicitly. This helper is
    the single place that resolves it. Here is the resolution order:

    1. ``LLM_TLS_INSECURE`` truthy -> ``False`` (disables verification and
       logs a loud one-time warning). Intended only for throwaway/development
       setups. It exposes API keys and prompts to MITM (man-in-the-middle) attacks.
    2. A CA bundle path from ``LLM_CA_BUNDLE``/``SSL_CERT_FILE``/
       ``REQUESTS_CA_BUNDLE`` (first non-empty wins) -> that path. This is
       the supported way to trust a corporate/self-signed CA.
    3. Otherwise ``True`` (verify against the system/certification trust store).

    Parameters
    ----------
    env : Any
        Environment mapping to read instead of the process environment.

    Returns
    -------
    bool | str
        Boolean result produced by the function.
    """
    global _TLS_INSECURE_WARNED
    if env_flag("LLM_TLS_INSECURE", default=False):
        if not _TLS_INSECURE_WARNED:
            logger.warning(
                "LLM_TLS_INSECURE is set: TLS certificate verification is "
                "DISABLED for outbound LLM calls. This exposes API keys and "
                "prompts to man-in-the-middle attacks. Prefer LLM_CA_BUNDLE "
                "(path to your corporate CA bundle) instead."
            )
            _TLS_INSECURE_WARNED = True
        return False
    for var in ("LLM_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        path = (env.get(var) or "").strip()
        if path:
            return path
    return True


def first_model_token(raw: str | None) -> str | None:
    """Return the first whitespace-separated token of ``LLM_MODEL``.

    ``LLM_MODEL`` may hold a space-separated list (for the UI model
    switcher). Only the first token is the active model.

    Parameters
    ----------
    raw : str | None
        Raw text value to parse.

    Returns
    -------
    str | None
        String result produced by the function.
    """
    if not raw or not raw.strip():
        return None
    return raw.split()[0]


# ---------------------------------------------------------------------------
# AALI model's.yaml fallback
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AaliChatModel:
    """A single resolved chat-model entry from the AALI model's configuration."""

    endpoint: str
    api_key: str | None
    model: str
    model_type: str
    auth_style: str  # "bearer" or "azure-api-key"
    source: Path


_CONFIG_DIR = Path("Ansys") / "Aali" / "config"
# AALI has shipped both ``models.config`` and ``models.yaml`` over time;
# probe every known filename in priority order. ``models.yaml`` is the
# current AALI default; ``models.config`` is the legacy name (probed last).
_CONFIG_FILENAMES = ("models.yaml", "models.yml", "models.config")


def _aali_candidate_paths() -> list[Path]:
    """Ordered list of locations to probe for the AALI model's file.

    Returns
    -------
    list[Path]
        Collection containing the operation results.
    """
    roots: list[Path] = []

    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        roots.append(Path(local_app))

    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        roots.append(Path(xdg))
    home = Path.home()
    roots.append(home / ".local" / "share")
    roots.append(home / "Library" / "Application Support")  # macOS

    paths: list[Path] = [root / _CONFIG_DIR / name for root in roots for name in _CONFIG_FILENAMES]

    override = os.environ.get("AALI_MODELS_CONFIG")
    if override:
        paths.insert(0, Path(override))

    return paths


def _looks_like_azure(url: str, model_type: str) -> bool:
    """Return whether the endpoint appears to be an Azure OpenAI endpoint.

    Parameters
    ----------
    url : str
        Endpoint URL used by the client or backend.
    model_type : str
        Model type to supply to the function.

    Returns
    -------
    bool
        Boolean result of the operation.
    """
    return "azure.com" in url.lower() or model_type.lower().startswith("azure")


def normalise_endpoint(url: str) -> str:
    """Return a fully-qualified ``/chat/completions`` URL.

    Accepts ``https://host/v1``, ``https://host/v1/``,
    ``https://host/openai/v1/``, or a URL that already ends in
    ``/chat/completions`` (left untouched).

    Parameters
    ----------
    url : str
        URL to supply to the function.

    Returns
    -------
    str
        String result produced by the function.
    """
    cleaned = url.rstrip("/")
    if cleaned.endswith("/chat/completions"):
        return cleaned
    return f"{cleaned}/chat/completions"


def load_aali_chat_model() -> AaliChatModel | None:
    """Return the first usable chat model from the AALI configuration or ``None``.

    Never raises: any I/O or parse error is logged and yields ``None`` so
    the caller can fall back to its own defaults.

    Returns
    -------
    AaliChatModel | None
        Result produced by the function.
    """
    try:
        import yaml  # PyYAML — optional dependency
    except ImportError:
        logger.debug("PyYAML not installed; skipping AALI model's configuration lookup.")
        return None

    for path in _aali_candidate_paths():
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        try:
            with path.open("r", encoding="utf-8") as fh:
                doc = yaml.safe_load(fh) or {}
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("Could not read AALI model's configuration at %s: %s", path, exc)
            continue

        chat_models = doc.get("CHAT_MODELS") if isinstance(doc, dict) else None
        if not isinstance(chat_models, list) or not chat_models:
            continue

        first = chat_models[0]
        if not isinstance(first, dict):
            continue

        url = str(first.get("URL", "")).strip()
        if not url:
            continue
        model = str(first.get("MODEL_NAME", "")).strip() or DEFAULT_MODEL
        model_type = str(first.get("MODEL_TYPE", "")).strip()
        api_key_raw = first.get("API_KEY")
        api_key = str(api_key_raw).strip() if api_key_raw else None
        if api_key and api_key.lower() in {"none", "null", ""}:
            api_key = None

        endpoint = normalise_endpoint(url)
        auth_style = "azure-api-key" if _looks_like_azure(url, model_type) else "bearer"

        logger.info(
            "Loaded LLM defaults from AALI model's configuration (%s): model=%s endpoint=%s",
            path,
            model,
            endpoint,
        )
        return AaliChatModel(
            endpoint=endpoint,
            api_key=api_key,
            model=model,
            model_type=model_type or "openai",
            auth_style=auth_style,
            source=path,
        )

    return None


# ---------------------------------------------------------------------------
# Endpoint / key / model resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelConfig:
    """A fully resolved endpoint/key/model/authorization triplet (plus style)."""

    endpoint: str | None
    api_key: str | None
    model: str
    auth_style: str  # "bearer" or "azure-api-key"


def resolve_model_config(
    *,
    endpoint: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    auth_style: str | None = None,
    aali_fallback: Optional[Callable[[], AaliChatModel | None]] = None,
    env=os.environ,
) -> ModelConfig:
    """Resolve the endpoint/key/model/authorization triplet from a uniform order.

    Resolution order (first non-empty wins):

    1. Explicit arguments.
    2. Environment variables ``LLM_ENDPOINT``/``LLM_API_KEY``/
       ``LLM_MODEL``/``LLM_AUTH_STYLE``.
    3. ``aali_fallback()`` isonly consulted when no endpoint is set via
       (1) or (2), and only when a callable is supplied. Pass
       :func:`load_aali_chat_model` to opt in. Pass ``None`` (default)
       to keep resolution strictly environment-driven (and deterministic in
       tests).

    ``model`` always resolves to a concrete string (:data:`DEFAULT_MODEL`
    when nothing else is set). ``endpoint`` may be ``None`` so the caller
    can decide whether a missing endpoint is fatal.

    Parameters
    ----------
    endpoint : str | None
        Endpoint to supply to the function.
    api_key : str | None
        API key to supply to the function.
    model : str | None
        Model to supply to the function.
    auth_style : str | None
        Authorization style to supply to the function.
    aali_fallback : Optional[Callable[[], AaliChatModel | None]]
        AALI fallback to supply to the function.
    env : Any
        Environment mapping to read instead of the process environment.

    Returns
    -------
    ModelConfig
        Result produced by the function.
    """
    env_endpoint = env.get("LLM_ENDPOINT")
    env_api_key = env.get("LLM_API_KEY")
    env_model = first_model_token(env.get("LLM_MODEL"))
    env_auth = env.get("LLM_AUTH_STYLE")

    aali: AaliChatModel | None = None
    if aali_fallback is not None and not (endpoint or env_endpoint):
        aali = aali_fallback()

    resolved_endpoint = endpoint or env_endpoint or (aali.endpoint if aali else None)
    resolved_api_key = api_key or env_api_key or (aali.api_key if aali else None)
    resolved_model = model or env_model or (aali.model if aali else DEFAULT_MODEL)
    resolved_auth = (auth_style or env_auth or (aali.auth_style if aali else "bearer")).lower()

    return ModelConfig(
        endpoint=resolved_endpoint,
        api_key=resolved_api_key,
        model=resolved_model,
        auth_style=resolved_auth,
    )


# ---------------------------------------------------------------------------
# Per-model wire-format quirks
# ---------------------------------------------------------------------------

# Static, in-process per-model wire-format quirks. Match is by
# case-insensitive prefix on the model name. Add new entries here when a
# new model family ships with a different request shape. Never sniff
# error strings at runtime.
_MODEL_QUIRKS: tuple[tuple[str, dict[str, Any]], ...] = (
    # GPT-5 family + reasoning (o-series) models: ``max_completion_tokens``
    # only, temperature locked to the default (must be omitted from body).
    ("gpt-5", {"max_tokens_param": "max_completion_tokens", "send_temperature": False}),
    ("o1", {"max_tokens_param": "max_completion_tokens", "send_temperature": False}),
    ("o3", {"max_tokens_param": "max_completion_tokens", "send_temperature": False}),
    ("o4", {"max_tokens_param": "max_completion_tokens", "send_temperature": False}),
)


def resolve_model_quirks(model: str) -> dict[str, Any]:
    """Return the wire-format quirk dictionary for ``model`` (empty if none).

    Parameters
    ----------
    model : str
        Model to supply to the function.

    Returns
    -------
    dict[str, Any]
        Mapping containing the operation result.
    """
    name = (model or "").lower()
    for prefix, quirks in _MODEL_QUIRKS:
        if name.startswith(prefix):
            return dict(quirks)
    return {}


def max_tokens_param_for(model: str) -> str:
    """Resolve the request field used to cap output tokens for ``model``.

    Explicit ``LLM_MAX_TOKENS_PARAM`` environment wins. Otherwise, per-model
    quirks. Otherwise, the OpenAI default ``max_tokens``.

    Parameters
    ----------
    model : str
        Model to supply to the function.

    Returns
    -------
    str
        String result produced by the function.
    """
    return os.environ.get("LLM_MAX_TOKENS_PARAM") or resolve_model_quirks(model).get(
        "max_tokens_param", "max_tokens"
    )


def send_temperature_for(model: str) -> bool:
    """Whether ``temperature`` may be sent in the body for ``model``.

    Explicit ``LLM_SEND_TEMPERATURE`` environment wins. Otherwise, per-model
    quirks (reasoning models reject a non-default temperature).

    Parameters
    ----------
    model : str
        Model to supply to the function.

    Returns
    -------
    bool
        Boolean result produced by the function.
    """
    return env_flag(
        "LLM_SEND_TEMPERATURE",
        default=resolve_model_quirks(model).get("send_temperature", True),
    )


# ---------------------------------------------------------------------------
# Prompt-cache routing hint
# ---------------------------------------------------------------------------


def prompt_cache_key(model: str, messages: Sequence[dict[str, Any]]) -> str:
    """Derive a STABLE cache-routing key from model and a leading system prompt.

    The cacheable prefix every request shares is the static system prompt
    (and tool specifications derived from it). Keying on its hash groups all turns
    of all conversations using the same prompt onto the same cache node,
    maximizing prefix reuse while staying independent of per-turn volatile
    content. Returns ``""`` when no system message is present.

    Parameters
    ----------
    model : str
        Model to supply to the function.
    messages : Sequence[dict[str, Any]]
        Messages to supply to the function.

    Returns
    -------
    str
        String result produced by the function.
    """
    seed: str | None = None
    for m in messages:
        if m.get("role") == "system":
            content = m.get("content")
            if isinstance(content, str) and content:
                seed = content
            break
    if not seed:
        return ""
    digest = hashlib.sha256(f"{model}\x00{seed}".encode("utf-8", "ignore")).hexdigest()
    return f"fluids-{digest[:24]}"


# ---------------------------------------------------------------------------
# Request body + auth headers
# ---------------------------------------------------------------------------


def build_chat_body(
    *,
    model: str,
    messages: Sequence[dict[str, Any]],
    max_tokens: int | None = None,
    temperature: float | None = None,
    max_tokens_param: str | None = None,
    send_temperature: bool | None = None,
    send_cache_key: bool = True,
    response_format: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an OpenAI ``/chat/completions`` request body, model-agnostic.

    Honors per-model wire-format quirks so the same call works against
    gpt-4o-class, gpt-5/o-series reasoning models, and stripped-down
    local servers:

    * ``max_tokens`` is written under ``max_tokens`` or
      ``max_completion_tokens`` per :func:`max_tokens_param_for`. When
      ``max_tokens`` is ``None``, no token cap is sent at all (preserves
      "uncapped" callers like the codegen pipeline).
    * ``temperature`` is dropped entirely for models that reject it
      (:func:`send_temperature_for`) or when ``temperature is None``.
    * A stable ``prompt_cache_key`` is added when ``send_cache_key`` and
      ``LLM_SEND_CACHE_KEY`` are both on. Disable via
      ``LLM_SEND_CACHE_KEY=0`` for endpoints that reject unknown fields.

    Callers that have already resolved quirks once (for example, the long-lived
    agent provider) may pass ``max_tokens_param``/``send_temperature``
    explicitly to skip per-call resolution.

    Parameters
    ----------
    model : str
        Model to supply to the function.
    messages : Sequence[dict[str, Any]]
        Messages to supply to the function.
    max_tokens : int | None
        Maximum number of tokens to supply to the function.
    temperature : float | None
        Temperature to supply to the function.
    max_tokens_param : str | None
        Maximum tokens parameter to supply to the function.
    send_temperature : bool | None
        Send temperature to supply to the function.
    send_cache_key : bool
        Send cache key to supply to the function.
    response_format : dict[str, Any] | None
        Response format to supply to the function.

    Returns
    -------
    dict[str, Any]
        Mapping containing the operation result.
    """
    msgs = list(messages)
    if max_tokens_param is None:
        max_tokens_param = max_tokens_param_for(model)
    if send_temperature is None:
        send_temperature = send_temperature_for(model)

    body: dict[str, Any] = {"model": model, "messages": msgs}
    if max_tokens is not None:
        body[max_tokens_param] = max_tokens
    if temperature is not None and send_temperature:
        body["temperature"] = temperature
    if response_format is not None:
        body["response_format"] = response_format
    if send_cache_key and env_flag("LLM_SEND_CACHE_KEY", default=True):
        key = prompt_cache_key(model, msgs)
        if key:
            body["prompt_cache_key"] = key
    return body


def auth_headers(
    api_key: str | None,
    *,
    auth_style: str = "bearer",
    base: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build request headers honoring the configured authorization style.

    * ``bearer`` (default) → ``Authorization: Bearer <key>``
    * ``azure-api-key`` → ``api-key: <key>`` (Azure OpenAI)

    Returns a fresh dictionary. ``base`` (if given) is copied, not mutated.

    Parameters
    ----------
    api_key : str | None
        API key to supply to the function.
    auth_style : str
        Authorization style to supply to the function.
    base : dict[str, str] | None
        Base to supply to the function.

    Returns
    -------
    dict[str, str]
        Mapping containing the operation result.
    """
    headers: dict[str, str] = dict(base) if base else {"Content-Type": "application/json"}
    if api_key:
        if (auth_style or "bearer").lower() == "azure-api-key":
            headers["api-key"] = api_key
        else:
            headers["Authorization"] = f"Bearer {api_key}"
    return headers


# ---------------------------------------------------------------------------
# Tolerant JSON parsing
# ---------------------------------------------------------------------------

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_json_object(text: str | None) -> dict[str, Any] | None:
    r"""Best-effort parse of the first ``{...}`` JSON object in ``text``.

    Tolerates Markdown fences (for example, \`\`\`json ... \`\`\`) and
    surrounding prose because some chat completions disregard a "JSON only"
    instruction. Returns ``None`` when no object can be parsed.

    Parameters
    ----------
    text : str | None
        Text value to parse, normalize, or write.

    Returns
    -------
    dict[str, Any] | None
        Mapping containing the operation result.
    """
    if not text:
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if candidate.lower().startswith("json"):
            candidate = candidate[4:].lstrip()
        if candidate.endswith("```"):
            candidate = candidate[:-3]
        candidate = candidate.strip()
    try:
        obj = json.loads(candidate)
    except (ValueError, TypeError):
        match = _JSON_OBJECT_RE.search(candidate)
        if match is None:
            logger.debug("parse_json_object: no JSON object found")
            return None
        try:
            obj = json.loads(match.group(0))
        except (ValueError, TypeError):
            return None
    return obj if isinstance(obj, dict) else None


# ---------------------------------------------------------------------------
# Provider capability profile + native multi-provider transport
# ---------------------------------------------------------------------------
#
# Everything below turns the wire-format core into a *provider-agnostic*
# transport: one :class:`LLMProfile` describes a model's capabilities
# (route, tool mode, per-provider token-caching mechanism, retry), and the
# :func:`acall` / :func:`call` / :func:`astream` seam is the single place
# an LLM HTTP request is issued. Native vendor APIs (OpenAI, Azure,
# Anthropic, Gemini) are reached through the LiteLLM SDK *as an in-process
# library* (not a proxy). Custom OpenAI-compatible endpoints use the
# direct httpx path. LiteLLM itself is MIT/free. The only cost is the
# underlying provider's tokens. Its anonymous telemetry is disabled.


class LLMTransportError(RuntimeError):
    """Raised when an LLM transport call fails (network/provider error)."""


# Canonical provider ids.
PROVIDER_OPENAI = "openai"
PROVIDER_AZURE = "azure"
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_GEMINI = "gemini"
PROVIDER_COMPAT = "compat"  # generic OpenAI-compatible endpoint / local

_KNOWN_PROVIDERS = frozenset(
    {PROVIDER_OPENAI, PROVIDER_AZURE, PROVIDER_ANTHROPIC, PROVIDER_GEMINI, PROVIDER_COMPAT}
)

# Token-caching mechanisms, one per provider family.
CACHE_OPENAI_AUTO = "openai_auto"  # automatic prefix cache plus prompt_cache_key
CACHE_ANTHROPIC = "anthropic_cache_control"  # explicit ephemeral breakpoint
CACHE_GEMINI = "gemini_context"  # implicit/context cache
CACHE_NONE = "none"

# Transports.
TRANSPORT_LITELLM = "litellm"
TRANSPORT_COMPAT = "openai_compat"

# Default well-known hosts per provider (used by the egress allowlist
# guard). Azure is dynamic (resolved from api_base) so it is not listed.
PROVIDER_HOSTS: dict[str, frozenset[str]] = {
    PROVIDER_OPENAI: frozenset({"api.openai.com"}),
    PROVIDER_ANTHROPIC: frozenset({"api.anthropic.com"}),
    PROVIDER_GEMINI: frozenset({"generativelanguage.googleapis.com"}),
}

# Conservative default cache TTLs (seconds) used to size keep-alive.
_CACHE_TTL_DEFAULTS = {
    CACHE_OPENAI_AUTO: 300,
    CACHE_ANTHROPIC: 300,  # ephemeral 5-minute window (1h via beta)
    CACHE_GEMINI: 300,
    CACHE_NONE: 0,
}


def detect_provider(model: str | None, endpoint: str | None, *, env=os.environ) -> str:
    """Infer the provider identifier from explicit environment, model route, or hints.

    Order: ``LLM_PROVIDER`` environment → ``provider/model`` route prefix →
    endpoint host → model-name family → :data:`PROVIDER_OPENAI`.

    Parameters
    ----------
    model : str | None
        Model to supply to the function.
    endpoint : str | None
        Endpoint to supply to the function.
    env : Any
        Environment mapping to read instead of the process environment.

    Returns
    -------
    str
        String result produced by the function.
    """
    explicit = (env.get("LLM_PROVIDER") or "").strip().lower()
    if explicit in _KNOWN_PROVIDERS:
        return explicit

    name = (model or "").strip().lower()
    if "/" in name:
        prefix = name.split("/", 1)[0]
        if prefix in _KNOWN_PROVIDERS:
            return prefix
        if prefix in {"vertex_ai", "google"}:
            return PROVIDER_GEMINI

    host = ""
    if endpoint:
        try:
            from urllib.parse import urlparse

            host = (urlparse(endpoint).hostname or "").lower()
        except Exception:
            host = ""
    if host:
        if "azure" in host:
            return PROVIDER_AZURE
        if "anthropic" in host:
            return PROVIDER_ANTHROPIC
        if "googleapis" in host or "gemini" in host:
            return PROVIDER_GEMINI
        if "openai.com" in host:
            return PROVIDER_OPENAI
        # Any other explicit endpoint host → custom OpenAI-compatible.
        return PROVIDER_COMPAT

    if name.startswith("claude"):
        return PROVIDER_ANTHROPIC
    if name.startswith("gemini"):
        return PROVIDER_GEMINI
    return PROVIDER_OPENAI


def resolve_litellm_route(provider: str, model: str) -> str:
    """Return the LiteLLM ``provider/model`` route string for ``model``.

    Parameters
    ----------
    provider : str
        Provider to supply to the function.
    model : str
        Model to supply to the function.

    Returns
    -------
    str
        String result produced by the function.
    """
    name = (model or "").strip()
    if "/" in name:
        return name  # already a route (e.g. "anthropic/claude-3-5-sonnet")
    if provider == PROVIDER_AZURE:
        return f"azure/{name}"
    if provider == PROVIDER_ANTHROPIC:
        return f"anthropic/{name}"
    if provider == PROVIDER_GEMINI:
        return f"gemini/{name}"
    if provider == PROVIDER_OPENAI:
        return f"openai/{name}"
    return name


_PROVIDER_KEY_ENVS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "AZURE_API_KEY",
    "LLM_API_KEY",
)


def native_provider_configured(model: str | None, *, env=os.environ) -> bool:
    """Whether a native (LiteLLM) provider is *explicitly* configured.

    A bare default (provider ``openai``, no endpoint, no key) must NOT be
    treated as usable, or callers like ``llm_assist`` would report
    availability and then fail at call time. Returns ``True`` only when
    there is an explicit signal: ``LLM_PROVIDER`` is set, the model is a
    native/route-prefixed name, or a provider API key is present.

    Parameters
    ----------
    model : str | None
        Model to supply to the function.
    env : Any
        Environment mapping to read instead of the process environment.

    Returns
    -------
    bool
        Boolean result produced by the function.
    """
    if (env.get("LLM_PROVIDER") or "").strip():
        return True
    name = (model or "").strip().lower()
    if "/" in name or name.startswith("claude") or name.startswith("gemini"):
        return True
    return any((env.get(k) or "").strip() for k in _PROVIDER_KEY_ENVS)


def default_cache_mechanism(provider: str) -> str:
    """Return the default token-caching mechanism for ``provider``.

    Parameters
    ----------
    provider : str
        Provider to supply to the function.

    Returns
    -------
    str
        String result produced by the function.
    """
    if provider in (PROVIDER_OPENAI, PROVIDER_AZURE):
        return CACHE_OPENAI_AUTO
    if provider == PROVIDER_ANTHROPIC:
        return CACHE_ANTHROPIC
    if provider == PROVIDER_GEMINI:
        return CACHE_GEMINI
    return CACHE_NONE


@dataclass(frozen=True)
class CacheSpec:
    """Provider-agnostic token-caching descriptor."""

    mechanism: str = CACHE_NONE
    ttl_seconds: int = 0
    send_key: bool = True

    @property
    def enabled(self) -> bool:
        """Return whether the provider is enabled.

        Returns
        -------
        bool
            Boolean result of the operation.
        """
        return self.mechanism != CACHE_NONE


@dataclass(frozen=True)
class RetrySpec:
    """Retry/backoff policy applied by the transport seam."""

    max_attempts: int = 3
    backoff_base: float = 0.5
    timeout_s: float = 60.0


@dataclass(frozen=True)
class LLMProfile:
    """A fully resolved, provider-agnostic capability descriptor.

    One descriptor drives every LLM interaction: which provider/route to
    call, whether native tool-calling is available, the per-provider
    token-caching mechanism, and the retry policy. Resolved once via
    :func:`resolve_profile` and read by the transport seam and callers.
    """

    provider: str
    model: str
    route: str
    endpoint: str | None
    transport: str
    tool_mode: str  # "native" | "json_envelope"
    supports_streaming: bool
    supports_json_mode: bool
    max_tokens_param: str
    send_temperature: bool
    cache: CacheSpec
    retry: RetrySpec
    auth_style: str = "bearer"


def resolve_profile(
    *,
    model: str | None = None,
    endpoint: str | None = None,
    auth_style: str | None = None,
    env=os.environ,
    aali_fallback: Optional[Callable[[], AaliChatModel | None]] = None,
) -> LLMProfile:
    """Resolve an :class:`LLMProfile` from environment plus model/endpoint hints.

    Precedence mirrors :func:`resolve_model_config`: explicit arguments →
    ``LLM_*`` environment plus optional AALI configuration → safe defaults.

    Parameters
    ----------
    model : str | None
        Model to supply to the function.
    endpoint : str | None
        Endpoint to supply to the function.
    auth_style : str | None
        Authorization style to supply to the function.
    env : Any
        Environment mapping to read instead of the process environment.
    aali_fallback : Optional[Callable[[], AaliChatModel | None]]
        AALI fallback to supply to the function.

    Returns
    -------
    LLMProfile
        Result produced by the function.
    """
    cfg = resolve_model_config(
        endpoint=endpoint,
        model=model,
        auth_style=auth_style,
        aali_fallback=aali_fallback,
        env=env,
    )
    provider = detect_provider(cfg.model, cfg.endpoint, env=env)

    transport = (env.get("LLM_TRANSPORT") or "auto").strip().lower()
    if transport not in (TRANSPORT_LITELLM, TRANSPORT_COMPAT, "auto"):
        transport = "auto"
    if transport == "auto":
        # An EXPLICIT endpoint always means "POST OpenAI-style to this URL":
        # use the dependency-free httpx path, even when the host name happens
        # to contain a vendor token (e.g. ``*.openai.azure.com``). This keeps
        # OpenAI / Azure-OpenAI / any OpenAI-compatible gateway working
        # without requiring the optional ``litellm`` package, and matches the
        # behavior from before native multi-provider support landed. The
        # native LiteLLM transport is reserved for KEY-based access with no
        # endpoint (true native vendor APIs). To force the native SDK against
        # an endpoint, set ``LLM_TRANSPORT=litellm`` explicitly.
        if cfg.endpoint:
            transport = TRANSPORT_COMPAT
        elif provider in (PROVIDER_ANTHROPIC, PROVIDER_GEMINI, PROVIDER_AZURE, PROVIDER_OPENAI):
            transport = TRANSPORT_LITELLM
        else:
            transport = TRANSPORT_COMPAT

    mechanism = (env.get("LLM_CACHE_MECHANISM") or "").strip().lower()
    if mechanism not in (CACHE_OPENAI_AUTO, CACHE_ANTHROPIC, CACHE_GEMINI, CACHE_NONE):
        mechanism = default_cache_mechanism(provider)
    ttl_raw = env.get("LLM_CACHE_TTL_SECONDS")
    try:
        ttl = int(ttl_raw) if ttl_raw else _CACHE_TTL_DEFAULTS.get(mechanism, 0)
    except (TypeError, ValueError):
        ttl = _CACHE_TTL_DEFAULTS.get(mechanism, 0)

    tool_mode = (env.get("LLM_TOOL_MODE") or "native").strip().lower()
    if tool_mode not in ("native", "json_envelope"):
        tool_mode = "native"

    try:
        max_attempts = int(env.get("LLM_MAX_RETRIES") or 3)
    except (TypeError, ValueError):
        max_attempts = 3
    try:
        timeout_s = float(env.get("LLM_TIMEOUT_SECONDS") or 60.0)
    except (TypeError, ValueError):
        timeout_s = 60.0

    return LLMProfile(
        provider=provider,
        model=cfg.model,
        route=resolve_litellm_route(provider, cfg.model),
        endpoint=cfg.endpoint,
        transport=transport,
        tool_mode=tool_mode,
        supports_streaming=env_flag("LLM_SUPPORTS_STREAMING", default=True),
        supports_json_mode=env_flag("LLM_SUPPORTS_JSON_MODE", default=True),
        max_tokens_param=max_tokens_param_for(cfg.model),
        send_temperature=send_temperature_for(cfg.model),
        cache=CacheSpec(
            mechanism=mechanism,
            ttl_seconds=ttl,
            send_key=env_flag("LLM_SEND_CACHE_KEY", default=True),
        ),
        retry=RetrySpec(max_attempts=max_attempts, timeout_s=timeout_s),
        auth_style=cfg.auth_style,
    )


# ---------------------------------------------------------------------------
# Per-provider request adaptation
# ---------------------------------------------------------------------------


def apply_anthropic_cache_control(
    messages: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply Anthropic ephemeral cache control to the system prompt.

    Return a copy of ``messages`` with an Anthropic ephemeral cache
    breakpoint on the (first) system prompt.

    Anthropic prompt caching is opt-in: the stable prefix must carry a
    ``cache_control`` marker. The end of the system message is marked so the
    entire system prompt (the large, stable prefix) is cached. A no-op
    when there is no string-content system message.

    Parameters
    ----------
    messages : Sequence[dict[str, Any]]
        Messages to supply to the function.

    Returns
    -------
    list[dict[str, Any]]
        Mapping containing the operation result.
    """
    out: list[dict[str, Any]] = []
    marked = False
    for m in messages:
        if (
            not marked
            and m.get("role") == "system"
            and isinstance(m.get("content"), str)
            and m.get("content")
        ):
            blocks = [
                {
                    "type": "text",
                    "text": m["content"],
                    "cache_control": {"type": "ephemeral"},
                }
            ]
            new_m = dict(m)
            new_m["content"] = blocks
            out.append(new_m)
            marked = True
        else:
            out.append(dict(m))
    return out


def normalize_anthropic_messages(
    messages: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return a copy of ``messages`` that is valid for the Anthropic API.

    OpenAI tolerates a conversation that ends with a ``system`` (or
    ``assistant``) message and treats it as a prefill; Anthropic rejects
    it with *"This model does not support assistant message prefill. The
    conversation must end with a user message."* Two shape differences are
    reconciled here so the same OpenAI-shaped transcript works on both:

    1. **Mid-conversation ``system`` messages** (e.g. the per-turn
       plan-state / live-context messages the agent loop re-injects) are
       converted to ``assistant`` messages. Anthropic only accepts
       ``system`` as the dedicated top-level field, so any non-leading
       ``system`` turn must be carried in the conversation under one of
       the ``user`` / ``assistant`` roles.
    2. **A trailing non-user message** (the now-converted plan-state turn,
       or a leftover assistant turn) would leave the conversation ending
       on ``assistant`` — which Anthropic reads as a prefill and rejects.
       If the list does not end with ``user``, a minimal ``user``
       continuation turn is appended so the conversation ends correctly.

    The (first) leading ``system`` message is left untouched — LiteLLM
    maps it onto Anthropic's ``system`` field.

    Parameters
    ----------
    messages : Sequence[dict[str, Any]]
        Messages supplied to the function.

    Returns
    -------
    list[dict[str, Any]]
        Anthropic-valid copy of ``messages``.
    """
    out: list[dict[str, Any]] = []
    leading_system_seen = False
    for m in messages:
        role = m.get("role")
        if role == "system":
            if not leading_system_seen and not out:
                # The single leading system prompt → Anthropic system field.
                leading_system_seen = True
                out.append(dict(m))
                continue
            # Any later system message becomes an assistant turn.
            converted = dict(m)
            converted["role"] = "assistant"
            out.append(converted)
            continue
        out.append(dict(m))

    # Ensure the conversation ends with a user message.
    if out and out[-1].get("role") != "user":
        out.append({"role": "user", "content": "Continue."})
    return out


def build_litellm_kwargs(
    profile: LLMProfile,
    messages: Sequence[dict[str, Any]],
    *,
    tools: Sequence[dict[str, Any]] | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    response_format: dict[str, Any] | None = None,
    api_key: str | None = None,
    api_base: str | None = None,
    api_version: str | None = None,
    stream: bool = False,
) -> dict[str, Any]:
    """Assemble keyword arguments for ``litellm.(a)completion``.

    Applies the per-provider caching mechanism, native tool schema, and
    credentials. The retry is owned by the seam (``num_retries=0``).

    Parameters
    ----------
    profile : LLMProfile
        Profile to supply to the function.
    messages : Sequence[dict[str, Any]]
        Messages to supply to the function.
    tools : Sequence[dict[str, Any]] | None
        Tools to supply to the function.
    max_tokens : int | None
        Maximum number of tokens to supply to the function.
    temperature : float | None
        Temperature to supply to the function.
    response_format : dict[str, Any] | None
        Response format to supply to the function.
    api_key : str | None
        API key to supply to the function.
    api_base : str | None
        API base to supply to the function.
    api_version : str | None
        API version to supply to the function.
    stream : bool
        Stream to supply to the function.

    Returns
    -------
    dict[str, Any]
        Mapping containing the operation result.
    """
    msgs = list(messages)
    if profile.provider == PROVIDER_ANTHROPIC:
        # Anthropic rejects a conversation that ends with a non-user
        # message (it reads it as an assistant prefill) and only accepts
        # ``system`` as a top-level field. Reconcile the OpenAI-shaped
        # transcript BEFORE applying the cache breakpoint so the marker
        # still lands on the (untouched) leading system message.
        msgs = normalize_anthropic_messages(msgs)
    if profile.cache.mechanism == CACHE_ANTHROPIC and profile.cache.send_key:
        msgs = apply_anthropic_cache_control(msgs)

    kwargs: dict[str, Any] = {
        "model": profile.route,
        "messages": msgs,
        "num_retries": 0,
        "drop_params": True,  # let LiteLLM drop unsupported params per model
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if temperature is not None and profile.send_temperature:
        kwargs["temperature"] = temperature
    if tools:
        kwargs["tools"] = list(tools)
        kwargs["tool_choice"] = "auto"
    if response_format is not None:
        kwargs["response_format"] = response_format
    if profile.cache.mechanism == CACHE_OPENAI_AUTO and profile.cache.send_key:
        key = prompt_cache_key(profile.model, messages)
        if key:
            kwargs["prompt_cache_key"] = key
    if api_key:
        kwargs["api_key"] = api_key
    if api_base:
        kwargs["api_base"] = api_base
    if api_version:
        kwargs["api_version"] = api_version
    return kwargs


# ---------------------------------------------------------------------------
# Usage normalization (cross-provider token accounting)
# ---------------------------------------------------------------------------


def _usage_to_dict(usage: Any) -> dict[str, Any]:
    """Convert token usage data to a dictionary.

    Parameters
    ----------
    usage : Any
        Usage to supply to the function.

    Returns
    -------
    dict[str, Any]
        Mapping containing the operation result.
    """
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return usage
    for attr in ("model_dump", "dict", "to_dict"):
        fn = getattr(usage, attr, None)
        if callable(fn):
            try:
                d = fn()
                if isinstance(d, dict):
                    return d
            except Exception:
                logger.warning("Could not call %s() on usage object: %s", attr, usage)
    return {
        k: getattr(usage, k)
        for k in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "prompt_tokens_details",
        )
        if hasattr(usage, k)
    }


def normalize_usage(usage: Any) -> dict[str, int]:
    """Map any provider's usage object/dictionary to a canonical token dictionary.

    Canonical keys: ``prompt_tokens``, ``completion_tokens``,
    ``total_tokens``, ``cached_prompt_tokens`` (cache reads),
    ``cache_creation_tokens`` (cache writes — Anthropic, billed higher).
    Handles OpenAI/Azure ``prompt_tokens_details.cached_tokens``,
    Anthropic ``cache_read_input_tokens`` / ``cache_creation_input_tokens``,
    and Gemini ``cachedContentTokenCount``.

    Parameters
    ----------
    usage : Any
        Usage to supply to the function.

    Returns
    -------
    dict[str, int]
        Mapping containing the operation result.
    """
    d = _usage_to_dict(usage)

    def _int(v: Any) -> int:
        """Convert the response value to an integer.

        Parameters
        ----------
        v : Any
            V to supply to the function.

        Returns
        -------
        int
            Configured integer limit used by the helper.
        """
        return int(v) if isinstance(v, (int, float)) else 0

    out: dict[str, int] = {
        "prompt_tokens": _int(d.get("prompt_tokens")),
        "completion_tokens": _int(d.get("completion_tokens")),
        "total_tokens": _int(d.get("total_tokens")),
    }

    cached = 0
    details = d.get("prompt_tokens_details")
    if isinstance(details, dict):
        cached = _int(details.get("cached_tokens"))
    elif details is not None:
        cached = _int(getattr(details, "cached_tokens", 0))
    cached = max(
        cached,
        _int(d.get("cache_read_input_tokens")),
        _int(d.get("cachedContentTokenCount")),
    )
    if cached:
        out["cached_prompt_tokens"] = cached

    creation = _int(d.get("cache_creation_input_tokens"))
    if creation:
        out["cache_creation_tokens"] = creation
    return out


# ---------------------------------------------------------------------------
# Egress guards + transport seam
# ---------------------------------------------------------------------------


def _guard_egress(
    profile: LLMProfile,
    *,
    api_base: str | None,
    allowed_hosts: frozenset[str] | set[str] | None,
    offline: bool,
) -> None:
    """Enforce the offline kill switch and the host allowlist.

    Parameters
    ----------
    profile : LLMProfile
        Profile to supply to the function.
    api_base : str | None
        API base to supply to the function.
    allowed_hosts : frozenset[str] | set[str] | None
        Allowed hosts to supply to the function.
    offline : bool
        Offline to supply to the function.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    if offline:
        raise LLMTransportError("offline mode is set; refusing to call any LLM endpoint.")
    if not allowed_hosts:
        return
    from urllib.parse import urlparse

    hosts: set[str] = set()
    target = api_base or profile.endpoint
    if target:
        try:
            h = (urlparse(target).hostname or "").lower()
            if h:
                hosts.add(h)
        except Exception:
            h = ""
    hosts |= set(PROVIDER_HOSTS.get(profile.provider, frozenset()))
    if not hosts:
        return  # nothing concrete to check (e.g. azure without api_base)
    allowed = {h.lower() for h in allowed_hosts}
    if not (hosts & allowed):
        raise LLMTransportError(
            f"refusing to call hosts {sorted(hosts)!r}; not in allowed LLM "
            f"hosts {sorted(allowed)!r}. Set FLUIDS_AGENT_ALLOWED_LLM_HOSTS."
        )


def _import_litellm():
    """Import LiteLLM and report whether it is available.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    import sys as _sys

    if getattr(_sys, "frozen", False) and "tokenizers" not in _sys.modules:
        import types as _types

        _stub = _types.ModuleType("tokenizers")
        _stub.__doc__ = "Stub — native tokenizers unavailable in frozen env."
        _stub.__path__ = []
        _stub.__file__ = "<frozen-stub>"

        class _Tok:
            def __init__(self, *a, **kw):
                raise ImportError("tokenizers native ext not available in frozen env")

            @classmethod
            def from_pretrained(cls, *a, **kw):
                raise ImportError("tokenizers native ext not available in frozen env")

        _stub.Tokenizer = _Tok
        _sys.modules["tokenizers"] = _stub
        _sys.modules["tokenizers.tokenizers"] = _stub
        del _types, _stub

    try:
        import litellm  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise LLMTransportError(
            "litellm is required for native multi-provider transport; "
            "install with `pip install ansys-fluent-mcp[providers]`."
        ) from exc
    # Disable anonymous telemetry — nothing phones home.
    try:
        litellm.telemetry = False
    except Exception as exc:
        logger.debug("Could not disable LiteLLM telemetry flag: %s", exc)
    return litellm


async def acall(
    profile: LLMProfile,
    messages: Sequence[dict[str, Any]],
    *,
    tools: Sequence[dict[str, Any]] | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    response_format: dict[str, Any] | None = None,
    api_key: str | None = None,
    api_base: str | None = None,
    api_version: str | None = None,
    allowed_hosts: frozenset[str] | set[str] | None = None,
    offline: bool = False,
) -> dict[str, Any]:
    """Async transport seam, which is the single place an LLM call is issued.

    Returns an OpenAI-shaped response dictionary (``choices`` plus ``usage``) so
    existing parsers work unchanged. Dispatches to LiteLLM for native
    providers or the httpx OpenAI-compatible path for custom endpoints.

    Parameters
    ----------
    profile : LLMProfile
        Profile to supply to the function.
    messages : Sequence[dict[str, Any]]
        Messages to supply to the function.
    tools : Sequence[dict[str, Any]] | None
        Tools to supply to the function.
    max_tokens : int | None
        Maximum number of tokens to supply to the function.
    temperature : float | None
        Temperature to supply to the function.
    response_format : dict[str, Any] | None
        Response format to supply to the function.
    api_key : str | None
        API key to supply to the function.
    api_base : str | None
        API base to supply to the function.
    api_version : str | None
        API version to supply to the function.
    allowed_hosts : frozenset[str] | set[str] | None
        Allowed hosts to supply to the function.
    offline : bool
        Offline to supply to the function.

    Returns
    -------
    dict[str, Any]
        Mapping containing the operation result.
    """
    import asyncio

    _guard_egress(profile, api_base=api_base, allowed_hosts=allowed_hosts, offline=offline)

    last_exc: Exception | None = None
    for attempt in range(max(1, profile.retry.max_attempts)):
        try:
            if profile.transport == TRANSPORT_LITELLM:
                litellm = _import_litellm()
                kwargs = build_litellm_kwargs(
                    profile,
                    messages,
                    tools=tools,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    response_format=response_format,
                    api_key=api_key,
                    api_base=api_base,
                    api_version=api_version,
                )
                resp = await litellm.acompletion(timeout=profile.retry.timeout_s, **kwargs)
                return resp.model_dump() if hasattr(resp, "model_dump") else dict(resp)
            return await _acall_httpx(
                profile,
                messages,
                tools=tools,
                max_tokens=max_tokens,
                temperature=temperature,
                response_format=response_format,
                api_key=api_key,
            )
        except LLMTransportError:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt + 1 >= max(1, profile.retry.max_attempts):
                break
            await asyncio.sleep(profile.retry.backoff_base * (2**attempt))
    raise LLMTransportError(f"LLM call failed: {last_exc}") from last_exc


def call(
    profile: LLMProfile,
    messages: Sequence[dict[str, Any]],
    *,
    tools: Sequence[dict[str, Any]] | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    response_format: dict[str, Any] | None = None,
    api_key: str | None = None,
    api_base: str | None = None,
    api_version: str | None = None,
    allowed_hosts: frozenset[str] | set[str] | None = None,
    offline: bool = False,
) -> dict[str, Any]:
    """Return synchronous counterpart to :func:`acall`.

    Parameters
    ----------
    profile : LLMProfile
        Profile to supply to the function.
    messages : Sequence[dict[str, Any]]
        Messages to supply to the function.
    tools : Sequence[dict[str, Any]] | None
        Tools to supply to the function.
    max_tokens : int | None
        Maximum number of tokens to supply to the function.
    temperature : float | None
        Temperature to supply to the function.
    response_format : dict[str, Any] | None
        Response format to supply to the function.
    api_key : str | None
        API key to supply to the function.
    api_base : str | None
        API base to supply to the function.
    api_version : str | None
        API version to supply to the function.
    allowed_hosts : frozenset[str] | set[str] | None
        Allowed hosts to supply to the function.
    offline : bool
        Offline to supply to the function.

    Returns
    -------
    dict[str, Any]
        Mapping containing the operation result.
    """
    import time

    _guard_egress(profile, api_base=api_base, allowed_hosts=allowed_hosts, offline=offline)

    last_exc: Exception | None = None
    for attempt in range(max(1, profile.retry.max_attempts)):
        try:
            if profile.transport == TRANSPORT_LITELLM:
                litellm = _import_litellm()
                kwargs = build_litellm_kwargs(
                    profile,
                    messages,
                    tools=tools,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    response_format=response_format,
                    api_key=api_key,
                    api_base=api_base,
                    api_version=api_version,
                )
                resp = litellm.completion(timeout=profile.retry.timeout_s, **kwargs)
                return resp.model_dump() if hasattr(resp, "model_dump") else dict(resp)
            return _call_httpx(
                profile,
                messages,
                tools=tools,
                max_tokens=max_tokens,
                temperature=temperature,
                response_format=response_format,
                api_key=api_key,
            )
        except LLMTransportError:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt + 1 >= max(1, profile.retry.max_attempts):
                break
            time.sleep(profile.retry.backoff_base * (2**attempt))
    raise LLMTransportError(f"LLM call failed: {last_exc}") from last_exc


async def astream(
    profile: LLMProfile,
    messages: Sequence[dict[str, Any]],
    *,
    tools: Sequence[dict[str, Any]] | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    api_key: str | None = None,
    api_base: str | None = None,
    api_version: str | None = None,
    allowed_hosts: frozenset[str] | set[str] | None = None,
    offline: bool = False,
):
    """Async streaming seam — yields OpenAI-shaped chunk dicts.

    Parameters
    ----------
    profile : LLMProfile
        Profile to supply to the function.
    messages : Sequence[dict[str, Any]]
        Messages to supply to the function.
    tools : Sequence[dict[str, Any]] | None
        Tools to supply to the function.
    max_tokens : int | None
        Maximum number of tokens to supply to the function.
    temperature : float | None
        Temperature to supply to the function.
    api_key : str | None
        API key to supply to the function.
    api_base : str | None
        API base to supply to the function.
    api_version : str | None
        API version to supply to the function.
    allowed_hosts : frozenset[str] | set[str] | None
        Allowed hosts to supply to the function.
    offline : bool
        Offline to supply to the function.

    Returns
    -------
    Any
        Result produced by the function.
    """
    _guard_egress(profile, api_base=api_base, allowed_hosts=allowed_hosts, offline=offline)
    if profile.transport != TRANSPORT_LITELLM:
        raise LLMTransportError(
            "astream is only supported on the LiteLLM transport; use acall for OpenAI-compatible endpoints."  # noqa: E501
        )
    litellm = _import_litellm()
    kwargs = build_litellm_kwargs(
        profile,
        messages,
        tools=tools,
        max_tokens=max_tokens,
        temperature=temperature,
        api_key=api_key,
        api_base=api_base,
        api_version=api_version,
        stream=True,
    )
    stream = await litellm.acompletion(stream=True, timeout=profile.retry.timeout_s, **kwargs)
    async for chunk in stream:
        yield chunk.model_dump() if hasattr(chunk, "model_dump") else dict(chunk)


async def warm_cache(
    profile: LLMProfile,
    system_prompt: str,
    *,
    tools: Sequence[dict[str, Any]] | None = None,
    api_key: str | None = None,
    api_base: str | None = None,
    api_version: str | None = None,
    allowed_hosts: frozenset[str] | set[str] | None = None,
    offline: bool = False,
) -> dict[str, int] | None:
    """Prime the provider's prompt cache with the stable system prefix.

    Sends a minimal completion (the system prefix plus a one-token reply cap)
    so the next real turn reads the cached prefix instead of re-ingesting
    it after an idle period. No-op (returns ``None``) when the profile has
    no caching mechanism, when offline, or when there is no prefix.
    Errors are swallowed. Warming is best-effort and must never break a
    request path. Returns the normalized usage on success.

    Parameters
    ----------
    profile : LLMProfile
        Profile to supply to the function.
    system_prompt : str
        System prompt to supply to the function.
    tools : Sequence[dict[str, Any]] | None
        Tools to supply to the function.
    api_key : str | None
        API key to supply to the function.
    api_base : str | None
        API base to supply to the function.
    api_version : str | None
        API version to supply to the function.
    allowed_hosts : frozenset[str] | set[str] | None
        Allowed hosts to supply to the function.
    offline : bool
        Offline to supply to the function.

    Returns
    -------
    dict[str, int] | None
        Mapping containing the operation result.
    """
    if not profile.cache.enabled or not system_prompt or offline:
        return None
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "ping"},
    ]
    try:
        resp = await acall(
            profile,
            messages,
            tools=tools,
            max_tokens=1,
            api_key=api_key,
            api_base=api_base,
            api_version=api_version,
            allowed_hosts=allowed_hosts,
            offline=offline,
        )
    except Exception as exc:
        logger.debug("warm_cache failed (ignored): %s", exc)
        return None
    return normalize_usage(resp.get("usage") if isinstance(resp, dict) else None)


def _build_compat_request(
    profile: LLMProfile,
    messages: Sequence[dict[str, Any]],
    *,
    tools: Sequence[dict[str, Any]] | None,
    max_tokens: int | None,
    temperature: float | None,
    response_format: dict[str, Any] | None,
    api_key: str | None,
) -> tuple[str, dict[str, Any], dict[str, str]]:
    """Build compat request.

    Parameters
    ----------
    profile : LLMProfile
        Profile to supply to the function.
    messages : Sequence[dict[str, Any]]
        Messages to supply to the function.
    tools : Sequence[dict[str, Any]] | None
        Tools to supply to the function.
    max_tokens : int | None
        Maximum number of tokens to supply to the function.
    temperature : float | None
        Temperature to supply to the function.
    response_format : dict[str, Any] | None
        Response format to supply to the function.
    api_key : str | None
        API key used to authenticate requests.

    Returns
    -------
    tuple[str, dict[str, Any], dict[str, str]]
        Tuple containing the operation result.
    """
    if not profile.endpoint:
        raise LLMTransportError("no endpoint configured for OpenAI-compatible transport.")
    url = normalise_endpoint(profile.endpoint)
    body = build_chat_body(
        model=profile.model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        max_tokens_param=profile.max_tokens_param,
        send_temperature=profile.send_temperature,
        send_cache_key=profile.cache.mechanism == CACHE_OPENAI_AUTO and profile.cache.send_key,
        response_format=response_format,
    )
    if tools:
        body["tools"] = list(tools)
        body["tool_choice"] = "auto"
    headers = auth_headers(api_key, auth_style=profile.auth_style)
    return url, body, headers


async def _acall_httpx(
    profile, messages, *, tools, max_tokens, temperature, response_format, api_key
) -> dict[str, Any]:
    """Call the OpenAI-compatible endpoint asynchronously with httpx.

    Parameters
    ----------
    profile : Any
        Profile to supply to the function.
    messages : Any
        Messages to supply to the function.
    tools : Any
        Tools to supply to the function.
    max_tokens : Any
        Maximum number of tokens to supply to the function.
    temperature : Any
        Temperature to supply to the function.
    response_format : Any
        Response format to supply to the function.
    api_key : Any
        API key used to authenticate requests.

    Returns
    -------
    dict[str, Any]
        Mapping containing the operation result.
    """
    import httpx

    url, body, headers = _build_compat_request(
        profile,
        messages,
        tools=tools,
        max_tokens=max_tokens,
        temperature=temperature,
        response_format=response_format,
        api_key=api_key,
    )
    async with httpx.AsyncClient(
        verify=resolve_tls_verify(), timeout=profile.retry.timeout_s
    ) as client:
        resp = await client.post(url, json=body, headers=headers)
        resp.raise_for_status()
        return resp.json()


def _call_httpx(
    profile, messages, *, tools, max_tokens, temperature, response_format, api_key
) -> dict[str, Any]:
    """Call the OpenAI-compatible endpoint synchronously with httpx.

    Parameters
    ----------
    profile : Any
        Profile to supply to the function.
    messages : Any
        Messages to supply to the function.
    tools : Any
        Tools to supply to the function.
    max_tokens : Any
        Maximum number of tokens to supply to the function.
    temperature : Any
        Temperature to supply to the function.
    response_format : Any
        Response format to supply to the function.
    api_key : Any
        API key used to authenticate requests.

    Returns
    -------
    dict[str, Any]
        Mapping containing the operation result.
    """
    import httpx

    url, body, headers = _build_compat_request(
        profile,
        messages,
        tools=tools,
        max_tokens=max_tokens,
        temperature=temperature,
        response_format=response_format,
        api_key=api_key,
    )
    with httpx.Client(verify=resolve_tls_verify(), timeout=profile.retry.timeout_s) as client:
        resp = client.post(url, json=body, headers=headers)
        resp.raise_for_status()
        return resp.json()


__all__ = [
    "DEFAULT_MODEL",
    "env_flag",
    "resolve_tls_verify",
    "first_model_token",
    "AaliChatModel",
    "load_aali_chat_model",
    "normalise_endpoint",
    "ModelConfig",
    "resolve_model_config",
    "resolve_model_quirks",
    "max_tokens_param_for",
    "send_temperature_for",
    "prompt_cache_key",
    "build_chat_body",
    "auth_headers",
    "parse_json_object",
    # provider-agnostic transport
    "LLMTransportError",
    "PROVIDER_OPENAI",
    "PROVIDER_AZURE",
    "PROVIDER_ANTHROPIC",
    "PROVIDER_GEMINI",
    "PROVIDER_COMPAT",
    "PROVIDER_HOSTS",
    "CACHE_OPENAI_AUTO",
    "CACHE_ANTHROPIC",
    "CACHE_GEMINI",
    "CACHE_NONE",
    "TRANSPORT_LITELLM",
    "TRANSPORT_COMPAT",
    "detect_provider",
    "resolve_litellm_route",
    "native_provider_configured",
    "default_cache_mechanism",
    "CacheSpec",
    "RetrySpec",
    "LLMProfile",
    "resolve_profile",
    "apply_anthropic_cache_control",
    "build_litellm_kwargs",
    "normalize_usage",
    "acall",
    "call",
    "astream",
    "warm_cache",
]
