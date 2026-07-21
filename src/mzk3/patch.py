"""Pure text transforms replacing the bash `sed` edits on downloaded YAML."""

from __future__ import annotations

import re

_ENVIRONMENTD_RE = re.compile(r"environmentdImageRef: materialize/environmentd:v[0-9.]*")


def patch_region(text: str) -> str:
    """`region: "kind"` -> `region: "k3s"` (operator values file)."""
    return text.replace('region: "kind"', 'region: "k3s"')


def patch_environmentd_image(text: str, version: str) -> str:
    """Pin the Materialize instance image to `version`."""
    return _ENVIRONMENTD_RE.sub(
        f"environmentdImageRef: materialize/environmentd:{version}", text
    )


def patch_environmentd_resources(text: str, cpu: str) -> str:
    """Set the Materialize CR's `environmentdResourceRequirements` CPU.

    Inserted at the spec level, right after `environmentdImageRef`. Idempotent:
    a no-op if the field is already present.
    """
    if "environmentdResourceRequirements:" in text:
        return text
    block = (
        f'  environmentdResourceRequirements:\n'
        f'    requests:\n'
        f'      cpu: "{cpu}"\n'
        f'    limits:\n'
        f'      cpu: "{cpu}"'
    )
    return re.sub(
        r"^(\s*environmentdImageRef:.*)$",
        lambda m: f"{m.group(1)}\n{block}",
        text,
        count=1,
        flags=re.MULTILINE,
    )


def patch_persist_backend_host(text: str, from_name: str, to_name: str) -> str:
    """Repoint the persist backend S3 endpoint from one in-cluster service to
    another (e.g. minio -> rustfs).

    Targets the fully-qualified service DNS name specifically so it never
    touches the credentials, bucket name, or region, which also contain the
    substring "minio".
    """
    suffix = ".materialize.svc.cluster.local"
    return text.replace(f"{from_name}{suffix}", f"{to_name}{suffix}")


def patch_license_key(text: str, key: str) -> str:
    """Fill the empty `license_key: ""` field. Newlines stripped from the key."""
    clean = key.replace("\n", "")
    return text.replace('license_key: ""', f'license_key: "{clean}"')
