"""Trusted test facade; candidate executes in a separate Python subprocess.

Reference mutations are forwarded so copy-semantics assertions remain meaningful.
This is NOT a general adversarial code execution service.
"""
import json
import os
import select
import subprocess
import sys
import time

_clients = []
_shared_proc = None


class RemoteDict(dict):
    def __init__(self, client, ref, items):
        super().__init__(items)
        self.client, self.ref = client, ref

    def __setitem__(self, key, value):
        self.client._rpc({"op": "set", "ref": self.ref, "key": key, "value": value})
        super().__setitem__(key, value)


class TodoStore:
    def __init__(self):
        global _shared_proc
        if _shared_proc is None:
            _shared_proc = subprocess.Popen([sys.executable, "-I", "-B", "/acceptance/worker.py"],
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.proc = _shared_proc
        _clients.append(self)
        self.store_id = self._rpc({"op": "new"})
        if type(self.store_id) is not int or self.store_id <= 0:
            raise RuntimeError("Candidate returned invalid store identifier")

    def _rpc(self, request):
        if request["op"] not in ("new", "set"):
            request = {**request, "store": self.store_id}
        self.proc.stdin.write((json.dumps(request) + "\n").encode())
        self.proc.stdin.flush()
        deadline, raw = time.monotonic() + 2, b""
        while b"\n" not in raw and len(raw) <= 65536:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.proc.stdout], [], [], remaining)[0]:
                raise RuntimeError("Candidate RPC timed out")
            chunk = os.read(self.proc.stdout.fileno(), 4096)
            if not chunk:
                raise RuntimeError("Candidate worker exited before response")
            raw += chunk
        if len(raw) > 65536 or not raw.endswith(b"\n") or raw.count(b"\n") != 1:
            raise RuntimeError("Candidate returned invalid RPC output")
        response = json.loads(raw)
        if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
            raise RuntimeError("Candidate returned invalid RPC schema")
        if not response["ok"]:
            error = {"ValueError": ValueError, "KeyError": KeyError, "TypeError": TypeError}.get(response.get("type"), RuntimeError)
            raise error(str(response.get("type")) + ": " + str(response.get("message", ""))[:1000])
        return self._decode(response.get("value"))

    def _decode(self, value):
        if isinstance(value, dict) and "$ref" in value:
            return RemoteDict(self, value["$ref"], value["items"])
        if isinstance(value, list):
            return [self._decode(item) for item in value]
        return value

    def _call(self, method, args, kwargs):
        request = {"op": method, "args": list(args)}
        if kwargs:
            request["kwargs"] = kwargs
        return self._rpc(request)

    def add(self, *args, **kwargs):
        return self._call("add", args, kwargs)

    def list(self, *args, **kwargs):
        return self._call("list", args, kwargs)

    def complete(self, *args, **kwargs):
        return self._call("complete", args, kwargs)

    def rename(self, *args, **kwargs):
        return self._call("rename", args, kwargs)

    def delete(self, *args, **kwargs):
        return self._call("delete", args, kwargs)

    def batch_complete(self, *args, **kwargs):
        return self._call("batch_complete", args, kwargs)

    def set_priority(self, *args, **kwargs):
        return self._call("set_priority", args, kwargs)

    def query(self, *args, **kwargs):
        return self._call("query", args, kwargs)

    def export_json(self, *args, **kwargs):
        return self._call("export_json", args, kwargs)

    def import_json(self, *args, **kwargs):
        return self._call("import_json", args, kwargs)


def close_all():
    global _shared_proc
    processes = {id(client.proc): client.proc for client in _clients}
    for proc in processes.values():
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for pipe in (proc.stdin, proc.stdout):
            if pipe is not None:
                pipe.close()
    _clients.clear()
    _shared_proc = None
