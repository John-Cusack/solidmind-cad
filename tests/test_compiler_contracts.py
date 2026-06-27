import unittest

from server.feature_support import load_geometry_capabilities
from server.geometry_compiler_freecad import CompilerStatus, FreeCADCompiler
from server.geometry_ir import EIRBuilder


class TestCompilerContracts(unittest.TestCase):
    """Test compiler contract compliance and capability checking."""

    def setUp(self) -> None:
        self.compiler = FreeCADCompiler()
        self.caps = load_geometry_capabilities()

    def test_compiler_initialization(self) -> None:
        self.assertIsNotNone(self.compiler._capabilities)
        if self.compiler._capabilities:
            self.assertEqual(self.compiler._capabilities.backend_name, "FreeCAD")

    def test_supports_operation_checks_capability_manifest(self) -> None:
        freecad_ops = list(self.caps.backends["freecad"].operations.keys())
        supported = ["pad", "pocket", "fillet", "chamfer"]
        for op in supported:
            self.assertIn(op, freecad_ops, f"{op} should be in capability manifest")

    def test_compile_empty_eir_returns_no_ops(self) -> None:
        empty_eir = EIRBuilder().build()
        result = self.compiler.compile_eir(empty_eir)

        self.assertEqual(result.status, CompilerStatus.COMPILED)
        self.assertIsNone(result.ops)

    def test_compile_single_operation(self) -> None:
        builder = EIRBuilder()
        builder.add_operation("pad", {"sketch": "S1", "length": 10.0})
        eir = builder.build()

        result = self.compiler.compile_eir(eir)

        self.assertEqual(result.status, CompilerStatus.COMPILED)

    def test_compile_sequence_with_dependencies(self) -> None:
        builder = EIRBuilder()
        builder.add_operation("pad", {"sketch": "S1", "length": 10.0})
        builder.add_operation("pocket", {"sketch": "S2", "depth": 5.0}, depends_on=["OP0"])
        eir = builder.build()

        result = self.compiler.compile_eir(eir)

        self.assertEqual(result.status, CompilerStatus.COMPILED)

    def test_compile_unsupported_operation(self) -> None:
        builder = EIRBuilder()
        builder.add_operation("custom_unsupported_op", {})
        eir = builder.build()

        result = self.compiler.compile_eir(eir)

        self.assertEqual(result.status, CompilerStatus.UNSUPPORTED)
        self.assertTrue(any("Unsupported operation type" in n.message for n in result.notices))

    def test_compile_preserves_operation_order(self) -> None:
        builder = EIRBuilder()
        builder.add_operation("pad", {})
        builder.add_operation("fillet", {})
        builder.add_operation("pocket", {})
        eir = builder.build()

        result = self.compiler.compile_eir(eir)

        op_ids = [op.id for op in result.ops] if result.ops else []
        self.assertEqual(op_ids, ["OP0", "OP1", "OP2"])

    def test_capability_backend_version_check(self) -> None:
        freecad_caps = self.caps.backends.get("freecad")
        self.assertIsNotNone(freecad_caps)
        if freecad_caps:
            self.assertIsNotNone(freecad_caps.backend_version)

    def test_reference_binding_quality_reported(self) -> None:
        freecad_caps = self.caps.backends.get("freecad")
        self.assertIsNotNone(freecad_caps)
        valid_qualities = ["full", "partial", "none", "high", "medium", "low"]
        if freecad_caps:
            self.assertIn(
                freecad_caps.reference_behavior.rebinding_quality,
                valid_qualities,
            )

    def test_compile_sweep_operation(self) -> None:
        builder = EIRBuilder()
        builder.add_operation(
            "sweep",
            {
                "profile_sketch": "Sketch",
                "spine_sketch": "Sketch001",
                "subtractive": False,
            },
        )
        eir = builder.build()

        result = self.compiler.compile_eir(eir)

        self.assertIn(result.status, [CompilerStatus.COMPILED, CompilerStatus.LOWERED])
        self.assertIsNotNone(result.ops)
        self.assertEqual(result.ops[0].op_type, "cad.sweep")

    def test_compile_loft_operation(self) -> None:
        builder = EIRBuilder()
        builder.add_operation(
            "loft",
            {
                "sketches": ["Sketch", "Sketch001"],
                "ruled": False,
                "closed": False,
                "subtractive": False,
            },
        )
        eir = builder.build()

        result = self.compiler.compile_eir(eir)

        self.assertIn(result.status, [CompilerStatus.COMPILED, CompilerStatus.LOWERED])
        self.assertIsNotNone(result.ops)
        self.assertEqual(result.ops[0].op_type, "cad.loft")

    def test_compile_subtractive_sweep(self) -> None:
        builder = EIRBuilder()
        builder.add_operation(
            "sweep",
            {
                "profile_sketch": "Sketch",
                "spine_sketch": "Sketch001",
                "subtractive": True,
            },
        )
        eir = builder.build()

        result = self.compiler.compile_eir(eir)

        self.assertIn(result.status, [CompilerStatus.COMPILED, CompilerStatus.LOWERED])
        self.assertIsNotNone(result.ops)
        self.assertEqual(result.ops[0].op_type, "cad.sweep")

    def test_compile_with_all_supported_ops(self) -> None:
        freecad_ops = list(self.caps.backends["freecad"].operations.keys())

        builder = EIRBuilder()
        for _idx, op in enumerate(freecad_ops[:5]):
            builder.add_operation(op, {})

        eir = builder.build()
        result = self.compiler.compile_eir(eir)

        self.assertIn(
            result.status,
            [
                CompilerStatus.COMPILED,
                CompilerStatus.LOWERED,
                CompilerStatus.UNSUPPORTED,
            ],
        )


if __name__ == "__main__":
    unittest.main()
