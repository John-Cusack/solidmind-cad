import unittest
from pathlib import Path

try:
    import jsonschema
except ModuleNotFoundError:
    jsonschema = None

from server.paths import repo_root


@unittest.skipIf(jsonschema is None, "jsonschema not installed")
class TestGIRSchema(unittest.TestCase):
    def setUp(self) -> None:
        schema_path = repo_root() / "schemas" / "gir.schema.json"
        self.schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "gir_version": {"type": "string"},
                "frames": {"type": "array"},
                "features": {"type": "array"},
                "metadata": {"type": "object"},
            },
            "required": ["gir_version"],
            "additionalProperties": False,
        }

    def test_valid_gir_structure(self) -> None:
        valid_gir = {
            "gir_version": "1.0",
            "frames": [],
            "features": [],
            "metadata": {},
        }
        validator = jsonschema.Draft202012Validator(self.schema)
        self.assertIsNone(validator.validate(valid_gir))

    def test_missing_version(self) -> None:
        invalid_gir = {
            "frames": [],
            "features": [],
        }
        validator = jsonschema.Draft202012Validator(self.schema)
        with self.assertRaises(jsonschema.ValidationError):
            validator.validate(invalid_gir)

    def test_additional_properties_rejected(self) -> None:
        invalid_gir = {
            "gir_version": "1.0",
            "frames": [],
            "features": [],
            "unexpected_field": "should_fail",
        }
        validator = jsonschema.Draft202012Validator(self.schema)
        with self.assertRaises(jsonschema.ValidationError):
            validator.validate(invalid_gir)


if __name__ == "__main__":
    unittest.main()
