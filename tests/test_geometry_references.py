from __future__ import annotations

import unittest

from server.geometry_ir import ReferenceToken
from server.geometry_references import ReferenceResolver, _parse_ref_string


class TestParseRefString(unittest.TestCase):
    """Test reference string parsing."""

    def test_valid_ref_string(self) -> None:
        token = _parse_ref_string("ref:F1:top_face")
        self.assertIsNotNone(token)
        self.assertEqual(token.token, "ref:F1:top_face")
        self.assertEqual(token.origin_op_id, "F1")
        self.assertEqual(token.selector, {"type": "top_face"})

    def test_invalid_ref_string(self) -> None:
        token = _parse_ref_string("Face6")
        self.assertIsNone(token)

    def test_short_ref_string(self) -> None:
        token = _parse_ref_string("ref:F1")
        self.assertIsNone(token)


class TestReferenceResolver(unittest.TestCase):
    """Test reference resolution."""

    def setUp(self) -> None:
        self.resolver = ReferenceResolver()

    def test_resolve_top_face(self) -> None:
        token = ReferenceToken(
            token="ref:OP1:top_face",
            origin_op_id="OP1",
        )
        result = self.resolver.resolve(token)
        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.selected, "Face6")
        self.assertEqual(result.drift_class, "none")

    def test_resolve_top_face_with_context(self) -> None:
        token = ReferenceToken(
            token="ref:OP1:top_face",
            origin_op_id="OP1",
        )
        result = self.resolver.resolve(token, context={"top_face": "Face8"})
        self.assertEqual(result.selected, "Face8")

    def test_resolve_bottom_face(self) -> None:
        token = ReferenceToken(
            token="ref:OP1:bottom_face",
            origin_op_id="OP1",
        )
        result = self.resolver.resolve(token)
        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.selected, "Face1")

    def test_resolve_vertical_edges(self) -> None:
        token = ReferenceToken(
            token="ref:OP1:vertical_edges",
            origin_op_id="OP1",
        )
        edges = ["Edge1", "Edge3", "Edge5", "Edge7"]
        result = self.resolver.resolve(token, context={"vertical_edges": edges})
        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.matches, edges)

    def test_cached_resolution(self) -> None:
        token = ReferenceToken(
            token="ref:OP1:top_face",
            origin_op_id="OP1",
        )
        r1 = self.resolver.resolve(token, context={"top_face": "Face10"})
        self.assertEqual(r1.selected, "Face10")

        # Second resolve should use cache
        r2 = self.resolver.resolve(token, context={"top_face": "Face99"})
        self.assertEqual(r2.selected, "Face10")  # cached value

    def test_reference_map_populated(self) -> None:
        token = ReferenceToken(
            token="ref:OP1:top_face",
            origin_op_id="OP1",
        )
        self.resolver.resolve(token)
        self.assertIn("ref:OP1:top_face", self.resolver.reference_map)

    def test_resolve_face_ref_string(self) -> None:
        result = self.resolver.resolve_face_ref("ref:F1:top_face")
        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.selected, "Face6")

    def test_resolve_face_ref_invalid(self) -> None:
        result = self.resolver.resolve_face_ref("not_a_ref")
        self.assertEqual(result.status, "unresolved")

    def test_resolve_direct_edge_name(self) -> None:
        results = self.resolver.resolve_edge_refs(["Edge1", "Edge3"])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].selected, "Edge1")
        self.assertEqual(results[1].selected, "Edge3")

    def test_register_result_creates_mappings(self) -> None:
        self.resolver.register_result("OP0", {"result_name": "Pad"})
        self.assertIn("ref:OP0:top_face", self.resolver.reference_map)

    def test_unresolved_reference(self) -> None:
        token = ReferenceToken(
            token="ref:OP1:unknown_selector",
            origin_op_id="OP1",
        )
        result = self.resolver.resolve(token)
        self.assertEqual(result.status, "unresolved")
        self.assertEqual(result.drift_class, "unresolved")


class TestInvariantBasedResolution(unittest.TestCase):
    """Test invariant-based reference resolution."""

    def test_resolve_by_normal_invariant(self) -> None:
        resolver = ReferenceResolver()
        token = ReferenceToken(
            token="ref:OP1:custom",
            origin_op_id="OP1",
            invariants={"normal": [0, 0, 1]},
        )
        context = {
            "topology": {
                "faces": {
                    "Face1": {"normal": [0, 0, -1]},
                    "Face6": {"normal": [0, 0, 1]},
                },
            },
        }
        result = resolver.resolve(token, context)
        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.selected, "Face6")

    def test_no_match_major_drift(self) -> None:
        resolver = ReferenceResolver()
        token = ReferenceToken(
            token="ref:OP1:custom",
            origin_op_id="OP1",
            invariants={"normal": [1, 0, 0]},
        )
        context = {
            "topology": {
                "faces": {
                    "Face1": {"normal": [0, 0, -1]},
                    "Face6": {"normal": [0, 0, 1]},
                },
            },
        }
        result = resolver.resolve(token, context)
        self.assertEqual(result.status, "unresolved")
        self.assertEqual(result.drift_class, "major")


if __name__ == "__main__":
    unittest.main()
