Configuration
=============

PyFluent-MCP is configured through ``FLUIDS_MCP_*`` and ``LLM_*`` environment variables.
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
   * - ``FLUIDS_MCP_LLM_MAX_STEPS``
     - Cap on LLM codegen tool-loop iterations (default ``30``)
   * - ``FLUIDS_MCP_API_RETRIEVER_URL`` / ``FLUIDS_MCP_QDRANT_URL``
     - Opt-in semantic API retrievers (offline by default)
   * - ``FLUIDS_MCP_INTENT_GUARD``
     - ``0`` turns off the ``run_code`` crash-signature guard (default on)

LLM configuration
-----------------

Some features (``codegen``, ``clarify``) use an LLM. PyFluent-MCP is model- and
provider-agnostic: native vendor APIs (OpenAI, Azure, Anthropic, Gemini) via the LiteLLM
SDK, or any OpenAI-compatible endpoint.

Quick start
~~~~~~~~~~~

**OpenAI**

.. code-block:: bash

   export OPENAI_API_KEY="<your-openai-key>"
   export LLM_PROVIDER="openai"
   export LLM_MODEL="gpt-4o"

**Anthropic (Claude)**

.. code-block:: bash

   export ANTHROPIC_API_KEY="<your-anthropic-key>"
   export LLM_PROVIDER="anthropic"
   export LLM_MODEL="claude-3-5-sonnet"

**Google Gemini**

.. code-block:: bash

   export GEMINI_API_KEY="<your-gemini-key>"
   export LLM_PROVIDER="gemini"
   export LLM_MODEL="gemini-1.5-pro"

**Custom OpenAI-compatible endpoint**

.. code-block:: bash

   export LLM_ENDPOINT="<the-url-you-were-given>"
   export LLM_API_KEY="<your-key>"
   export LLM_MODEL="gpt-4o"

Install the native provider connector once:

.. code-block:: bash

   pip install "ansys-fluent-mcp[providers]"

Full model reference
~~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 30 15 55

   * - Variable
     - Default
     - Purpose
   * - ``LLM_PROVIDER``
     - auto
     - ``openai`` / ``azure`` / ``anthropic`` / ``gemini`` / ``compat``
   * - ``LLM_TRANSPORT``
     - auto
     - ``litellm`` (native APIs) or ``openai_compat`` (httpx to ``LLM_ENDPOINT``)
   * - ``LLM_ENDPOINT``
     - unset
     - OpenAI-compatible chat-completions URL
   * - ``LLM_API_KEY``
     - unset
     - Bearer token for the compat path
   * - ``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY`` / ``GEMINI_API_KEY`` / ``AZURE_API_KEY``
     - unset
     - Per-vendor key for the native (LiteLLM) path
   * - ``LLM_MODEL``
     - ``gpt-4o-mini``
     - Model name
   * - ``LLM_CACHE_MECHANISM``
     - auto
     - ``openai_auto`` / ``anthropic_cache_control`` / ``gemini_context`` / ``none``
   * - ``LLM_MAX_RETRIES`` / ``LLM_TIMEOUT_SECONDS``
     - ``3`` / ``60``
     - Transport seam retry/timeout

Transport security and network egress
-------------------------------------

TLS certificate verification is **on by default** for every outbound LLM and retrieval
call.

.. list-table::
   :header-rows: 1
   :widths: 35 65

   * - Variable
     - Purpose
   * - ``LLM_CA_BUNDLE``
     - Path to a PEM CA bundle (also reads ``SSL_CERT_FILE`` / ``REQUESTS_CA_BUNDLE``)
   * - ``LLM_TLS_INSECURE``
     - ``1`` turns off TLS verification (dev only; logs a warning)
   * - ``FLUIDS_AGENT_OFFLINE``
     - ``1`` forbids ALL outbound LLM and network-retrieval calls
   * - ``FLUIDS_AGENT_ALLOWED_LLM_HOSTS``
     - Comma-separated host allowlist enforced before any outbound call

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
