"""Tests for the cad.measure_between server bridge."""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch


class TestMeasureBetween(unittest.TestCase):
    @patch("server.tools_cad.get_client")
    def test_body_to_body(self, mock_get_client: Any) -> None:
        from server.tools_cad import cad_measure_between

        mock_client = mock_get_client.return_value
        mock_client.send_command.return_value = {
            "distance_mm": 25.0,
            "point_a": [10.0, 0.0, 0.0],
            "point_b": [35.0, 0.0, 0.0],
        }

        result = cad_measure_between(ref_a="Body", ref_b="Body001")
        self.assertTrue(result["ok"])
        self.assertEqual(result["distance_mm"], 25.0)
        mock_client.send_command.assert_called_once_with(
            "measure_between",
            ref_a="Body",
            ref_b="Body001",
        )

    @patch("server.tools_cad.get_client")
    def test_point_to_body(self, mock_get_client: Any) -> None:
        from server.tools_cad import cad_measure_between

        mock_client = mock_get_client.return_value
        mock_client.send_command.return_value = {
            "distance_mm": 5.0,
            "point_a": [0.0, 0.0, 0.0],
            "point_b": [5.0, 0.0, 0.0],
        }

        result = cad_measure_between(ref_a=[0.0, 0.0, 0.0], ref_b="Body")
        self.assertTrue(result["ok"])
        self.assertEqual(result["distance_mm"], 5.0)
        mock_client.send_command.assert_called_once_with(
            "measure_between",
            ref_a=[0.0, 0.0, 0.0],
            ref_b="Body",
        )

    @patch("server.tools_cad.get_client")
    def test_face_reference(self, mock_get_client: Any) -> None:
        from server.tools_cad import cad_measure_between

        mock_client = mock_get_client.return_value
        mock_client.send_command.return_value = {
            "distance_mm": 12.5,
            "point_a": [0.0, 0.0, 10.0],
            "point_b": [0.0, 0.0, 22.5],
        }

        result = cad_measure_between(ref_a="Body.Face3", ref_b="Body001.Face1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["distance_mm"], 12.5)
        mock_client.send_command.assert_called_once_with(
            "measure_between",
            ref_a="Body.Face3",
            ref_b="Body001.Face1",
        )

    @patch("server.tools_cad.get_client")
    def test_with_doc(self, mock_get_client: Any) -> None:
        from server.tools_cad import cad_measure_between

        mock_client = mock_get_client.return_value
        mock_client.send_command.return_value = {
            "distance_mm": 10.0,
            "point_a": [0, 0, 0],
            "point_b": [10, 0, 0],
        }

        result = cad_measure_between(ref_a="Body", ref_b="Body001", doc="MyDoc")
        self.assertTrue(result["ok"])
        mock_client.send_command.assert_called_once_with(
            "measure_between",
            ref_a="Body",
            ref_b="Body001",
            doc="MyDoc",
        )


if __name__ == "__main__":
    unittest.main()
