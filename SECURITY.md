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

HTTP retrieval and remote MCP calls verify TLS certificates by default.

### Do I need to provide a certificate?

In most cases, **no**. This package does not issue, generate, or bundle
certificates. It only verifies the certificates presented by the endpoints you
connect to.

- Public HTTPS endpoints typically work with Python's default certificate
  trust store.
- Behind a corporate proxy/firewall that intercepts TLS, or for internal
  self-signed endpoints, provide your organization's CA bundle.

### How to get and use a corporate CA bundle

1. Obtain the CA bundle (a `.pem`/`.crt` file) from your IT/security team or
   export it from your proxy infrastructure.
2. Point one of these environment variables at the file:

   ```bash
   export FLUIDS_MCP_CA_BUNDLE=/path/to/corporate-ca-bundle.pem
   # alternatives: SSL_CERT_FILE or REQUESTS_CA_BUNDLE
   ```

### Serving the MCP over HTTP

This package does not terminate TLS for its own HTTP transport. Bind it to
`127.0.0.1` and place remote access behind your own authenticated,
TLS-terminating proxy.

### Reference

| Control | Effect |
|---------|--------|
| `FLUIDS_MCP_CA_BUNDLE` (or `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE`) | Trust a corporate/self-signed CA by pointing at its PEM bundle |
| `FLUIDS_MCP_VERIFY_TLS=0` | Disable TLS verification for development/testing only |

## Secrets

- `.env` files are git-ignored; only `.env.example` (a template with no
  secrets) is tracked.
- Do not paste credentials, tokens, or internal endpoints into issues or logs.
