"""Option 3: Mock-worker E2E test — builds real geometry via MCP, no LLM.

This test requires FreeCAD running with the solidmind-cad addon loaded
(socket server on port 9876). Skip if not available.

The mock worker builds a known simple part (20×20×10 cube with Ø10×5 boss)
using direct MCP tool calls, exports STEP/STL, writes metadata, then the
orchestrator pipeline validates the results through gates G0→G4.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from orchestrator.runner import (
    build_worker_prompts,
    check_gate_g0,
    check_gate_g1,
    check_gate_g3,
    check_gate_g4,
    collect_worker_results,
    init_run,
    save_spec,
    transition,
)
from orchestrator.spec import (
    CoordinateFrame,
    Interface,
    ManufacturingSpec,
    MasterSpec,
    MatingSemantic,
    Objective,
    SpecStatus,
    Subsystem,
    SubsystemKind,
    ToleranceSchema,
    ValidationCheckPoint,
    ValidationMethod,
)


def _freecad_available() -> bool:
    """Check if FreeCAD addon is reachable on port 9876."""
    try:
        from server.freecad_client import FreeCADClient
        client = FreeCADClient()
        client.connect(timeout=2.0)
        client.send_command("ping")
        client.disconnect()
        return True
    except Exception:
        return False


def _send(cmd: str, **args: Any) -> dict[str, Any]:
    """Send a command to FreeCAD and return the result."""
    from server.freecad_client import FreeCADClient
    client = FreeCADClient()
    client.connect(timeout=5.0)
    try:
        return client.send_command(cmd, **args)
    finally:
        client.disconnect()


def _build_cube_with_boss(output_dir: Path) -> dict[str, Any]:
    """Build a 20×20×10 cube with a Ø10×5 boss on top. Returns metadata.

    This is the "mock worker" — it does exactly what a real worker would
    do, but deterministically without an LLM.  Uses the low-level socket
    commands (new_sketch → sketch_populate → close_sketch → pad).

    IMPORTANT: FreeCAD may rename objects (Body → Body001) so we always
    use the returned name, not the requested name.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. New document — use returned doc name
    doc_result = _send("new_document", name="mock_e2e")
    doc_name = doc_result.get("name", "mock_e2e")

    # 2. New body — use returned body name
    body_result = _send("new_body", name="cube_a", doc=doc_name)
    body_name = body_result["name"]

    # 3. Sketch the base rectangle on XY plane
    sk = _send("new_sketch", body=body_name, plane="XY", doc=doc_name)
    _send("sketch_populate", sketch=sk["sketch"], doc=doc_name, elements=[
        {"type": "rect", "x": -10, "y": -10, "w": 20, "h": 20},
    ])
    _send("close_sketch", sketch=sk["sketch"], doc=doc_name)

    # 4. Pad the base 10mm
    _send("pad", sketch=sk["sketch"], length=10, doc=doc_name)

    # 5. Sketch the boss circle
    sk2 = _send("new_sketch", body=body_name, plane="XY", doc=doc_name)
    _send("sketch_populate", sketch=sk2["sketch"], doc=doc_name, elements=[
        {"type": "circle", "cx": 0, "cy": 0, "r": 5},
    ])
    _send("close_sketch", sketch=sk2["sketch"], doc=doc_name)

    # 6. Pad the boss 5mm
    _send("pad", sketch=sk2["sketch"], length=5, doc=doc_name)

    # 7. Export STEP
    step_path = str(output_dir / "cube_a.step")
    _send("export", path=step_path, format="step", doc=doc_name)

    # 8. Export STL
    stl_path = str(output_dir / "cube_a.stl")
    _send("export", path=stl_path, format="stl", doc=doc_name)

    # 9. Screenshot (may fail headless)
    try:
        _send("screenshot", path=str(output_dir / "cube_a.png"), doc=doc_name)
    except Exception:
        pass

    # 10. Measure
    try:
        dims = _send("get_dimensions", body=body_name, doc=doc_name)
    except Exception:
        dims = {}

    # 11. Write metadata
    metadata = {
        "subsystem": "cube_a",
        "doc_name": doc_name,
        "body_name": body_name,
        "claimed_mass_kg": dims.get("mass_kg", 0.045),
        "claimed_bounding_box_mm": dims.get("bounding_box", [20, 20, 15]),
        "interface_actuals": {
            "ifc_mate": {
                "boss_diameter": 10.0,
                "boss_height": 5.0,
            },
        },
        "screenshots": ["cube_a.png"],
        "deviations": [],
        "notes": "Mock worker — deterministic build",
    }
    (output_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))
    return metadata


