"""Help output should be genuinely useful, and `help` should be a command."""

import pytest

from mzk3.config import COMMANDS, build_parser, resolve


def help_text():
    return build_parser().format_help()


def test_help_is_a_command():
    assert "help" in COMMANDS
    cmd, _ = resolve(["help"])
    assert cmd == "help"


def test_no_command_resolves_to_help_not_error():
    # bash printed usage when no command was given; don't hard-error
    cmd, _ = resolve([])
    assert cmd == "help"


def test_help_lists_every_command_with_a_description():
    text = help_text()
    for name in COMMANDS:
        assert name in text
    # each real command has a one-line explanation
    assert "first-time setup" in text          # install
    assert "Upgrade an existing" in text        # upgrade
    assert "destroys all data" in text.lower()  # reset


def test_help_documents_flags_with_descriptions():
    text = help_text()
    assert "--license-key" in text
    assert "license key" in text.lower()
    assert "--create-cluster" in text
    assert "--install-dashboards" in text
    assert "Skip confirmation" in text


def test_help_documents_environment_variables():
    text = help_text()
    for var in ("MZ_VERSION", "MZ_LICENSE_KEY", "K3D_CLUSTER_NAME", "MZ_NAMESPACE"):
        assert var in text


def test_help_shows_examples_and_default_version():
    text = help_text()
    assert "Examples:" in text
    assert "mzk3 install --create-cluster" in text
    from mzk3.config import DEFAULT_VERSION
    assert DEFAULT_VERSION in text  # default surfaced in help


def test_help_explains_cluster_safety():
    text = help_text().lower()
    assert "only installs on" in text or "clusters it created" in text
    assert "--force" in text


def test_dash_h_exits_zero(capsys):
    with pytest.raises(SystemExit) as e:
        build_parser().parse_args(["-h"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "Examples:" in out
