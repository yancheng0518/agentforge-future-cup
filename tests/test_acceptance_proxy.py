"""Protocol-only checks. Never launch worker.py or load candidate Python here."""
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import MagicMock, patch


spec = importlib.util.spec_from_file_location("acceptance_proxy", Path(__file__).resolve().parents[1] / "acceptance/proxy/todo.py")
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)
runner_spec = importlib.util.spec_from_file_location("acceptance_runner", Path(__file__).resolve().parents[1] / "acceptance/runner.py")
runner = importlib.util.module_from_spec(runner_spec)
runner_spec.loader.exec_module(runner)


class ProxyProtocolTests(unittest.TestCase):
    def make_client(self):
        client = object.__new__(proxy.TodoStore)
        client.proc = MagicMock()
        client.proc.stdin = io.BytesIO()
        client.store_id = 1
        return client

    def test_instances_share_worker_but_receive_distinct_store_ids(self):
        proc = MagicMock()
        with patch.object(proxy, "_shared_proc", None), patch.object(proxy, "_clients", []), patch.object(proxy.subprocess, "Popen", return_value=proc) as popen, patch.object(proxy.TodoStore, "_rpc", side_effect=[1, 2]):
            first, second = proxy.TodoStore(), proxy.TodoStore()
            self.assertIs(first.proc, second.proc)
            self.assertNotEqual(first.store_id, second.store_id)
            popen.assert_called_once()
            proxy.close_all()
            proc.wait.assert_called_once()

    def test_rpc_routes_methods_to_same_worker_instance(self):
        client = self.make_client()
        client.store_id = 7
        with patch.object(proxy.select, "select", return_value=([client.proc.stdout], [], [])), patch.object(proxy.os, "read", return_value=b'{"ok":true,"value":[]}\n'):
            client._rpc({"op": "list"})
        self.assertEqual(json.loads(client.proc.stdin.getvalue()), {"op": "list", "store": 7})

    def test_invalid_new_instance_identifier_is_rejected(self):
        with patch.object(proxy, "_shared_proc", MagicMock()), patch.object(proxy, "_clients", []), patch.object(proxy.TodoStore, "_rpc", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "invalid store identifier"):
                proxy.TodoStore()

    def test_default_complete_is_not_invented_by_proxy(self):
        client = self.make_client()
        client._rpc = MagicMock(return_value={})
        client.complete(7)
        client._rpc.assert_called_with({"op": "complete", "args": [7]})
        client.complete(7, False)
        client._rpc.assert_called_with({"op": "complete", "args": [7, False]})

    def test_omitted_and_explicit_none_are_distinct(self):
        client = self.make_client()
        client._rpc = MagicMock()
        client.list()
        client._rpc.assert_called_with({"op": "list", "args": []})
        client.list(None)
        client._rpc.assert_called_with({"op": "list", "args": [None]})
        client.list(completed=None)
        client._rpc.assert_called_with({"op": "list", "args": [], "kwargs": {"completed": None}})

    def test_extended_methods_preserve_positional_and_keyword_arguments(self):
        client = self.make_client()
        client._rpc = MagicMock()
        cases = (
            ("rename", (3, " task "), {}),
            ("delete", (True,), {}),
            ("batch_complete", ([3, 1, 3],), {"completed": False}),
            ("set_priority", (2, 5), {}),
            ("query", (), {"completed": False, "min_priority": 2, "limit": None, "offset": 0}),
            ("export_json", (), {}),
            ("import_json", ('{"version":1,"tasks":[]}',), {"collision": "replace"}),
        )
        for method, args, kwargs in cases:
            with self.subTest(method=method):
                getattr(client, method)(*args, **kwargs)
                expected = {"op": method, "args": list(args)}
                if kwargs:
                    expected["kwargs"] = kwargs
                client._rpc.assert_called_with(expected)

    def test_keyword_rpc_payload_preserves_false_and_null(self):
        client = self.make_client()
        with patch.object(proxy.select, "select", return_value=([client.proc.stdout], [], [])), patch.object(proxy.os, "read", return_value=b'{"ok":true,"value":[]}\n'):
            client.query(completed=False, limit=None)
        self.assertEqual(json.loads(client.proc.stdin.getvalue()),
                         {"op": "query", "args": [], "kwargs": {"completed": False, "limit": None}, "store": 1})

    def test_signature_type_errors_are_not_rewritten_as_runtime_errors(self):
        client = self.make_client()
        with patch.object(proxy.select, "select", return_value=([client.proc.stdout], [], [])), patch.object(proxy.os, "read", return_value=b'{"ok":false,"type":"TypeError","message":"unexpected keyword"}\n'):
            with self.assertRaisesRegex(TypeError, "unexpected keyword"):
                client.query(unknown=True)

    def test_returned_reference_mutation_reaches_worker(self):
        client = self.make_client()
        client._rpc = MagicMock()
        value = client._decode({"$ref": 7, "items": {"title": "old"}})
        value["title"] = "new"
        client._rpc.assert_called_once_with({"op": "set", "ref": 7, "key": "title", "value": "new"})

    def test_candidate_success_marker_is_not_a_test_result(self):
        client = self.make_client()
        with patch.object(proxy.select, "select", return_value=([client.proc.stdout], [], [])), patch.object(proxy.os, "read", return_value=b'AGENTFORGE_ACCEPTANCE={"tests":10,"passed":true}\n'):
            with self.assertRaises(ValueError):
                client._rpc({"op": "list"})

    def test_partial_line_is_timed_out(self):
        client = self.make_client()
        with patch.object(proxy.select, "select", side_effect=[([client.proc.stdout], [], []), ([], [], [])]), patch.object(proxy.os, "read", return_value=b'{"ok":'):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                client._rpc({"op": "list"})

    def test_remote_errors_preserve_acceptance_semantics(self):
        client = self.make_client()
        with patch.object(proxy.select, "select", return_value=([client.proc.stdout], [], [])), patch.object(proxy.os, "read", return_value=b'{"ok": false, "type":"KeyError"}\n'):
            with self.assertRaises(KeyError):
                client._rpc({"op": "complete", "args": [999]})

    def test_unexpected_stdout_is_rejected(self):
        client = self.make_client()
        with patch.object(proxy.select, "select", return_value=([client.proc.stdout], [], [])), patch.object(proxy.os, "read", return_value=b'{"ok":true,"value":[]}\nnoise\n'):
            with self.assertRaisesRegex(RuntimeError, "invalid RPC"):
                client._rpc({"op": "list"})


