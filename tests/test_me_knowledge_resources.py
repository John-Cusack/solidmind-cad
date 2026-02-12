from __future__ import annotations

import unittest

from server.resources import list_resources, read_resource


ME_KNOWLEDGE_URIS = [
    "resource://me_knowledge/index.yml",
    "resource://me_knowledge/domain_tags.yml",
    "resource://me_knowledge/archetypes/turbocharger_turbine_wheel_v1.yml",
    "resource://me_knowledge/constraint_templates/turbocharger_turbine_wheel_v1.yml",
    "resource://me_knowledge/standards_sources.yml",
]


class TestMEKnowledgeResources(unittest.TestCase):
    def test_resources_registered(self) -> None:
        uris = {entry["uri"] for entry in list_resources()}
        for uri in ME_KNOWLEDGE_URIS:
            self.assertIn(uri, uris)

    def test_resources_readable(self) -> None:
        for uri in ME_KNOWLEDGE_URIS:
            with self.subTest(uri=uri):
                payload = read_resource(uri)
                self.assertEqual(payload["uri"], uri)
                self.assertEqual(payload["mimeType"], "text/yaml")
                self.assertGreater(len(payload["text"]), 0)


if __name__ == "__main__":
    unittest.main()
