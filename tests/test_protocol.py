"""Tests for the FreeCAD addon protocol module."""
from __future__ import annotations

import unittest

from freecad_addon.protocol import Command, Response, decode_line, encode_message


class TestCommand(unittest.TestCase):
    def test_to_json_minimal(self) -> None:
        cmd = Command(cmd="ping")
        j = cmd.to_json()
        self.assertIn('"cmd":"ping"', j)
        self.assertIn('"args":{}', j)

    def test_to_json_with_args(self) -> None:
        cmd = Command(cmd="new_document", args={"name": "Test"})
        j = cmd.to_json()
        self.assertIn('"cmd":"new_document"', j)
        self.assertIn('"name":"Test"', j)

    def test_from_json(self) -> None:
        cmd = Command.from_json('{"cmd": "pad", "args": {"length": 10.0}}')
        self.assertEqual(cmd.cmd, "pad")
        self.assertEqual(cmd.args["length"], 10.0)

    def test_from_json_no_args(self) -> None:
        cmd = Command.from_json('{"cmd": "ping"}')
        self.assertEqual(cmd.cmd, "ping")
        self.assertEqual(cmd.args, {})

    def test_roundtrip(self) -> None:
        original = Command(cmd="sketch_rect", args={"x": 0, "y": 0, "w": 100, "h": 50})
        restored = Command.from_json(original.to_json())
        self.assertEqual(original.cmd, restored.cmd)
        self.assertEqual(original.args, restored.args)


class TestResponse(unittest.TestCase):
    def test_success(self) -> None:
        r = Response.success({"name": "Body"})
        self.assertTrue(r.ok)
        self.assertEqual(r.result, {"name": "Body"})
        self.assertIsNone(r.error)

    def test_failure(self) -> None:
        r = Response.failure("Something broke")
        self.assertFalse(r.ok)
        self.assertIsNone(r.result)
        self.assertEqual(r.error, "Something broke")

    def test_success_to_json(self) -> None:
        r = Response.success({"pong": True})
        j = r.to_json()
        self.assertIn('"ok":true', j)
        self.assertIn('"result"', j)
        self.assertNotIn('"error"', j)

    def test_failure_to_json(self) -> None:
        r = Response.failure("bad")
        j = r.to_json()
        self.assertIn('"ok":false', j)
        self.assertIn('"error":"bad"', j)
        self.assertNotIn('"result"', j)

    def test_from_json_success(self) -> None:
        r = Response.from_json('{"ok": true, "result": {"x": 1}}')
        self.assertTrue(r.ok)
        self.assertEqual(r.result, {"x": 1})

    def test_from_json_failure(self) -> None:
        r = Response.from_json('{"ok": false, "error": "not found"}')
        self.assertFalse(r.ok)
        self.assertEqual(r.error, "not found")

    def test_roundtrip(self) -> None:
        original = Response.success({"features": [1, 2, 3]})
        restored = Response.from_json(original.to_json())
        self.assertEqual(original.ok, restored.ok)
        self.assertEqual(original.result, restored.result)


class TestEncodeDecode(unittest.TestCase):
    def test_encode_command(self) -> None:
        cmd = Command(cmd="ping")
        data = encode_message(cmd)
        self.assertTrue(data.endswith(b"\n"))
        self.assertIsInstance(data, bytes)

    def test_encode_response(self) -> None:
        resp = Response.success(None)
        data = encode_message(resp)
        self.assertTrue(data.endswith(b"\n"))

    def test_decode_line(self) -> None:
        data = b'{"cmd":"ping","args":{}}\n'
        result = decode_line(data)
        self.assertEqual(result["cmd"], "ping")


if __name__ == "__main__":
    unittest.main()
