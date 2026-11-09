"""Bounded multi-file Python workspace for the autonomous orchestration kernel.

The host only reads and writes source text. All acceptance and candidate Python
runs in Docker; the trusted unittest parent calls a separate candidate process.
This is an isolation boundary for accidental failures, not a proof against an
actively malicious candidate sharing the container's UID and process namespace.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import time
import uuid

from .runtime import RuntimeErrorSafe

MAX_FILES = 32
MAX_FILE_BYTES = 65536
MAX_PROJECT_BYTES = 262144
RUNNER_FILES = ("runner.py", "bridge.py", "worker.py")


class SandboxIntegrityError(RuntimeErrorSafe):
    """Candidate state could not be restored; this run must stop permanently."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _path(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 160:
        raise RuntimeErrorSafe("候选文件路径无效。")
    parts = value.split("/")
    if ("\\" in value or not value.endswith(".py") or len(parts) > 8
            or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part[:-3] if index == len(parts) - 1 else part)
                   for index, part in enumerate(parts))
            or any(part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))} for part in parts)):
        raise RuntimeErrorSafe("候选路径只支持项目内的 Python 模块和包。")
    return str(PurePosixPath(value))


def _sources(value, *, allow_empty=False):
    if not isinstance(value, dict) or not (0 if allow_empty else 1) <= len(value) <= MAX_FILES:
        raise RuntimeErrorSafe("候选项目需要 1 至 32 个 Python 源文件。")
    result, normalized = {}, set()
    for name, content in value.items():
        name = _path(name)
        if name.casefold() in normalized:
            raise RuntimeErrorSafe("候选文件存在大小写冲突。")
        normalized.add(name.casefold())
        if not isinstance(content, str) or "\x00" in content or len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise RuntimeErrorSafe("候选源文件必须是最多 64 KiB 的 UTF-8 文本。")
        result[name] = content
    if sum(len(text.encode("utf-8")) for text in result.values()) > MAX_PROJECT_BYTES:
        raise RuntimeErrorSafe("候选项目超过 256 KiB 限制。")
    # Windows cannot distinguish package directories that differ only in case.
    directories = {}
    for name in result:
        for parent in PurePosixPath(name).parents:
            key = str(parent).casefold()
            if key in directories and directories[key] != str(parent):
                raise RuntimeErrorSafe("候选目录存在大小写冲突。")
            directories[key] = str(parent)
    return dict(sorted(result.items()))


def validate_bundle(bundle):
    """Validate and detach a trusted input bundle; no Python is executed."""
    if not isinstance(bundle, dict) or set(bundle) != {"requirement", "files", "acceptance", "provenance"}:
        raise RuntimeErrorSafe("任务包需要且仅接受 requirement/files/acceptance/provenance。")
    requirement, acceptance, provenance = bundle["requirement"], bundle["acceptance"], bundle["provenance"]
    if not isinstance(requirement, str) or not 1 <= len(requirement.strip()) <= 16000:
        raise RuntimeErrorSafe("需求必须为 1 至 16000 字符。")
    if not isinstance(acceptance, str) or not acceptance.strip() or "\x00" in acceptance or len(acceptance.encode("utf-8")) > 65536:
        raise RuntimeErrorSafe("独立验收必须为最多 64 KiB 的 Python unittest 文本。")
    if not isinstance(provenance, dict) or provenance.get("kind") not in ("natural", "seeded_defect"):
        raise RuntimeErrorSafe("任务包必须声明 natural 或 seeded_defect 来源。")
    try:
        if len(_json(provenance)) > 8000:
            raise ValueError()
        provenance = json.loads(_json(provenance))
    except (TypeError, ValueError):
        raise RuntimeErrorSafe("任务来源需要有效且不超过 8000 字符的 JSON。") from None
    return {"requirement": requirement, "files": _sources(bundle["files"]), "acceptance": acceptance, "provenance": provenance}


