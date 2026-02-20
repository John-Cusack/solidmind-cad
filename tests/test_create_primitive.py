"""Tests for cad.create_primitive and cad.create_primitives MCP tools.

Mock-based tests following the pattern in tests/test_tools_cad.py.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from server.freecad_client import FreeCADCommandError
from server.tools_cad import cad_create_primitive, cad_create_primitives


def _mock_client() -> MagicMock:
    """Create a mock FreeCAD client."""
    client = MagicMock()
    client.is_connected = True
    return client


class TestCadCreatePrimitive(unittest.TestCase):
    """Tests for cad_create_primitive bridge function."""

    @patch("server.tools_cad.get_client")
    def test_box_basic(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "body": "Servo1",
            "pad": "Pad",
            "sketch": "Sketch",
            "position": [0.0, 0.0, 0.0],
            "rotation_angle_deg": 0.0,
            "rotation_axis": [0.0, 0.0, 1.0],
            "bbox_mm": [32.0, 24.0, 24.0],
        }
        mock_get.return_value = client

        result = cad_create_primitive(
            name="Servo1",
            shape="box",
            dimensions={"length": 32, "width": 24, "height": 24},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["body"], "Servo1")
        client.send_command.assert_called_once_with(
            "create_primitive",
            name="Servo1",
            shape="box",
            dimensions={"length": 32, "width": 24, "height": 24},
            verify=False,
            rotation_angle_deg=0.0,
        )

    @patch("server.tools_cad.get_client")
    def test_cylinder_basic(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "body": "Standoff1",
            "pad": "Pad",
            "sketch": "Sketch",
            "position": [0.0, 0.0, 0.0],
            "rotation_angle_deg": 0.0,
            "rotation_axis": [0.0, 0.0, 1.0],
            "bbox_mm": [10.0, 10.0, 20.0],
        }
        mock_get.return_value = client

        result = cad_create_primitive(
            name="Standoff1",
            shape="cylinder",
            dimensions={"radius": 5, "height": 20},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["body"], "Standoff1")
        client.send_command.assert_called_once_with(
            "create_primitive",
            name="Standoff1",
            shape="cylinder",
            dimensions={"radius": 5, "height": 20},
            verify=False,
            rotation_angle_deg=0.0,
        )

    @patch("server.tools_cad.get_client")
    def test_with_placement(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "body": "Servo1",
            "pad": "Pad",
            "sketch": "Sketch",
            "position": [70.0, 75.0, 158.0],
            "rotation_angle_deg": 47.0,
            "rotation_axis": [0.0, 0.0, 1.0],
            "bbox_mm": [32.0, 24.0, 24.0],
        }
        mock_get.return_value = client

        result = cad_create_primitive(
            name="Servo1",
            shape="box",
            dimensions={"length": 32, "width": 24, "height": 24},
            position=[70, 75, 158],
            rotation_axis=[0, 0, 1],
            rotation_angle_deg=47,
        )

        self.assertTrue(result["ok"])
        call_kwargs = client.send_command.call_args[1]
        self.assertEqual(call_kwargs["position"], [70, 75, 158])
        self.assertEqual(call_kwargs["rotation_axis"], [0, 0, 1])
        self.assertEqual(call_kwargs["rotation_angle_deg"], 47)

    @patch("server.tools_cad.get_client")
    def test_default_verify_false(self, mock_get: MagicMock) -> None:
        """Single primitive defaults to verify=False."""
        client = _mock_client()
        client.send_command.return_value = {"body": "B", "pad": "Pad", "sketch": "Sketch"}
        mock_get.return_value = client

        cad_create_primitive(name="B", shape="box", dimensions={"length": 1, "width": 1, "height": 1})

        call_kwargs = client.send_command.call_args[1]
        self.assertFalse(call_kwargs["verify"])

    @patch("server.tools_cad.get_client")
    def test_command_error(self, mock_get: MagicMock) -> None:
        """FreeCADCommandError is wrapped into error result."""
        client = _mock_client()
        client.send_command.side_effect = FreeCADCommandError("bad shape")
        mock_get.return_value = client

        result = cad_create_primitive(
            name="X",
            shape="box",
            dimensions={"length": 1, "width": 1, "height": 1},
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "COMMAND_ERROR")


class TestCadCreatePrimitives(unittest.TestCase):
    """Tests for cad_create_primitives bridge function."""

    @patch("server.tools_cad.get_client")
    def test_batch_creates_multiple(self, mock_get: MagicMock) -> None:
        client = _mock_client()
        client.send_command.return_value = {
            "created": [
                {"body": "A", "pad": "Pad", "sketch": "Sketch"},
                {"body": "B", "pad": "Pad001", "sketch": "Sketch001"},
                {"body": "C", "pad": "Pad002", "sketch": "Sketch002"},
            ],
            "failed": [],
        }
        mock_get.return_value = client

        items = [
            {"name": "A", "shape": "box", "dimensions": {"length": 10, "width": 10, "height": 10}},
            {"name": "B", "shape": "box", "dimensions": {"length": 20, "width": 20, "height": 20}},
            {"name": "C", "shape": "cylinder", "dimensions": {"radius": 5, "height": 15}},
        ]
        result = cad_create_primitives(items=items)

        self.assertTrue(result["ok"])
        self.assertEqual(len(result["created"]), 3)
        self.assertEqual(len(result["failed"]), 0)
        client.send_command.assert_called_once()

    @patch("server.tools_cad.get_client")
    def test_batch_timeout_scales(self, mock_get: MagicMock) -> None:
        """Timeout should scale with number of items: max(30, 30 + N*5)."""
        client = _mock_client()
        client.send_command.return_value = {"created": [], "failed": []}
        mock_get.return_value = client

        items = [
            {"name": f"P{i}", "shape": "box", "dimensions": {"length": 1, "width": 1, "height": 1}}
            for i in range(10)
        ]
        cad_create_primitives(items=items)

        call_kwargs = client.send_command.call_args[1]
        # timeout = max(30, 30 + 10*5) = 80
        self.assertEqual(call_kwargs["timeout"], 80.0)

    @patch("server.tools_cad.get_client")
    def test_batch_default_verify_true(self, mock_get: MagicMock) -> None:
        """Batch defaults to verify=True."""
        client = _mock_client()
        client.send_command.return_value = {"created": [], "failed": []}
        mock_get.return_value = client

        items = [{"name": "A", "shape": "box", "dimensions": {"length": 1, "width": 1, "height": 1}}]
        cad_create_primitives(items=items)

        call_kwargs = client.send_command.call_args[1]
        self.assertTrue(call_kwargs["verify"])


if __name__ == "__main__":
    unittest.main()
