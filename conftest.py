"""Repository-wide pytest collection policy."""

from __future__ import annotations

import pytest


EXTERNAL_MARKER = "external"


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("mini-agent")
    group.addoption(
        "--run-external",
        action="store_true",
        default=False,
        help="allow tests that access configured model/MCP services or the network",
    )


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    if config.getoption("--run-external"):
        return

    external_items = []
    offline_items = []
    for item in items:
        target = (
            external_items
            if item.get_closest_marker(EXTERNAL_MARKER) is not None
            else offline_items
        )
        target.append(item)
    if not external_items:
        return

    items[:] = offline_items
    config.hook.pytest_deselected(items=external_items)
