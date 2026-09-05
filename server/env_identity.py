"""Environment identity for content-addressed evaluation.

Cache keys and evaluation results carry the identity of the environment that
produced them, so a result computed under one interpreter/package set is never
mistaken for one computed under another.

Equality policy (tranche 1, analytic tier): results are bit-exact **within one
environment identity**. A hash match on a cache key (which includes this
identity) is exact by construction; comparing results across different
identities is a cross-version comparison and must be explicit, never silent.
"""

from __future__ import annotations

import hashlib
import platform
from importlib import metadata

from server import jcs

# Packages whose versions can influence numeric results in the analytic tier.
# Field/world tiers will extend this with solver build identities.
_PROBED_PACKAGES = ("numpy", "scipy")

_cached_identity: dict | None = None
_cached_hash: str | None = None


def environment_identity() -> dict:
    """Return the identity of the current execution environment.

    Stable within a process; cheap to recompute across processes.
    """
    global _cached_identity
    if _cached_identity is not None:
        return _cached_identity

    packages: dict[str, str] = {}
    for name in _PROBED_PACKAGES:
        try:
            packages[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            continue

    _cached_identity = {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
            "release": platform.release(),
        },
        "packages": packages,
        # Reserved: field/world tiers record solver binary identities here.
        "solver_builds": {},
    }
    return _cached_identity


def environment_identity_hash() -> str:
    """sha256 over the JCS-canonical environment identity."""
    global _cached_hash
    if _cached_hash is None:
        canonical = jcs.canonicalize(environment_identity())
        _cached_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return _cached_hash
