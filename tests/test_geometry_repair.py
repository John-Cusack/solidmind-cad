from __future__ import annotations

import unittest

from server.geometry_executor import ExecutionStep, ExecutionTrace
from server.geometry_ir import CompiledOp
from server.geometry_repair import recommend_repairs
from server.geometry_verify import VerificationReport, VerificationResult


class TestGeometryRepair(unittest.TestCase):
    def test_recommend_repairs_from_failures(self) -> None:
        step = ExecutionStep(
            op=CompiledOp(id="OP1", op_type="cad.fillet", inputs={}),
            status="failed",
            output={"status": "error"},
        )
        trace = ExecutionTrace(steps=[step])

        report = VerificationReport(
            results=[
                VerificationResult(
                    check_id="internal_radius",
                    check_type="internal_radius",
                    passed=False,
                    severity="warning",
                    message="too small",
                )
            ],
            notices=[],
            passed=False,
            report_hash="",
            metadata={},
        )

        recs = recommend_repairs(
            execution_trace=trace,
            verification_report=report,
            planning_plan=None,
        )
        self.assertTrue(recs)
        ids = {r.playbook_id for r in recs}
        self.assertIn("topology_drift_cascade", ids)
        self.assertIn("fillet_failure", ids)


if __name__ == "__main__":
    unittest.main()
