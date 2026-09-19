# Copyright (C) 2026 Synopsys, Inc. and ANSYS, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
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

"""Classifier + reroute coverage for the extended run_calculation family.

The default ``classify_path`` originally recognised only the two
top-level command paths under ``solution.run_calculation``
(``iterate`` / ``dual_time_iterate``). It now also recognises every
mode-gated leaf under ``.parameters.*``, ``.transient_controls.*``,
and ``.pseudo_time_settings.*`` so the agent's executor can reason
about whether a proposed write matches the live time regime.
"""

from __future__ import annotations

import pytest

from ansys.fluent.mcp.solve.lib.active_path import (
    PathGroup,
    SolverMode,
    classify_path,
    reroute,
)


def _steady_mode() -> SolverMode:
    return SolverMode(transient=False)


def _transient_mode() -> SolverMode:
    return SolverMode(transient=True)


# --- classify_path ----------------------------------------------------


@pytest.mark.parametrize(
    "path, expected_family",
    [
        ("solution.run_calculation.iterate", "iterate"),
        ("solution.run_calculation.iterate.number_of_iterations", "iterate"),
        ("solution.run_calculation.parameters.iter_count", "iterate"),
        ("solution.run_calculation.parameters.iterations", "iterate"),
    ],
)
def test_classify_steady_run_calculation_leaves(path: str, expected_family: str) -> None:
    info = classify_path(path)
    assert info is not None
    assert info.group is PathGroup.RUN
    assert info.family == expected_family


@pytest.mark.parametrize(
    "path",
    [
        "solution.run_calculation.dual_time_iterate",
        "solution.run_calculation.dual_time_iterate.number_of_time_steps",
        "solution.run_calculation.parameters.time_step_count",
        "solution.run_calculation.parameters.time_step_size",
        "solution.run_calculation.parameters.max_iter_per_time_step",
        "solution.run_calculation.parameters.number_of_time_steps",
        "solution.run_calculation.parameters.adaptive_time_stepping",
        "solution.run_calculation.parameters.extrapolate_vars",
        "solution.run_calculation.transient_controls",
        "solution.run_calculation.transient_controls.type",
        "solution.run_calculation.pseudo_time_settings",
        "solution.run_calculation.pseudo_time_settings.time_step_method",
    ],
)
def test_classify_transient_run_calculation_leaves(path: str) -> None:
    info = classify_path(path)
    assert info is not None
    assert info.group is PathGroup.RUN
    assert info.family == "dual_time_iterate"


def test_classify_ignores_unknown_run_calculation_leaf() -> None:
    """Unrecognised leaves must return ``None`` (never false-block)."""
    assert classify_path("solution.run_calculation.some_new_field_2028") is None


# --- reroute ----------------------------------------------------------


def test_reroute_steady_leaf_under_transient_returns_no_sibling() -> None:
    """A ``parameters.iter_count`` write under a transient session has
    NO semantically-equivalent sibling — the classifier surfaces the
    mismatch as ``active=False`` with ``correct_path=None`` so the
    agent's executor treats it as inactive_skipped and steers the user
    to flip ``setup.general.solver.time`` first."""
    r = reroute(
        "solution.run_calculation.parameters.iter_count",
        _transient_mode(),
    )
    assert r.active is False
    assert r.correct_path is None
    assert r.active_family == "dual_time_iterate"
    assert "solver.time" in (r.reason or "")


def test_reroute_transient_leaf_under_steady_returns_no_sibling() -> None:
    r = reroute(
        "solution.run_calculation.parameters.time_step_count",
        _steady_mode(),
    )
    assert r.active is False
    assert r.correct_path is None
    assert r.active_family == "iterate"


def test_reroute_command_path_still_switches_to_sibling_command() -> None:
    """Top-level command paths retain their original behaviour:
    ``iterate`` under transient reroutes to ``dual_time_iterate`` and
    vice-versa."""
    r = reroute(
        "solution.run_calculation.iterate",
        _transient_mode(),
    )
    assert r.active is False
    assert r.correct_path == "solution.run_calculation.dual_time_iterate"

    r2 = reroute(
        "solution.run_calculation.dual_time_iterate",
        _steady_mode(),
    )
    assert r2.active is False
    assert r2.correct_path == "solution.run_calculation.iterate"


def test_reroute_matching_mode_leaves_path_alone() -> None:
    """Steady leaf under steady mode is active."""
    r = reroute(
        "solution.run_calculation.parameters.iter_count",
        _steady_mode(),
    )
    assert r.active is True

    r2 = reroute(
        "solution.run_calculation.parameters.time_step_count",
        _transient_mode(),
    )
    assert r2.active is True
