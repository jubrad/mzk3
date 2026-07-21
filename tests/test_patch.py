"""Pure text transforms that replace the bash `sed` calls."""

from mzk3.patch import (
    patch_environmentd_image,
    patch_environmentd_resources,
    patch_license_key,
    patch_persist_backend_host,
    patch_region,
)

_SECRET = (
    's3://minio:minio123@bucket/12345678-1234-1234-1234-123456789012'
    "?endpoint=http%3A%2F%2Fminio.materialize.svc.cluster.local%3A9000&region=minio"
)


def test_patch_region_kind_to_k3s():
    assert patch_region('region: "kind"') == 'region: "k3s"'


def test_patch_region_leaves_other_text():
    text = 'foo: bar\nregion: "kind"\nbaz: qux\n'
    assert patch_region(text) == 'foo: bar\nregion: "k3s"\nbaz: qux\n'


def test_patch_region_noop_when_absent():
    text = 'region: "aws"\n'
    assert patch_region(text) == text


def test_patch_environmentd_image_replaces_version():
    text = "environmentdImageRef: materialize/environmentd:v26.4.0"
    assert (
        patch_environmentd_image(text, "v27.0.0")
        == "environmentdImageRef: materialize/environmentd:v27.0.0"
    )


def test_patch_environmentd_image_handles_multi_segment_version():
    text = "environmentdImageRef: materialize/environmentd:v26.4.10"
    assert (
        patch_environmentd_image(text, "v27.0.0")
        == "environmentdImageRef: materialize/environmentd:v27.0.0"
    )


def test_patch_license_key_fills_empty():
    text = 'license_key: ""'
    assert patch_license_key(text, "ABC-123") == 'license_key: "ABC-123"'


def test_patch_license_key_strips_newlines_from_key():
    # bash did `cat file | tr -d '\n'`
    assert patch_license_key('license_key: ""', "ABC\n123\n") == 'license_key: "ABC123"'


def test_patch_persist_backend_host_swaps_only_the_fqdn():
    out = patch_persist_backend_host(_SECRET, "minio", "rustfs")
    # endpoint host is repointed...
    assert "rustfs.materialize.svc.cluster.local" in out
    assert "minio.materialize.svc.cluster.local" not in out
    # ...but the credentials and bucket (which also contain "minio") are untouched
    assert "s3://minio:minio123@bucket/" in out
    assert "region=minio" in out


def test_patch_persist_backend_host_noop_for_minio():
    assert patch_persist_backend_host(_SECRET, "minio", "minio") == _SECRET


_CR = """\
apiVersion: materialize.cloud/v1alpha1
kind: Materialize
metadata:
  name: 12345678
spec:
  environmentdImageRef: materialize/environmentd:v26.30.0
  backendSecretName: materialize-backend
  authenticatorKind: None
"""


def test_patch_environmentd_resources_sets_cpu_under_spec():
    out = patch_environmentd_resources(_CR, "2")
    assert "environmentdResourceRequirements:" in out
    # cpu requests and limits both set to the requested value
    assert out.count('cpu: "2"') == 2
    # inserted at spec level (2-space indent), nested keys deeper
    assert "\n  environmentdResourceRequirements:\n" in out
    assert "    requests:\n" in out and "    limits:\n" in out
    assert "      cpu: \"2\"" in out
    # untouched fields remain
    assert "backendSecretName: materialize-backend" in out


def test_patch_environmentd_resources_idempotent():
    once = patch_environmentd_resources(_CR, "2")
    twice = patch_environmentd_resources(once, "2")
    assert once == twice
