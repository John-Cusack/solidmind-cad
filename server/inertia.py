"""Mass-property utilities for sim/airframe pipelines.

Provides shape-correct inertia formulas (box, thin disk, cylinder) and a
parallel-axis aggregator that combines per-body inertias into a single
tensor about a common centre of mass.

This module is the single source of truth for inertia math.  Code paths
that need an inertia tensor — SDF/URDF emission, PX4 airframe generation,
multi-body chassis aggregation — should call into here rather than
re-deriving formulas inline.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

_Axis = Literal["x", "y", "z"]


@dataclass(frozen=True, slots=True)
class Inertia6:
    """Symmetric 3x3 inertia tensor stored as 6 unique entries (kg·m²).

    Convention matches URDF/SDF inertia element order:
    ixx, ixy, ixz, iyy, iyz, izz.  Off-diagonal terms are zero for
    aligned principal-axis bodies (boxes, disks, cylinders) and become
    non-zero only when an aggregation translates a body off-axis.
    """

    ixx: float
    ixy: float
    ixz: float
    iyy: float
    iyz: float
    izz: float

    def as_tuple(self) -> tuple[float, float, float, float, float, float]:
        """Return the canonical 6-tuple (ixx, ixy, ixz, iyy, iyz, izz)."""
        return (self.ixx, self.ixy, self.ixz, self.iyy, self.iyz, self.izz)

    def __add__(self, other: Inertia6) -> Inertia6:
        return Inertia6(
            ixx=self.ixx + other.ixx,
            ixy=self.ixy + other.ixy,
            ixz=self.ixz + other.ixz,
            iyy=self.iyy + other.iyy,
            iyz=self.iyz + other.iyz,
            izz=self.izz + other.izz,
        )


def box_inertia(mass_kg: float, dx_m: float, dy_m: float, dz_m: float) -> Inertia6:
    """Inertia of a uniform-density rectangular cuboid about its centre.

    Standard formulas: ixx = m/12 · (dy² + dz²), etc.
    """
    if mass_kg <= 0.0:
        return Inertia6(0, 0, 0, 0, 0, 0)
    factor = mass_kg / 12.0
    return Inertia6(
        ixx=factor * (dy_m**2 + dz_m**2),
        ixy=0.0,
        ixz=0.0,
        iyy=factor * (dx_m**2 + dz_m**2),
        iyz=0.0,
        izz=factor * (dx_m**2 + dy_m**2),
    )


def thin_disk_inertia(
    mass_kg: float,
    radius_m: float,
    thickness_m: float = 0.0,
    *,
    axis: _Axis = "z",
) -> Inertia6:
    """Inertia of a thin uniform-density disk about its centre.

    For a thin disk spinning about its symmetry axis, the moment about
    the spin axis is twice the moment about either equatorial axis:
    izz = m·r²/2 (spin), ixx = iyy = m·r²/4 (equatorial).

    Adding a non-zero thickness folds in the small (m·t²/12) cylindrical
    contribution along the equatorial axes, which keeps the formula
    consistent with the cylinder limit as thickness grows.
    """
    if mass_kg <= 0.0 or radius_m <= 0.0:
        return Inertia6(0, 0, 0, 0, 0, 0)
    spin = 0.5 * mass_kg * radius_m**2
    equatorial = 0.25 * mass_kg * radius_m**2 + (mass_kg * thickness_m**2) / 12.0
    if axis == "z":
        return Inertia6(equatorial, 0.0, 0.0, equatorial, 0.0, spin)
    if axis == "y":
        return Inertia6(equatorial, 0.0, 0.0, spin, 0.0, equatorial)
    if axis == "x":
        return Inertia6(spin, 0.0, 0.0, equatorial, 0.0, equatorial)
    raise ValueError(f"axis must be 'x', 'y', or 'z'; got {axis!r}")


def cylinder_inertia(
    mass_kg: float,
    radius_m: float,
    length_m: float,
    *,
    axis: _Axis = "z",
) -> Inertia6:
    """Inertia of a solid uniform-density cylinder about its centre.

    Spin axis: m·r²/2.  Equatorial axes: m/12 · (3r² + h²).
    """
    if mass_kg <= 0.0 or radius_m <= 0.0:
        return Inertia6(0, 0, 0, 0, 0, 0)
    spin = 0.5 * mass_kg * radius_m**2
    equatorial = mass_kg / 12.0 * (3.0 * radius_m**2 + length_m**2)
    if axis == "z":
        return Inertia6(equatorial, 0.0, 0.0, equatorial, 0.0, spin)
    if axis == "y":
        return Inertia6(equatorial, 0.0, 0.0, spin, 0.0, equatorial)
    if axis == "x":
        return Inertia6(spin, 0.0, 0.0, equatorial, 0.0, equatorial)
    raise ValueError(f"axis must be 'x', 'y', or 'z'; got {axis!r}")


@dataclass(frozen=True, slots=True)
class InertiaContribution:
    """One body's mass + inertia + offset for parallel-axis aggregation."""

    mass_kg: float
    com_offset_m: tuple[float, float, float]  # offset from aggregation origin
    body_local: Inertia6  # inertia about this body's own COM


