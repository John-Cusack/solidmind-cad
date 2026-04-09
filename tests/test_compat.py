"""Tests for freecad_addon.compat — FreeCAD compatibility layer.

All tests mock FreeCAD modules since the test environment doesn't have FreeCAD.
"""
from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import MagicMock, patch


def _setup_mock_freecad(version: tuple[int, int] = (1, 0)) -> MagicMock:
    """Create a mock FreeCAD module with given version and inject into sys.modules."""
    mock_fc = MagicMock()
    mock_fc.Version.return_value = [str(version[0]), str(version[1]), "0"]
    return mock_fc


class TestVersionDetection(unittest.TestCase):
    def test_parse_v1_0(self):
        mock_fc = _setup_mock_freecad((1, 0))
        with patch.dict(sys.modules, {"FreeCAD": mock_fc}):
            # Re-import to trigger version detection
            import importlib
            if "freecad_addon.compat" in sys.modules:
                del sys.modules["freecad_addon.compat"]
            import freecad_addon.compat as compat
            importlib.reload(compat)
            self.assertEqual(compat.VERSION_TUPLE, (1, 0))
            self.assertTrue(compat.IS_V1_PLUS)
            self.assertFalse(compat.IS_V1_1_PLUS)

    def test_parse_v1_1(self):
        mock_fc = _setup_mock_freecad((1, 1))
        with patch.dict(sys.modules, {"FreeCAD": mock_fc}):
            import importlib
            if "freecad_addon.compat" in sys.modules:
                del sys.modules["freecad_addon.compat"]
            import freecad_addon.compat as compat
            importlib.reload(compat)
            self.assertEqual(compat.VERSION_TUPLE, (1, 1))
            self.assertTrue(compat.IS_V1_PLUS)
            self.assertTrue(compat.IS_V1_1_PLUS)

    def test_parse_v0_21(self):
        mock_fc = _setup_mock_freecad((0, 21))
        with patch.dict(sys.modules, {"FreeCAD": mock_fc}):
            import importlib
            if "freecad_addon.compat" in sys.modules:
                del sys.modules["freecad_addon.compat"]
            import freecad_addon.compat as compat
            importlib.reload(compat)
            self.assertEqual(compat.VERSION_TUPLE, (0, 21))
            self.assertFalse(compat.IS_V1_PLUS)
            self.assertFalse(compat.IS_V1_1_PLUS)


class TestSetSketchSupport(unittest.TestCase):
    def setUp(self):
        mock_fc = _setup_mock_freecad((1, 0))
        self._patcher = patch.dict(sys.modules, {"FreeCAD": mock_fc})
        self._patcher.start()
        import importlib
        if "freecad_addon.compat" in sys.modules:
            del sys.modules["freecad_addon.compat"]
        import freecad_addon.compat as compat
        importlib.reload(compat)
        self.compat = compat

    def tearDown(self):
        self._patcher.stop()

    def test_uses_attachment_support(self):
        sketch = MagicMock(spec=["AttachmentSupport", "MapMode"])
        self.compat.set_sketch_support(sketch, "support_val")
        self.assertEqual(sketch.AttachmentSupport, "support_val")
        self.assertEqual(sketch.MapMode, "FlatFace")

    def test_falls_back_to_support(self):
        sketch = MagicMock(spec=["Support", "MapMode"])
        self.compat.set_sketch_support(sketch, "support_val")
        self.assertEqual(sketch.Support, "support_val")
        self.assertEqual(sketch.MapMode, "FlatFace")

    def test_raises_if_neither(self):
        sketch = MagicMock(spec=["MapMode"])
        with self.assertRaises(AttributeError):
            self.compat.set_sketch_support(sketch, "val")


