.. _ref_release_notes:

Release notes
#############

This section contains the release notes for PyFluent-MCP.

.. vale off

.. towncrier release notes start

`0.1.0 <https://github.com/ansys/pyfluent-mcp/releases/tag/v0.1.0>`_ - July 10, 2026
====================================================================================

.. tab-set::


  .. tab-item:: Added

    .. list-table::
        :header-rows: 0
        :widths: auto

        * - Guard _import_litellm against the tokenizers
          - `#9 <https://github.com/ansys/pyfluent-mcp/pull/9>`_

        * - Raise unit-test coverage above 80%, expand the live-Fluent integration tests, and remove bandit skips as part of the technical review.
          - `#13 <https://github.com/ansys/pyfluent-mcp/pull/13>`_

        * - Add numpy-style docstrings across the public API so the autoapi reference documentation renders complete signatures and parameter descriptions.
          - `#24 <https://github.com/ansys/pyfluent-mcp/pull/24>`_

        * - Add Anthropic message normalization for LiteLLM transport
          - `#26 <https://github.com/ansys/pyfluent-mcp/pull/26>`_

        * - Probe APIs for Settings APIs
          - `#28 <https://github.com/ansys/pyfluent-mcp/pull/28>`_

        * - Deterministic path-discovery + write-target resolver for the Solve leaf
          - `#33 <https://github.com/ansys/pyfluent-mcp/pull/33>`_


  .. tab-item:: Fixed

    .. list-table::
        :header-rows: 0
        :widths: auto

        * - Follow-up fixes prior to public release
          - `#6 <https://github.com/ansys/pyfluent-mcp/pull/6>`_

        * - Links
          - `#7 <https://github.com/ansys/pyfluent-mcp/pull/7>`_

        * - GitHub runner label change
          - `#11 <https://github.com/ansys/pyfluent-mcp/pull/11>`_

        * - Fix Vale documentation style violations so the Ansys documentation style check passes.
          - `#25 <https://github.com/ansys/pyfluent-mcp/pull/25>`_

        * - License in \`\`SECURITY.md\`\`
          - `#32 <https://github.com/ansys/pyfluent-mcp/pull/32>`_


  .. tab-item:: Documentation

    .. list-table::
        :header-rows: 0
        :widths: auto

        * - Editorial review of all RST files and docstrings in PY files.
          - `#27 <https://github.com/ansys/pyfluent-mcp/pull/27>`_

        * - Edit to use active voice and shorter, clearer sentences
          - `#31 <https://github.com/ansys/pyfluent-mcp/pull/31>`_


  .. tab-item:: Dependencies

    .. list-table::
        :header-rows: 0
        :widths: auto

        * - Bump the pip-deps group with 6 updates
          - `#1 <https://github.com/ansys/pyfluent-mcp/pull/1>`_

        * - Bump actions/checkout from 6.0.3 to 7.0.0 in the actions group across 1 directory
          - `#5 <https://github.com/ansys/pyfluent-mcp/pull/5>`_


  .. tab-item:: Maintenance

    .. list-table::
        :header-rows: 0
        :widths: auto

        * - Update and align Ansys action version to v10.3.3
          - `#4 <https://github.com/ansys/pyfluent-mcp/pull/4>`_


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
