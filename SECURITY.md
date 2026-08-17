<!--
Copyright (C) 2026 ANSYS, Inc. and/or its affiliates.
SPDX-License-Identifier: Apache-2.0


Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
-->

# Security Policy

## Supported Versions

| Version    | Supported          |
| ---------- | ------------------ |
| 0.1.x      | :white_check_mark: |

## Reporting a vulnerability

> [!CAUTION]
> Do not use GitHub issues to report any security vulnerabilities.

If you detect a vulnerability, contact the [PyAnsys Core team](mailto:pyansys.core@ansys.com),
mentioning the repository and the details of your finding. The team will address it as soon as possible.

Provide the PyAnsys Core team with this information:

- Any specific configuration settings needed to reproduce the problem
- Step-by-step guidance to reproduce the problem
- The exact location of the problematic source code, including tag, branch, commit, or a direct URL
- The potential consequences of the vulnerability, along with a description of how an attacker could take advantage of the issue

## Threat model and trust boundary

`ansys-fluent-mcp` exposes tools that drive a live ANSYS Fluent session.
In particular, `run_code` executes Python against the solver session. The
MCP server is designed to run **next to a trusted user**, not as a public
multi-tenant service.

- **Do NOT expose the MCP server (stdio/HTTP) to an untrusted network.**
  Anyone who can reach it can drive Fluent and run code in the server
  process.
- The code-execution path is guarded by an AST sandbox and a restricted
  builtins set, but this is a defense-in-depth measure, **not** an
  isolation boundary suitable for hostile input. Treat the MCP surface as
  privileged.
- When running an HTTP transport, bind it to `127.0.0.1` and place any
  remote access behind your own authenticated, TLS-terminating proxy.

## Network egress and TLS

All outbound LLM and retrieval calls verify TLS certificates **by
default**.

### Do I need to provide a certificate?

In most cases, **no**. This package does **not** issue, generate, or bundle
any certificate of its own — it only *verifies* the certificates presented by
the LLM/retrieval endpoints you connect to.

- **Public LLM providers (OpenAI, Azure, Anthropic, Gemini, …):** nothing to
  do. Their certificates are signed by publicly-trusted CAs that Python's
  built-in trust store (`certifi`) already recognizes — calls just work.
- **Behind a corporate proxy/firewall that intercepts TLS** (Zscaler,
  Netskope, etc.) **or an internal/self-signed endpoint:** you must supply
  your organization's CA bundle so the package can trust it.

### How to get and use a corporate CA bundle

1. Obtain the CA bundle (a `.pem`/`.crt` file) from **your IT/security team**
   or export it from your corporate proxy. This package cannot create one for
   you — it comes from your organization's certificate authority.
2. Point one of these environment variables at the file (first non-empty wins):

   ```bash
   export LLM_CA_BUNDLE=/path/to/corporate-ca-bundle.pem
   # alternatives, checked in this order: SSL_CERT_FILE, REQUESTS_CA_BUNDLE
   ```

   Unlike `requests`, the underlying `httpx` client does not auto-read
   `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE`, so this package resolves the bundle
   explicitly and passes it to the TLS layer for you.

### Serving the MCP over HTTP

This package does not terminate TLS for its own HTTP transport. Bind it to
`127.0.0.1` and put any remote access behind your own authenticated,
TLS-terminating proxy (nginx, Caddy, …) using a certificate you provision
there (e.g. Let's Encrypt or your corporate CA).

### Reference

| Control | Effect |
|---------|--------|
| `LLM_CA_BUNDLE` (or `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE`) | Trust a corporate/self-signed CA by pointing at its PEM bundle |
| `LLM_TLS_INSECURE=1` | Disables TLS verification (logs a warning). Development only — exposes keys/prompts to MITM |
| `FLUIDS_AGENT_OFFLINE=1` | Kill switch: forbids all outbound LLM/retrieval calls |
| `FLUIDS_AGENT_ALLOWED_LLM_HOSTS` | Comma-separated host allowlist enforced before any outbound call |

## Secrets

- Provider API keys are read from environment variables only and are never
  written to disk by this package.
- `.env` files are git-ignored; only `.env.example` (a template with no
  secrets) is tracked.
- Do not paste secrets into prompts, issues, or logs.
