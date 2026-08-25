"""Offline regression tests for retry control flow and numeric boundaries."""

import pytest

from mini_agent.retry import RetryConfig, RetryExhaustedError, async_retry


def test_runtime_retry_config_rejects_negative_max_retries() -> None:
    with pytest.raises(ValueError, match="max_retries"):
        RetryConfig(max_retries=-1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("max_retries", "expected_attempts"),
    [(0, 1), (2, 3)],
)
async def test_max_retries_counts_attempts_after_the_initial_call(
    max_retries,
    expected_attempts,
) -> None:
    calls = 0
    failure = RuntimeError("model failed")

    @async_retry(
        RetryConfig(
            max_retries=max_retries,
            initial_delay=0,
        )
    )
    async def fail() -> None:
        nonlocal calls
        calls += 1
        raise failure

    with pytest.raises(RetryExhaustedError) as raised:
        await fail()

    assert calls == expected_attempts
    assert raised.value.attempts == expected_attempts
    assert raised.value.last_exception is failure
