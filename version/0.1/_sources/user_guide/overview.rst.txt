Overview
========

What is PyFluent-MCP?
---------------------

Use PyFluent-MCP (``ansys.fluent.mcp``) as a bridge between AI assistants and Ansys
Fluent. It uses the Model Context Protocol (MCP) to expose PyFluent capabilities as
standardized tools that AI systems can call.

What is MCP?
~~~~~~~~~~~~

MCP is a standardized interface for connecting AI systems to external tools and data
sources. It allows AI assistants to perform the following tasks:

- Discover available tools and their capabilities.
- Call tools with structured parameters.
- Receive results and error information.
- Maintain state across multiple interactions.

How does MCP work?
~~~~~~~~~~~~~~~~~~

- **Client connection**: An MCP-compatible client (such as Claude or Copilot) connects
  to the PyFluent-MCP server.
- **Tool discovery**: The client discovers available tools for controlling Fluent.
- **Tool execution**: The client calls tools with appropriate parameters.
- **Result return**: The server returns results or errors to the client.
- **Interaction loop**: The cycle continues for the duration of the session.

Understand the architecture
---------------------------

PyFluent-MCP includes several key components under the ``ansys.fluent.mcp`` namespace:

- **MCP server** (:class:`~ansys.fluent.mcp.solve.SolveMCP`): Implements the MCP
  protocol and handles client connections.
- **Tool surface**: 22 stateless tools for connection, discovery, code generation, execution,
  and reporting.
- **PyFluent backend**: In-process gRPC to a local or remote Fluent solver.
- **Settings catalog**: Offline schema (~62k paths) plus optional semantic retriever.
- **Codegen pipeline**: Natural language → PyFluent Python via an LLM.
- **AST sandbox**: Validates Python before it reaches the solver.

Design tenets
-------------

- **Stateless per call.** Each tool call is self-contained except for the live Fluent
  connection and the ``run_code`` REPL namespace.
- **The LLM never writes Fluent directly.** Python only runs through ``run_code`` /
  ``validate_code``, which pass through an AST sandbox.
- **Offline-first knowledge.** ``find_api``, ``get_help``, and ``codegen`` work from
  the bundled settings schema without any network service.
- **Pluggable backends.** PyFluent ships in-box. Other backends can be contributed via
  entry points.

Explore use cases
-----------------

**Solver setup**
    Use AI to configure boundary conditions, materials, and numerics interactively.

**Interactive analysis**
    Ask an AI assistant to inspect residuals, mesh quality, or setup summaries.

**Script generation**
    Generate PyFluent settings-API code from natural language and execute it safely.

**Case comparison**
    Diff two case files to see what changed between versions.

**Learning tool**
    Use an AI assistant as a tutor for learning Fluent's settings API and PyFluent.

Next steps
----------

- Learn about available :doc:`tools_and_capabilities`.
- Review :doc:`configuration` for environment variables and LLM setup.
- Review :doc:`best_practices` for effective use.
- Explore the :doc:`../user_guide/tools_and_capabilities` for technical details.
