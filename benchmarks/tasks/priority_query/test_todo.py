"""Independent priority-query acceptance; candidates run only in Docker."""
import unittest
from todo import TodoStore, close_all


class PriorityQueryAcceptance(unittest.TestCase):
    def tearDown(self):
        close_all()

    def test_baseline_compatibility_and_default_priority(self):
        store = TodoStore()
        task = store.add("  first  ")
        self.assertEqual(task, {"id": 1, "title": "first", "completed": False, "priority": 1})
        with self.assertRaises(ValueError):
            store.add(" ")
        self.assertEqual(store.complete(task["id"])["priority"], 1)
        self.assertEqual(store.list(False), [])
        self.assertEqual(store.complete(task["id"], False)["completed"], False)
        with self.assertRaises(KeyError):
            store.complete(999)

    def test_set_priority_keeps_state_and_returns_copy(self):
        store = TodoStore()
        task = store.add("one")
        store.complete(task["id"])
        for priority in (0, 3, 2, 1):
            result = store.set_priority(task["id"], priority)
            self.assertEqual(result, {"id": task["id"], "title": "one", "completed": True, "priority": priority})
        result["priority"] = 0
        self.assertEqual(store.list()[0]["priority"], 1)

    def test_set_priority_validation_preserves_existing_task(self):
        store = TodoStore()
        task = store.add("keep")
        for priority in (True, False, -1, 4, 1.0, "1", None):
            with self.subTest(priority=priority), self.assertRaises(ValueError):
                store.set_priority(task["id"], priority)
        for task_id in (True, False, 0, -1, 1.0, "1", None):
            with self.subTest(task_id=task_id), self.assertRaises(ValueError):
                store.set_priority(task_id, 2)
        with self.assertRaises(KeyError):
            store.set_priority(999, 2)
        self.assertEqual(store.list(), [task])

    def test_sort_uses_descending_priority_then_ascending_id(self):
        store = TodoStore()
        tasks = [store.add(str(i)) for i in range(5)]
        for task, priority in zip(tasks, (2, 3, 2, 3, 0)):
            store.set_priority(task["id"], priority)
        self.assertEqual([t["id"] for t in store.query()], [2, 4, 1, 3, 5])
        self.assertEqual([t["id"] for t in store.list()], [1, 2, 3, 4, 5])

    def test_combined_completion_and_priority_filters(self):
        store = TodoStore()
        for i, priority in enumerate((3, 2, 1, 3), 1):
            task = store.add(str(i))
            store.set_priority(task["id"], priority)
        store.complete(1)
        store.complete(3)
        self.assertEqual([t["id"] for t in store.query(completed=True, min_priority=2)], [1])
        self.assertEqual([t["id"] for t in store.query(completed=False, min_priority=2)], [4, 2])
        self.assertEqual([t["id"] for t in store.query(completed=None, min_priority=3)], [1, 4])

    def test_filter_sort_then_paginate(self):
        store = TodoStore()
        for i, priority in enumerate((3, 1, 3, 2, 2), 1):
            task = store.add(str(i))
            store.set_priority(task["id"], priority)
        store.complete(1)
        self.assertEqual([t["id"] for t in store.query(False, 1, 2, 1)], [4, 5])
        self.assertEqual([t["id"] for t in store.query(completed=False, offset=3)], [2])
        self.assertEqual(store.query(limit=0), [])
        self.assertEqual(store.query(offset=99), [])
        self.assertEqual([t["id"] for t in store.query(limit=None, offset=1)], [3, 4, 5, 2])

    def test_query_strict_parameter_validation(self):
        store = TodoStore()
        task = store.add("keep")
        invalid = {"completed": (0, 1, "true", [], {}),
                   "min_priority": (True, False, -1, 4, 1.0, "1", None),
                   "limit": (True, False, -1, 1.0, "1"),
                   "offset": (True, False, -1, 1.0, "1", None)}
        for name, values in invalid.items():
            for value in values:
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    store.query(**{name: value})
        self.assertEqual(store.list(), [task])

    def test_query_and_list_values_are_independent_copies(self):
        store = TodoStore()
        a, b = store.add("a"), store.add("b")
        a["priority"] = 3
        store.set_priority(b["id"], 2)
        result = store.query()
        result[0]["title"] = "outside"
        result[0]["priority"] = 0
        store.list()[0]["completed"] = True
        self.assertEqual(store.list()[0], {"id": 1, "title": "a", "completed": False, "priority": 1})
        self.assertEqual(store.list()[1], {"id": 2, "title": "b", "completed": False, "priority": 2})

    def test_instances_do_not_share_priorities_or_tasks(self):
        first, second = TodoStore(), TodoStore()
        a, b = first.add("a"), second.add("b")
        first.set_priority(a["id"], 3)
        self.assertEqual(second.query(), [b])
        self.assertEqual(second.query()[0]["priority"], 1)
        self.assertEqual(first.query()[0]["priority"], 3)

    def test_empty_store_still_validates_query_parameters(self):
        store = TodoStore()
        self.assertEqual(store.query(), [])
        self.assertEqual(store.query(completed=False, min_priority=3, limit=0, offset=50), [])
        with self.assertRaises(ValueError):
            store.query(completed=1, limit=0)
        with self.assertRaises(ValueError):
            store.query(offset=-1)
