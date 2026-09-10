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


@pytest.fixture
def license_file(tmp_path):
    """A (dummy) license key file: install/recreate-environment refuse to run
    without one. Never validated by mzk3 itself, only passed through."""
    f = tmp_path / "license.key"
    f.write_text("dummy-license-key\n")
    return str(f)
