# PyFluent-MCP

[![PyAnsys](https://img.shields.io/badge/Py-Ansys-ffc107.svg?logo=data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAABDklEQVQ4jWNgoDfg5+OQgMJ/0AqCqXGQMEBAwBEKQj5gGDjQsA80UeCDscxrD4YhGsgABEELnC5zAwAu6ACKQDAQzNBFwAAVdgFEAnfDiQAATyIBaAFgCbkAI5DQwAVGAYkAMA4gHgg2AC+AAgQIABggagAqyAD4AACkR7cEdcEBQOPjIvAEtRDoAbYLANQAZGsBEAFeBwCsAY0HgGCAAEQTaDj7xQABItJ+S3DsQAAAABJRU5ErkJggg==)](https://docs.pyansys.com/)
[![Python](https://img.shields.io/badge/Python-3.13%2B-blue)](https://www.python.org/)
[![Apache](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

PyFluent-MCP (`ansys-fluent-mcp`) gives you a [Model Context Protocol (MCP)](https://modelcontextprotocol.io/)
server that lets AI assistants interact with Ansys Fluent through
[PyFluent](https://fluent.docs.pyansys.com/). You can use natural language
to set up, run, and postprocess CFD solver workflows.

PyFluent-MCP is built on PyAnsys Common MCP
([ansys-common-mcp](https://github.com/ansys/pyansys-common-mcp)), the shared
PyAnsys MCP foundation.

You can run this package as a standalone MCP server. It also serves as the
open-source **core + solve** foundation for higher-level products. Those
products depend on this package and call solve tools over MCP. Dependency
direction stays one-way. This package never depends on, imports, or
references products that consume it.

For architecture details, diagrams, and full tool references, see the
[PyFluent-MCP documentation](https://fluent-mcp.docs.pyansys.com). For contribution information, including how
to build documentation locally, see [Contribute](https://fluent-mcp.docs.pyansys.com/version/stable/getting_started/contribution.html).

## Overview

The server is a **stateless** MCP leaf. Your external LLM host (VS Code
Copilot, Claude Desktop, Cursor, or a custom agent) calls a focused tool set
The LLM never writes to Fluent directly. The
server runs Python only through a validated, sandboxed execution path.

Key features include:

- **Manage connections dynamically**: Launch a new Fluent session, attach to an
  existing session (local or remote), or disconnect on demand.
- **Inspect the live settings tree**: Explore the active Fluent settings API,
  named objects, allowed values, and targeted context.
- **Generate code offline first**: Generate Fluent settings-API code from
  natural language by using the bundled settings schema, without a network
  service.
- **Run code through a validated execution path**: Run or precheck Python in a persistent
  session behind an AST sandbox.
- **Review results and diagnostics**: Summarize setup, build a simulation
  report, inspect mesh quality, list fields, compare case files, and capture
  screenshots.
- **Use model- and provider-agnostic LLM support**: Work with any model and provider through a
  single in-process transport seam: native APIs (OpenAI, Azure, Anthropic, and Google Gemini)
  via LiteLLM, or an OpenAI-compatible endpoint.
- **Extend with pluggable backends**: Use the local PyFluent backend by default
  and add execution backends through separately installed entry-point packages.

## Tool surface

You can use 22 tools exposed by the server:

| Group | Tools |
|-------|-------|
| Connection and session | `connect`, `disconnect`, `session_status`, `solver_status` |
| Schema discovery | `find_api`, `get_help`, `get_state`, `get_targeted_context` |
| Named objects | `list_named_objects`, `find_named_object`, `select_named_objects` |
| Code generation and execution | `codegen`, `clarify`, `run_code`, `validate_code` |
| Reporting and inspection | `summarize_setup`, `simulation_report`, `screenshot`, `manage_component` |
| Mesh/fields/compare | `mesh_quality`, `list_fields`, `compare_files` |

## Requirements

| Requirement | When needed | Notes |
|-------------|-------------|-------|
| Python 3.13 or later | Always | 3.13 and 3.14 supported |
| Core runtime dependencies | Always (installed automatically) | `ansys-common-mcp`, `fastmcp`, `httpx`, `pydantic` |
| [PyFluent](https://fluent.docs.pyansys.com/) (`ansys-fluent-core` 0.27 or later) | To drive a live Fluent session (`connect`, `run_code`, `get_state`, `mesh_quality`, …) | The `pyfluent` extra, which is the execution backend |
| A licensed local ANSYS Fluent installation | To actually launch/attach a solver | PyFluent talks to this Fluent installation over gRPC |
| `h5py` 3.0 or later | Only for `compare_files` on `.h5`/`.cas.h5` files | The `file-probe` extra |

> **PyFluent is required for live-session tools.** You install it with the
> optional `pyfluent` extra, not as a hard dependency. That design keeps
> offline-only tools (`find_api`, `get_help`, `codegen`, `clarify`, and
> `validate_code`) usable without Fluent. Any tool that touches a solver,
> including `connect`, `run_code`, `get_state`, `summarize_setup`,
> `mesh_quality`, and `screenshot`, requires `ansys-fluent-core` and a licensed
> Fluent installation on your machine.

## Installation

Install the latest release:

```bash
pip install ansys-fluent-mcp
```

To also pull in the local PyFluent backend (required for live Fluent
sessions):

```bash
pip install "ansys-fluent-mcp[pyfluent]"
```

For more information, see [Requirements](#requirements).

To add the optional HDF5 file-probe support used by `compare_files`:

```bash
pip install "ansys-fluent-mcp[pyfluent,file-probe]"
```

If you want an editable developer installation with the PyFluent backend and test dependencies:

```bash
git clone https://github.com/ansys/pyfluent-mcp.git
cd pyfluent-mcp
pip install -e ".[pyfluent,tests]"
```

## Usage

Use STDIO for desktop MCP clients that launch the server process. Use HTTP only on trusted networks or behind infrastructure that provides authentication and TLS.

Run the server over STDIO (the default MCP transport):

```bash
ansys-fluent-mcp
```

Or run the server over streamable HTTP:

```bash
ansys-fluent-mcp --transport http --host 127.0.0.1 --port 8000
```

Starting the MCP server only makes the tools available. You still need an
MCP-compatible client, such as VS Code Copilot, Claude Desktop, Cursor, or
another assistant host, to connect to it and call those tools. Register the
server in the client's MCP configuration after choosing a transport.

If you run a local Windows checkout, point your client at the virtual
environment entry point. For VS Code MCP support, add a server entry like this
to your VS Code MCP configuration:

```json
{
  "servers": {
    "ansys-fluent-mcp": {
      "type": "stdio",
      "command": "D:\\Development\\fluent\\pyfluent-mcp\\.venv\\Scripts\\ansys-fluent-mcp.exe"
    }
  }
}
```

## Configuration

You configure the server through `FLUIDS_MCP_*` environment variables. Common variables are listed here:

| Variable | Effect |
|----------|--------|
| `FLUIDS_MCP_SETTINGS_JSON` | Override the bundled settings schema with an external file |
| `FLUIDS_MCP_LOG_LEVEL` | Set the log level (default `INFO`) |
| `FLUIDS_MCP_API_RETRIEVER_URL`/`FLUIDS_MCP_QDRANT_URL` | Opt in to semantic API retrievers (offline by default) |
| `FLUIDS_MCP_DISABLE_SESSION_LOGS` | Set to `1` to disable session logs |
| `FLUIDS_MCP_LLM_MAX_STEPS` | Set a cap on LLM codegen tool-loop iterations (default `30`) |

### Model- and provider-agnostic LLM configuration

#### Plain-English quick start

Some features use an AI model. You set three environment variables:

- Set the AI provider.
- Set the AI model.
- Set the API key.

Pick the example that matches your setup. Replace `<...>` and start the
server. The system resolves the rest automatically.

**OpenAI key**
```bash
export OPENAI_API_KEY="<your-openai-key>"
export LLM_PROVIDER="openai"
export LLM_MODEL="gpt-4o"
```

**Anthropic (Claude) key**
```bash
export ANTHROPIC_API_KEY="<your-anthropic-key>"
export LLM_PROVIDER="anthropic"
export LLM_MODEL="claude-3-5-sonnet"
```

**Google Gemini key**
```bash
export GEMINI_API_KEY="<your-gemini-key>"
export LLM_PROVIDER="gemini"
export LLM_MODEL="gemini-1.5-pro"
```

**A URL and key from your organization** (Azure OpenAI or any
OpenAI-compatible gateway)
```bash
export LLM_ENDPOINT="<the-url-you-were-given>"
export LLM_API_KEY="<your-key>"
export LLM_MODEL="gpt-4o"
```

For the first three options, install provider connectors once:
`pip install ansys-fluent-mcp[providers]`. The connector is free. You pay
only your AI provider. To change models or providers later, update the
variables and restart. The next section provides a full reference on advanced tuning.

#### Full reference

Every LLM-driven feature is model- and provider-agnostic. The server chooses
between two transports based on your model and endpoint:

- **Native vendor APIs** (OpenAI, Azure OpenAI, Anthropic, Google Gemini): The
  server reaches these APIs through the **LiteLLM SDK as an in-process
  library**, not a proxy. Set `LLM_PROVIDER` (or use a native model name such
  as `claude-3-5-sonnet`) and the matching API key.
- **OpenAI-compatible `/chat/completions`** for a custom endpoint (vLLM,
  Ollama, or a gateway): Set `LLM_ENDPOINT`. The server sends requests directly
  through `httpx`.

A single capability profile (`LLMProfile`) controls route selection,
transport, tool mode, provider-specific token caching, and retry behavior. The
`llm_wire.acall()`, `llm_wire.call()`, and `llm_wire.astream()` methods are the
only request paths. Resolution and request shaping live in
`ansys.fluent.mcp.common.llm_wire`. `common/config.py` surfaces the
resolved provider/transport.

> **Cost:** The LiteLLM SDK is MIT licensed and free to use. You pay only your
> provider token costs through your own keys. Anonymous LiteLLM telemetry is
> disabled by the integration.

| Variable | Default | Purpose |
|----------|---------|---------|
| `LLM_PROVIDER` | auto | `openai`/`azure`/`anthropic`/`gemini`/`compat` (inferred from model/endpoint when unset) |
| `LLM_TRANSPORT` | `auto` | `litellm` (native APIs) or `openai_compat` (httpx to `LLM_ENDPOINT`) |
| `LLM_ENDPOINT` | unset | OpenAI-compatible chat-completions URL (custom/local endpoints) |
| `LLM_API_KEY` | unset | Bearer token (or Azure key) for the compat path |
| `OPENAI_API_KEY`/`ANTHROPIC_API_KEY`/`GEMINI_API_KEY`/`AZURE_API_KEY` | unset | Per-vendor key for the native (LiteLLM) path |
| `LLM_API_BASE`/`LLM_API_VERSION` | unset | Azure base/version (also read from `AZURE_API_BASE` / `AZURE_API_VERSION`) |
| `LLM_MODEL` | `gpt-4o-mini` | Model name (first whitespace token is active); may be a `provider/model` route |
| `LLM_AUTH_STYLE` | auto | `bearer` or `azure-api-key` (compat path) |
| `LLM_CACHE_MECHANISM` | auto | `openai_auto`/`anthropic_cache_control`/`gemini_context`/`none` |
| `LLM_CACHE_TTL_SECONDS` | per-provider | Cache TTL hint used to size keep-alive |
| `LLM_MAX_TOKENS_PARAM` | auto | `max_tokens` vs `max_completion_tokens` (auto for gpt-5 / o-series) |
| `LLM_SEND_TEMPERATURE` | auto | `0` to omit `temperature` (auto-off for gpt-5/o-series) |
| `LLM_SEND_CACHE_KEY` | `1` | `0` to omit the cache routing hint/cache breakpoint |
| `LLM_MAX_RETRIES`/`LLM_TIMEOUT_SECONDS` | `3`/`60` | Transport seam retry/timeout |

### TLS and network egress controls

TLS certificate verification is enabled by default for every outbound LLM and
retrieval call. If you need to trust a corporate or self-signed CA, point the
client to your CA bundle instead of disabling verification.

| Variable | Default | Purpose |
|----------|---------|---------|
| `LLM_CA_BUNDLE` | unset | Path to a PEM CA bundle used to verify TLS (also reads `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`) |
| `LLM_TLS_INSECURE` | `0` | `1` disables TLS verification (logs a loud warning; exposes keys/prompts to MITM — development only) |
| `FLUIDS_AGENT_OFFLINE` | `0` | `1` forbids ALL outbound LLM and network-retrieval calls (kill switch) |
| `FLUIDS_AGENT_ALLOWED_LLM_HOSTS` | unset | Comma-separated host allowlist enforced before any outbound call |

The server applies **per-provider token caching** automatically.

- OpenAI and Azure use automatic prefix caching and a stable `prompt_cache_key`.
- Anthropic uses an ephemeral `cache_control` breakpoint on the stable system
  prefix.
- Gemini relies on implicit context caching.

The server normalizes usage across providers into canonical
`cached_prompt_tokens` (cache reads) and `cache_creation_tokens` (cache writes).

To install the native providers:

```bash
pip install ansys-fluent-mcp[providers]
```

Model-specific logic is isolated to a small prefix-keyed quirk table in
`llm_wire` (currently gpt-5, o1, o3, and o4). When you add a model family
there, every call site inherits the behavior without per-call special casing
or runtime error-string sniffing.

## License

This project is licensed under the Apache 2.0 license agreement. See the
[LICENSE](LICENSE) file for details.

## Resources

- [PyFluent documentation](https://fluent.docs.pyansys.com/)
- [Model Context Protocol](https://modelcontextprotocol.io/)
- [ansys-common-mcp](https://github.com/ansys/pyansys-common-mcp)

For general PyAnsys questions, email pyansys.core@ansys.com.