def _build_cube_with_hole(output_dir: Path, doc_name: str | None = None) -> dict[str, Any]:
    """Build a 20×20×10 cube with a Ø10×5 pocket on top. Returns metadata."""
    output_dir.mkdir(parents=True, exist_ok=True)

    kwargs: dict[str, Any] = {}
    if doc_name:
        kwargs["doc"] = doc_name

    # 1. New body
    body_result = _send("new_body", name="cube_b", **kwargs)
    body_name = body_result["name"]

    # 2. Sketch base rectangle
    sk = _send("new_sketch", body=body_name, plane="XY", **kwargs)
    _send("sketch_populate", sketch=sk["sketch"], elements=[
        {"type": "rect", "x": -10, "y": -10, "w": 20, "h": 20},
    ], **kwargs)
    _send("close_sketch", sketch=sk["sketch"], **kwargs)

    # 3. Pad base 10mm
    _send("pad", sketch=sk["sketch"], length=10, **kwargs)

    # 4. Sketch hole circle
    sk2 = _send("new_sketch", body=body_name, plane="XY", **kwargs)
    _send("sketch_populate", sketch=sk2["sketch"], elements=[
        {"type": "circle", "cx": 0, "cy": 0, "r": 5},
    ], **kwargs)
    _send("close_sketch", sketch=sk2["sketch"], **kwargs)

    # 5. Pocket 5mm deep
    _send("pocket", sketch=sk2["sketch"], length=5, **kwargs)

    # 6. Export
    _send("export", path=str(output_dir / "cube_b.step"), format="step", **kwargs)
    _send("export", path=str(output_dir / "cube_b.stl"), format="stl", **kwargs)

    # 7. Metadata
    metadata = {
        "subsystem": "cube_b",
        "body_name": body_name,
        "claimed_mass_kg": 0.038,
        "claimed_bounding_box_mm": [20, 20, 10],
        "interface_actuals": {
            "ifc_mate": {
                "hole_diameter": 10.0,
                "hole_depth": 5.0,
            },
        },
        "screenshots": [],
        "deviations": [],
        "notes": "Mock worker — deterministic build",
    }
    (output_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))
    return metadata


def _make_e2e_spec() -> MasterSpec:
    """Spec for the two-cube E2E test."""
    spec = MasterSpec(
        name="Mock Worker E2E",
        description="Two cubes: boss/hole interface",
        global_constraints={"max_mass_kg": 0.5},
        objectives=[
            Objective(name="mass", direction="minimize", unit="kg", weight=1.0),
        ],
    )
    ifc = Interface(
        id="ifc_mate",
        name="Boss-hole fit",
        subsystem_a="cube_a",
        port_a="boss",
        subsystem_b="cube_b",
        port_b="hole",
        geometry={"type": "cylinder", "diameter_mm": 10, "depth_mm": 5},
        frame_a=CoordinateFrame(origin_mm=[0, 0, 10]),
        frame_b=CoordinateFrame(origin_mm=[0, 0, 10]),
        mating=MatingSemantic(type="cylindrical_fit", engagement_length_mm=5),
        runout_or_concentricity=0.01,
        tolerances=ToleranceSchema(
            fit_class="H7/h6",
            dimensional={"diameter_mm": {"nominal": 10, "upper": 0.015, "lower": 0}},
        ),
        validation=ValidationMethod(
            check_points=[
                ValidationCheckPoint(feature="boss_diameter", expected_mm=10.0, tolerance_mm=0.02),
            ],
        ),
    )
    spec.interfaces.append(ifc)

    cube_a = Subsystem(
        id="sa", name="cube_a",
        description="Cube with boss",
        kind=SubsystemKind.GENERATED,
        envelope_mm=[20, 20, 15],
        mass_budget_kg=0.1,
        material="aluminum",
        interfaces=["ifc_mate"],
        worker_count=1,
        manufacturing=ManufacturingSpec(process="CNC_milling"),
    )
    cube_b = Subsystem(
        id="sb", name="cube_b",
        description="Cube with hole",
        kind=SubsystemKind.GENERATED,
        envelope_mm=[20, 20, 10],
        mass_budget_kg=0.1,
        material="aluminum",
        interfaces=["ifc_mate"],
        worker_count=1,
        manufacturing=ManufacturingSpec(process="CNC_milling"),
    )
    spec.subsystems.extend([cube_a, cube_b])
    return spec


