"""Regression tests for the repository's default pytest safety boundary."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXTERNAL_TESTS = {
    "tests/test_mcp.py::test_mcp_tools_loading",
    "tests/test_mcp.py::test_git_mcp_tool_availability",
    "tests/test_mcp.py::test_mcp_tool_execution",
    "tests/test_mcp.py::test_connection_timeout_on_unreachable_server",
    "tests/test_mcp.py::test_per_server_timeout_override_in_config",
}
OFFLINE_MCP_TEST = (
    "tests/test_mcp.py::TestDetermineConnectionType::test_stdio_with_command_only"
)
OFFLINE_ASYNC_MCP_TEST = "tests/test_mcp.py::test_url_config_validation"
COLLECTION_TARGETS = ("tests/test_mcp.py",)


def _run_collection(
    tmp_path: Path,
    *args: str,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("PYTEST_ADDOPTS", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "--color=no",
            "-c",
            str(PROJECT_ROOT / "pyproject.toml"),
            "-o",
            f"cache_dir={tmp_path / 'pytest-cache'}",
            *args,
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _collect_test_ids(tmp_path: Path, *extra_args: str) -> set[str]:
    result = _run_collection(tmp_path, *extra_args, *COLLECTION_TARGETS)
    assert result.returncode == 0, result.stdout + result.stderr
    return {
        line.strip()
        for line in result.stdout.splitlines()
        if line.startswith("tests/") and "::" in line
    }


def test_external_tests_require_explicit_collection_opt_in(tmp_path: Path) -> None:
    default_tests = _collect_test_ids(tmp_path)
    assert EXTERNAL_TESTS.isdisjoint(default_tests)
    assert OFFLINE_MCP_TEST in default_tests

    no_conftest_tests = _collect_test_ids(tmp_path, "--noconftest")
    assert EXTERNAL_TESTS.isdisjoint(no_conftest_tests)
    assert OFFLINE_MCP_TEST in no_conftest_tests

    asyncio_tests = _collect_test_ids(tmp_path, "-m", "asyncio")
    assert EXTERNAL_TESTS.isdisjoint(asyncio_tests)
    assert OFFLINE_ASYNC_MCP_TEST in asyncio_tests

    external_tests = _collect_test_ids(
        tmp_path,
        "--run-external",
        "-m",
        "external",
    )
    assert external_tests == EXTERNAL_TESTS


def test_unknown_marker_fails_during_collection(tmp_path: Path) -> None:
    misspelled_marker_test = tmp_path / "test_misspelled_marker.py"
    misspelled_marker_test.write_text(
        "import pytest\n\n"
        "@pytest.mark.extrenal\n"
        "def test_never_runs():\n"
        "    raise AssertionError('collection must fail before this test runs')\n",
        encoding="utf-8",
    )

    result = _run_collection(
        tmp_path,
        "--noconftest",
        str(misspelled_marker_test),
    )
    assert result.returncode != 0
    assert "extrenal" in result.stdout + result.stderr
