"""Trusted real-HTTP acceptance bridge; only used inside the isolated container.

Candidate code runs in a separate child process, never imported by this module.
The child still shares a container UID: this is not adversarial process isolation.
"""
import collections
import http.client
import json as json_module
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import unquote

_process = None
_startup_error = None
_log_chunks = collections.deque(maxlen=16)
_calls = 0
MAX_BODY = 16384
MAX_RESPONSE = 131072
MAX_REQUESTS = 64


def _drain(stream):
    try:
        while True:
            value = stream.read(1024)
            if not value:
                break
            _log_chunks.append(value)
    except (OSError, ValueError):
        pass


def _diagnostic():
    return b"".join(_log_chunks)[-8000:].decode("utf-8", errors="replace")


def start_server(entrypoint):
    """A startup error becomes an error of each real test, never an empty pass."""
    global _process, _startup_error, _calls
    _calls, _startup_error = 0, None
    _log_chunks.clear()
    try:
        _process = subprocess.Popen([sys.executable, "-I", "-B", "/runner/serve.py", entrypoint],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    cwd="/candidate", start_new_session=True)
        threading.Thread(target=_drain, args=(_process.stdout,), daemon=True).start()
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if _process.poll() is not None:
                _startup_error = "Web application exited during startup (code %s)." % _process.returncode
                return
            try:
                with socket.create_connection(("127.0.0.1", 8080), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.05)
        _startup_error = "Web application did not listen on 127.0.0.1:8080 within 6 seconds."
    except (OSError, ValueError) as exc:
        _startup_error = "Web application could not start: " + type(exc).__name__


def stop_server():
    global _process
    process, _process = _process, None
    if process is None:
        return
    try:
        # Also remove reloader children if candidate disregarded the no-reload contract.
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=3)
    if process.stdout is not None:
        process.stdout.close()


def server_evidence():
    return {"startup_error": _startup_error, "http_requests": _calls,
            "server_exit_code": _process.poll() if _process is not None else None,
            "log_tail": _diagnostic()}


def _target(method, path):
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"):
        raise ValueError("Unsupported HTTP method")
    if (not isinstance(path, str) or not 1 <= len(path) <= 2048 or not path.startswith("/")
            or path.startswith("//") or "\\" in path or "#" in path
            or any(ord(char) < 32 or ord(char) == 127 for char in path)):
        raise ValueError("HTTP path must stay within this server")
    decoded = path.split("?", 1)[0]
    for _ in range(3):
        decoded = unquote(decoded)
    if decoded.startswith("//") or "\\" in decoded or ".." in decoded.split("/") or any(ord(char) < 32 for char in decoded):
        raise ValueError("HTTP path traversal is not allowed")


def request(method, path, *, json=None, headers=None):
    global _calls
    _target(method, path)
    if _calls >= MAX_REQUESTS:
        raise RuntimeError("Frozen HTTP suite exceeded its 64-request budget")
    _calls += 1
    if _startup_error:
        raise RuntimeError(_startup_error + "\n" + _diagnostic())
    if _process is None or _process.poll() is not None:
        raise RuntimeError("Web application is not running.\n" + _diagnostic())
    body = None if json is None else json_module.dumps(json, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if body is not None and len(body) > MAX_BODY:
        raise ValueError("HTTP request body exceeds 16 KiB")
    if headers is None:
        headers = {}
    if not isinstance(headers, dict) or len(headers) > 8:
        raise ValueError("HTTP request headers must be a bounded object")
    outgoing = {"Accept": "application/json, text/html;q=0.9, */*;q=0.5"}
    for name, value in headers.items():
        if (not isinstance(name, str) or name.lower() not in {"accept", "content-type", "if-none-match"}
                or not isinstance(value, str) or len(value) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ValueError("HTTP acceptance request header is not allowed")
        outgoing[name.lower()] = value
    if body is not None:
        outgoing["Content-Type"] = "application/json"
    connection = http.client.HTTPConnection("127.0.0.1", 8080, timeout=3)
    try:
        connection.request(method, path, body=body, headers=outgoing)
        response = connection.getresponse()
        data = response.read(MAX_RESPONSE + 1)
        if len(data) > MAX_RESPONSE:
            raise RuntimeError("HTTP response exceeds 128 KiB")
        returned_headers = {name.lower(): value for name, value in response.getheaders()}
        if sum(len(k) + len(v) for k, v in returned_headers.items()) > 16384:
            raise RuntimeError("HTTP response headers exceed 16 KiB")
        text = data.decode("utf-8", errors="replace")
        try:
            parsed = json_module.loads(text)
        except ValueError:
            parsed = None
        return {"status": response.status, "headers": returned_headers, "text": text, "json": parsed}
    except (OSError, http.client.HTTPException) as exc:
        raise RuntimeError("HTTP request failed: " + type(exc).__name__ + "\n" + _diagnostic()) from None
    finally:
        connection.close()
