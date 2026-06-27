import unittest

from server.geometry_ir import Quantity, compute_gir_hash


class TestIRHashing(unittest.TestCase):
    def test_quantity_without_tolerance(self) -> None:
        q1 = Quantity(100.0, "mm")
        q2 = Quantity(100.0, "mm")
        self.assertEqual(q1, q2)

    def test_quantity_with_tolerance(self) -> None:
        from server.geometry_ir import Tolerance

        tol = Tolerance(model="plus_minus", value=0.1)
        q1 = Quantity(100.0, "mm", tol)
        q2 = Quantity(100.0, "mm", tol)
        self.assertEqual(q1, q2)

    def test_ir_hash_stability(self) -> None:
        from server.geometry_ir import GIRBuilder

        builder1 = GIRBuilder()
        frame1 = builder1.add_global_frame()
        dims = {"length": Quantity(100.0, "mm"), "width": Quantity(50.0, "mm")}
        builder1.add_primitive("box", dims, frame_id=frame1)
        gir1 = builder1.build()
        hash1 = compute_gir_hash(gir1)

        builder2 = GIRBuilder()
        frame2 = builder2.add_global_frame()
        dims2 = {"length": Quantity(100.0, "mm"), "width": Quantity(50.0, "mm")}
        builder2.add_primitive("box", dims2, frame_id=frame2)
        gir2 = builder2.build()
        hash2 = compute_gir_hash(gir2)

        self.assertEqual(hash1, hash2)

    def test_ir_hash_differentiates_content(self) -> None:
        from server.geometry_ir import GIRBuilder

        builder1 = GIRBuilder()
        frame1 = builder1.add_global_frame()
        dims1 = {"length": Quantity(100.0, "mm"), "width": Quantity(50.0, "mm")}
        builder1.add_primitive("box", dims1, frame_id=frame1)

        builder2 = GIRBuilder()
        frame2 = builder2.add_global_frame()
        dims2 = {"length": Quantity(101.0, "mm"), "width": Quantity(50.0, "mm")}
        builder2.add_primitive("box", dims2, frame_id=frame2)

        hash1 = compute_gir_hash(builder1.build())
        hash2 = compute_gir_hash(builder2.build())

        self.assertNotEqual(hash1, hash2)

    def test_hash_independent_of_order(self) -> None:
        from server.geometry_ir import GIRBuilder

        builder1 = GIRBuilder()
        builder1.add_global_frame()
        builder1.add_primitive("box", {"length": Quantity(10.0, "mm")})
        builder1.add_primitive(
            "cylinder", {"radius": Quantity(5.0, "mm"), "height": Quantity(20.0, "mm")}
        )

        builder2 = GIRBuilder()
        builder2.add_global_frame()
        builder2.add_primitive("box", {"length": Quantity(10.0, "mm")})
        builder2.add_primitive(
            "cylinder", {"radius": Quantity(5.0, "mm"), "height": Quantity(20.0, "mm")}
        )

        hash1 = compute_gir_hash(builder1.build())
        hash2 = compute_gir_hash(builder2.build())

        self.assertEqual(hash1, hash2)


if __name__ == "__main__":
    unittest.main()
