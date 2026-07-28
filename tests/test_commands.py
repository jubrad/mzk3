"""Builders that assemble argv lists for helm / kubectl / k3d.

Pure: given a Config they return a list of strings, no subprocess.
"""

import json

from mzk3.config import resolve
from mzk3 import commands as c


def cfg(*argv, env=None):
    _, config = resolve(list(argv), env=env or {})
    return config


def test_k3d_create_cluster_carries_label():
    argv = c.k3d_create("mzk3-cluster")
    assert argv[:3] == ["k3d", "cluster", "create"]
    assert "mzk3-cluster" in argv
    assert "--runtime-label" in argv
    i = argv.index("--runtime-label")
    assert argv[i + 1] == "created-by=mzk3@server:*"


def test_operator_install_uses_versions_and_values():
    conf = cfg("install", "-v", "v27.0.0")
    argv = c.operator_install(conf)
    assert argv[:3] == ["helm", "upgrade", "--install"]
    assert conf.release_name in argv
    assert "materialize/materialize-operator" in argv
    assert "--version" in argv and argv[argv.index("--version") + 1] == "v27.0.0"
    assert "--namespace=materialize" in argv
    assert "-f" in argv and argv[argv.index("-f") + 1] == conf.values_file
    assert "--wait" in argv


def test_instance_image_patch_is_valid_merge_json():
    argv = c.patch_instance_image("mymz", "materialize-environment", "materialize/environmentd:v27.0.0")
    assert argv[:2] == ["kubectl", "patch"]
    assert "materialize" in argv and "mymz" in argv
    assert argv[argv.index("--type") + 1] == "merge"
    payload = json.loads(argv[argv.index("-p") + 1])
    assert payload == {"spec": {"environmentdImageRef": "materialize/environmentd:v27.0.0"}}


def test_rollout_patch_promotes_without_force():
    argv = c.patch_rollout("mymz", "materialize-environment", "UUID-1", force=False)
    payload = json.loads(argv[argv.index("-p") + 1])
    # forcePromote so the 0dt cutover completes; no forceRollout without --force
    assert payload == {"spec": {"requestRollout": "UUID-1", "forcePromote": "UUID-1"}}


def test_rollout_patch_with_force_sets_all():
    argv = c.patch_rollout("mymz", "materialize-environment", "UUID-1", force=True)
    payload = json.loads(argv[argv.index("-p") + 1])
    assert payload == {"spec": {"requestRollout": "UUID-1", "forcePromote": "UUID-1",
                                "forceRollout": "UUID-1"}}


def test_environmentd_image_ref_default_and_override():
    assert c.environmentd_image_ref(cfg("install", "-v", "v27.0.0")) == \
        "materialize/environmentd:v27.0.0"
    conf = cfg("install", "--environmentd-image", "jubrad/environmentd:v9-dev")
    assert c.environmentd_image_ref(conf) == "jubrad/environmentd:v9-dev"


def test_derive_operator_image_from_environmentd_ref():
    assert c.derive_operator_image("jubrad/environmentd:v26.35.0-dev.0--x") == \
        ("jubrad/orchestratord", "v26.35.0-dev.0--x")
    assert c.derive_operator_image("localhost:5000/me/environmentd:t") == \
        ("localhost:5000/me/orchestratord", "t")


def test_operator_install_custom_image_sets_operator_and_derives_repo():
    conf = cfg("install", "--environmentd-image", "jubrad/environmentd:tag9")
    argv = c.operator_install(conf)
    assert "operator.image.repository=jubrad/orchestratord" in argv
    assert "operator.image.tag=tag9" in argv


def test_operator_install_local_repo_uses_local_chart_no_version():
    conf = cfg("install", "--local-repo", "/home/me/materialize", "-v", "v27.0.0")
    argv = c.operator_install(conf)
    assert "/home/me/materialize/misc/helm-charts/operator" in argv
    assert "materialize/materialize-operator" not in argv
    assert "--version" not in argv  # local chart carries its own version


def test_operator_upgrade_targets_operator_version_not_mz_version():
    conf = cfg("upgrade", "-v", "v27.0.0", "-o", "v26.5.0")
    argv = c.operator_upgrade(conf)
    assert argv[:2] == ["helm", "upgrade"]
    assert "--install" not in argv  # upgrade, not install
    assert argv[argv.index("--version") + 1] == "v26.5.0"


def test_operator_upgrade_matches_install_observability_flags():
    # upgrade must not silently drop observability config that install set
    conf = cfg("upgrade", "-v", "v27.0.0")
    up = c.operator_upgrade(conf)
    inst = c.operator_install(conf)
    for flag in ("observability.enabled=true",
                 "observability.podMetrics.enabled=true",
                 "observability.prometheus.scrapeAnnotations.enabled=true",
                 "operator.cloudProvider.region=k3s"):
        assert flag in up, flag
        assert flag in inst, flag
    assert "--wait" in up


def test_download_url_for_operator_values_uses_operator_version():
    conf = cfg("install", "-v", "v27.0.0", "-o", "v26.5.0")
    url = c.operator_values_url(conf)
    assert "v26.5.0" in url
    assert url.endswith("misc/helm-charts/operator/values.yaml")


def test_download_url_for_materialize_uses_mz_version():
    conf = cfg("install", "-v", "v27.0.0", "-o", "v26.5.0")
    url = c.materialize_manifest_url(conf)
    assert "v27.0.0" in url
    assert url.endswith("misc/helm-charts/testing/materialize.yaml")


def test_monitoring_tarball_url_points_at_the_release_tag():
    url = c.monitoring_tarball_url("v0.6.0")
    assert url.startswith("https://codeload.github.com/MaterializeInc/materialize-monitoring/")
    assert url.endswith("refs/tags/materialize-monitoring/v0.6.0")


def test_monitoring_tarball_url_defaults_to_pinned_version():
    assert c.MONITORING_VERSION in c.monitoring_tarball_url()


def test_rustfs_operator_tarball_url_points_at_pinned_tag():
    url = c.rustfs_operator_tarball_url()
    assert url == ("https://codeload.github.com/rustfs/operator/tar.gz/refs/tags/"
                   + c.RUSTFS_OPERATOR_VERSION)


def test_rustfs_credentials_meet_operator_minimum_length():
    # operator rejects access/secret keys shorter than 8 chars
    assert len(c.RUSTFS_ACCESS_KEY) >= 8
    assert len(c.RUSTFS_SECRET_KEY) >= 8


def test_environmentd_pods_selector_matches_the_operator_labels():
    # environmentd runs as a StatefulSet; its pods carry materialize.cloud/app,
    # NOT app.kubernetes.io/component. (The old deployment selector matched
    # nothing and the readiness wait silently no-op'd.)
    argv = c.get_environmentd_pods("materialize-environment")
    assert argv[:3] == ["kubectl", "get", "pod"]
    assert "-l" in argv and argv[argv.index("-l") + 1] == "materialize.cloud/app=environmentd"
    assert argv[argv.index("-n") + 1] == "materialize-environment"
    assert argv[argv.index("-o") + 1] == "name"


def test_wait_environmentd_waits_on_pod_readiness():
    argv = c.wait_environmentd("materialize-environment", timeout="120s")
    assert argv[:2] == ["kubectl", "wait"]
    assert "pod" in argv
    assert "--for=condition=Ready" in argv
    assert argv[argv.index("-l") + 1] == "materialize.cloud/app=environmentd"
    assert "--timeout=120s" in argv
    # must NOT use the dead deployment/component selector
    assert "deployment" not in argv
    assert "app.kubernetes.io/component=environmentd" not in argv
