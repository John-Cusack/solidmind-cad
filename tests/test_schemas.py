import json
import unittest

import jsonschema
import yaml

from server.geometry_planning import plan_geometry


class TestSchemaValidation(unittest.TestCase):
    """Validate all schema files using jsonschema."""

    @staticmethod
    def _sample_spec() -> dict:
        return {
            "meta": {"process": "cnc", "units": "mm"},
            "part": {"envelope": {"x": 100, "y": 50, "z": 20}},
            "geometry": {
                "hole_features": [
                    {
                        "id": "h1",
                        "diameter": {"value": 5, "unit": "mm"},
                        "depth": {"value": 20, "unit": "mm"},
                    }
                ]
            },
        }

    def test_gir_schema_valid_json(self) -> None:
        with open("schemas/gir.schema.json") as f:
            schema = json.load(f)
        self.assertIn("$schema", schema)
        self.assertIn("$defs", schema)

    def test_eir_schema_valid_json(self) -> None:
        with open("schemas/eir.schema.json") as f:
            schema = json.load(f)
        self.assertIn("$schema", schema)
        self.assertIn("$defs", schema)

    def test_shared_definitions_valid_json(self) -> None:
        with open("schemas/shared_definitions.json") as f:
            schema = json.load(f)
        self.assertIn("$schema", schema)
        self.assertIn("$defs", schema)

    def test_geometry_capabilities_schema_valid_json(self) -> None:
        with open("schemas/geometry_capabilities.schema.json") as f:
            schema = json.load(f)
        self.assertIn("$schema", schema)

    def test_verification_policy_schema_valid_json(self) -> None:
        with open("schemas/verification_policy.schema.json") as f:
            schema = json.load(f)
        self.assertIn("$schema", schema)

    def test_planning_policy_schema_valid_json(self) -> None:
        with open("schemas/planning_policy.schema.json") as f:
            schema = json.load(f)
        self.assertIn("$schema", schema)

    def test_planning_plan_schema_valid_json(self) -> None:
        with open("schemas/planning_plan.schema.json") as f:
            schema = json.load(f)
        self.assertIn("$schema", schema)

    def test_gir_schema_has_required_fields(self) -> None:
        with open("schemas/gir.schema.json") as f:
            schema = json.load(f)
        self.assertIn("$defs", schema)

    def test_eir_schema_has_required_fields(self) -> None:
        with open("schemas/eir.schema.json") as f:
            schema = json.load(f)
        self.assertIn("$defs", schema)

    def test_shared_definitions_quantity_schema(self) -> None:
        with open("schemas/shared_definitions.json") as f:
            schema = json.load(f)

        self.assertIn("$defs", schema)
        self.assertIn("quantity", schema["$defs"])
        self.assertIn("type", schema["$defs"]["quantity"])
        self.assertIn("properties", schema["$defs"]["quantity"])

    def test_shared_definitions_point3d_schema(self) -> None:
        with open("schemas/shared_definitions.json") as f:
            schema = json.load(f)

        self.assertIn("$defs", schema)
        self.assertIn("point3d", schema["$defs"])

    def test_generated_gir_instance_matches_core_contract(self) -> None:
        with open("schemas/gir.schema.json") as f:
            schema = json.load(f)

        instance = plan_geometry(self._sample_spec())["gir"]
        self.assertIn("gir_version", instance)
        self.assertIn("frames", instance)
        self.assertIn("features", instance)
        self.assertIn("metadata", instance)
        self.assertIsInstance(instance["frames"], list)
        self.assertIsInstance(instance["features"], list)
        jsonschema.validate(instance, schema)

    def test_generated_eir_instance_matches_core_contract(self) -> None:
        with open("schemas/eir.schema.json") as f:
            schema = json.load(f)

        instance = plan_geometry(self._sample_spec())["eir"]
        self.assertIn("eir_version", instance)
        self.assertIn("operations", instance)
        self.assertIn("dependency_graph", instance)
        self.assertIn("metadata", instance)
        self.assertIsInstance(instance["operations"], list)
        self.assertIsInstance(instance["dependency_graph"], dict)
        jsonschema.validate(instance, schema)

    def test_schema_version_present(self) -> None:
        yaml_files = [
            "feature_support/geometry_capabilities.yml",
            "feature_support/verification_policy.yml",
            "feature_support/planning_policy.yml",
        ]

        for fname in yaml_files:
            with open(fname) as f:
                data = yaml.safe_load(f)
            self.assertIn("version", data, f"{fname} missing version field")


if __name__ == "__main__":
    unittest.main()
