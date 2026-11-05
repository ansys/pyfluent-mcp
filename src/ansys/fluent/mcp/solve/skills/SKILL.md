---
name: ansys-fluent-mcp
description: |
  Drive an ANSYS Fluent solver session through the standalone
  `ansys-fluent-mcp` MCP server. USE WHEN the user asks to load a
  case/mesh, inspect physics, change settings, run iterations, or
  query results — and the chat client is connected directly to the
  MCP (VS Code Copilot, Cursor, Claude Desktop). EXPOSES a 22-tool
  surface: `session_status`, `connect`, `disconnect`, `find_api`,
  `get_help`, `get_state`,
  `get_targeted_context`, `list_named_objects`, `find_named_object`,
  `select_named_objects`, `summarize_setup`, `simulation_report`,
  `solver_status`, `run_code`, `validate_code`, `screenshot`,
  `mesh_quality`, `list_fields`, `compare_files`.
  This is a STATELESS leaf: there is no plan/journal/rollback and no
  `propose_step` / `finalize` planning machinery — `run_code` mutates
  the live solver immediately. DO NOT USE FOR generic Python /
  CFD-theory questions unrelated to Fluent.
---

# ANSYS Fluent MCP (standalone) — Authoring Playbook

This skill targets the tool surface that `ansys-fluent-mcp` ships.
The server gives you composable primitives — discovery, state read,
schema discovery, validation, sandboxed execution — and you build
everything else by chaining them. It is **stateless**: each call stands alone, there
is no plan builder, journal, or undo.

## Tool roster (this is what you actually have)

| Tool | One-line purpose |
|---|---|
| `session_status` | Are we connected? Which backend? |
| `connect(ip?, port?, ...)` | Launch a local Fluent session or attach to a running one. |
| `disconnect` | Tear down the session. |
| `solver_status` | Is the solver busy iterating? |
| `find_api(query, limit?, kind?)` | Lexical (or, if configured, semantic) search over the Fluent settings tree. Returns ranked paths + signatures. |
| `get_help(path)` | Per-node help text. |
| `get_state(path, projection?)` | Read any settings path. `path` is a STRING. |
| `get_targeted_context(...)` | Trimmed live-context snapshot for a specific intent. |
| `list_named_objects(path)` | Enumerate the keys of a NamedObject collection. |
| `find_named_object(path, name, fuzzy?)` | Exact or fuzzy name lookup. |
| `select_named_objects(path, where=[...])` | Filter a NamedObject collection by predicates. |
| `summarize_setup(scope?)` | Compact digest of models / BCs / materials / numerics / reference values. |
| `simulation_report` | Structured solution report on the loaded case. |
| `run_code(code)` | Execute a sandboxed PyFluent Python snippet. **This is your mutator.** |
| `validate_code(code)` | AST + signature check (no execution). Always run before `run_code` for untrusted code. |
| `screenshot(filename?, width?, height?)` | Capture the active graphics window. |
| `mesh_quality(include_check?)` | Live skewness / orthogonal-quality / aspect-ratio histograms (+ optional `mesh.check()`). Route *every* "show mesh quality" / "skewness" / "orthogonal quality" / "aspect ratio" / "check mesh" intent here. |
| `list_fields(scope?)` | Enumerate scalar/vector fields available in the loaded case (pressure, velocity-magnitude, …). |
| `compare_files(path_a, path_b)` | Diff two case/mesh files in two ephemeral PyFluent sessions; returns a markdown summary table per family. Live workspace session is not touched. |

That is the complete surface. There is **no** `propose_step`,
`finalize`, `summarize_session`, `inspect_api`, `describe_command`,
`get_allowed_values`, `query_reports`, `validate_setup`,
`diagnose_divergence`, or engineering-correlation helper
(`compute_htc`, `lookup_emissivity`, …) on this server. Don't call
them — they don't exist here.

## The composition pattern

For nearly every intent, the loop is:

```
1. Discover    → find_api / get_help / list_named_objects / summarize_setup
2. Author      → your host or user supplies PyFluent settings-API Python
3. Validate    → validate_code (offline AST check)
4. Execute     → run_code (mutates the live solver)
5. Verify      → get_state / summarize_setup / simulation_report
```

Intent-to-code translation is not part of this leaf.
If a host wants to translate user intent into Python, it should do so
outside this server, then pass the resulting Python through
`validate_code` before calling `run_code`.

## Path roots (universal Fluent knowledge)

PyFluent has exactly **five** top-level path roots:

`setup.` | `solution.` | `results.` | `file.` | `mesh.`

`setup.solution.*` **does not exist**.

## Recipes for common intents

### "Show mesh quality / skewness / orthogonal quality / aspect ratio / check mesh"

Use the **`mesh_quality`** tool — it returns structured skewness /
orthogonal-quality / aspect-ratio histograms and, when
`include_check=true`, also embeds the output of Fluent's
`mesh.check()`:

```
mesh_quality(include_check=true)
```

Do NOT route these intents through `run_code`, `simulation_report`,
or `summarize_setup` — the dedicated tool already wraps the raw
PyFluent calls and normalises their output across versions.

### "What fields are available?" / "list scalar fields"

```
list_fields(scope="any")     # any | cell | node | face
```

Returns the flat list usable in report defs, contours, vectors.

### "Compare two case files"

