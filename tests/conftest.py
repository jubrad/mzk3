"""Integration tests are opt-in: they spin up a real k3d cluster.

By default they are skipped so `pytest` stays a fast, hermetic unit run.
Run them with:

    uv run pytest --run-integration
"""

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="run integration tests that spin up a real k3d cluster",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-integration"):
        return
    skip = pytest.mark.skip(reason="needs --run-integration (real k3d cluster)")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
