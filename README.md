# PyFluent-MCP

[![PyAnsys](https://img.shields.io/badge/Py-Ansys-ffc107.svg?logo=data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAABDklEQVQ4jWNgoDfg5+OQgMJ/0AqCqXGQMEBAwBEKQj5gGDjQsA80UeCDscxrD4YhGsgABEELnC5zAwAu6ACKQDAQzNBFwAAVdgFEAnfDiQAATyIBaAFgCbkAI5DQwAVGAYkAMA4gHgg2AC+AAgQIABggagAqyAD4AACkR7cEdcEBQOPjIvAEtRDoAbYLANQAZGsBEAFeBwCsAY0HgGCAAEQTaDj7xQABItJ+S3DsQAAAABJRU5ErkJggg==)](https://docs.pyansys.com/)
[![Python](https://img.shields.io/badge/Python-3.12%20%7C%203.13-blue)](https://www.python.org/)
[![Apache](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

PyFluent-MCP (`ansys-fluent-mcp`) gives you a [Model Context Protocol (MCP)](https://modelcontextprotocol.io/)
server that lets AI assistants interact with Ansys Fluent through
[PyFluent](https://fluent.docs.pyansys.com/).

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

The server is a **stateless** MCP leaf. Your MCP host (VS Code Copilot,
Claude Desktop, Cursor, or a custom agent) calls a focused tool set. Fluent
mutations run only through validated MCP tools, and Python executes only
through a validated, sandboxed execution path.

Key features include:

- **Manage connections dynamically**: Launch a new Fluent session, attach to an
  existing session (local or remote), or disconnect on demand.
- **Inspect the live settings tree**: Explore the active Fluent settings API,
  named objects, allowed values, and targeted context.
- **Run code through a validated execution path**: Run or precheck Python in a persistent
  session behind an AST sandbox.
- **Review results and diagnostics**: Summarize setup, build a simulation
  report, inspect mesh quality, list fields, compare case files, and capture
  screenshots.
- **Extend with pluggable backends**: Use the local PyFluent backend by default
  and add execution backends through separately installed entry-point packages.

PyFluent-MCP itself is deterministic infrastructure. It does not own model
runtime selection, provider orchestration, transport policy, retries, or
agent loops. Those concerns live in higher-level host products such as
`fluids-mcp`, which consume this package over the MCP wire.

## Tool surface

You can use 20 tools exposed by the server:

| Group | Tools |
|-------|-------|
| Connection and session | `connect`, `disconnect`, `session_status`, `solver_status` |
| Schema discovery | `find_api`, `get_help`, `get_state`, `get_targeted_context` |
| Named objects | `list_named_objects`, `find_named_object`, `select_named_objects` |
| Execution and validation | `run_code`, `validate_code` |
| Reporting and inspection | `summarize_setup`, `simulation_report`, `screenshot` |
| Mesh/fields/compare | `mesh_quality`, `list_fields`, `compare_files` |

## Requirements

| Requirement | When needed | Notes |
|-------------|-------------|-------|
| Python 3.12 or 3.13 | Always | Capped below 3.14 by the pinned `ansys-fluent-core` (see below) |
| Core runtime dependencies | Always (installed automatically) | `ansys-common-mcp`, `fastmcp`, `httpx`, `pydantic` |
| [PyFluent](https://fluent.docs.pyansys.com/) (`ansys-fluent-core` `>=0.37.2,<0.38`) | To drive a live Fluent session (`connect`, `run_code`, `get_state`, `mesh_quality`, …) | Installed automatically. Pinned to the 0.37.x line, the last that supports Fluent 2024 R1 (24.1); 0.38+ requires Fluent 2024 R2 |
| A licensed local ANSYS Fluent installation | To actually launch/attach a solver | PyFluent talks to this Fluent installation over gRPC |
| `h5py` 3.0 or later | Only for `compare_files` on `.h5`/`.cas.h5` files | The `file-probe` extra |

> **PyFluent is required for live-session tools and is installed automatically.**
> Offline-only tools such as `find_api`, `get_help`, and `validate_code`
> still work without a local Fluent installation. Any tool that touches a
> solver, including `connect`, `run_code`, `get_state`, `summarize_setup`,
> `mesh_quality`, and `screenshot`, requires a licensed Fluent installation
> on your machine.

## Installation

Install the latest release:

```bash
pip install ansys-fluent-mcp
```

To add the optional HDF5 file-probe support used by `compare_files`:

```bash
pip install "ansys-fluent-mcp[file-probe]"
```

If you want an editable developer installation with test dependencies:

```bash
git clone https://github.com/ansys/pyfluent-mcp.git
cd pyfluent-mcp
pip install -e ".[tests]"
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
| `FLUIDS_MCP_FLUENT_VERSION` | Target Fluent release for offline checks (`24.1`, `241` or `v24_1`) |
| `FLUIDS_MCP_LOG_LEVEL` | Set the log level (default `INFO`) |
| `FLUIDS_MCP_DISABLE_SESSION_LOGS` | Set to `1` to disable session logs |
| `FLUIDS_MCP_MAX_STEPS` | Set a cap on MCP tool-loop iterations (default `30`) |

### Connecting to Fluent 2024 R1 (24.1) and other older releases

The `connect` tool delegates to PyFluent, so it can attach to any Fluent
release the installed `ansys-fluent-core` supports. This project pins
`ansys-fluent-core` to `>=0.37.2,<0.38` — the last line that still
supports Fluent 2024 R1 (24.1); `ansys-fluent-core` 0.38 raised its floor
to Fluent 2024 R2. That pin also caps Python at 3.13 (0.37.x requires
`<3.14`). If you do **not** need 24.1 and want newer Fluent / PyFluent,
relax the pin to `ansys-fluent-core>=0.41` and restore Python 3.14 in
`pyproject.toml`.

To attach to a running Fluent 24.1 session, start Fluent's gRPC server
(in the Fluent GUI: **File → Applications → Server → Start...**, or launch
with `fluent 3ddp -sifile=server.txt`) and call `connect` with either the
`server_info_file` or the `ip`/`port`/`password` reported there.

The bundled offline settings schema is generated from a newer Fluent
release, so `validate_code`, `describe_path`, and `find_api` can report
paths that do not exist in 24.1. On connect the server reads the live
Fluent version and narrows those checks to it automatically. For fully
accurate offline checks, export a 24.1 snapshot once on a machine with
Fluent 24.1 installed:

```python
import gzip, json, pathlib
import ansys.fluent.core as pyfluent

solver = pyfluent.launch_fluent(product_version="24.1", mode="solver")
# Full settings tree (the same structure the bundled snapshots use).
static_info = solver.settings.get_static_info()
out = pathlib.Path("settings_241.json.gz")
with gzip.open(out, "wt", encoding="utf-8") as fh:
    json.dump(static_info, fh)
solver.exit()
```

The exact accessor for the raw settings tree varies slightly between
PyFluent versions (`solver.settings.get_static_info()`,
`solver._settings_service.get_static_info()`, or the
`ansys.fluent.core.codegen` helpers) -- any of them produce a compatible
JSON document. Then point the server at it with
`FLUIDS_MCP_SETTINGS_JSON=/path/to/settings_241.json.gz`, or drop the file
into `src/ansys/fluent/mcp/solve/data/` and set
`FLUIDS_MCP_FLUENT_VERSION=241`.

## Host ownership and architecture boundaries

`ansys-fluent-mcp` is the deterministic MCP substrate and solve leaf.
It intentionally does not own:

- model provider selection
- model routing
- transport orchestration
- caching policy
- retry management
- agent loops
- workflow reasoning

Those capabilities belong in higher-level orchestration products such as
`fluids-mcp`, VS Code Copilot agents, Claude Desktop workflows, or other
external MCP hosts.

This package focuses on:

- Fluent tool execution
- schema retrieval and grounding
- settings introspection
- validated Python execution
- deterministic MCP tooling
- backend abstractions

The architecture intentionally keeps dependency flow one-way:

```text
agent/orchestrator/runtime
            ↓
     ansys-fluent-mcp
```

The substrate never depends on the orchestration layer.

## License

This project is licensed under the Apache 2.0 license agreement. See the
[LICENSE](LICENSE) file for details.

## Resources

- [PyFluent documentation](https://fluent.docs.pyansys.com/)
- [Model Context Protocol](https://modelcontextprotocol.io/)
- [ansys-common-mcp](https://github.com/ansys/pyansys-common-mcp)

For general PyAnsys questions, email pyansys.core@ansys.com.
