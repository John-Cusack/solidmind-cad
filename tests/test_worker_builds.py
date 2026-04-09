"""Unit tests for ``orchestrator.worker_builds`` package skeleton.

These tests don't require a running FreeCAD addon — they only exercise
the package imports, the ``common`` helpers that don't touch the
socket, and the behavior of the readiness probes when no addon is
reachable. The build_geometry path and per-part-class builders are
tested separately in ``tests/test_orchestrator_real_worker_e2e.py``,
which is gated by ``freecad_ready()``.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.worker_builds import common


class TestPackageImports(unittest.TestCase):
    """The package skeleton should be importable without side effects."""

    def test_package_imports(self) -> None:
        import orchestrator.worker_builds  # noqa: F401

    def test_common_module_exposes_expected_symbols(self) -> None:
        self.assertTrue(hasattr(common, "freecad_ready"))
        self.assertTrue(hasattr(common, "freecad_ready_with_import_step"))
        self.assertTrue(hasattr(common, "build_geometry"))
        self.assertTrue(hasattr(common, "TaskStub"))
        self.assertTrue(hasattr(common, "read_metadata"))
        self.assertTrue(hasattr(common, "override_claimed_measurements"))


class TestTaskStub(unittest.TestCase):
    """TaskStub is the shape-compat stand-in for A2ATask."""

    def test_default_progress_is_empty_list(self) -> None:
        task = common.TaskStub()
        self.assertEqual(task.progress, [])

    def test_progress_append_works(self) -> None:
        task = common.TaskStub()
        task.progress.append("step 1")
        task.progress.append("step 2")
        self.assertEqual(task.progress, ["step 1", "step 2"])

    def test_log_emits_without_raising(self) -> None:
        task = common.TaskStub()
        task.progress.append("hello")
        # Just verify it doesn't blow up — the log output is best-effort.
        task.log()

    def test_each_instance_has_independent_progress(self) -> None:
        # Guards against the classic dataclass-with-mutable-default bug.
        t1 = common.TaskStub()
        t2 = common.TaskStub()
        t1.progress.append("from t1")
        self.assertEqual(t2.progress, [])


class TestHostPortEnvOverrides(unittest.TestCase):
    def test_default_host(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("FREECAD_HOST", None)
            self.assertEqual(common.fc_host(), "127.0.0.1")

    def test_env_override_host(self) -> None:
        with patch.dict("os.environ", {"FREECAD_HOST": "10.0.0.5"}):
            self.assertEqual(common.fc_host(), "10.0.0.5")

    def test_default_port(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("FREECAD_PORT", None)
            self.assertEqual(common.fc_port(), 9876)

    def test_env_override_port(self) -> None:
        with patch.dict("os.environ", {"FREECAD_PORT": "9999"}):
            self.assertEqual(common.fc_port(), 9999)


class TestFreecadReadyNegative(unittest.TestCase):
    """When FreeCAD isn't running, the probe should return False, not raise."""

    def test_ready_returns_false_on_closed_port(self) -> None:
        # Port 1 is reserved/closed on every Linux system.
        self.assertFalse(common.freecad_ready(host="127.0.0.1", port=1))

    def test_ready_with_import_step_returns_false_on_closed_port(self) -> None:
        self.assertFalse(
            common.freecad_ready_with_import_step(host="127.0.0.1", port=1)
        )

    def test_ready_returns_false_on_bad_host(self) -> None:
        # 192.0.2.0/24 is TEST-NET-1, reserved for documentation and
        # guaranteed unroutable.
        self.assertFalse(
            common.freecad_ready(host="192.0.2.1", port=9876, timeout=0.5)
        )


class TestBuildGeometryRequiresFreecad(unittest.TestCase):
    """build_geometry() should fail cleanly when the addon isn't reachable."""

    def test_raises_runtime_error_without_addon(self) -> None:
        with patch.object(common, "freecad_ready", return_value=False):
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(RuntimeError) as ctx:
                    common.build_geometry(
                        {"name": "probe", "build_type": "envelope"},
                        Path(tmp),
                    )
                self.assertIn("not reachable", str(ctx.exception))


class TestMetadataHelpers(unittest.TestCase):
    def test_read_metadata_roundtrip(self) -> None:
        payload = {
            "subsystem": "probe",
            "claimed_mass_kg": 0.042,
            "interface_actuals": {"ifc1": {"bore_dia": 8.005}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "metadata.json").write_text(json.dumps(payload))
            loaded = common.read_metadata(out)
            self.assertEqual(loaded["subsystem"], "probe")
            self.assertEqual(loaded["interface_actuals"]["ifc1"]["bore_dia"], 8.005)

    def test_read_metadata_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                common.read_metadata(Path(tmp))

    def test_override_claimed_measurements(self) -> None:
        original = {
            "subsystem": "probe",
            "interface_actuals": {"ifc1": {"bore_dia": 8.0}},
            "notes": "preserved",
        }
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "metadata.json").write_text(json.dumps(original))

            updated = common.override_claimed_measurements(
                out,
                {"ifc1": {"bore_dia": 8.2}},  # drifted
            )

            # Returned dict reflects the override...
            self.assertEqual(updated["interface_actuals"]["ifc1"]["bore_dia"], 8.2)
            # ...and so does the file on disk...
            on_disk = json.loads((out / "metadata.json").read_text())
            self.assertEqual(on_disk["interface_actuals"]["ifc1"]["bore_dia"], 8.2)
            # ...but other fields are preserved.
            self.assertEqual(on_disk["notes"], "preserved")
            self.assertEqual(on_disk["subsystem"], "probe")


if __name__ == "__main__":
    unittest.main()
