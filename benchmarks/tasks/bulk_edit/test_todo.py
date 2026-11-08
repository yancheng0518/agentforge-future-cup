"""Independent bulk-edit acceptance; run only via the isolated trusted facade."""
import unittest
from todo import TodoStore, close_all


class BulkEditAcceptance(unittest.TestCase):
    def tearDown(self):
        close_all()

    def test_baseline_completion_and_title_compatibility(self):
        store = TodoStore()
        task = store.add("  first  ")
        self.assertEqual(task, {"id": 1, "title": "first", "completed": False})
        with self.assertRaises(ValueError):
            store.add("  ")
        self.assertTrue(store.complete(task["id"])["completed"])
        self.assertEqual(store.list(False), [])
        self.assertEqual(len(store.list(True)), 1)
        self.assertFalse(store.complete(task["id"], False)["completed"])
        with self.assertRaises(KeyError):
            store.complete(999)

    def test_rename_preserves_state_order_and_returns_copy(self):
        store = TodoStore()
        a, b = store.add("a"), store.add("b")
        store.complete(a["id"])
        result = store.rename(a["id"], "  b  ")
        self.assertEqual(result, {"id": a["id"], "title": "b", "completed": True})
        result["title"] = "external"
        self.assertEqual([t["id"] for t in store.list()], [a["id"], b["id"]])
        self.assertEqual(store.list()[0]["title"], "b")

    def test_rename_rejects_invalid_inputs_without_mutation(self):
        store = TodoStore()
        task = store.add("original")
        for title in ("", " \t ", None, 3, False):
            with self.subTest(title=title), self.assertRaises(ValueError):
                store.rename(task["id"], title)
        for task_id in (True, False, 0, -1, 1.0, "1", None):
            with self.subTest(task_id=task_id), self.assertRaises(ValueError):
                store.rename(task_id, "new")
        with self.assertRaises(KeyError):
            store.rename(999, "new")
        self.assertEqual(store.list(), [task])

    def test_delete_preserves_order_and_never_reuses_id(self):
        store = TodoStore()
        a, b, c = store.add("a"), store.add("b"), store.add("c")
        store.complete(b["id"])
        removed = store.delete(b["id"])
        self.assertEqual(removed, {"id": b["id"], "title": "b", "completed": True})
        removed["title"] = "external"
        self.assertEqual([t["id"] for t in store.list()], [a["id"], c["id"]])
        self.assertGreater(store.add("d")["id"], c["id"])

    def test_delete_rejects_invalid_or_unknown_ids(self):
        store = TodoStore()
        task = store.add("keep")
        for task_id in (True, False, 0, -1, 1.0, "1", None):
            with self.subTest(task_id=task_id), self.assertRaises(ValueError):
                store.delete(task_id)
        with self.assertRaises(KeyError):
            store.delete(999)
        self.assertEqual(store.list(), [task])
        store.delete(task["id"])
        with self.assertRaises(KeyError):
            store.delete(task["id"])

    def test_batch_preserves_request_order_and_returns_copies(self):
        store = TodoStore()
        a, b, c = store.add("a"), store.add("b"), store.add("c")
        result = store.batch_complete([c["id"], a["id"]])
        self.assertEqual([t["id"] for t in result], [c["id"], a["id"]])
        self.assertTrue(all(t["completed"] for t in result))
        result[0]["completed"] = False
        self.assertEqual([t["id"] for t in store.list()], [a["id"], b["id"], c["id"]])
        self.assertEqual([t["id"] for t in store.list(True)], [a["id"], c["id"]])

    def test_batch_validation_is_atomic(self):
        store = TodoStore()
        a, b = store.add("a"), store.add("b")
        invalid_lists = (None, "1", 1, {}, [a["id"], a["id"]],
                         [a["id"], True], [a["id"], 0], [a["id"], "2"], [1.0])
        for task_ids in invalid_lists:
            with self.subTest(task_ids=task_ids), self.assertRaises(ValueError):
                store.batch_complete(task_ids)
            self.assertEqual(store.list(), [a, b])
        for completed in (0, 1, None, "yes", []):
            with self.subTest(completed=completed), self.assertRaises(ValueError):
                store.batch_complete([a["id"], b["id"]], completed)
            self.assertEqual(store.list(), [a, b])

    def test_unknown_late_batch_id_does_not_partially_modify(self):
        store = TodoStore()
        a, b = store.add("a"), store.add("b")
        with self.assertRaises(KeyError):
            store.batch_complete([a["id"], 999, b["id"]])
        self.assertEqual(store.list(), [a, b])

    def test_empty_batch_and_reopen(self):
        store = TodoStore()
        a, b = store.add("a"), store.add("b")
        self.assertEqual(store.batch_complete([]), [])
        self.assertEqual(store.list(), [a, b])
        store.batch_complete([a["id"], b["id"]], True)
        result = store.batch_complete([b["id"], a["id"]], completed=False)
        self.assertFalse(any(t["completed"] for t in result))
        self.assertEqual(store.list(True), [])

    def test_instances_are_independent_after_bulk_edits(self):
        first, second = TodoStore(), TodoStore()
        task = first.add("first")
        first.rename(task["id"], "changed")
        first.batch_complete([task["id"]])
        self.assertEqual(second.list(), [])
        self.assertEqual(second.add("second")["id"], 1)
        self.assertEqual(first.list()[0]["title"], "changed")
