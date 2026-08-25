"""Offline regression tests for retry control flow and numeric boundaries."""

import pytest

from mini_agent.retry import RetryConfig, RetryExhaustedError, async_retry


def test_runtime_retry_config_rejects_negative_max_retries() -> None:
    with pytest.raises(ValueError, match="max_retries"):
        RetryConfig(max_retries=-1)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("initial_delay", -1),
        ("max_delay", -1),
        ("exponential_base", 0),
        ("exponential_base", -1),
        ("initial_delay", float("nan")),
        ("initial_delay", float("inf")),
        ("max_delay", float("nan")),
        ("max_delay", float("inf")),
        ("exponential_base", float("nan")),
        ("exponential_base", float("inf")),
    ],
)
def test_runtime_retry_config_rejects_invalid_backoff_values(
    field,
    value,
) -> None:
    with pytest.raises(ValueError, match=field):
        RetryConfig(**{field: value})


def test_runtime_retry_config_accepts_finite_backoff_boundaries() -> None:
    config = RetryConfig(
        initial_delay=0,
        max_delay=0,
        exponential_base=0.5,
    )

    assert config.initial_delay == 0
    assert config.max_delay == 0
    assert config.exponential_base == 0.5


@pytest.mark.parametrize(
    ("config", "attempt", "expected_delay"),
    [
        (
            RetryConfig(
                initial_delay=0,
                max_delay=60,
                exponential_base=1e308,
            ),
            2,
            0,
        ),
        (
            RetryConfig(
                initial_delay=1,
                max_delay=60,
                exponential_base=1e308,
            ),
            2,
            60,
        ),
        (
            RetryConfig(
                initial_delay=8,
                max_delay=10,
                exponential_base=0.5,
            ),
            1,
            4,
        ),
        (
            RetryConfig(
                initial_delay=8,
                max_delay=3,
                exponential_base=0.5,
            ),
            0,
            3,
        ),
    ],
)
def test_delay_calculation_is_finite_and_bounded(
    config,
    attempt,
    expected_delay,
) -> None:
    assert config.calculate_delay(attempt) == expected_delay


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
