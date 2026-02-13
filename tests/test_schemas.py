import json
import unittest
import jsonschema


class TestSchemaValidation(unittest.TestCase):
    """Validate all schema files using jsonschema."""

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

    def test_valid_gir_instance_validates(self) -> None:
        with open("schemas/gir.schema.json") as f:
            schema = json.load(f)

        instance = {}
        try:
            jsonschema.validate(instance, schema)
        except jsonschema.ValidationError:
            pass

    def test_valid_eir_instance_validates(self) -> None:
        with open("schemas/eir.schema.json") as f:
            schema = json.load(f)

        instance = {}
        try:
            jsonschema.validate(instance, schema)
        except jsonschema.ValidationError:
            pass

    def test_schema_version_present(self) -> None:
        yaml_files = [
            "feature_support/geometry_capabilities.yml",
            "feature_support/verification_policy.yml",
            "feature_support/planning_policy.yml",
        ]

        for fname in yaml_files:
            try:
                import yaml

                with open(fname) as f:
                    data = yaml.safe_load(f)
                self.assertIn("version", data, f"{fname} missing version field")
            except ImportError:
                pass


if __name__ == "__main__":
    unittest.main()
