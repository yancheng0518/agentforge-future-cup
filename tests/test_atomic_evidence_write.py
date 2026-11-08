"""Bounded filesystem retries only; no workflow, model, or candidate execution."""
import errno
import json
from pathlib import Path
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import evaluate_tasks as writer


def test_normal_write_is_atomic_and_requires_no_retry(tmp_path, monkeypatch):
    target = tmp_path / "evidence.json"
    target.write_text('{"old": true}', encoding="utf-8")
    monkeypatch.setattr(writer.time, "sleep", lambda _: pytest.fail("No retry expected"))
    writer.write_json(target, {"status": "complete", "title": "真实记录"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"status": "complete", "title": "真实记录"}
    assert not target.with_name("evidence.json.pending").exists()


def test_transient_permission_errors_retry_the_same_serialized_pending_file(tmp_path, monkeypatch):
    target = tmp_path / "evidence.json"
    pending = target.with_name(target.name + ".pending")
    target.write_text('{"old": true}', encoding="utf-8")
    original_replace, original_write = Path.replace, Path.write_text
    attempts, writes, sleeps = [], [], []

    def tracked_write(path, content, *args, **kwargs):
        writes.append((path, content))
        return original_write(path, content, *args, **kwargs)

    def temporarily_denied(path, destination):
        attempts.append((path, destination, path.read_bytes()))
        assert json.loads(target.read_text(encoding="utf-8")) == {"old": True}
        if len(attempts) < 3:
            raise PermissionError(errno.EACCES, "synthetic sharing violation")
        return original_replace(path, destination)

    monkeypatch.setattr(Path, "write_text", tracked_write)
    monkeypatch.setattr(Path, "replace", temporarily_denied)
    monkeypatch.setattr(writer.time, "sleep", sleeps.append)
    writer.write_json(target, {"status": "complete", "token": "not-a-real-credential"}, "not-a-real-credential")
    assert len(writes) == 1 and writes[0][0] == pending
    assert len(attempts) == 3
    assert all(path == pending and destination == target for path, destination, _ in attempts)
    assert len({content for _, _, content in attempts}) == 1
    assert b"not-a-real-credential" not in attempts[0][2]
    assert sleeps == [0.05, 0.1]
    assert json.loads(target.read_text(encoding="utf-8"))["status"] == "complete"
    assert not pending.exists()


def test_exhausted_permission_errors_preserve_old_target_pending_and_last_exception(tmp_path, monkeypatch):
    target = tmp_path / "evidence.json"
    pending = target.with_name(target.name + ".pending")
    original = b'{"immutable_old_record":true}'
    target.write_bytes(original)
    failures = [PermissionError(errno.EACCES, f"synthetic denial {i}") for i in range(6)]
    attempts, sleeps = [], []

    def denied(path, destination):
        attempts.append((path, destination, path.read_bytes()))
        raise failures[len(attempts) - 1]

    monkeypatch.setattr(Path, "replace", denied)
    monkeypatch.setattr(writer.time, "sleep", sleeps.append)
    with pytest.raises(PermissionError) as caught:
        writer.write_json(target, {"new_record": True})
    assert caught.value is failures[-1]
    assert len(attempts) == 6
    assert len({data for _, _, data in attempts}) == 1
    assert len(sleeps) == 5 and sum(sleeps) == pytest.approx(1.55)
    assert sum(sleeps) <= 2
    assert target.read_bytes() == original
    assert json.loads(pending.read_text(encoding="utf-8")) == {"new_record": True}


def test_other_replace_errors_propagate_immediately(tmp_path, monkeypatch):
    target = tmp_path / "evidence.json"
    pending = target.with_name(target.name + ".pending")
    target.write_text('{"old": true}', encoding="utf-8")
    failure = OSError(errno.EIO, "synthetic I/O failure")
    attempts = []

    def failed(path, destination):
        attempts.append((path, destination))
        raise failure

    monkeypatch.setattr(Path, "replace", failed)
    monkeypatch.setattr(writer.time, "sleep", lambda _: pytest.fail("Only PermissionError is retryable"))
    with pytest.raises(OSError) as caught:
        writer.write_json(target, {"new": True})
    assert caught.value is failure
    assert len(attempts) == 1
    assert json.loads(target.read_text(encoding="utf-8")) == {"old": True}
    assert json.loads(pending.read_text(encoding="utf-8")) == {"new": True}


def test_pending_write_permission_error_is_not_retried(tmp_path, monkeypatch):
    target = tmp_path / "evidence.json"
    target.write_text('{"old": true}', encoding="utf-8")
    failure = PermissionError(errno.EACCES, "synthetic pending write denied")
    writes = []

    def denied(path, *_args, **_kwargs):
        writes.append(path)
        raise failure

    monkeypatch.setattr(Path, "write_text", denied)
    monkeypatch.setattr(Path, "replace", lambda *_: pytest.fail("No replacement before successful write"))
    monkeypatch.setattr(writer.time, "sleep", lambda _: pytest.fail("Pending writes are not retried"))
    with pytest.raises(PermissionError) as caught:
        writer.write_json(target, {"new": True})
    assert caught.value is failure
    assert len(writes) == 1
    assert json.loads(target.read_text(encoding="utf-8")) == {"old": True}


@pytest.mark.skipif(sys.platform != "win32", reason="Real Windows file sharing semantics")
def test_real_windows_read_handle_lock_is_retried_without_rewriting_payload(tmp_path, monkeypatch):
    import ctypes
    from ctypes import wintypes

    target = tmp_path / "evidence.json"
    target.write_text('{"old": true}', encoding="utf-8")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    # Read sharing is allowed; delete sharing is deliberately not allowed.
    handle = kernel.CreateFileW(str(target), 0x80000000, 1, None, 3, 0x80, None)
    assert handle != ctypes.c_void_p(-1).value, ctypes.get_last_error()
    errors = []
    original_replace = Path.replace

    def actual_replace(path, destination):
        try:
            return original_replace(path, destination)
        except PermissionError as exc:
            errors.append(exc.winerror)
            raise

    def release():
        time.sleep(0.12)
        kernel.CloseHandle(handle)

    monkeypatch.setattr(Path, "replace", actual_replace)
    thread = threading.Thread(target=release)
    thread.start()
    try:
        writer.write_json(target, {"saved_after_actual_handle_release": True})
    finally:
        thread.join()
    assert errors and all(code in (5, 32) for code in errors)
    assert json.loads(target.read_bytes()) == {"saved_after_actual_handle_release": True}
    assert not target.with_name(target.name + ".pending").exists()

