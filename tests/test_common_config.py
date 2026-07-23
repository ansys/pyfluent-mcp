# Copyright (C) 2026 Synopsys, Inc. and ANSYS, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import pytest

from ansys.fluent.mcp.common.config import ConfigError, load_config, validate_config


def test_load_config_uses_defaults_for_empty_env():
    """Verify that load config uses defaults for empty env.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    config = load_config({})

    assert config.http_timeout == 300.0
    assert config.verify_tls is True
    assert config.llm_max_steps == 30
    assert config.llm_model == "gpt-4o-mini"
    assert config.llm_auth_style == "bearer"
    assert config.log_level == "INFO"
    assert config.warnings == ()


def test_load_config_reads_known_environment_values():
    """Verify that load config reads known environment values.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    config = load_config(
        {
            "FLUIDS_MCP_HTTP_TIMEOUT": "12.5",
            "FLUIDS_MCP_VERIFY_TLS": "off",
            "FLUIDS_MCP_LLM_MAX_STEPS": "4",
            "FLUIDS_MCP_LOG_LEVEL": "debug",
            "FLUIDS_MCP_API_RETRIEVER_URL": "https://retriever.example",
            "FLUIDS_MCP_QDRANT_API_KEY": "key",
            "LLM_ENDPOINT": "https://llm.example/v1",
            "LLM_MODEL": "gpt-4o other-model",
            "LLM_AUTH_STYLE": "azure-api-key",
        }
    )

    assert config.http_timeout == 12.5
    assert config.verify_tls is False
    assert config.llm_max_steps == 4
    assert config.log_level == "DEBUG"
    assert config.api_retriever_url == "https://retriever.example"
    assert config.qdrant_api_key == "key"
    assert config.llm_endpoint == "https://llm.example/v1"
    assert config.llm_model == "gpt-4o"
    assert config.llm_auth_style == "azure-api-key"
    assert any("VERIFY_TLS is disabled" in warning for warning in config.warnings)


def test_load_config_warns_for_unknown_fluids_mcp_variable():
    """Verify that load config warns for unknown fluids mcp variable.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    config = load_config({"FLUIDS_MCP_QRDANT_URL": "typo"})

    assert len(config.warnings) == 1
    assert "FLUIDS_MCP_QRDANT_URL" in config.warnings[0]


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"FLUIDS_MCP_VERIFY_TLS": "maybe"}, "valid boolean"),
        ({"FLUIDS_MCP_HTTP_TIMEOUT": "0"}, "must be >"),
        ({"FLUIDS_MCP_LLM_MAX_STEPS": "0"}, "must be >="),
        ({"FLUIDS_MCP_LOG_LEVEL": "trace"}, "must be one of"),
        ({"LLM_AUTH_STYLE": "basic"}, "LLM_AUTH_STYLE"),
    ],
)
def test_load_config_rejects_invalid_values(env, message):
    """Verify that load config rejects invalid values.

    Parameters
    ----------
    env : Any
        Environment mapping to read instead of the process environment.
    message : Any
        Message text to format, log, or return.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    with pytest.raises(ConfigError, match=message):
        load_config(env)


def test_validate_config_returns_loaded_config():
    """Verify that validate config returns loaded config.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    assert validate_config({"FLUIDS_MCP_LLM_MAX_STEPS": "2"}).llm_max_steps == 2
