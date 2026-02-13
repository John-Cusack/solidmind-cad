import unittest
from unittest.mock import MagicMock, patch

from server.geometry_ir import EIRBuilder, CompiledOp, Invariant
from server.geometry_executor import Executor, ExecutionStep, ExecutionTrace


class TestExecutorBehavior(unittest.TestCase):
    """Test executor execution behavior."""

    def setUp(self) -> None:
        self.executor = Executor()

    def test_executor_initialization(self) -> None:
        self.assertEqual(len(self.executor._steps), 0)
        self.assertEqual(len(self.executor._notices), 0)

    def test_execute_single_operation(self) -> None:
        builder = EIRBuilder()
        builder.add_operation("pad", {"sketch": "S1"})
        eir = builder.build()

        trace = self.executor.execute_plan(eir.operations, backend="freecad")

        self.assertEqual(len(trace.steps), 1)
        self.assertEqual(trace.steps[0].status, "completed")

    def test_execute_plan_preserves_order(self) -> None:
        builder = EIRBuilder()
        for i in range(3):
            builder.add_operation("pad", {})
        eir = builder.build()

        trace = self.executor.execute_plan(eir.operations, backend="freecad")

        self.assertEqual(len(trace.steps), 3)
        op_ids = [step.op.id for step in trace.steps]
        self.assertEqual(op_ids, ["OP0", "OP1", "OP2"])

    def test_execute_operation_with_invariants(self) -> None:
        builder = EIRBuilder()
        builder.add_operation(
            "pad",
            {"sketch": "S1"},
            invariants=[Invariant(type="valid_geometry", threshold=0.0)],
        )
        eir = builder.build()

        trace = self.executor.execute_plan(eir.operations, backend="freecad")

        self.assertEqual(len(trace.steps), 1)
        self.assertEqual(trace.steps[0].status, "completed")

    def test_execute_operation_failure_handling(self) -> None:
        with patch.object(self.executor, "_execute_operation") as mock_exec:
            mock_exec.return_value = ExecutionStep(
                op=CompiledOp(id="OP0", op_type="pad", inputs={}),
                status="failed",
                output={"error": "simulated"},
            )

            builder = EIRBuilder()
            builder.add_operation("pad", {})
            eir = builder.build()

            trace = self.executor.execute_plan(eir.operations, backend="freecad")

            self.assertEqual(len(trace.steps), 1)

    def test_reference_map_populated_during_execution(self) -> None:
        builder = EIRBuilder()
        builder.add_operation("pad", {"sketch": "S1"})
        eir = builder.build()

        trace = self.executor.execute_plan(eir.operations, backend="freecad")

        self.assertIsNotNone(trace.reference_map)

    def test_notices_collected_during_execution(self) -> None:
        builder = EIRBuilder()
        builder.add_operation("pad", {})
        eir = builder.build()

        trace = self.executor.execute_plan(eir.operations, backend="freecad")

        self.assertIsNotNone(trace.notices)


if __name__ == "__main__":
    unittest.main()
