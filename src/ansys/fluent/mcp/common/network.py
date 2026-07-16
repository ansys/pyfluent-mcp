from __future__ import annotations

import os


def env_flag(name: str, *, default: bool = False) -> bool:
    """Return a normalized boolean environment flag."""

    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    return value in {"1", "true", "yes", "on"}


def resolve_tls_verify(env: dict[str, str] | None = None) -> bool | str:
    """Resolve TLS verification configuration from environment."""

    src = env or os.environ
    if src.get("FLUIDS_MCP_CA_BUNDLE"):
        return src["FLUIDS_MCP_CA_BUNDLE"]
    if src.get("SSL_CERT_FILE"):
        return src["SSL_CERT_FILE"]
    if src.get("REQUESTS_CA_BUNDLE"):
        return src["REQUESTS_CA_BUNDLE"]
    verify_tls = (src.get("FLUIDS_MCP_VERIFY_TLS") or "true").strip().lower()
    return verify_tls not in {"0", "false", "off", "no"}