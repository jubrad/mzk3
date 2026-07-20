"""The subprocess boundary. Everything side-effecting goes through Runner."""

from mzk3.runner import Runner


def test_dry_run_records_but_does_not_execute():
    r = Runner(dry_run=True)
    res = r.run(["kubectl", "apply", "-f", "nope.yaml"])
    assert r.calls == [["kubectl", "apply", "-f", "nope.yaml"]]
    assert res.returncode == 0  # dry run always "succeeds"


def test_real_capture_returns_stdout():
    r = Runner()
    res = r.run(["printf", "hello"], capture=True)
    assert res.stdout == "hello"
    assert res.returncode == 0
    assert r.calls == [["printf", "hello"]]


def test_check_false_swallows_nonzero():
    r = Runner()
    res = r.run(["false"], check=False)
    assert res.returncode != 0  # did not raise


def test_which_detects_missing_binary():
    r = Runner()
    assert r.which("definitely-not-a-real-binary-xyz") is None
    assert r.which("sh") is not None
