"""Explicit Docker application execution; candidate source is never imported on host."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
import re
import time
from uuid import uuid4

from .autonomous_sandbox import AutonomousSandbox, _sources, _json
from .runtime import RuntimeErrorSafe
from .storage import now


def digest(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def validate_state(value):
    if not isinstance(value, dict) or len(value) > 32:
        raise RuntimeErrorSafe('应用数据最多 32 个文件。')
    total = 0
    names = set()
    for name, content in value.items():
        if (not isinstance(name, str) or len(name) > 160 or len(name.split('/')) > 5
                or any(not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}', part)
                       or part in ('.', '..') for part in name.split('/'))
                or not isinstance(content, str) or len(content) > 88000):
            raise RuntimeErrorSafe('应用数据路径或内容不符合约束。')
        if name.casefold() in names:
            raise RuntimeErrorSafe('应用数据存在大小写冲突。')
        names.add(name.casefold())
        try:
            total += len(base64.b64decode(content, validate=True))
        except ValueError:
            raise RuntimeErrorSafe('应用数据编码无效。') from None
    if total > 65536:
        raise RuntimeErrorSafe('应用持久数据超过 64 KiB。')
    return value


class ProjectRunner:
    def __init__(self, runtime, directory, store):
        self.runtime, self.directory, self.store = runtime, Path(directory), store
        self.locks = {}

    async def execute(self, project_id, revision, entrypoint, args):
        if not isinstance(revision, dict) or revision.get('project_id') != project_id:
            raise ValueError('项目版本不能跨项目运行。')
        registered = self.store.revision(project_id, revision.get('id', ''))
        if registered != revision:
            raise ValueError('应用运行需要该项目真实的不可变版本。')
        files = _sources(revision['files'])
        if entrypoint not in files or not isinstance(args, list) or len(args) > 32 or any(
                not isinstance(arg, str) or '\x00' in arg or len(arg) > 1000 for arg in args):
            raise ValueError('请选择版本内的 Python 入口，参数最多 32 项、每项 1000 字符。')
        if not re.fullmatch(r'[a-f0-9]{32}', project_id):
            raise ValueError('项目标识无效。')
        async with self.locks.setdefault(project_id, asyncio.Lock()):
            return await self._execute(project_id, revision, entrypoint, args, files)

    async def _execute(self, project_id, revision, entrypoint, arguments, files):
        identity, nonce = uuid4().hex, uuid4().hex
        start = time.monotonic()
        base = self.directory / 'executions' / identity
        check = lambda path: AutonomousSandbox._check(path, self.directory)
        check(base)
        base.mkdir(parents=True)
        stored_state = self.store.application_state(project_id)
        state = validate_state(stored_state['state'])
        proposed_state = None
        record = dict(id=identity, project_id=project_id, revision_id=revision['id'], created_at=now(),
                      source_sha256=digest(files), entrypoint=entrypoint, args=arguments,
                      kind='application_execution', status='blocked', isolated=False,
                      container_invoked=False, exit_code=None, stdout='', stderr='', image=None,
                      data_before_sha256=digest(state), data_after_sha256=digest(state), data_saved=False,
                      reason='', test_claim=False)
        if record['source_sha256'] != revision['source_sha256']:
            raise RuntimeErrorSafe('项目版本源码哈希不一致。')
        for name, text in files.items():
            target = base / 'candidate' / name
            check(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding='utf-8', newline='')
        request = dict(entrypoint=entrypoint, args=arguments, state=state, nonce=nonce)
        (base / 'input').mkdir()
        (base / 'input/request.json').write_text(_json(request), encoding='utf-8')
        runner = self.runtime.root / 'sandbox/project/run_app.py'
        runner_bytes = runner.read_bytes()
        (base / 'runner').mkdir()
        (base / 'runner/run_app.py').write_bytes(runner_bytes)
        record['runner_sha256'] = hashlib.sha256(runner_bytes).hexdigest()
        record['request_sha256'] = digest(request)
        name = 'agentforge-app-' + identity[:16]
        try:
            health = await self.runtime.health()
            if not health['sandbox']['available']:
                raise RuntimeErrorSafe(health['sandbox']['reason'])
            code, image, _ = await self.runtime._command(self.runtime._docker_command(
                'image', 'inspect', '--format', '{{.Id}}', self.runtime.settings['image']), timeout=15)
            image = image.strip()
            if code or not re.fullmatch(r'sha256:[a-f0-9]{64}', image):
                raise RuntimeErrorSafe('不能确认 Docker 镜像 ID，未执行应用。')
            record['image'] = image
            command = self.runtime._docker_command('run', '--rm', '--name', name, '--network', 'none',
                '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                '--memory', '128m', '--memory-swap', '128m', '--cpus', '1', '--pids-limit', '64',
                '--user', '65534:65534', '--workdir', '/data',
                '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m,mode=1777', '--tmpfs', '/data:rw,noexec,nosuid,size=16m,mode=1777')
            for folder, target in (('candidate', '/candidate'), ('runner', '/runner'), ('input', '/input')):
                check(base / folder)
                mount = await self.runtime._mount_path(base / folder)
                if ',' in mount:
                    raise RuntimeErrorSafe('Docker 挂载路径不支持逗号。')
                command += ['--mount', f'type=bind,source={mount},target={target},readonly']
            command += [image, 'python', '-I', '-B', '/runner/run_app.py']
            record['container_invoked'] = True
            code, stdout, stderr = await self.runtime._command(command, timeout=25)
            record['container_exit_code'] = code
            prefix = 'AGENTFORGE_APPLICATION='
            receipts = [json.loads(line[len(prefix):]) for line in stdout.splitlines() if line.startswith(prefix)]
            if code or len(receipts) != 1 or receipts[0].get('nonce') != nonce:
                raise RuntimeErrorSafe('应用容器缺少有效完成回执；不保存数据。')
            receipt = receipts[0]
            record.update(isolated=True, exit_code=receipt.get('exit_code'),
                          stdout=str(receipt.get('stdout', ''))[:12000], stderr=str(receipt.get('stderr', ''))[:8000],
                          stdout_truncated=receipt.get('stdout_truncated', False),
                          stderr_truncated=receipt.get('stderr_truncated', False), reason=receipt.get('reason', ''))
            if receipt.get('timed_out'):
                record['timed_out'] = True
            # Verify read-only source bytes still match the immutable revision.
            current = {path: (base / 'candidate' / path).read_text(encoding='utf-8') for path in files}
            if digest(current) != revision['source_sha256']:
                raise RuntimeErrorSafe('运行期间源码变化；回执不能用于该版本。')
            if receipt.get('completed') is True and type(receipt.get('exit_code')) is int:
                record['status'] = 'succeeded' if receipt['exit_code'] == 0 else 'failed'
            if record['status'] == 'succeeded' and receipt.get('state_valid') is True:
                updated = validate_state(receipt['state'])
                (base / 'data-after.json').write_text(_json(updated), encoding='utf-8')
                proposed_state = updated
                record.update(data_saved=True, data_after_sha256=digest(updated))
            elif record['status'] == 'succeeded':
                record['status'] = 'failed'
        except asyncio.CancelledError:
            proposed_state = None
            record.update(status='blocked', completion_unknown=True, data_saved=False,
                          data_after_sha256=digest(state), reason='运行被中断；保留原数据，不宣称应用已完成。')
            raise
        except (OSError, ValueError, RuntimeErrorSafe, TimeoutError) as exc:
            proposed_state = None
            record.update(status='blocked', data_saved=False, data_after_sha256=digest(state),
                          reason=str(exc) if isinstance(exc, RuntimeErrorSafe) else type(exc).__name__)
            if isinstance(exc, TimeoutError):
                record['timed_out'] = True
        finally:
            try:
                await self.runtime._command(self.runtime._docker_command('rm', '-f', name), timeout=5)
            except (OSError, RuntimeErrorSafe, TimeoutError, asyncio.CancelledError):
                pass
            record['duration_ms'] = round((time.monotonic() - start) * 1000)
            # A prepared intent is not a completion claim. State and its execution
            # receipt become authoritative together in one SQLite transaction.
            (base / 'record-intent.json').write_text(_json(record), encoding='utf-8')
            self.store.commit_execution(project_id, record, state=proposed_state,
                                        expected_state_sha256=stored_state['sha256'])
            try:
                (base / 'record.json').write_text(_json(record), encoding='utf-8')
            except OSError:
                # The committed DB receipt stays true even if a derived file export fails.
                record['file_export_failed'] = True
        return record
