"""Trusted unittest bridge; each call gets a fresh isolated candidate process."""
import builtins
import json
import subprocess
import sys


class RemoteError(RuntimeError):
    pass


_EXCEPTIONS = {name: getattr(builtins, name) for name in (
    "ValueError", "TypeError", "KeyError", "IndexError", "RuntimeError", "ZeroDivisionError",
    "OverflowError", "AssertionError", "NotImplementedError", "AttributeError", "LookupError")}


def call(module, function, *args, **kwargs):
    payload = json.dumps({"module": module, "function": function, "args": args, "kwargs": kwargs}, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(payload) > 65536:
        raise ValueError("Candidate call input exceeds 64 KiB")
    try:
        process = subprocess.run([sys.executable, "-I", "-B", "/runner/worker.py"], input=payload,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, cwd="/tmp")
    except subprocess.TimeoutExpired:
        raise RemoteError("Candidate call exceeded 5 seconds") from None
    if process.returncode or len(process.stdout) > 65536 or len(process.stderr) > 65536:
        raise RemoteError("Candidate process failed or exceeded output limits: " + process.stderr[:2000].decode("utf-8", errors="replace"))
    try:
        response = json.loads(process.stdout)
        if type(response.get("ok")) is not bool:
            raise ValueError()
    except (ValueError, AttributeError):
        raise RemoteError("Candidate process did not return a valid result") from None
    if response["ok"]:
        return response["value"]
    exception = _EXCEPTIONS.get(response.get("type"), RemoteError)
    raise exception(str(response.get("message", "Candidate failed")))