class TestFindObject(unittest.TestCase):
    def setUp(self):
        mock_fc = _setup_mock_freecad((1, 0))
        self._patcher = patch.dict(sys.modules, {"FreeCAD": mock_fc})
        self._patcher.start()
        import importlib
        if "freecad_addon.compat" in sys.modules:
            del sys.modules["freecad_addon.compat"]
        import freecad_addon.compat as compat
        importlib.reload(compat)
        self.compat = compat

    def tearDown(self):
        self._patcher.stop()

    def test_direct_lookup(self):
        doc = MagicMock()
        obj = MagicMock()
        obj.Name = "MyJoint"
        doc.getObject.return_value = obj
        result = self.compat.find_object(doc, "MyJoint")
        self.assertEqual(result, obj)

    def test_suffix_fallback(self):
        doc = MagicMock()
        obj = MagicMock()
        obj.Name = "MyJoint001"
        doc.getObject.side_effect = lambda name: obj if name == "MyJoint001" else None
        doc.Objects = []
        result = self.compat.find_object(doc, "MyJoint")
        self.assertEqual(result, obj)

    def test_group_search(self):
        doc = MagicMock()
        doc.getObject.return_value = None

        child = MagicMock()
        child.Name = "sun_rev"
        child.Label = "sun_rev"

        group = MagicMock()
        group.Name = "JointGroup"
        group.Label = "JointGroup"
        group.TypeId = "App::DocumentObjectGroup"
        group.Group = [child]

        doc.Objects = [group]

        result = self.compat.find_object(doc, "sun_rev")
        self.assertEqual(result, child)

    def test_not_found(self):
        doc = MagicMock()
        doc.getObject.return_value = None
        doc.Objects = []
        result = self.compat.find_object(doc, "nonexistent")
        self.assertIsNone(result)


class TestFindJointInAssembly(unittest.TestCase):
    def setUp(self):
        mock_fc = _setup_mock_freecad((1, 0))
        self._patcher = patch.dict(sys.modules, {"FreeCAD": mock_fc})
        self._patcher.start()
        import importlib
        if "freecad_addon.compat" in sys.modules:
            del sys.modules["freecad_addon.compat"]
        import freecad_addon.compat as compat
        importlib.reload(compat)
        self.compat = compat

    def tearDown(self):
        self._patcher.stop()

    def test_direct_lookup(self):
        doc = MagicMock()
        asm = MagicMock()
        joint = MagicMock()
        joint.Name = "sun_rev"
        doc.getObject.return_value = joint
        result = self.compat.find_joint_in_assembly(doc, asm, "sun_rev")
        self.assertEqual(result, joint)

    def test_group_traversal(self):
        """Joint found via walking asm_obj.Group → JointGroup → children."""
        doc = MagicMock()
        doc.getObject.return_value = None  # direct lookup fails

        joint = MagicMock()
        joint.Name = "sun_rev"
        joint.Label = "sun_rev"

        joint_group = MagicMock()
        joint_group.Name = "Joints"
        joint_group.Group = [joint]

        asm = MagicMock()
        asm.Group = [joint_group]

        result = self.compat.find_joint_in_assembly(doc, asm, "sun_rev")
        self.assertEqual(result, joint)

    def test_group_traversal_by_label(self):
        """Joint found via Label match in Group traversal."""
        doc = MagicMock()
        doc.getObject.return_value = None

        joint = MagicMock()
        joint.Name = "Joint001"
        joint.Label = "sun_rev"

        joint_group = MagicMock()
        joint_group.Name = "Joints"
        joint_group.Group = [joint]

        asm = MagicMock()
        asm.Group = [joint_group]

        result = self.compat.find_joint_in_assembly(doc, asm, "sun_rev")
        self.assertEqual(result, joint)

    def test_falls_back_to_doc_getobject(self):
        """Last resort: doc.getObject when Group traversal finds nothing."""
        doc = MagicMock()
        fallback_obj = MagicMock()
        fallback_obj.Name = "some_joint"
        doc.getObject.return_value = fallback_obj

        asm = MagicMock()
        asm.Group = []  # empty group, nothing to traverse

        result = self.compat.find_joint_in_assembly(doc, asm, "some_joint")
        self.assertEqual(result, fallback_obj)


