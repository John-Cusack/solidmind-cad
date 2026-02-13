import unittest

from server.feature_support import load_geometry_capabilities, load_verification_policy


class TestCapabilityManifest(unittest.TestCase):
    def test_load_geometry_capabilities(self) -> None:
        caps = load_geometry_capabilities()

        self.assertEqual(caps.version, "1.0")
        self.assertIn("freecad", caps.backends)

        freecad = caps.backends["freecad"]
        self.assertEqual(freecad.backend_name, "FreeCAD")

        self.assertIn("pad", freecad.operations)
        pad_op = freecad.operations["pad"]
        self.assertEqual(pad_op.status, "Yes")
        self.assertEqual(pad_op.stability, "stable")

    def test_verification_policy_load(self) -> None:
        policy = load_verification_policy()

        self.assertEqual(policy.version, "1.0")
        self.assertIn("cnc_aluminum", policy.policies)
        self.assertIn("print_3d_pla", policy.policies)

        cnc_policy = policy.policies["cnc_aluminum"]
        self.assertEqual(cnc_policy.process, "cnc")
        self.assertEqual(cnc_policy.material_family, "aluminum")
        self.assertGreater(cnc_policy.thresholds.wall_thickness_min_mm, 0)

        self.assertIn("THIN_WALL", policy.notice_severity_mapping)

    def test_backend_op_capability_structure(self) -> None:
        caps = load_geometry_capabilities()

        for backend_key, backend in caps.backends.items():
            self.assertIsNotNone(backend.backend_name)
            self.assertIsNotNone(backend.backend_version)
            self.assertIsInstance(backend.operations, dict)
            self.assertIsInstance(backend.reference_behavior.rebinding_quality, str)

    def test_verification_baseline_checks(self) -> None:
        policy = load_verification_policy()

        for policy_key, pol in policy.policies.items():
            self.assertGreater(len(pol.baseline_checks), 0)

            enabled_checks = [c for c in pol.baseline_checks if c.enabled]
            self.assertGreater(
                len(enabled_checks),
                0,
                f"Policy {policy_key} has no enabled baseline checks",
            )

            for check in pol.baseline_checks:
                self.assertIsNotNone(check.check_id)
                self.assertIsNotNone(check.check_type)


if __name__ == "__main__":
    unittest.main()
