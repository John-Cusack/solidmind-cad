"""Tests for the model registry: chains, ablation, identity, hidden catalog."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from server.eval_stages import STAGES, AnalyticLatchStage, StageError, resolve_stages
from server.model_registry import (
    ModelChain,
    ModelRegistryError,
    ablate,
    catalog_for,
    commit_chain,
    enable,
    full_chain,
    hidden_catalog,
    load_chain,
    uncalibrated_terms,
)


class TestCatalog(unittest.TestCase):
    def test_catalog_lookup(self) -> None:
        terms = catalog_for("analytic_latch")
        self.assertEqual(set(terms), {"bending", "stress_concentration"})
        with self.assertRaises(ModelRegistryError):
            catalog_for("warp_drive")

    def test_hidden_catalog_withholds(self) -> None:
        visible = hidden_catalog({"analytic_acoustic_bearing": {"coherence_loss"}})
        self.assertNotIn("coherence_loss", visible["analytic_acoustic_bearing"])
        self.assertIn("crlb", visible["analytic_acoustic_bearing"])
        # Other stages are untouched.
        self.assertEqual(set(visible["analytic_latch"]), {"bending", "stress_concentration"})

    def test_uncalibrated_terms_reported(self) -> None:
        names = {t.id for t in uncalibrated_terms(full_chain())}
        self.assertIn("coherence_loss", names)
        self.assertIn("anomaly", names)
        self.assertNotIn("bending", names)


class TestChainOps(unittest.TestCase):
    def test_full_chain(self) -> None:
        chain = full_chain(["analytic_latch"])
        self.assertEqual(
            chain.terms_for("analytic_latch"), frozenset({"bending", "stress_concentration"})
        )
        self.assertIsNone(chain.terms_for("analytic_acoustic_bearing"))

    def test_ablate_and_enable_round_trip(self) -> None:
        chain = full_chain(["analytic_acoustic_bearing"])
        ablated = ablate(chain, "analytic_acoustic_bearing", "coherence_loss")
        self.assertNotIn("coherence_loss", ablated.terms_for("analytic_acoustic_bearing"))
        restored = enable(ablated, "analytic_acoustic_bearing", "coherence_loss")
        self.assertEqual(
            restored.terms_for("analytic_acoustic_bearing"),
            chain.terms_for("analytic_acoustic_bearing"),
        )

    def test_ablate_unknown_raises(self) -> None:
        chain = full_chain(["analytic_latch"])
        with self.assertRaises(ModelRegistryError):
            ablate(chain, "analytic_latch", "not_a_term")
        with self.assertRaises(ModelRegistryError):
            ablate(chain, "analytic_acoustic_bearing", "crlb")  # stage not in chain

    def test_serde_round_trip(self) -> None:
        chain = full_chain()
        self.assertEqual(ModelChain.from_dict(chain.to_dict()).chains, chain.chains)

    def test_bad_schema_rejected(self) -> None:
        with self.assertRaises(ModelRegistryError):
            ModelChain.from_dict({"schema": "bogus", "chains": {}})


class TestChainStore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "artifacts"

    def test_commit_load_round_trip(self) -> None:
        chain = full_chain()
        h = commit_chain(chain, root=self.root)
        self.assertEqual(load_chain(h, root=self.root).chains, chain.chains)

    def test_ablation_changes_hash(self) -> None:
        full = commit_chain(full_chain(), root=self.root)
        ablated = commit_chain(
            ablate(full_chain(), "analytic_acoustic_bearing", "coherence_loss"), root=self.root
        )
        self.assertNotEqual(full, ablated)

    def test_unknown_term_rejected_at_commit(self) -> None:
        with self.assertRaises(ModelRegistryError):
            commit_chain(ModelChain(chains={"analytic_latch": ("ghost",)}), root=self.root)

    def test_load_unknown_raises(self) -> None:
        with self.assertRaises(ModelRegistryError):
            load_chain("f" * 64, root=self.root)


class TestStageConfiguration(unittest.TestCase):
    def test_default_identity_unchanged(self) -> None:
        self.assertEqual(STAGES["analytic_latch"].identity, "analytic_latch/v1")
        self.assertEqual(
            STAGES["analytic_acoustic_bearing"].identity, "analytic_acoustic_bearing/v1"
        )

    def test_configured_identity_reflects_terms(self) -> None:
        ablated = STAGES["analytic_latch"].configure(frozenset({"bending"}))
        self.assertEqual(ablated.identity, "analytic_latch/v1-terms:bending")
        self.assertIsNot(ablated, STAGES["analytic_latch"])

    def test_configure_with_default_terms_returns_self(self) -> None:
        stage = STAGES["analytic_latch"]
        self.assertIs(stage.configure(frozenset(AnalyticLatchStage.TERMS)), stage)
        self.assertIs(stage.configure(None), stage)

    def test_unknown_term_rejected(self) -> None:
        with self.assertRaises(StageError):
            STAGES["analytic_latch"].configure(frozenset({"ghost"}))

    def test_resolve_stages_applies_chain(self) -> None:
        chain = ablate(full_chain(), "analytic_latch", "stress_concentration")
        stages = resolve_stages(["analytic_latch"], chain)
        self.assertEqual(stages["analytic_latch"].terms, frozenset({"bending"}))
        self.assertEqual(
            resolve_stages(["analytic_latch"], None)["analytic_latch"].terms,
            frozenset(AnalyticLatchStage.TERMS),
        )

    def test_resolve_unknown_model(self) -> None:
        with self.assertRaises(StageError):
            resolve_stages(["warp_drive"], None)


if __name__ == "__main__":
    unittest.main()
