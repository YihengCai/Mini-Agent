"""Offline structural checks for the core harness boundary."""

import ast
from pathlib import Path


PROJECT_ROOT = Path(__file__).parents[1]
CORE_FILES = (
    PROJECT_ROOT / "mini_agent" / "core" / "agent.py",
    PROJECT_ROOT / "mini_agent" / "core" / "events.py",
)


def test_core_does_not_import_ui_logging_or_transport_modules():
    forbidden_modules = {"acp", "cli", "cli_events", "logger", "utils"}
    forbidden_names = {"AgentLogger", "Colors", "calculate_display_width"}

    for path in CORE_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        imported_names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        print_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
        ]

        assert imported_modules.isdisjoint(forbidden_modules), path
        assert imported_names.isdisjoint(forbidden_names), path
        assert print_calls == [], path


def test_distribution_has_no_acp_surface():
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    lock_file = (PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8")

    assert not (PROJECT_ROOT / "mini_agent" / "acp").exists()
    assert not (PROJECT_ROOT / "tests" / "test_acp.py").exists()
    assert "agent-client-protocol" not in pyproject
    assert "mini-agent-acp" not in pyproject
    assert "agent-client-protocol" not in lock_file
