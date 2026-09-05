"""Evaluator stages — domain physics as tiered, identified models.

A stage is a pure model: ``derive_inputs`` maps (structure, params, scenario)
to a self-contained inputs document (this is what gets materialized and
fingerprinted), and ``run`` maps that document to metrics and checks.

Every stage is built from named **terms** (see ``server/model_registry.py``).
A stage configured with a non-default term set reports a different
``identity``, and identity participates in the evaluation cache key — so an
ablated model can never replay a full model's cached result. This is what
makes the benchmark's *missing mechanism* defect class executable.

Tranche 1-2 ship the ANALYTIC tier only:

- ``analytic_latch`` — foam-dart latch tooth-root screen (mechanical).
- ``analytic_acoustic_bearing`` — TDOA bearing error and triangulated
  position error for a distributed acoustic detection mesh.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from server import acoustics, json_pointer
from server.analysis_materials import get_material
from server.analysis_models import CheckStatus
from server.dg_models import StructureDoc
from server.eval_models import MetricValue
from server.model_registry import ModelChain
from server.screen_stress import screen_stress, stress_concentration_factor

# Factor-of-safety and margin values are clamped so a zero-stress corner case
# never produces a non-finite number (JCS rejects non-finite floats).
_CLAMP = 1.0e9

_STATUS_CODES = {CheckStatus.PASS: 2.0, CheckStatus.WARN: 1.0, CheckStatus.FAIL: 0.0}


class StageError(ValueError):
    pass


class Tier(str, Enum):
    ANALYTIC = "analytic"
    FIELD = "field"  # reserved: Gmsh -> CalculiX/Elmer via preCICE
    WORLD = "world"  # reserved: Gazebo/PX4, waveform synthesis, ns-3


@dataclass(frozen=True, slots=True)
class StageOutput:
    metrics: dict[str, MetricValue] = field(default_factory=dict)
    checks: list[dict[str, Any]] = field(default_factory=list)


def _leaf_value(params: dict[str, Any], path: str) -> Any:
    try:
        leaf = json_pointer.get(params, path)
    except json_pointer.JsonPointerError as e:
        raise StageError(f"Required parameter missing: {path}") from e
    if not isinstance(leaf, dict) or "value" not in leaf:
        raise StageError(f"Parameter at {path} is not a value leaf")
    return leaf["value"]


class Stage(ABC):
    name: str
    tier: Tier
    version: str = "v1"
    TERMS: tuple[str, ...] = ()

    def __init__(self, terms: frozenset[str] | None = None) -> None:
        self.terms = frozenset(self.TERMS) if terms is None else frozenset(terms)
        unknown = self.terms - set(self.TERMS)
        if unknown:
            raise StageError(f"Unknown term(s) {sorted(unknown)} for stage {self.name!r}")

    def configure(self, terms: frozenset[str] | None) -> Stage:
        """A copy of this stage with a specific term set enabled."""
        if terms is None or frozenset(terms) == self.terms:
            return self
        return type(self)(terms=frozenset(terms))

    @property
    def identity(self) -> str:
        """Model identity — part of the evaluation cache key."""
        base = f"{self.name}/{self.version}"
        if self.terms == frozenset(self.TERMS):
            return base
        return f"{base}-terms:{'+'.join(sorted(self.terms))}"

    @abstractmethod
    def derive_inputs(
        self, structure: dict[str, Any], params: dict[str, Any], scenario: str
    ) -> dict[str, Any]:
        """Pure mapping to a self-contained inputs document (fingerprinted)."""

    @abstractmethod
    def run(self, inputs: dict[str, Any], seed: int) -> StageOutput:
        """Pure evaluation of the derived inputs."""


class AnalyticLatchStage(Stage):
    """Latch-sear tooth-root screen (bending + fillet Kt), analytic tier.

    Scenarios:
    - ``latch_hold``  — static full-cock hold force (k * x_max)
    - ``latch_catch`` — the governing sear load: hold force x the dynamic
      impact factor (suddenly-applied load; run.py's canonical case)

    Terms: ``bending`` (required), ``stress_concentration`` (ablatable — with
    it disabled the screen cannot see a sharp root, so an optimizer is free to
    drive the fillet to zero).
    """

    name = "analytic_latch"
    tier = Tier.ANALYTIC
    TERMS = ("bending", "stress_concentration")

    SCENARIOS = ("latch_hold", "latch_catch")

    def derive_inputs(
        self, structure: dict[str, Any], params: dict[str, Any], scenario: str
    ) -> dict[str, Any]:
        if scenario not in self.SCENARIOS:
            raise StageError(
                f"Unknown scenario {scenario!r} for {self.name}; expected one of {self.SCENARIOS}"
            )
        if "bending" not in self.terms:
            raise StageError(f"{self.name} requires the 'bending' term")

        width_mm = float(_leaf_value(params, "/parts/latch_sear/specs/tooth_width_mm"))
        arm_mm = float(_leaf_value(params, "/parts/latch_sear/specs/tooth_height_mm"))
        root_mm = float(_leaf_value(params, "/parts/latch_sear/specs/root_mm"))
        fillet_mm = float(_leaf_value(params, "/parts/latch_sear/specs/fillet_mm"))
        spring_k = float(_leaf_value(params, "/physical_defaults/spring_k_n_per_m"))
        x_max_mm = float(_leaf_value(params, "/constraints/max_spring_compression_mm"))
        target_fos = float(_leaf_value(params, "/constraints/structural_fos_target"))

        if root_mm <= 0.0:
            raise StageError("latch root thickness must be positive")

        hold_force_n = spring_k * (x_max_mm / 1000.0)
        force_n = hold_force_n
        if scenario == "latch_catch":
            impact = float(_leaf_value(params, "/scenario_defaults/latch_impact_factor"))
            force_n = hold_force_n * impact

        doc = StructureDoc.from_dict(structure)
        material_name = doc.materials.get("default", "pla")
        material = get_material(material_name)
        if material is None:
            raise StageError(f"Unknown material {material_name!r}")

        inputs: dict[str, Any] = {
            "section": {"type": "rectangle", "width_mm": width_mm, "height_mm": root_mm},
            "load": {"force_n": force_n, "length_mm": arm_mm},
            "yield_strength_mpa": float(material.yield_strength_mpa),
            "target_fos": target_fos,
        }
        if "stress_concentration" in self.terms:
            inputs["stress_concentration"] = {
                "feature": "fillet",
                "ratio": fillet_mm / root_mm,
            }
        return inputs

    def run(self, inputs: dict[str, Any], seed: int) -> StageOutput:
        del seed  # the analytic screen is deterministic; seed is echoed on the result
        try:
            check = screen_stress(name="latch tooth_root", **inputs)
        except ValueError as e:
            raise StageError(str(e)) from e

        scf = inputs.get("stress_concentration")
        kt = (
            stress_concentration_factor(scf["feature"], float(scf["ratio"]))
            if scf is not None
            else 1.0
        )
        peak_mpa = float(check.measured)
        yield_mpa = float(check.limit)
        fos = min(yield_mpa / peak_mpa, _CLAMP) if peak_mpa > 0 else _CLAMP

        metrics = {
            "latch_fos": MetricValue(value=fos, unit="1"),
            "peak_stress_mpa": MetricValue(value=peak_mpa, unit="MPa"),
            "sigma_nom_mpa": MetricValue(value=peak_mpa / kt, unit="MPa"),
            "kt": MetricValue(value=kt, unit="1"),
            "screen_status_code": MetricValue(value=_STATUS_CODES[check.status], unit="1"),
        }
        return StageOutput(metrics=metrics, checks=[check.to_dict()])


class AnalyticAcousticBearingStage(Stage):
    """TDOA bearing error and triangulated position error, analytic tier.

    Scenarios select the atmospheric state:
    - ``acoustic_calm``       — low turbulence
    - ``acoustic_convective`` — daytime convective turbulence

    Terms:
    - ``crlb`` (required) — Knapp-Carter delay variance.
    - ``coherence_loss`` (ablatable) — turbulence decorrelation across the
      aperture. Disabled, bearing error falls as 1/aperture with no penalty,
      so an optimizer drives the aperture to its bound and reports a position
      error the array cannot achieve.
    - ``anomaly`` (ablatable) — threshold/ambiguity blending. Disabled, the
      CRLB is reported as if it held below threshold, which is optimistic
      exactly at the coverage boundary.
    """

    name = "analytic_acoustic_bearing"
    tier = Tier.ANALYTIC
    TERMS = ("crlb", "coherence_loss", "anomaly")

    SCENARIOS = ("acoustic_calm", "acoustic_convective")

    def derive_inputs(
        self, structure: dict[str, Any], params: dict[str, Any], scenario: str
    ) -> dict[str, Any]:
        if scenario not in self.SCENARIOS:
            raise StageError(
                f"Unknown scenario {scenario!r} for {self.name}; expected one of {self.SCENARIOS}"
            )
        if "crlb" not in self.terms:
            raise StageError(f"{self.name} requires the 'crlb' term")

        doc = StructureDoc.from_dict(structure)
        node_ids = sorted(c.id for c in doc.components if c.kind == "sensor_node")
        if len(node_ids) < 2:
            raise StageError("acoustic triangulation needs at least two sensor nodes")

        # Plain lists, not tuples: the derived inputs document is JCS-canonicalized.
        nodes = [
            [
                float(_leaf_value(params, f"/nodes/{nid}/x_m")),
                float(_leaf_value(params, f"/nodes/{nid}/y_m")),
            ]
            for nid in node_ids
        ]
        cn2_key = "cn2_calm" if scenario == "acoustic_calm" else "cn2_convective"
        return {
            "nodes": nodes,
            "node_ids": node_ids,
            "aperture_m": float(_leaf_value(params, "/array/aperture_m")),
            "integration_time_s": float(_leaf_value(params, "/array/integration_time_s")),
            "target": [
                float(_leaf_value(params, "/target/x_m")),
                float(_leaf_value(params, "/target/y_m")),
            ],
            "f_lo_hz": float(_leaf_value(params, "/source/f_lo_hz")),
            "f_hi_hz": float(_leaf_value(params, "/source/f_hi_hz")),
            "snr_db": float(_leaf_value(params, "/source/snr_db")),
            "cn2": float(_leaf_value(params, f"/environment/{cn2_key}")),
            "position_requirement_m": float(
                _leaf_value(params, "/constraints/position_requirement_m")
            ),
            "terms": sorted(self.terms),
        }

    def run(self, inputs: dict[str, Any], seed: int) -> StageOutput:
        del seed  # analytic and deterministic
        terms = set(inputs.get("terms", self.TERMS))
        aperture = float(inputs["aperture_m"])
        if aperture <= 0.0:
            raise StageError("aperture must be positive")
        f_lo, f_hi = float(inputs["f_lo_hz"]), float(inputs["f_hi_hz"])
        if f_hi <= f_lo:
            raise StageError("source band needs f_hi > f_lo")

        snr_linear = 10.0 ** (float(inputs["snr_db"]) / 10.0)
        integration_time = float(inputs["integration_time_s"])
        cn2 = float(inputs["cn2"])
        target = (float(inputs["target"][0]), float(inputs["target"][1]))
        bandwidth = f_hi - f_lo
        delay_spread_s = 2.0 * aperture / acoustics.SPEED_OF_SOUND_M_S

        sigmas: list[float] = []
        anomalies: list[float] = []
        for nx, ny in inputs["nodes"]:
            path_m = math.hypot(target[0] - nx, target[1] - ny)

            def coherence2_at(f: float, _p: float = path_m) -> float:
                return acoustics.band_coherence2(
                    f,
                    snr_linear=snr_linear,
                    separation_m=aperture,
                    path_m=_p,
                    cn2=cn2,
                    include_turbulence="coherence_loss" in terms,
                )

            crlb_var = acoustics.tdoa_variance_s2(
                f_lo_hz=f_lo,
                f_hi_hz=f_hi,
                integration_time_s=integration_time,
                coherence2_at=coherence2_at,
            )
            mid_coherence2 = coherence2_at(0.5 * (f_lo + f_hi))
            if "anomaly" in terms:
                p_anom = acoustics.anomaly_probability(
                    bandwidth_hz=bandwidth,
                    integration_time_s=integration_time,
                    coherence2=mid_coherence2,
                    delay_spread_s=delay_spread_s,
                )
                var = acoustics.effective_tdoa_variance(
                    crlb_variance_s2=crlb_var,
                    anomaly_prob=p_anom,
                    delay_spread_s=delay_spread_s,
                )
            else:
                p_anom = 0.0
                var = crlb_var
            anomalies.append(p_anom)
            # Bearing is measured relative to the node's array broadside, which
            # the mesh orients toward its coverage sector; take broadside.
            sigmas.append(
                acoustics.bearing_sigma_rad(
                    tdoa_sigma_s=math.sqrt(var), aperture_m=aperture, bearing_rad=0.0
                )
            )

        position_rms, gdop = acoustics.fuse_bearings(
            [(float(x), float(y)) for x, y in inputs["nodes"]], target, sigmas
        )
        position_rms = min(position_rms, _CLAMP)
        gdop = min(gdop, _CLAMP)
        requirement = float(inputs["position_requirement_m"])
        worst_anomaly = max(anomalies) if anomalies else 0.0
        mean_bearing_deg = math.degrees(sum(sigmas) / len(sigmas))
        margin = min(requirement / position_rms, _CLAMP) if position_rms > 0 else _CLAMP
        detected = 1.0 if (position_rms <= requirement and worst_anomaly <= 0.1) else 0.0

        metrics = {
            "position_rms_m": MetricValue(value=position_rms, unit="m"),
            "position_margin": MetricValue(value=margin, unit="1"),
            # Array span, reported as a build/logistics proxy. Optimizing it
            # alone is a wrong-objective, not a cheap design.
            "aperture_m": MetricValue(value=aperture, unit="m"),
            "bearing_sigma_deg": MetricValue(value=mean_bearing_deg, unit="deg"),
            "p_anomaly": MetricValue(value=worst_anomaly, unit="1"),
            "gdop": MetricValue(value=gdop, unit="1"),
            "detected": MetricValue(value=detected, unit="1"),
        }
        check = {
            "name": "acoustic bearing screen",
            "status": "pass" if detected else "fail",
            "message": (
                f"position RMS {position_rms:.1f} m vs requirement {requirement:.1f} m, "
                f"bearing sigma {mean_bearing_deg:.2f} deg, P(anomaly)={worst_anomaly:.3f}"
            ),
            "measured": round(position_rms, 4),
            "limit": requirement,
        }
        return StageOutput(metrics=metrics, checks=[check])


STAGES: dict[str, Stage] = {
    AnalyticLatchStage.name: AnalyticLatchStage(),
    AnalyticAcousticBearingStage.name: AnalyticAcousticBearingStage(),
}


def resolve_stages(
    models: tuple[str, ...] | list[str], chain: ModelChain | None
) -> dict[str, Stage]:
    """Configured stage instances for a request's models under a model chain."""
    resolved: dict[str, Stage] = {}
    for name in models:
        if name not in STAGES:
            raise StageError(f"Unknown model {name!r}")
        stage = STAGES[name]
        resolved[name] = stage.configure(chain.terms_for(name)) if chain is not None else stage
    return resolved
