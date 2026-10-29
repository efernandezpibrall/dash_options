"""Fingerprint application execution code using package-relative resources."""

import hashlib
from dash_options.core.paths import PACKAGE_ROOT


def execution_fingerprint(resources):
    digest = hashlib.sha256()
    for name in ("core/code_identity.py", *resources):
        digest.update(name.encode())
        digest.update((PACKAGE_ROOT / name).read_bytes())
    return digest.hexdigest()
