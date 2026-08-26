"""Offline tests for configuration companion-file provenance."""

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import mini_agent.cli as cli
import mini_agent.config as config_module


DEFAULT_SYSTEM_PROMPT = (
    "You are Mini-Agent, a coding assistant that can use configured tools "
    "to complete tasks."
)


def make_runtime_config(*, prompt_path: str, mcp_path: str):
    return SimpleNamespace(
        llm=SimpleNamespace(model="offline"),
        agent=SimpleNamespace(
            max_steps=1,
            system_prompt_path=prompt_path,
        ),
        tools=SimpleNamespace(
            enable_bash=False,
            enable_skills=False,
            enable_mcp=True,
            enable_file_tools=False,
            mcp_config_path=mcp_path,
            mcp=SimpleNamespace(
                connect_timeout=10.0,
                execute_timeout=60.0,
                sse_read_timeout=120.0,
            ),
        ),
    )


def test_relative_companion_path_is_bound_to_selected_config_parent(tmp_path) -> None:
    config_path = tmp_path / "selected" / "config.yaml"

    companion = config_module.resolve_config_companion(
        config_path,
        "nested/system_prompt.md",
    )

    assert companion == config_path.parent / "nested" / "system_prompt.md"


def test_absolute_companion_path_is_preserved(tmp_path) -> None:
    absolute_path = tmp_path / "explicit" / "system_prompt.md"

    companion = config_module.resolve_config_companion(
        tmp_path / "selected" / "config.yaml",
        absolute_path,
    )

    assert companion == absolute_path


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "expected_prompt", "expected_mcp_source"),
    [
        ("relative-present", "selected prompt", "selected"),
        ("relative-missing", DEFAULT_SYSTEM_PROMPT, None),
        ("absolute", "absolute prompt", "absolute"),
    ],
)
async def test_runtime_uses_only_companions_from_selected_config_source(
    monkeypatch,
    tmp_path,
    case,
    expected_prompt,
    expected_mcp_source,
) -> None:
    selected_dir = tmp_path / "selected"
    shadow_dir = tmp_path / "shadow"
    absolute_dir = tmp_path / "absolute"
    selected_dir.mkdir()
    shadow_dir.mkdir()
    absolute_dir.mkdir()
    config_path = selected_dir / "config.yaml"
    config_path.write_text("selected main config", encoding="utf-8")

    (shadow_dir / "system_prompt.md").write_text(
        "shadow prompt",
        encoding="utf-8",
    )
    (shadow_dir / "mcp.json").write_text("shadow mcp", encoding="utf-8")

    prompt_path = "system_prompt.md"
    mcp_path = "mcp.json"
    expected_mcp_path = None
    if case == "relative-present":
        (selected_dir / prompt_path).write_text(expected_prompt, encoding="utf-8")
        (selected_dir / mcp_path).write_text("selected mcp", encoding="utf-8")
        expected_mcp_path = selected_dir / mcp_path
    elif case == "absolute":
        absolute_prompt_path = absolute_dir / "explicit-prompt.md"
        absolute_mcp_path = absolute_dir / "explicit-mcp.json"
        absolute_prompt_path.write_text(expected_prompt, encoding="utf-8")
        absolute_mcp_path.write_text("absolute mcp", encoding="utf-8")
        prompt_path = str(absolute_prompt_path)
        mcp_path = str(absolute_mcp_path)
        expected_mcp_path = absolute_mcp_path

    captured_prompts = []
    loaded_mcp_paths = []

    class RecordingAgentSession:
        def __init__(self, *, system_prompt, **_kwargs) -> None:
            captured_prompts.append(system_prompt)

        def start_turn(self, _task, *, event_sink):
            return object()

    class RecordingMCPManager:
        async def load_tools(self, path: str):
            loaded_mcp_paths.append(Path(path))
            return []

    async def complete_turn(_turn):
        return None

    def shadow_search(_cls, filename):
        return shadow_dir / Path(filename).name

    monkeypatch.setattr(cli, "AgentSession", RecordingAgentSession)
    monkeypatch.setattr(cli, "wait_for_turn", complete_turn)
    monkeypatch.setattr(cli, "print_stats", lambda *_args: None)
    monkeypatch.setattr(
        cli.Config,
        "find_config_file",
        classmethod(shadow_search),
    )

    await cli._run_configured_runtime(
        tmp_path / "workspace",
        task="offline",
        config=make_runtime_config(
            prompt_path=prompt_path,
            mcp_path=mcp_path,
        ),
        config_path=config_path,
        llm_client=object(),
        session_start=datetime.now(),
        shell_manager=object(),
        mcp_manager=RecordingMCPManager(),
    )

    assert captured_prompts == [expected_prompt]
    assert loaded_mcp_paths == (
        [] if expected_mcp_source is None else [expected_mcp_path]
    )
