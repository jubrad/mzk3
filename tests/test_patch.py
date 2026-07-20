"""Pure text transforms that replace the bash `sed` calls."""

from mzk3.patch import patch_environmentd_image, patch_license_key, patch_region


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