class TestSetPropertySafe(unittest.TestCase):
    def setUp(self):
        mock_fc = _setup_mock_freecad((1, 0))
        self._patcher = patch.dict(sys.modules, {"FreeCAD": mock_fc})
        self._patcher.start()
        import importlib
        if "freecad_addon.compat" in sys.modules:
            del sys.modules["freecad_addon.compat"]
        import freecad_addon.compat as compat
        importlib.reload(compat)
        self.compat = compat

    def tearDown(self):
        self._patcher.stop()

    def test_primary_property(self):
        obj = MagicMock(spec=["AttachmentSupport", "MapMode"])
        result = self.compat.set_property_safe(obj, "AttachmentSupport", "val")
        self.assertTrue(result)
        self.assertEqual(obj.AttachmentSupport, "val")

    def test_fallback_property(self):
        obj = MagicMock(spec=["Support", "MapMode"])
        result = self.compat.set_property_safe(obj, "AttachmentSupport", "val", ["Support"])
        self.assertTrue(result)
        self.assertEqual(obj.Support, "val")

    def test_no_property_found(self):
        obj = MagicMock(spec=["MapMode"])
        result = self.compat.set_property_safe(obj, "AttachmentSupport", "val", ["Support"])
        self.assertFalse(result)


class TestListObjectsLike(unittest.TestCase):
    def setUp(self):
        mock_fc = _setup_mock_freecad((1, 0))
        self._patcher = patch.dict(sys.modules, {"FreeCAD": mock_fc})
        self._patcher.start()
        import importlib
        if "freecad_addon.compat" in sys.modules:
            del sys.modules["freecad_addon.compat"]
        import freecad_addon.compat as compat
        importlib.reload(compat)
        self.compat = compat

    def tearDown(self):
        self._patcher.stop()

    def test_matches_by_name(self):
        doc = MagicMock()
        obj1 = MagicMock()
        obj1.Name = "Joint_revolute"
        obj1.TypeId = "App::FeaturePython"
        obj1.Label = "Joint"
        obj2 = MagicMock()
        obj2.Name = "Body"
        obj2.TypeId = "PartDesign::Body"
        obj2.Label = "Body"
        doc.Objects = [obj1, obj2]

        result = self.compat.list_objects_like(doc, "Joint")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Joint_revolute")


class TestRequireV1Plus(unittest.TestCase):
    def test_raises_on_v0_21(self):
        mock_fc = _setup_mock_freecad((0, 21))
        with patch.dict(sys.modules, {"FreeCAD": mock_fc}):
            import importlib
            if "freecad_addon.compat" in sys.modules:
                del sys.modules["freecad_addon.compat"]
            import freecad_addon.compat as compat
            importlib.reload(compat)
            with self.assertRaises(RuntimeError) as ctx:
                compat.require_v1_plus()
            self.assertIn("1.0+", str(ctx.exception))
            self.assertIn("0.21", str(ctx.exception))

    def test_passes_on_v1_0(self):
        mock_fc = _setup_mock_freecad((1, 0))
        with patch.dict(sys.modules, {"FreeCAD": mock_fc}):
            import importlib
            if "freecad_addon.compat" in sys.modules:
                del sys.modules["freecad_addon.compat"]
            import freecad_addon.compat as compat
            importlib.reload(compat)
            # Should not raise
            compat.require_v1_plus()


class TestFreecadInfo(unittest.TestCase):
    def test_returns_dict(self):
        mock_fc = _setup_mock_freecad((1, 0))
        with patch.dict(sys.modules, {"FreeCAD": mock_fc}):
            import importlib
            if "freecad_addon.compat" in sys.modules:
                del sys.modules["freecad_addon.compat"]
            import freecad_addon.compat as compat
            importlib.reload(compat)
            info = compat.freecad_info()
            self.assertEqual(info["version"], [1, 0])
            self.assertTrue(info["is_v1_plus"])
            self.assertIn("modules", info)
            self.assertIn("qt_backend", info)


if __name__ == "__main__":
    unittest.main()