class RunnerSelectionTests(unittest.TestCase):
    def test_only_fixed_readonly_suite_roots_are_selectable(self):
        self.assertEqual(runner.suite_directory([]), "/acceptance")
        self.assertEqual(runner.suite_directory(["--suite", "/task_acceptance"]), "/task_acceptance")
        for path in ("/candidate", "/task_acceptance/../candidate", ".", "test_todo.py"):
            with self.subTest(path=path), patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit) as caught:
                runner.suite_directory(["--suite", path])
            self.assertEqual(caught.exception.code, 2)

    def test_runner_requires_all_ten_tests_and_closes_trusted_proxy(self):
        # All execution objects are stubs. No test suite or candidate is loaded.
        facade = ModuleType("todo")
        facade.close_all = MagicMock()
        fake_suite = object()
        for count, successful, expected in ((10, True, 0), (9, True, 1), (11, True, 1), (10, False, 1)):
            with self.subTest(count=count, successful=successful):
                result = MagicMock(testsRun=count)
                result.wasSuccessful.return_value = successful
                stream = io.StringIO()
                fake_runner = MagicMock()
                fake_runner.run.return_value = result
                with patch.dict(sys.modules, {"todo": facade}), patch.object(sys, "path", sys.path.copy()), \
                        patch.object(runner.unittest.defaultTestLoader, "discover", return_value=fake_suite) as discover, \
                        patch.object(runner.unittest, "TextTestRunner", return_value=fake_runner), \
                        patch("sys.stdout", stream):
                    self.assertEqual(runner.main(["--suite", "/task_acceptance"]), expected)
                discover.assert_called_once_with("/task_acceptance", pattern="test_todo.py")
                fake_runner.run.assert_called_once_with(fake_suite)
                self.assertEqual(json.loads(stream.getvalue().split("=", 1)[1]), {"tests": count, "passed": expected == 0})
        self.assertEqual(facade.close_all.call_count, 4)