```
compare_files(path_a="/abs/path/A.cas.h5", path_b="/abs/path/B.cas.h5")
```

Both files open in **separate ephemeral PyFluent sessions** with
`lightweight_setup=true` — your live workspace session is NOT
touched. Returns a markdown summary table in the `summary` field;
echo that string verbatim to the user.

### "Compute h for natural convection?" / "Porous resistance?" / "Emissivity / roughness of X?"

These engineering correlations and material lookups are **not on
this server**. Ground the value yourself (textbook / handbook) and
use it in host-authored Python rather than calling a non-existent
tool.

### "Show me the current setup"

`summarize_setup(scope='models')` or `summarize_setup(scope='bcs')`
— don't dump the whole tree. Scoped output keeps client context
small and the response on-topic.

### "Enable energy" / "set BC outlet to pressure-outlet" / "switch turbulence to k-ω SST"

Author PyFluent settings-API Python, validate it with `validate_code`,
then execute it with `run_code`.

Always show the generated code to the user before `run_code` — it
mutates the solver immediately and there is **no undo** on this
server.

### "What's the path for <X>?"

```
find_api(query="velocity inlet thermal temperature", kind="path")
```

Pick the most-specific match and then:

```
get_help(path="setup.boundary_conditions.velocity_inlet.<zone>.thermal.t")
```

### "List my zones / BCs / materials"

```
list_named_objects(path="setup.boundary_conditions.velocity_inlet")
list_named_objects(path="setup.cell_zone_conditions.fluid")
list_named_objects(path="setup.materials.fluid")
```

For predicate filtering, use `select_named_objects` with `where=[...]`.

### "Read a value"

`get_state(path="setup.models.viscous.k_omega_model")` — pass a
**string**, not a list. The result is the live value.

### "Iterate N steps"

```
run_code(code="solver.solution.run_calculation.iterate(iter_count=200)")
```

There's no streaming progress on this surface — you'll get the
final residual snapshot. Use `solver_status` to confirm completion.

### "Load a case / mesh"

```
run_code(code="solver.file.read_case(file_name=r'<path>')")
run_code(code="solver.file.read_data(file_name=r'<path>')")
```

## Golden rules

- **NEVER** emit `.tui.*` calls from `run_code`. Always
  `solver.settings.*` / `solver.<root>.<path>...` (the settings API
  is what's available in 27.1).
- `.list()` / `.list_properties()` are **void** — they print only.
  Use `list_named_objects` instead for programmatic discovery.
- `run_code` mutates the live solver. There is **no journal /
  rollback** on this surface. Show the user the code before you run
  it.
- Named expressions REQUIRE units when dimensional:
  `'288.15 [K]'`, `'1.225 [kg/m^3]'`, `'1 [m/s]'`. Bare numbers
  silently break downstream report defs and BC values.
- `get_state(path=...)` takes a **string**, not a list. Same for
  `find_api(query=...)`.

## Anti-patterns (do not do)

- `.tui.*` calls of any kind, including via `run_code`.
- Calling planning/governance tools (`propose_step`, `finalize`,
  `summarize_session`, `inspect_api`, `validate_setup`) — they are
  NOT part of this server.
- Routing "mesh quality" / "skewness" / "orthogonal quality" /
  "aspect ratio" / "check mesh" through `simulation_report`,
  `summarize_setup`, or hand-written `run_code` — call the
  dedicated `mesh_quality` tool instead (it returns the structured
  histograms directly and optionally wraps Fluent's `mesh.check()`).
- Calling `run_code` with intent strings (it expects Python, not
  executable code). Translate intent outside this leaf, validate the
  resulting Python, then execute it with `run_code`.
- Skipping `validate_code` on host-authored Python for safety-critical
  operations (model toggles, BC writes, `file.write_*`).

## Planning, retries, and recovery — host responsibility

This server is **stateless**. It deliberately ships no planner, no
journal, no recipe registry, and no rollback. Multi-step plan
construction, undo, retry policies, and "propose → confirm → apply"
flows are the **host's responsibility** (VS Code Copilot, Cursor,
Claude Desktop, or any custom agent that connects over MCP).

If the host has its own planner / agent loop, let it drive the
discover → generate → validate → execute → verify cycle. This server
will:

- accept and run each step,
- return rich error envelopes the host's planner can branch on,
- never retry, queue, or sequence steps internally.

### What every tool result carries (host contract)

- **Behavior hints.** Every tool declares `readOnlyHint`,
  `destructiveHint` and `idempotentHint`. Read-only tools are safe to
  run without confirmation; `run_code` is destructive and must not be
  retried blindly.
- **Error guidance.** Every error result has `retryable` and, when
  known, `next_action`. Follow `next_action`; never resend a call with
  `retryable: false` unchanged.
- **Timeouts are safe to retry.** A long call that outlives your
  request timeout keeps running. An identical retry joins the
  still-running call (or, for idempotent tools such as `connect`,
  returns its finished result once) instead of starting the work again.
- **Trace id.** Error results carry `trace_id`; the server logs the
  same id for every call.
- **Missing user input.** A result may carry `input_request`; ask the
  user for that argument and call again with it.
- **Resources.** `fluent://session/status`,
  `fluent://solver/status` and `fluent://case/named-objects` are
  attachable context. Workflow prompts belong to the host.

Turn the host contract off with `FLUIDS_MCP_HOST_CONTRACT=0` if your
host implements its own.
