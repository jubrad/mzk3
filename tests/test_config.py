"""Config resolution: defaults < environment < flags."""

from mzk3.config import DEFAULT_VERSION, resolve


def test_defaults_when_nothing_set():
    cmd, cfg = resolve(["install"], env={})
    assert cmd == "install"
    assert cfg.version == DEFAULT_VERSION
    # operator version defaults to the materialize version
    assert cfg.operator_version == DEFAULT_VERSION
    assert cfg.namespace == "materialize"
    assert cfg.release_name == "my-materialize-operator"
    assert cfg.instance_ns == "materialize-environment"
    assert cfg.values_file == "sample-values-k3s.yaml"
    assert cfg.cluster_name == "mzk3-cluster"
    assert cfg.license_key_file is None
    assert cfg.skip_confirm is False
    assert cfg.force is False
    assert cfg.install_dashboards is False
    assert cfg.create_cluster is False


def test_env_overrides_defaults():
    env = {
        "MZ_VERSION": "v26.9.0",
        "MZ_NAMESPACE": "mz-ns",
        "MZ_RELEASE_NAME": "op",
        "MZ_INSTANCE_NS": "mz-inst",
        "MZ_VALUES_FILE": "custom.yaml",
        "K3D_CLUSTER_NAME": "envcluster",
        "MZ_LICENSE_KEY": "/tmp/lic.key",
        "MZ_SKIP_CONFIRM": "true",
        "MZ_INSTALL_DASHBOARDS": "true",
    }
    cmd, cfg = resolve(["status"], env=env)
    assert cfg.version == "v26.9.0"
    assert cfg.operator_version == "v26.9.0"  # still tracks version
    assert cfg.namespace == "mz-ns"
    assert cfg.release_name == "op"
    assert cfg.instance_ns == "mz-inst"
    assert cfg.values_file == "custom.yaml"
    assert cfg.cluster_name == "envcluster"
    assert cfg.license_key_file == "/tmp/lic.key"
    assert cfg.skip_confirm is True
    assert cfg.install_dashboards is True


def test_flags_override_env():
    env = {"MZ_VERSION": "v26.9.0", "K3D_CLUSTER_NAME": "envcluster"}
    cmd, cfg = resolve(
        ["install", "-v", "v27.0.0", "-c", "flagcluster", "--yes", "--force"],
        env=env,
    )
    assert cfg.version == "v27.0.0"
    assert cfg.operator_version == "v27.0.0"
    assert cfg.cluster_name == "flagcluster"
    assert cfg.skip_confirm is True
    assert cfg.force is True


def test_operator_version_independent_of_version():
    _, cfg = resolve(["upgrade", "-v", "v27.0.0", "-o", "v26.5.0"])
    assert cfg.version == "v27.0.0"
    assert cfg.operator_version == "v26.5.0"


def test_env_operator_version_still_lets_version_flag_stand_alone():
    env = {"MZ_OPERATOR_VERSION": "v26.1.0"}
    _, cfg = resolve(["upgrade", "-v", "v27.0.0"], env=env)
    assert cfg.version == "v27.0.0"
    assert cfg.operator_version == "v26.1.0"


def test_storage_backend_defaults_to_minio():
    _, cfg = resolve(["install"])
    assert cfg.storage_backend == "minio"


def test_storage_backend_from_env():
    _, cfg = resolve(["install"], env={"MZ_STORAGE_BACKEND": "rustfs"})
    assert cfg.storage_backend == "rustfs"


def test_storage_backend_flag_overrides_env():
    _, cfg = resolve(["install", "--storage-backend", "rustfs"],
                     env={"MZ_STORAGE_BACKEND": "minio"})
    assert cfg.storage_backend == "rustfs"


def test_long_flags():
    _, cfg = resolve(
        [
            "install",
            "--version", "v27.0.0",
            "--operator-version", "v27.1.0",
            "--license-key", "/lic",
            "--cluster", "c",
            "--namespace", "n",
            "--release-name", "r",
            "--instance-ns", "i",
            "--values-file", "f.yaml",
            "--create-cluster",
            "--install-dashboards",
        ]
    )
    assert cfg.version == "v27.0.0"
    assert cfg.operator_version == "v27.1.0"
    assert cfg.license_key_file == "/lic"
    assert cfg.cluster_name == "c"
    assert cfg.namespace == "n"
    assert cfg.release_name == "r"
    assert cfg.instance_ns == "i"
    assert cfg.values_file == "f.yaml"
    assert cfg.create_cluster is True
    assert cfg.install_dashboards is True