class AutonomousSandbox:
    validate_bundle = staticmethod(validate_bundle)

    def __init__(self, runtime):
        self.runtime = runtime

    def _base(self, run_id):
        base = self.runtime._run_dir(run_id)
        # Reject reparse points before resolving subordinate paths.
        for path in (base, base / "candidate", base / "trusted", base / "runner", base / "evidence"):
            self._check(path, base)
        return base

    @staticmethod
    def _check(path, base):
        if not path.resolve().is_relative_to(base.resolve()):
            raise RuntimeErrorSafe("任务文件路径越出隔离工作区。")
        for item in (path, *path.parents):
            if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
                raise RuntimeErrorSafe("隔离工作区不允许符号链接或目录联接。")
            if item == base:
                break
        if path.is_file() and path.stat().st_nlink > 1:
            raise RuntimeErrorSafe("隔离工作区不允许硬链接。")

    def _read_text(self, path, base):
        self._check(path, base)
        return path.read_text(encoding="utf-8")

    def _write(self, path, text, base):
        self._check(path, base)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            temp.write_text(text, encoding="utf-8", newline="")
            os.replace(temp, path)
        finally:
            if temp.exists():
                temp.unlink()

    def _manifest(self, run_id):
        base = self._base(run_id)
        manifest = json.loads(self._read_text(base / "autonomous_manifest.json", base))
        frozen = json.loads(self._read_text(base / "bundle.json", base))
        if _digest(_json(frozen)) != manifest["bundle_sha256"]:
            raise RuntimeErrorSafe("冻结任务包已变化；停止运行。")
        if _digest(self._read_text(base / "trusted" / "test_acceptance.py", base)) != manifest["acceptance_sha256"]:
            raise RuntimeErrorSafe("冻结独立验收已变化；停止运行。")
        for filename, expected in manifest["runner_sha256"].items():
            if _digest(self._read_text(base / "runner" / filename, base)) != expected:
                raise RuntimeErrorSafe("冻结隔离执行器已变化；停止运行。")
        return manifest

    async def prepare(self, run_id, bundle):
        bundle = validate_bundle(bundle)
        base = self._base(run_id)
        if (base / "autonomous_manifest.json").exists():
            manifest = self._manifest(run_id)
            if manifest["bundle_sha256"] != _digest(_json(bundle)):
                raise RuntimeErrorSafe("已冻结的运行不能更换任务包。")
            return manifest
        if (base / "candidate").exists():
            raise RuntimeErrorSafe("候选目录已存在但缺少自主运行清单；拒绝覆盖。")
        runner = self.runtime.root / "sandbox" / "autonomous"
        runner_sources = {name: (runner / name).read_text(encoding="utf-8") for name in RUNNER_FILES}
        manifest = {"schema": "autonomous-python-v1", "bundle_sha256": _digest(_json(bundle)),
                    "acceptance_sha256": _digest(bundle["acceptance"]),
                    "runner_sha256": {name: _digest(source) for name, source in runner_sources.items()},
                    "baseline_sha256": _digest(_json(bundle["files"])), "provenance": bundle["provenance"]}
        self._write(base / "bundle.json", _json(bundle), base)
        self._write(base / "trusted" / "test_acceptance.py", bundle["acceptance"], base)
        for name, content in bundle["files"].items():
            self._write(base / "candidate" / name, content, base)
        for name, content in runner_sources.items():
            self._write(base / "runner" / name, content, base)
        self._write(base / "autonomous_manifest.json", _json(manifest), base)
        await self._snapshot(run_id, "prepared")
        return manifest

    async def read(self, run_id):
        base = self._base(run_id)
        self._manifest(run_id)
        candidate = base / "candidate"
        sources = {}
        for path in candidate.rglob("*"):
            self._check(path, base)
            if path.is_file():
                if path.stat().st_size > MAX_FILE_BYTES:
                    raise RuntimeErrorSafe("候选源文件超过 64 KiB 限制。")
                sources[path.relative_to(candidate).as_posix()] = self._read_text(path, base)
        return _sources(sources)

    async def fingerprint(self, run_id):
        return _digest(_json(await self.read(run_id)))

    async def _snapshot(self, run_id, action, extra=None):
        sources = await self.read(run_id)
        record = {"action": action, "candidate_sha256": _digest(_json(sources)), "files": sources, **(extra or {})}
        base = self._base(run_id)
        name = f"{time.time_ns()}-{action}-{uuid.uuid4().hex[:8]}.json"
        self._write(base / "evidence" / name, _json(record), base)
        return {"candidate_sha256": record["candidate_sha256"], "snapshot": "evidence/" + name}

    async def apply(self, run_id, files, allowed_paths):
        files = _sources(files, allow_empty=True)
        if not isinstance(allowed_paths, list) or len(allowed_paths) > MAX_FILES:
            raise RuntimeErrorSafe("子任务必须声明有界写入范围。")
        allowed = {_path(path) for path in allowed_paths}
        if not set(files).issubset(allowed):
            raise RuntimeErrorSafe("子 Agent 请求修改授权范围之外的文件。")
        before = await self.read(run_id)
        _sources({**before, **files})
        base = self._base(run_id)
        for path in files:
            self._check(base / "candidate" / path, base)
        before_record = await self._snapshot(run_id, "apply-before", {"requested_paths": sorted(files)})
        try:
            for path, content in files.items():
                self._write(base / "candidate" / path, content, base)
        except Exception:
            restored = True
            for path in files:
                try:
                    target = base / "candidate" / path
                    self._check(target, base)
                    if path in before:
                        self._write(target, before[path], base)
                    elif target.exists():
                        target.unlink()
                except Exception:
                    restored = False
            try:
                await self._snapshot(run_id, "apply-failed", {"rollback_completed": restored, "before_snapshot": before_record["snapshot"]})
            except Exception:
                restored = False
            reason = "已恢复修改前候选。" if restored else "回滚未获确认；该运行必须停止并保留现场。"
            error_type = RuntimeErrorSafe if restored else SandboxIntegrityError
            raise error_type("子任务多文件写入失败；" + reason) from None
        return await self._snapshot(run_id, "applied", {"changed_paths": sorted(path for path in files if files[path] != before.get(path)),
                                                       "before_sha256": _digest(_json(before))})

    async def test(self, run_id):
        started = time.monotonic()
        manifest = self._manifest(run_id)
        snapshot = await self._snapshot(run_id, "validation-start")
        result = {"status": "blocked", "exit_code": None, "isolated": False, "stdout": "", "stderr": "", "test_count": 0,
                  "candidate_sha256": snapshot["candidate_sha256"], "hash": snapshot["candidate_sha256"],
                  "acceptance_sha256": manifest["acceptance_sha256"], "runner_sha256": manifest["runner_sha256"],
                  "image": None, "container_invoked": False, "reason": "", "snapshot": snapshot["snapshot"]}
        health = await self.runtime.health()
        if not health["sandbox"]["available"]:
            result["reason"] = health["sandbox"]["reason"]
            await self._save_result(run_id, result, started)
            return result
        base = self._base(run_id)
        name, nonce = "agentforge-auto-" + uuid.uuid4().hex[:16], uuid.uuid4().hex
        try:
            code, image_id, error = await self.runtime._command(self.runtime._docker_command("image", "inspect", "--format", "{{.Id}}", self.runtime.settings["image"]), timeout=15)
            image_id = image_id.strip()
            if code or not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
                raise RuntimeErrorSafe("无法冻结 Docker 镜像 ID；未执行候选代码。")
            result["image"] = image_id
            args = self.runtime._docker_command("run", "--rm", "--name", name, "--network", "none", "--read-only", "--cap-drop", "ALL",
                       "--security-opt", "no-new-privileges", "--memory", "128m", "--memory-swap", "128m", "--cpus", "1", "--pids-limit", "64",
                       "--user", "65534:65534", "--workdir", "/tmp", "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m")
            for source, target in ((base / "candidate", "/candidate"), (base / "trusted", "/trusted"), (base / "runner", "/runner")):
                mount = await self.runtime._mount_path(source)
                if "," in mount:
                    raise RuntimeErrorSafe("Docker 挂载路径含不支持的逗号。")
                args.extend(["--mount", f"type=bind,source={mount},target={target},readonly"])
            args.extend([image_id, "python", "-I", "-B", "/runner/runner.py", nonce])
            result["container_invoked"] = True
            code, out, err = await self.runtime._command(args, timeout=40)
            result.update(exit_code=code, stdout=out, stderr=err, isolated=code not in (125, 126, 127), status="failed")
            prefix = "AGENTFORGE_AUTONOMOUS="
            attestations = []
            for line in out.splitlines():
                if line.startswith(prefix):
                    try:
                        attestations.append(json.loads(line[len(prefix):]))
                    except ValueError:
                        pass
            attestation = attestations[0] if len(attestations) == 1 else {}
            count = attestation.get("tests")
            if (attestation.get("nonce") == nonce and type(count) is int and count > 0
                    and type(attestation.get("discovered")) is int and attestation["discovered"] == count
                    and type(attestation.get("passed")) is bool):
                result["test_count"] = count
                result["attestation"] = attestation
                if code == 0 and attestation["passed"]:
                    result["status"] = "passed"
                elif not (code == 1 and attestation["passed"] is False):
                    result.update(status="blocked", isolated=False, reason="验收回执与容器退出状态矛盾。")
            if not result["isolated"]:
                result.update(status="blocked", reason="容器启动失败；未完成隔离验收。")
            elif not result.get("attestation"):
                result.update(status="blocked", isolated=False, reason="独立验收缺少有效且非空的完整测试回执。")
            # A host-side edit during the container run invalidates the result.
            if await self.fingerprint(run_id) != result["candidate_sha256"]:
                result.update(status="blocked", reason="候选在验证期间变化；本次结果不能用于完成判定。")
        except TimeoutError:
            result.update(status="blocked", isolated=False, reason="Docker 命令超时；隔离验收执行状态未获完整确认，已请求清理。")
        except asyncio.CancelledError:
            result.update(status="blocked", isolated=False, completion_unknown=True, reason="隔离验收被取消；执行是否完成未知，已请求清理。")
            await self._save_result(run_id, result, started)
            raise
        except (OSError, RuntimeErrorSafe) as exc:
            result.update(status="blocked", reason=str(exc))
        finally:
            try:
                await self.runtime._command(self.runtime._docker_command("rm", "-f", name), timeout=5)
            except (OSError, TimeoutError, RuntimeErrorSafe):
                pass
        await self._save_result(run_id, result, started)
        return result

    async def _save_result(self, run_id, result, started):
        result["duration_ms"] = round((time.monotonic() - started) * 1000)
        base = self._base(run_id)
        filename = f"{time.time_ns()}-validation-result-{uuid.uuid4().hex[:8]}.json"
        result["result_record"] = "evidence/" + filename
        self._write(base / "evidence" / filename, _json(result), base)