@unittest.skipUnless(_freecad_available(), "FreeCAD addon not running on port 9876")
class TestMockWorkerE2E(unittest.TestCase):
    """End-to-end test: mock workers build real geometry, orchestrator validates."""

    def setUp(self) -> None:
        self.td = tempfile.mkdtemp()
        self.run = init_run("E2E Test", run_dir=Path(self.td) / "e2e")
        self.run.spec = _make_e2e_spec()
        save_spec(self.run)

    def tearDown(self) -> None:
        shutil.rmtree(self.td, ignore_errors=True)

    def test_gates_pass_before_build(self) -> None:
        """G0, G1, G3 should pass on the spec alone."""
        ok0, issues0 = check_gate_g0(self.run.spec)
        self.assertTrue(ok0, f"G0: {issues0}")

        ok1, issues1 = check_gate_g1(self.run.spec)
        self.assertTrue(ok1, f"G1: {issues1}")

        ok3, issues3 = check_gate_g3(self.run.spec)
        self.assertTrue(ok3, f"G3: {issues3}")

    def test_mock_worker_builds_cube_a(self) -> None:
        """Mock worker builds cube_a and produces valid output."""
        output_dir = self.run.run_dir / "cube_a_0" / "output"
        metadata = _build_cube_with_boss(output_dir)

        # STEP file should exist
        step_files = list(output_dir.glob("*.step"))
        self.assertGreaterEqual(len(step_files), 1)

        # STEP file should be non-empty
        self.assertGreater(step_files[0].stat().st_size, 0)

        # Metadata should have interface actuals
        self.assertIn("ifc_mate", metadata["interface_actuals"])

    def test_mock_worker_builds_cube_b(self) -> None:
        """Mock worker builds cube_b and produces valid output."""
        # cube_b reuses the document created by cube_a
        out_a = self.run.run_dir / "cube_a_0" / "output"
        meta_a = _build_cube_with_boss(out_a)
        doc_name = meta_a.get("doc_name")

        output_dir = self.run.run_dir / "cube_b_0" / "output"
        metadata = _build_cube_with_hole(output_dir, doc_name=doc_name)

        step_files = list(output_dir.glob("*.step"))
        self.assertGreaterEqual(len(step_files), 1)
        self.assertGreater(step_files[0].stat().st_size, 0)

    def test_full_pipeline_with_mock_workers(self) -> None:
        """Full pipeline: spec → state transitions → mock build → G4 pass."""
        # Walk state machine
        transition(self.run, SpecStatus.NORMALIZING, reason="start")
        transition(self.run, SpecStatus.COUNCIL_REVIEW, reason="normalized")
        transition(self.run, SpecStatus.LAYOUT_FROZEN, reason="skeleton ok")
        transition(self.run, SpecStatus.INTERFACES_FROZEN, reason="ICDs frozen")
        transition(self.run, SpecStatus.BUILDING, reason="dispatching")

        # Mock workers build (cube_b reuses cube_a's document)
        out_a = self.run.run_dir / "cube_a_0" / "output"
        out_b = self.run.run_dir / "cube_b_0" / "output"
        meta_a = _build_cube_with_boss(out_a)
        _build_cube_with_hole(out_b, doc_name=meta_a.get("doc_name"))

        # G4: artifacts exist
        ok4, issues4 = check_gate_g4(self.run)
        self.assertTrue(ok4, f"G4: {issues4}")

        # Collect and verify
        results = collect_worker_results(self.run)
        self.assertEqual(len(results), 2)
        for r in results:
            self.assertEqual(r["status"], "complete")
            self.assertIn("metadata", r)

        # Continue state machine
        transition(self.run, SpecStatus.GEOMETRY_VALIDATING, reason="artifacts ok")

    def test_step_files_are_valid(self) -> None:
        """STEP files should start with ISO-10303 header."""
        output_dir = self.run.run_dir / "cube_a_0" / "output"
        _build_cube_with_boss(output_dir)

        step_file = output_dir / "cube_a.step"
        content = step_file.read_text()
        self.assertTrue(
            content.startswith("ISO-10303") or "ISO-10303" in content[:200],
            "STEP file doesn't contain ISO-10303 header",
        )

    def test_stl_files_are_valid(self) -> None:
        """STL files should start with 'solid' (ASCII) or binary header."""
        output_dir = self.run.run_dir / "cube_a_0" / "output"
        _build_cube_with_boss(output_dir)

        stl_file = output_dir / "cube_a.stl"
        content = stl_file.read_bytes()
        # ASCII STL starts with 'solid', binary has 80-byte header
        is_ascii = content[:5] == b"solid"
        is_binary = len(content) > 84  # 80-byte header + 4-byte tri count
        self.assertTrue(is_ascii or is_binary, "STL file format not recognized")

    def test_metadata_matches_spec_interface(self) -> None:
        """Metadata interface_actuals should reference spec interface IDs."""
        output_dir = self.run.run_dir / "cube_a_0" / "output"
        _build_cube_with_boss(output_dir)

        metadata = json.loads((output_dir / "metadata.json").read_text())
        spec_ifc_ids = {ifc.id for ifc in self.run.spec.interfaces}
        actual_ifc_ids = set(metadata["interface_actuals"].keys())

        # Every ID in actuals should be in the spec
        self.assertTrue(
            actual_ifc_ids.issubset(spec_ifc_ids),
            f"Metadata references unknown interfaces: {actual_ifc_ids - spec_ifc_ids}",
        )


if __name__ == "__main__":
    unittest.main()
