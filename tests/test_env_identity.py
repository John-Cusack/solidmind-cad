"""Tests for environment identity capture."""

from __future__ import annotations

import re
import unittest

from server import env_identity, jcs


class TestEnvironmentIdentity(unittest.TestCase):
    def test_identity_shape(self) -> None:
        ident = env_identity.environment_identity()
        self.assertIn("python", ident)
        self.assertIn("implementation", ident)
        self.assertEqual(set(ident["platform"].keys()), {"system", "machine", "release"})
        self.assertIsInstance(ident["packages"], dict)
        self.assertIsInstance(ident["solver_builds"], dict)

    def test_identity_is_jcs_canonicalizable(self) -> None:
        jcs.canonicalize(env_identity.environment_identity())

    def test_hash_stable_within_process(self) -> None:
        h1 = env_identity.environment_identity_hash()
        h2 = env_identity.environment_identity_hash()
        self.assertEqual(h1, h2)
        self.assertTrue(re.match(r"^[0-9a-f]{64}$", h1))


if __name__ == "__main__":
    unittest.main()
