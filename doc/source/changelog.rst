.. _ref_release_notes:

Release notes
#############

This section contains the release notes for PyFluent-MCP.

.. vale off

.. towncrier release notes start

`0.1.0 <https://github.com/ansys/pyfluent-mcp/releases/tag/v0.1.0>`_ - Initial release
========================================================================================

.. tab-set::

  .. tab-item:: Added

    .. list-table::
        :header-rows: 0
        :widths: auto

        * - Initial release of the standalone Fluent Solve MCP server plus the shared
            ``ansys.fluent.mcp.common`` infrastructure.
          -

        * - 22-tool MCP surface: connection, schema discovery, named objects, codegen,
            execution, reporting, and domain tools.
          -

        * - PyFluent backend with pluggable backend entry-point seam.
          -

        * - Offline-first settings schema catalog and LLM codegen pipeline.
          -

        * - Model- and provider-agnostic LLM transport via ``llm_wire``.
          -

  .. tab-item:: Security

    .. list-table::
        :header-rows: 0
        :widths: auto

        * - TLS certificate verification enabled by default for outbound LLM and
            retrieval calls.
          -

        * - Network retrievers honor ``FLUIDS_AGENT_OFFLINE`` and host allowlists.
          -
