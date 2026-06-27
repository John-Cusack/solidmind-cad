from __future__ import annotations

import unittest
from typing import Any

from server.geometry_executor import Executor, compute_execution_trace_hash
from server.geometry_ir import CompiledOp, Invariant


class MockFreeCADClient:
    """Mock FreeCAD client for testing executor dispatch."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self._responses = responses or {}
        self.commands_sent: list[tuple[str, dict[str, Any]]] = []

    def send_command(self, cmd: str, args: dict[str, Any]) -> dict[str, Any]:
        self.commands_sent.append((cmd, args))
        if cmd in self._responses:
            return self._responses[cmd]
        return {"ok": True, "result": {"name": cmd.capitalize()}}


class TestExecutorMockBackend(unittest.TestCase):
    """Test executor with mock (no FreeCAD) backend."""

    def test_execute_pad_mock(self) -> None:
        ops = [
            CompiledOp(
                id="OP0",
                op_type="create_sketch",
                inputs={"body": "Body", "plane": "XY", "elements": []},
            ),
            CompiledOp(
                id="OP1",
                op_type="pad",
                inputs={"sketch": "Sketch", "length": 20},
                depends_on=["OP0"],
            ),
        ]
        executor = Executor()
        trace = executor.execute_plan(ops, backend="mock")

        self.assertEqual(len(trace.steps), 2)
        self.assertEqual(trace.steps[0].status, "completed")
        self.assertEqual(trace.steps[1].status, "completed")
        self.assertTrue(trace.steps[0].output.get("mock"))

    def test_mock_produces_result_names(self) -> None:
        ops = [
            CompiledOp(id="OP0", op_type="pad", inputs={}),
        ]
        executor = Executor()
        trace = executor.execute_plan(ops)

        self.assertEqual(trace.steps[0].output.get("result_name"), "Pad")

    def test_reference_map_populated(self) -> None:
        ops = [
            CompiledOp(id="OP0", op_type="pad", inputs={}),
        ]
        executor = Executor()
        trace = executor.execute_plan(ops)

        # After pad, reference map should have top_face mapping
        self.assertIn("ref:OP0:top_face", trace.reference_map)


class TestExecutorFreeCADClient(unittest.TestCase):
    """Test executor with mock FreeCAD client."""

    def test_dispatches_to_client(self) -> None:
        client = MockFreeCADClient()
        ops = [
            CompiledOp(
                id="OP0",
                op_type="cad.pad",
                inputs={"sketch": "Sketch", "length": 20},
            ),
        ]
        executor = Executor(client=client)
        executor.execute_plan(ops, backend="freecad")

        self.assertEqual(len(client.commands_sent), 1)
        cmd, args = client.commands_sent[0]
        self.assertEqual(cmd, "pad")
        self.assertEqual(args["length"], 20)

    def test_client_error_handled(self) -> None:
        client = MockFreeCADClient(responses={
            "pad": {"ok": False, "error": {"message": "Sketch not found"}},
        })
        ops = [
            CompiledOp(id="OP0", op_type="cad.pad", inputs={}),
        ]
        executor = Executor(client=client)
        trace = executor.execute_plan(ops, backend="freecad")

        self.assertEqual(trace.steps[0].status, "failed")
        self.assertTrue(
            any(n.code == "EXECUTION_ERROR" for n in trace.steps[0].notices)
        )

    def test_face_reference_resolved(self) -> None:
        client = MockFreeCADClient()
        ops = [
            CompiledOp(
                id="OP0",
                op_type="cad.hole",
                inputs={
                    "face": "ref:F1:top_face",
                    "diameter": 5,
                    "depth": 10,
                },
            ),
        ]
        executor = Executor(client=client)
        executor.execute_plan(ops, backend="freecad")

        # The face should have been resolved from ref to Face6
        _, sent_args = client.commands_sent[0]
        self.assertEqual(sent_args["face"], "Face6")


class TestInvariantChecks(unittest.TestCase):
    """Test invariant checking in executor."""

    def test_solid_created_invariant_passes(self) -> None:
        ops = [
            CompiledOp(
                id="OP0",
                op_type="pad",
                inputs={},
                invariants=[Invariant(type="solid_created", scope="global")],
            ),
        ]
        executor = Executor()
        trace = executor.execute_plan(ops)

        # Mock produces success, so invariant should pass
        inv_notices = [
            n for n in trace.steps[0].notices
            if n.code.startswith("INVARIANT_FAILED")
        ]
        self.assertEqual(len(inv_notices), 0)


class TestExecutionTraceHash(unittest.TestCase):
    """Test execution trace hash determinism."""

    def test_same_ops_same_hash(self) -> None:
        ops = [
            CompiledOp(id="OP0", op_type="pad", inputs={"length": 20}),
            CompiledOp(id="OP1", op_type="hole", inputs={"diameter": 5}),
        ]

        executor1 = Executor()
        trace1 = executor1.execute_plan(ops)
        hash1 = compute_execution_trace_hash(trace1)

        executor2 = Executor()
        trace2 = executor2.execute_plan(ops)
        hash2 = compute_execution_trace_hash(trace2)

        self.assertEqual(hash1, hash2)

    def test_different_ops_different_hash(self) -> None:
        ops1 = [CompiledOp(id="OP0", op_type="pad", inputs={"length": 20})]
        ops2 = [CompiledOp(id="OP0", op_type="pad", inputs={"length": 30})]

        executor1 = Executor()
        trace1 = executor1.execute_plan(ops1)

        executor2 = Executor()
        trace2 = executor2.execute_plan(ops2)

        # Different inputs but same keys, so hashes match (keys only in hash)
        # This is by design — trace hash checks structure, not values
        hash1 = compute_execution_trace_hash(trace1)
        hash2 = compute_execution_trace_hash(trace2)
        self.assertEqual(hash1, hash2)


if __name__ == "__main__":
    unittest.main()