def _parallel_axis_shift(
    body_local: Inertia6,
    mass_kg: float,
    offset_m: tuple[float, float, float],
) -> Inertia6:
    """Shift inertia from body COM to a parent frame using the parallel-axis theorem.

    Given a body with mass m, inertia I_body about its own COM, and a
    COM offset r = (rx, ry, rz) from the parent frame, the inertia about
    the parent frame is:

        I_parent = I_body + m · ((r·r) E - r r^T)

    which expands to the diagonal additions m(ry² + rz²), m(rx² + rz²),
    m(rx² + ry²) and the off-diagonal subtractions -m·rx·ry, -m·rx·rz,
    -m·ry·rz.
    """
    rx, ry, rz = offset_m
    return Inertia6(
        ixx=body_local.ixx + mass_kg * (ry * ry + rz * rz),
        ixy=body_local.ixy - mass_kg * rx * ry,
        ixz=body_local.ixz - mass_kg * rx * rz,
        iyy=body_local.iyy + mass_kg * (rx * rx + rz * rz),
        iyz=body_local.iyz - mass_kg * ry * rz,
        izz=body_local.izz + mass_kg * (rx * rx + ry * ry),
    )


def aggregate(
    contributions: Iterable[InertiaContribution],
) -> tuple[float, tuple[float, float, float], Inertia6]:
    """Combine multiple bodies into a single mass + COM + inertia tensor.

    Each contribution carries its own COM offset (from a shared origin)
    and its body-local inertia.  Returns the total mass, the combined
    COM (about the same origin), and the inertia tensor about the
    combined COM.

    Workflow:
    1. Sum mass and weighted positions to find the combined COM.
    2. For each body, parallel-axis-shift its inertia from body COM to
       the combined COM.
    3. Sum the shifted inertias.

    Empty input or zero total mass returns (0, (0,0,0), Inertia6 zero).
    """
    contribs = list(contributions)
    if not contribs:
        return 0.0, (0.0, 0.0, 0.0), Inertia6(0, 0, 0, 0, 0, 0)

    total_mass = sum(c.mass_kg for c in contribs)
    if total_mass <= 0.0:
        return 0.0, (0.0, 0.0, 0.0), Inertia6(0, 0, 0, 0, 0, 0)

    com_x = sum(c.mass_kg * c.com_offset_m[0] for c in contribs) / total_mass
    com_y = sum(c.mass_kg * c.com_offset_m[1] for c in contribs) / total_mass
    com_z = sum(c.mass_kg * c.com_offset_m[2] for c in contribs) / total_mass
    combined_com = (com_x, com_y, com_z)

    total = Inertia6(0, 0, 0, 0, 0, 0)
    for c in contribs:
        offset = (
            c.com_offset_m[0] - com_x,
            c.com_offset_m[1] - com_y,
            c.com_offset_m[2] - com_z,
        )
        total = total + _parallel_axis_shift(c.body_local, c.mass_kg, offset)

    return total_mass, combined_com, total


# ---------------------------------------------------------------------------
# Sanity check helpers (exposed for debugging / tests)
# ---------------------------------------------------------------------------


def is_thin_disk_about_z(inertia: Inertia6, *, tol: float = 1e-6) -> bool:
    """Return True if the tensor matches a thin disk spinning about Z.

    Useful for regression tests: a healthy propeller inertia must have
    izz ≈ 2·ixx and ixx ≈ iyy with no off-diagonal coupling.
    """
    if inertia.ixx <= 0.0:
        return False
    return (
        abs(inertia.ixx - inertia.iyy) <= tol * max(inertia.ixx, 1.0)
        and abs(inertia.izz - 2.0 * inertia.ixx) <= tol * max(inertia.izz, 1.0)
        and abs(inertia.ixy) <= tol
        and abs(inertia.ixz) <= tol
        and abs(inertia.iyz) <= tol
    )
