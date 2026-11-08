"""Trusted acceptance suite, mounted read-only and run only inside Docker."""
import unittest
from todo import TodoStore, close_all


class TodoAcceptance(unittest.TestCase):
    def tearDown(self):
        # Multiple instances in one test share a candidate interpreter so a
        # class-level mutable task collection cannot pass by process isolation.
        close_all()

    def test_new_tasks_are_incomplete(self):
        self.assertIs(TodoStore().add("ship")['completed'], False)

    def test_complete_and_reopen(self):
        store = TodoStore()
        task = store.add("ship")
        self.assertIs(store.complete(task['id'])['completed'], True)
        self.assertIs(store.complete(task['id'], False)['completed'], False)

    def test_filter_completed(self):
        store = TodoStore()
        first = store.add("first")
        store.add("second")
        store.complete(first['id'])
        self.assertEqual([t['title'] for t in store.list(True)], ['first'])

    def test_filter_incomplete(self):
        store = TodoStore()
        store.complete(store.add("first")['id'])
        store.add("second")
        self.assertEqual([t['title'] for t in store.list(False)], ['second'])

    def test_unfiltered_list(self):
        store = TodoStore()
        store.complete(store.add("one")['id'])
        store.add("two")
        self.assertEqual(len(store.list()), 2)

    def test_invalid_and_trimmed_title(self):
        store = TodoStore()
        with self.assertRaises(ValueError):
            store.add("  ")
        self.assertEqual(store.add("  title  ")['title'], "title")

    def test_unknown_id(self):
        with self.assertRaises(KeyError):
            TodoStore().complete(999)

    def test_returned_values_are_copies(self):
        store = TodoStore()
        task = store.add("one")
        task['title'] = "changed"
        store.list()[0]['title'] = "changed again"
        store.complete(task['id'])['title'] = "again"
        self.assertEqual(store.list()[0]['title'], "one")

    def test_instances_are_independent(self):
        first, second = TodoStore(), TodoStore()
        first.add("one")
        self.assertEqual(second.list(), [])

    def test_ids_are_unique(self):
        store = TodoStore()
        self.assertNotEqual(store.add("a")['id'], store.add("b")['id'])
