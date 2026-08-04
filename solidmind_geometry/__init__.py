"""Parametric geometry generators, implemented in Rust.

The native extension is built from ``geometry/`` by maturin and lands beside
this file. A checkout that has never been installed does not have it, so say
what to do rather than raising a bare ModuleNotFoundError naming a module
nobody wrote by hand.
"""

from __future__ import annotations

try:
    from .solidmind_geometry import *  # noqa: F401,F403 — re-export native Rust extension
except ModuleNotFoundError as exc:  # pragma: no cover — depends on the checkout
    if exc.name != f"{__name__}.{__name__}":
        raise
    raise ModuleNotFoundError(
        "The solidmind_geometry native extension is not built. Install the "
        "package — 'python3 -m pip install -e .' from the repository root, "
        "which has maturin compile geometry/ — or rebuild in place with "
        "'cd geometry && cargo build --release && cp "
        "target/release/libsolidmind_geometry.so "
        "../solidmind_geometry/solidmind_geometry.so'.",
        name=exc.name,
    ) from exc
