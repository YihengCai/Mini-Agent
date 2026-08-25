"""Offline regression tests for exclusive Turn log allocation."""

from datetime import datetime

import mini_agent.logger as logger_module
from mini_agent.logger import AgentLogger


def test_same_timestamp_allocates_distinct_logs_without_overwrite(
    monkeypatch,
    tmp_path,
) -> None:
    fixed_time = datetime(2026, 8, 25, 12, 0, 0)

    class FixedClock:
        @classmethod
        def now(cls):
            return fixed_time

    monkeypatch.setattr(logger_module, "datetime", FixedClock)
    first = AgentLogger(log_dir=tmp_path)
    second = AgentLogger(log_dir=tmp_path)

    first.start_new_run()
    first.log_response("first-turn-fact")
    first_path = first.get_log_file_path()
    second.start_new_run()
    second_path = second.get_log_file_path()

    assert first_path == tmp_path / "agent_run_20260825_120000.log"
    assert second_path == tmp_path / "agent_run_20260825_120000_1.log"
    assert len(list(tmp_path.glob("*.log"))) == 2
    assert "first-turn-fact" in first_path.read_text(encoding="utf-8")
    assert "first-turn-fact" not in second_path.read_text(encoding="utf-8")


def test_log_filename_and_header_share_one_clock_sample(
    monkeypatch,
    tmp_path,
) -> None:
    moments = iter(
        [
            datetime(2026, 8, 25, 12, 0, 0),
            datetime(2026, 8, 25, 12, 0, 1),
        ]
    )
    calls = 0

    class AdvancingClock:
        @classmethod
        def now(cls):
            nonlocal calls
            calls += 1
            return next(moments)

    monkeypatch.setattr(logger_module, "datetime", AdvancingClock)
    logger = AgentLogger(log_dir=tmp_path / "nested")

    logger.start_new_run()

    log_path = logger.get_log_file_path()
    assert log_path.name == "agent_run_20260825_120000.log"
    assert "Agent Run Log - 2026-08-25 12:00:00" in log_path.read_text(
        encoding="utf-8"
    )
    assert calls == 1
