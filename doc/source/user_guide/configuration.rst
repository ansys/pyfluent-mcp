Configuration
=============

PyFluent-MCP is configured primarily through ``FLUIDS_MCP_*`` environment variables.
You can set these in your shell, MCP client configuration, or a ``.env`` file loaded by
your client.

General settings
----------------

.. list-table::
   :header-rows: 1
   :widths: 35 65

   * - Variable
     - Effect
   * - ``FLUIDS_MCP_SETTINGS_JSON``
     - External JSON file for overriding the bundled settings schema
   * - ``FLUIDS_MCP_LOG_LEVEL``
     - Server log level (default ``INFO``)
   * - ``FLUIDS_MCP_DISABLE_SESSION_LOGS``
     - ``1`` turns off session logs
   * - ``FLUIDS_MCP_MAX_STEPS``
     - Cap on deterministic tool-loop iterations (default ``30``)
   * - ``FLUIDS_MCP_INTENT_GUARD``
     - ``0`` turns off the ``run_code`` crash-signature guard (default on)
   * - ``FLUIDS_MCP_HOST_CONTRACT``
     - ``0`` turns off the host contract: trace ids, timeout-safe long
       operations, and error guidance (default on)

Execution and retrieval behavior
--------------------------------

PyFluent-MCP focuses on deterministic Fluent tooling and MCP transport
exposure.

The repository provides:

- validated Fluent execution
- schema retrieval and grounding
- backend abstraction
- deterministic tool execution
- MCP transport support

Host contract
-------------

Every tool declares MCP behavior hints (``readOnlyHint``, ``destructiveHint``,
``idempotentHint``) so clients such as VS Code can run read-only tools without
confirmation. Every tool call also gets:

- **Trace ids:** the server logs one line per call with a short trace id, its
  duration, and its outcome. Error results carry the same id as ``trace_id``.
- **Timeout-safe long operations:** when a client times out and retries a
  long-running mutating call, the original keeps running and the identical
  retry joins it. A finished result of an idempotent call such as ``connect``
  is returned once to that retry.
- **Error guidance:** every error result carries ``retryable`` and, when known,
  ``next_action``. A result with ``input_request`` tells the client to ask the
  user for the named argument and call again.

The solve leaf also publishes the ``fluent://session/status``,
``fluent://solver/status`` and ``fluent://case/named-objects`` resources and the
``fluent_setup_change`` and ``fluent_diagnose_divergence`` prompts.

Transport security
------------------

TLS certificate verification is enabled by default for outbound retrieval and
HTTP operations.

.. list-table::
   :header-rows: 1
   :widths: 35 65

   * - Variable
     - Purpose
   * - ``FLUIDS_MCP_CA_BUNDLE``
     - Path to a PEM CA bundle (also reads ``SSL_CERT_FILE`` / ``REQUESTS_CA_BUNDLE``)
   * - ``FLUIDS_MCP_VERIFY_TLS``
     - Turn TLS verification on or off (default on)
   * - ``FLUIDS_MCP_HTTP_TIMEOUT``
     - Timeout for outbound HTTP operations

Server command-line tool options
--------------------------------

The ``ansys-fluent-mcp`` console script accepts:

You can also launch the server as a module:

.. code-block:: bash

   python -m ansys.fluent.mcp

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Flag
     - Description
   * - ``--transport stdio|http``
     - MCP transport (default ``stdio``)
   * - ``--host`` / ``--port``
     - HTTP bind address (when ``--transport http``)
   * - ``--backend KIND``
     - Default backend kind until ``connect`` is called (ships ``pyfluent``)
   * - ``--log-level``
     - Log level (default ``INFO``)

Next steps
----------

- For passing environment variables through MCP clients, see :doc:`../getting_started/ide_configuration`.
