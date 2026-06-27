"""Tests for the server-side geometry store."""

from __future__ import annotations

import unittest

from server.geometry_store import clear, remove, retrieve, stats, store


class TestGeometryStore(unittest.TestCase):
    def setUp(self) -> None:
        clear()

    def tearDown(self) -> None:
        clear()

    def test_store_and_retrieve(self) -> None:
        elems = [{"type": "arc", "cx": 0, "cy": 0, "r": 10}]
        handle = store(elems)
        self.assertTrue(handle.startswith("geo_"))
        self.assertEqual(retrieve(handle), elems)

    def test_retrieve_nonexistent_returns_none(self) -> None:
        self.assertIsNone(retrieve("geo_doesnotexist"))

    def test_retrieve_does_not_remove(self) -> None:
        handle = store([{"type": "line"}])
        self.assertIsNotNone(retrieve(handle))
        self.assertIsNotNone(retrieve(handle))  # still there

    def test_multiple_handles_independent(self) -> None:
        h1 = store([{"type": "a"}])
        h2 = store([{"type": "b"}])
        self.assertNotEqual(h1, h2)
        self.assertEqual(retrieve(h1), [{"type": "a"}])
        self.assertEqual(retrieve(h2), [{"type": "b"}])

    def test_remove(self) -> None:
        handle = store([{"type": "x"}])
        self.assertTrue(remove(handle))
        self.assertIsNone(retrieve(handle))

    def test_remove_nonexistent(self) -> None:
        self.assertFalse(remove("geo_nope"))

    def test_clear(self) -> None:
        store([{"type": "a"}])
        store([{"type": "b"}])
        count = clear()
        self.assertEqual(count, 2)
        self.assertEqual(stats()["handle_count"], 0)

    def test_stats(self) -> None:
        store([{"type": "a"}, {"type": "b"}])
        store([{"type": "c"}])
        s = stats()
        self.assertEqual(s["handle_count"], 2)
        self.assertEqual(s["total_elements"], 3)

    def test_store_with_metadata(self) -> None:
        handle = store([{"type": "arc"}], metadata={"teeth": 18})
        self.assertIsNotNone(retrieve(handle))


if __name__ == "__main__":
    unittest.main()
