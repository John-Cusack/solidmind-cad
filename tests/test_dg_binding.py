"""Tests for layer-1 binding validation and application."""

from __future__ import annotations

import json
import unittest

from server import json_pointer
from server.dg_binding import (
    BINDING_PATH_UNDECLARED,
    BINDING_PATH_UNKNOWN,
    BINDING_TYPE_MISMATCH,
    Binding,
    BindingError,
    apply_bindings,
    domain_hits,
    validate_bindings,
)
from server.dg_import import import_brief
from server.paths import repo_root

FIXTURE = repo_root() / "examples" / "foam_dart_spring_launcher" / "design_brief.json"
FILLET = "/parts/latch_sear/specs/fillet_mm"


class BindingBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        brief = json.loads(FIXTURE.read_text())
        cls.structure, cls.params = import_brief(brief)


class TestValidate(BindingBase):
    def test_valid_binding_passes(self) -> None:
        validate_bindings(self.structure, self.params, [Binding(FILLET, 0.5)])

    def test_int_accepted_for_float(self) -> None:
        validate_bindings(self.structure, self.params, [Binding(FILLET, 1)])

    def test_unknown_path_hard_fails(self) -> None:
        with self.assertRaises(BindingError) as ctx:
            validate_bindings(self.structure, self.params, [Binding("/parts/nope/specs/x_mm", 1.0)])
        self.assertEqual(ctx.exception.code, BINDING_PATH_UNKNOWN)

    def test_malformed_pointer_hard_fails(self) -> None:
        with self.assertRaises(BindingError) as ctx:
            validate_bindings(self.structure, self.params, [Binding("no-leading-slash", 1.0)])
        self.assertEqual(ctx.exception.code, BINDING_PATH_UNKNOWN)

    def test_non_leaf_path_hard_fails(self) -> None:
        with self.assertRaises(BindingError) as ctx:
            validate_bindings(
                self.structure, self.params, [Binding("/parts/latch_sear/specs", 1.0)]
            )
        self.assertEqual(ctx.exception.code, BINDING_PATH_UNKNOWN)

    def test_raw_nested_value_is_not_a_leaf(self) -> None:
        # /layout/z_layers/base resolves to a bare float (stored raw, not a
        # {"value", "unit"} leaf) — rejected as not-a-value-leaf.
        with self.assertRaises(BindingError) as ctx:
            validate_bindings(self.structure, self.params, [Binding("/layout/z_layers/base", 1.0)])
        self.assertEqual(ctx.exception.code, BINDING_PATH_UNKNOWN)

    def test_declared_leaf_without_spec_hard_fails(self) -> None:
        # A value leaf present in the params doc but missing from
        # structure.param_specs (possible in hand-built docs) is UNDECLARED.
        from server.dg_models import PARAMS_SCHEMA

        params = {
            "schema": PARAMS_SCHEMA,
            "parts": {"x": {"specs": {"w_mm": {"value": 1.0, "unit": "mm"}}}},
        }
        with self.assertRaises(BindingError) as ctx:
            validate_bindings(self.structure, params, [Binding("/parts/x/specs/w_mm", 2.0)])
        self.assertEqual(ctx.exception.code, BINDING_PATH_UNDECLARED)

    def test_type_mismatch_hard_fails(self) -> None:
        with self.assertRaises(BindingError) as ctx:
            validate_bindings(self.structure, self.params, [Binding(FILLET, "wide")])
        self.assertEqual(ctx.exception.code, BINDING_TYPE_MISMATCH)

    def test_bool_rejected_for_float(self) -> None:
        with self.assertRaises(BindingError) as ctx:
            validate_bindings(self.structure, self.params, [Binding(FILLET, True)])
        self.assertEqual(ctx.exception.code, BINDING_TYPE_MISMATCH)


class TestApply(BindingBase):
    def test_apply_replaces_value_only(self) -> None:
        bound = apply_bindings(self.params, [Binding(FILLET, 0.5)])
        leaf = json_pointer.get(bound, FILLET)
        self.assertEqual(leaf, {"value": 0.5, "unit": "mm"})

    def test_apply_is_deep_copy(self) -> None:
        apply_bindings(self.params, [Binding(FILLET, 0.9)])
        self.assertEqual(json_pointer.get(self.params, FILLET)["value"], 0.0)

    def test_apply_missing_path_raises(self) -> None:
        with self.assertRaises((BindingError, json_pointer.JsonPointerError)):
            apply_bindings(self.params, [Binding("/nope", 1.0)])


class TestDomainHits(BindingBase):
    def test_in_domain_no_hits(self) -> None:
        bound = apply_bindings(self.params, [Binding(FILLET, 0.5)])
        hits = [h for h in domain_hits(self.structure, bound) if h["path"] == FILLET]
        self.assertEqual(hits, [])

    def test_out_of_domain_recorded(self) -> None:
        bound = apply_bindings(self.params, [Binding(FILLET, 2.0)])
        hits = [h for h in domain_hits(self.structure, bound) if h["path"] == FILLET]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["kind"], "above_max")
        self.assertEqual(hits[0]["value"], 2.0)


if __name__ == "__main__":
    unittest.main()
