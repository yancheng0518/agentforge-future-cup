"""Independent JSON-merge acceptance; run only through the isolated facade."""
import json
import unittest
from todo import TodoStore, close_all


def document(tasks, version=1):
    return json.dumps({"version": version, "tasks": tasks}, ensure_ascii=False)


def task(task_id, title, completed=False):
    return {"id": task_id, "title": title, "completed": completed}


class JsonMergeAcceptance(unittest.TestCase):
    def tearDown(self):
        close_all()

    def test_baseline_completion_and_copy_compatibility(self):
        store = TodoStore()
        result = store.add("  first  ")
        self.assertEqual(result, task(1, "first"))
        result["title"] = "outside"
        self.assertTrue(store.complete(1)["completed"])
        self.assertEqual(store.list(False), [])
        self.assertEqual(store.list(True)[0]["title"], "first")
        self.assertFalse(store.complete(1, False)["completed"])
        with self.assertRaises(ValueError):
            store.add(" ")
        with self.assertRaises(KeyError):
            store.complete(999)

    def test_export_schema_order_and_unicode_content(self):
        store = TodoStore()
        self.assertEqual(json.loads(store.export_json()), {"version": 1, "tasks": []})
        a = store.add('  中文 "引用"\n第二行  ')
        b = store.add("second")
        store.complete(b["id"])
        encoded = store.export_json()
        self.assertIsInstance(encoded, str)
        decoded = json.loads(encoded)
        self.assertEqual(decoded, {"version": 1, "tasks": [a, task(2, "second", True)]})
        decoded["tasks"][0]["title"] = "outside"
        self.assertEqual(store.list()[0]["title"], a["title"])

    def test_import_preserves_sparse_ids_and_advances_allocator(self):
        store = TodoStore()
        result = store.import_json(document([task(9, "  nine  ", True), task(3, "three")]))
        self.assertEqual(result, {"inserted": 2, "updated": 0, "skipped": 0})
        self.assertEqual(store.list(), [task(9, "nine", True), task(3, "three")])
        self.assertEqual(store.add("next")["id"], 10)

    def test_error_collision_is_atomic_including_id_allocator(self):
        store = TodoStore()
        before = store.add("existing")
        with self.assertRaises(ValueError):
            store.import_json(document([task(99, "new"), task(before["id"], "collision")]))
        self.assertEqual(store.list(), [before])
        self.assertEqual(store.add("next")["id"], 2)

    def test_replace_keeps_existing_positions_and_appends_in_input_order(self):
        store = TodoStore()
        a, b = store.add("a"), store.add("b")
        result = store.import_json(document([task(10, "ten"), task(b["id"], " B ", True), task(7, "seven")]), collision="replace")
        self.assertEqual(result, {"inserted": 2, "updated": 1, "skipped": 0})
        self.assertEqual(store.list(), [a, task(2, "B", True), task(10, "ten"), task(7, "seven")])
        self.assertEqual(store.add("next")["id"], 11)

    def test_skip_preserves_existing_values_but_validates_skipped_rows(self):
        store = TodoStore()
        a = store.add("a")
        result = store.import_json(document([task(9, "nine"), task(a["id"], "replace", True)]), "skip")
        self.assertEqual(result, {"inserted": 1, "updated": 0, "skipped": 1})
        before = store.list()
        self.assertEqual(before, [a, task(9, "nine")])
        with self.assertRaises(ValueError):
            store.import_json(document([task(20, "twenty"), task(a["id"], " ")]), "skip")
        self.assertEqual(store.list(), before)
        self.assertEqual(store.add("next")["id"], 10)

    def test_invalid_documents_are_atomic(self):
        invalid_payloads = [None, 3, {}, "", "not json", "[]", "null",
                            json.dumps({"version": 1}), json.dumps({"version": 1, "tasks": [], "extra": 1}),
                            json.dumps({"version": 1, "tasks": {}}), document([], True), document([], 2)]
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                store = TodoStore()
                before = store.add("keep")
                with self.assertRaises(ValueError):
                    store.import_json(payload)
                self.assertEqual(store.list(), [before])
                self.assertEqual(store.add("next")["id"], 2)
        for collision in (None, True, "merge", "ERROR", 1):
            with self.subTest(collision=collision), self.assertRaises(ValueError):
                TodoStore().import_json(document([]), collision)

    def test_invalid_or_duplicate_task_rows_are_atomic(self):
        invalid_rows = [None, [], {}, {"id": 3, "title": "x"},
                        {"id": 3, "title": "x", "completed": False, "extra": 1}]
        invalid_rows += [task(value, "x") for value in (True, False, 0, -1, 3.0, "3", None)]
        invalid_rows += [task(3, value) for value in (None, True, 3, "", " \t ")]
        invalid_rows += [task(3, "x", value) for value in (0, 1, None, "false")]
        for bad in invalid_rows:
            with self.subTest(bad=bad):
                store = TodoStore()
                before = store.add("keep")
                with self.assertRaises(ValueError):
                    store.import_json(document([task(99, "first"), bad]), "replace")
                self.assertEqual(store.list(), [before])
                self.assertEqual(store.add("next")["id"], 2)
        for mode in ("error", "replace", "skip"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                TodoStore().import_json(document([task(5, "a"), task(5, "b")]), mode)

    def test_roundtrip_copies_and_monotonic_next_id(self):
        original, copied = TodoStore(), TodoStore()
        original.import_json(document([task(7, "seven", True), task(3, "three")]))
        self.assertEqual(copied.import_json(original.export_json()), {"inserted": 2, "updated": 0, "skipped": 0})
        self.assertEqual(copied.list(), original.list())
        copied.list()[0]["title"] = "outside"
        self.assertEqual(copied.list()[0]["title"], "seven")
        self.assertEqual(copied.add("next")["id"], 8)
        copied.import_json(document([task(2, "two")]))
        self.assertEqual(copied.add("next again")["id"], 9)
        self.assertEqual(len(original.list()), 2)

    def test_empty_import_and_independent_instances(self):
        first, second = TodoStore(), TodoStore()
        first.import_json(document([task(12, "twelve", True)]))
        for mode in ("error", "replace", "skip"):
            self.assertEqual(first.import_json(document([]), mode), {"inserted": 0, "updated": 0, "skipped": 0})
        self.assertEqual(first.list(), [task(12, "twelve", True)])
        self.assertEqual(first.add("next")["id"], 13)
        self.assertEqual(second.list(), [])
        self.assertEqual(second.add("second")["id"], 1)
