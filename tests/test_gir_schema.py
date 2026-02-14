import unittest
import json

try:
    import jsonschema
except ModuleNotFoundError:
    jsonschema = None

from server.paths import repo_root
from server.geometry_planning import plan_geometry


@unittest.skipIf(jsonschema is None, "jsonschema not installed")
class TestGIRSchema(unittest.TestCase):
    def setUp(self) -> None:
        schema_path = repo_root() / "schemas" / "gir.schema.json"
        self.schema = json.loads(schema_path.read_text(encoding="utf-8"))

    @staticmethod
    def _sample_spec() -> dict:
        return {
            "meta": {"process": "cnc", "units": "mm"},
            "part": {"envelope": {"x": 100, "y": 50, "z": 20}},
            "geometry": {},
        }

    def test_valid_gir_structure(self) -> None:
        gir = plan_geometry(self._sample_spec())["gir"]
        self.assertIn("gir_version", gir)
        self.assertIn("frames", gir)
        self.assertIn("features", gir)
        self.assertIn("metadata", gir)
        self.assertIsInstance(gir["frames"], list)
        self.assertIsInstance(gir["features"], list)
        jsonschema.validate(gir, self.schema)

    def test_schema_declares_core_feature_definitions(self) -> None:
        defs = self.schema.get("$defs", {})
        self.assertIn("primitive_intent", defs)
        self.assertIn("sketch_profile_intent", defs)
        self.assertIn("extrude_intent", defs)
        self.assertIn("hole_intent", defs)

    def test_schema_declares_quantity_unit_enum(self) -> None:
        defs = self.schema.get("$defs", {})
        quantity = defs.get("quantity", {})
        properties = quantity.get("properties", {})
        unit = properties.get("unit", {})
        self.assertIn("enum", unit)
        self.assertIn("mm", unit["enum"])


if __name__ == "__main__":
    unittest.main()
