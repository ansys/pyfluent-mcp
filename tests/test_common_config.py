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
            "FLUIDS_MCP_LOG_LEVEL": "debug",
        }
    )

    assert config.http_timeout == 12.5
    assert config.verify_tls is False
    assert config.log_level == "DEBUG"
    assert any("VERIFY_TLS is disabled" in warning for warning in config.warnings)


def test_load_config_warns_for_unknown_configuration_variable():
    """Verify that load config warns for unknown configuration variable.

    Returns
    -------
    None
        The function completes through its side effects.
    """
    config = load_config({"FLUIDS_MCP_QRDANT_URL": "typo"})

    assert len(config.warnings) == 1
    assert "FLUIDS_MCP_QRDANT_URL" in config.warnings[0]


def test_registered_env_vars_do_not_warn(monkeypatch):
    """Verify that host-registered variables are accepted as known."""
    import ansys.fluent.mcp.common.config as config_module

    monkeypatch.setattr(config_module, "_REGISTERED_ENV_VARS", set())
    assert "FLUIDS_MCP_HOST_ONLY_KNOB" not in config_module.known_env_vars()

    config_module.register_known_env_vars("FLUIDS_MCP_HOST_ONLY_KNOB", "")
    config = load_config({"FLUIDS_MCP_HOST_ONLY_KNOB": "1", "FLUIDS_MCP_QRDANT_URL": "typo"})

    assert "FLUIDS_MCP_HOST_ONLY_KNOB" in config_module.known_env_vars()
    assert "" not in config_module.known_env_vars()
    assert len(config.warnings) == 1
    assert "FLUIDS_MCP_QRDANT_URL" in config.warnings[0]


def test_load_config_warns_for_agent_owned_max_steps_variables():
    """Verify that agent-owned step caps are not pyfluent-mcp config."""
    config = load_config(
        {
            "FLUIDS_MCP_LLM_MAX_STEPS": "4",
            "FLUIDS_MCP_MAX_STEPS": "4",
        }
    )

    assert len(config.warnings) == 2
    assert "FLUIDS_MCP_LLM_MAX_STEPS" in config.warnings[0]
    assert "FLUIDS_MCP_MAX_STEPS" in config.warnings[1]


@pytest.mark.parametrize(
    "name",
    [
        "FLUIDS_MCP_SETTINGS_JSON",
        "FLUIDS_MCP_BATCH_FAMILY_LIMIT",
        "FLUIDS_MCP_STRICT_VALIDATION",
        "FLUIDS_MCP_MAX_STATE_BYTES",
    ],
)
def test_variables_read_by_the_package_are_known(name):
    """Verify that variables the package honors never trigger the typo warning."""
    assert load_config({name: "1"}).warnings == ()


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"FLUIDS_MCP_VERIFY_TLS": "maybe"}, "valid boolean"),
        ({"FLUIDS_MCP_HTTP_TIMEOUT": "0"}, "must be >"),
        ({"FLUIDS_MCP_LOG_LEVEL": "trace"}, "must be one of"),
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
    assert validate_config({"FLUIDS_MCP_HTTP_TIMEOUT": "2"}).http_timeout == 2.0
