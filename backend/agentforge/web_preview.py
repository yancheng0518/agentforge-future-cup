"""Local preview of frozen Web sources through a network-none Docker container.

No candidate is imported on the host and no Docker port is published. A bounded
trusted Docker-exec probe is the only HTTP transport. UI callers must embed the
returned URL in a sandboxed iframe on the workbench's different origin; do not
present a candidate page as a trusted top-level workbench page.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
from http.cookies import CookieError, SimpleCookie
import json
from pathlib import Path, PurePosixPath
import re
import secrets
import time
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from .autonomous_sandbox import AutonomousSandbox, _json
from .runtime import RuntimeErrorSafe
from .storage import now

MAX_REQUEST_BYTES = 16384
MAX_RESPONSE_BYTES = 65536
MAX_HTTP_REQUESTS = 128
TERMINAL = {'stopped', 'expired', 'failed', 'interrupted'}
METHODS = {'GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'}
CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
       "form-action 'self'; frame-src 'none'; worker-src 'none'; media-src 'none'; "
       "sandbox allow-scripts allow-forms allow-same-origin")


def source_digest(files):
    return hashlib.sha256(_json(files).encode('utf-8')).hexdigest()


def validate_sources(files):
    """Text only; dependency/runtime policy is supplied separately, never source."""
    if not isinstance(files, dict) or not 1 <= len(files) <= 32:
        raise ValueError('Web 项目需要 1 至 32 个文本源文件。')
    result, seen, directories, total = {}, set(), {}, 0
    for path, content in files.items():
        if (not isinstance(path, str) or len(path) > 160 or '\\' in path or path.startswith('/')
                or len(path.split('/')) > 6 or PurePosixPath(path).suffix.lower() not in {'.py', '.html', '.css', '.js', '.json'}
                or any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]{0,63}', part) or part in ('.', '..')
                       or part.split('.', 1)[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *('COM'+str(i) for i in range(1,10)), *('LPT'+str(i) for i in range(1,10))}
                       for part in path.split('/'))):
            raise ValueError('Web 源码路径无效；不接收依赖、锁或运行配置文件。')
        if (not isinstance(content, str) or '\x00' in content or len(content.encode('utf-8')) > 65536
                or path.casefold() in seen):
            raise ValueError('Web 源文件必须为有界 UTF-8 文本，不能大小写冲突。')
        seen.add(path.casefold())
        for parent in PurePosixPath(path).parents:
            key = str(parent).casefold()
            if key in directories and directories[key] != str(parent):
                raise ValueError('Web 源码目录存在大小写冲突。')
            directories[key] = str(parent)
        total += len(content.encode('utf-8'))
        result[path] = content
    if total > 262144 or any(name in directories for name in seen):
        raise ValueError('Web 源码超过 256 KiB 或文件/目录冲突。')
    return dict(sorted(result.items()))


def validate_request(method, path, body='', headers=None):
    if method not in METHODS or not isinstance(path, str) or not 1 <= len(path) <= 4096:
        raise ValueError('HTTP 方法或路径无效。')
    decoded = path
    for _ in range(4):
        decoded = unquote(decoded)
    parsed = urlsplit(path)
    if (not path.startswith('/') or path.startswith('//') or parsed.scheme or parsed.netloc or parsed.fragment
            or '\\' in decoded or any(ord(c) < 32 or ord(c) == 127 for c in decoded)
            or any(part in ('.', '..') for part in decoded.split('?', 1)[0].split('/'))):
        raise ValueError('HTTP 工具只接受应用内相对路径。')
    if not isinstance(body, (str, bytes)):
        raise ValueError('HTTP body 必须为文本或字节。')
    encoded = body.encode('utf-8') if isinstance(body, str) else body
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ValueError('HTTP body 超过 16 KiB。')
    headers = {} if headers is None else headers
    if not isinstance(headers, dict) or len(headers) > 2:
        raise ValueError('HTTP headers 只接受 Accept 和 Content-Type。')
    normalized = {}
    for key, value in headers.items():
        if (not isinstance(key, str) or key.lower() not in ('accept', 'content-type')
                or not isinstance(value, str) or len(value) > 200 or key.lower() in normalized
                or any(ord(c) < 32 or ord(c) > 126 for c in value)):
            raise ValueError('HTTP 请求头不受支持。')
        normalized[key.lower()] = value
    return method, path, encoded, normalized


class WebPreviewManager:
    def __init__(self, runtime, directory):
        self.runtime = runtime
        self.directory = Path(directory).resolve()
        self.sessions = {}
        self._scope = hashlib.sha256(str(self.directory).encode()).hexdigest()[:20]

    def _base(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(r'[a-f0-9]{32}', identity):
            raise ValueError('预览 ID 无效。')
        path = self.directory / identity
        AutonomousSandbox._check(path, self.directory)
        return path

    def _event(self, record, event, data=None):
        base = self._base(record['id'])
        folder = base / 'events'
        AutonomousSandbox._check(folder, self.directory)
        folder.mkdir(parents=True, exist_ok=True)
        snapshot = {'event': event, 'at': now(), 'record': json.loads(_json(record)), 'data': data or {}}
        with (folder / f'{time.time_ns()}-{uuid4().hex[:8]}.json').open('x', encoding='utf-8') as stream:
            stream.write(_json(snapshot))
        return snapshot

    def get(self, identity):
        base = self._base(identity)
        events = sorted((base / 'events').glob('*.json'))
        if not events:
            return None
        AutonomousSandbox._check(events[-1], self.directory)
        return json.loads(events[-1].read_text(encoding='utf-8'))['record']

    def records(self, owner=None):
        records = []
        if not self.directory.exists():
            return records
        for folder in sorted(self.directory.iterdir()):
            if re.fullmatch(r'[a-f0-9]{32}', folder.name) and folder.is_dir():
                value = self.get(folder.name)
                if value and (owner is None or all(value.get('owner', {}).get(k) == v for k, v in owner.items())):
                    records.append(value)
        return sorted(records, key=lambda item: item['created_at'], reverse=True)

    async def start(self, files, *, image_id, entrypoint='app.py', owner=None, ttl_seconds=600):
        files = validate_sources(files)
        if (entrypoint not in files or not re.fullmatch(r'(?:[A-Za-z_][A-Za-z0-9_]*/)*[A-Za-z_][A-Za-z0-9_]*\.py', entrypoint)
                or not isinstance(image_id, str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', image_id)
                or type(ttl_seconds) is not int or not 10 <= ttl_seconds <= 600):
            raise ValueError('预览要求项目内 Python 入口、固定镜像 ID 与 10 至 600 秒期限。')
        if len(self.sessions) >= 4:
            raise RuntimeErrorSafe('最多同时运行 4 个 Web 预览，请先停止已有预览。')
        owner = {} if owner is None else owner
        if not isinstance(owner, dict) or len(_json(owner)) > 2000:
            raise ValueError('预览 owner 必须为有界 JSON。')
        identity, token = uuid4().hex, secrets.token_hex(24)
        base = self._base(identity)
        base.mkdir(parents=True, exist_ok=False)
        container = 'agentforge-web-' + identity
        record = {'id': identity, 'created_at': now(), 'status': 'starting', 'owner': owner,
                  'source_sha256': source_digest(files), 'image_id': image_id, 'entrypoint': entrypoint,
                  'container_name': container, 'container_invoked': False, 'isolated': False,
                  'ttl_seconds': ttl_seconds, 'url': None, 'port': None, 'request_count': 0,
                  'test_claim': False, 'application_data_persistent': False, 'reason': '',
                  'failure_kind': None, 'request_transport': 'docker_exec_loopback_only'}
        self._event(record, 'start_requested')
        session = {'record': record, 'files': files, 'token': token, 'server': None, 'timer': None,
                   'deadline': time.monotonic() + ttl_seconds, 'lock': asyncio.Lock(),
                   'requests': asyncio.Semaphore(4), 'browser_clients': 0}
        self.sessions[identity] = session
        try:
            for path, content in files.items():
                target = base / 'candidate' / path
                AutonomousSandbox._check(target, self.directory)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding='utf-8', newline='')
            (base / 'runner').mkdir()
            record['runner_sha256'] = {}
            for name in ('serve.py', 'http_probe.py'):
                content = (self.runtime.root / 'sandbox/web' / name).read_bytes()
                (base / 'runner' / name).write_bytes(content)
                record['runner_sha256'][name] = hashlib.sha256(content).hexdigest()
            code, image, _ = await self.runtime._command(self.runtime._docker_command(
                'image', 'inspect', '--format', '{{.Id}}', image_id), timeout=15)
            if code or image.strip() != image_id:
                record['failure_kind'] = 'docker_unavailable'
                raise RuntimeErrorSafe('依赖镜像不可用；未启动 Web 应用。')
            command = self.runtime._docker_command('run', '-d', '--name', container,
                '--label', 'agentforge.preview=' + identity, '--label', 'agentforge.scope=' + self._scope,
                '--network', 'none', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                '--memory', '256m', '--memory-swap', '256m', '--cpus', '1', '--pids-limit', '64',
                '--user', '65534:65534', '--workdir', '/candidate',
                '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m,mode=1777',
                '--tmpfs', '/data:rw,noexec,nosuid,size=16m,mode=1777',
                '--env', 'PORT=8080', '--env', 'FLASK_DEBUG=0', '--env', 'AGENTFORGE_DATA_DIR=/data',
                '--log-opt', 'max-size=1m', '--log-opt', 'max-file=1')
            for folder in ('candidate', 'runner'):
                mount = await self.runtime._mount_path(base / folder)
                if ',' in mount:
                    raise RuntimeErrorSafe('Docker 挂载路径不支持逗号。')
                command += ['--mount', f'type=bind,source={mount},target=/{folder},readonly']
            command += [image_id, 'python', '-I', '-B', '/runner/serve.py', entrypoint]
            record['container_invoked'] = True
            self._event(record, 'container_starting')
            code, output, error = await self.runtime._command(command, timeout=30)
            if code or not re.fullmatch(r'[a-f0-9]{64}', output.strip()):
                record['failure_kind'] = 'docker_start_failed'
                record['startup_stderr'] = error[-6000:]
                raise RuntimeErrorSafe('Docker 未确认 Web 容器启动；完整尝试已保留。')
            record['container_id'] = output.strip()
            await self._inspect(session)
            record['isolated'] = True
            ready = None
            deadline = time.monotonic() + min(45, ttl_seconds)
            while time.monotonic() < deadline:
                ready = await self._probe(session, 'GET', '/', b'', {}, origin='readiness')
                if ready.get('ok'):
                    break
                state = await self._state(session)
                if not state.get('Running'):
                    record['failure_kind'] = 'application_failed'
                    record['application_exit_code'] = state.get('ExitCode')
                    raise RuntimeErrorSafe('Web 应用启动后退出；请查看该次应用日志。')
                await asyncio.sleep(0.3)
            if not ready or not ready.get('ok'):
                record['failure_kind'] = 'application_not_ready'
                raise RuntimeErrorSafe('Web 应用未在期限内监听 127.0.0.1:8080。')
            server = await asyncio.start_server(lambda reader, writer: self._browser(session, reader, writer),
                                                host='127.0.0.1', port=0, limit=8192)
            session['server'] = server
            port = server.sockets[0].getsockname()[1]
            record.update(status='ready', port=port, origin=f'http://127.0.0.1:{port}',
                          url=f'http://127.0.0.1:{port}/_agentforge/{token}/',
                          initial_http_status=ready['status'], ready_at=now())
            session['deadline'] = time.monotonic() + ttl_seconds
            self._event(record, 'ready')
            session['timer'] = asyncio.create_task(self._expire(identity, ttl_seconds))
        except asyncio.CancelledError:
            record.update(status='interrupted', reason='预览启动被取消。', failure_kind='cancelled')
            await self._cleanup(session)
            self._event(record, 'interrupted')
            self.sessions.pop(identity, None)
            raise
        except (OSError, RuntimeErrorSafe, TimeoutError, ValueError, KeyError) as error:
            record.update(status='failed', reason=str(error) if isinstance(error, RuntimeErrorSafe) else type(error).__name__)
            record['failure_kind'] = record['failure_kind'] or 'preview_platform_error'
            await self._cleanup(session)
            self._event(record, 'failed')
            self.sessions.pop(identity, None)
        return json.loads(_json(record))

    async def _inspect(self, session):
        record = session['record']
        code, value, _ = await self.runtime._command(self.runtime._docker_command(
            'inspect', '--format', '{{json .}}', record['container_name']), timeout=15)
        if code:
            raise RuntimeErrorSafe('无法核实 Web 容器隔离配置。')
        data = json.loads(value)
        host, config = data.get('HostConfig', {}), data.get('Config', {})
        labels = config.get('Labels', {})
        mounts = data.get('Mounts', [])
        if (data.get('Image') != record['image_id'] or host.get('NetworkMode') != 'none'
                or host.get('ReadonlyRootfs') is not True or host.get('Privileged') is not False
                or 'ALL' not in host.get('CapDrop', []) or 'no-new-privileges' not in host.get('SecurityOpt', [])
                or host.get('Memory') != 268435456 or host.get('MemorySwap') != 268435456
                or host.get('NanoCpus') != 1000000000 or host.get('PidsLimit') != 64
                or config.get('User') != '65534:65534' or host.get('PortBindings')
                or labels.get('agentforge.preview') != record['id'] or labels.get('agentforge.scope') != self._scope
                or {m.get('Destination') for m in mounts} != {'/candidate', '/runner'}
                or any(m.get('RW') is not False or m.get('Type') != 'bind' for m in mounts)):
            raise RuntimeErrorSafe('Web 容器隔离配置不符合冻结策略，停止预览。')
        record['isolation'] = {'network': 'none', 'root_readonly': True, 'nonroot': True,
                               'capabilities_dropped': True, 'host_ports_published': False,
                               'mounts_readonly': True, 'resource_limits_verified': True}

    async def _state(self, session):
        code, value, _ = await self.runtime._command(self.runtime._docker_command('inspect', '--format',
            '{{json .State}}', session['record']['container_name']), timeout=10)
        return json.loads(value) if not code else {'Running': False, 'ExitCode': None}

    async def _probe(self, session, method, path, body, headers, *, origin):
        record = session['record']
        nonce, identity = uuid4().hex, uuid4().hex
        request = {'nonce': nonce, 'method': method, 'path': path, 'headers': headers,
                   'body_base64': base64.b64encode(body).decode('ascii')}
        encoded = base64.b64encode(_json(request).encode()).decode('ascii')
        begin = time.monotonic()
        result = {'id': identity, 'preview_id': record['id'], 'method': method, 'path': path,
                  'origin': origin, 'source_sha256': record['source_sha256'], 'image_id': record['image_id'],
                  'isolated': bool(record.get('isolated')), 'test_claim': False, 'ok': False}
        record['request_count'] += 1
        result['request_sequence'] = record['request_count']
        self._event(record, 'http_request_started', {'id': identity, 'request': request, 'origin': origin})
        cancelled = False
        try:
            code, output, error = await self.runtime._command(self.runtime._docker_command('exec',
                record['container_name'], 'python', '-I', '-B', '/runner/http_probe.py', encoded), timeout=12)
            if code:
                result.update(error='docker_exec_failed', transport_stderr=error[-2000:])
            else:
                receipt = json.loads(output)
                if not isinstance(receipt, dict) or receipt.get('nonce') != nonce:
                    raise RuntimeErrorSafe('Web HTTP 回执缺少有效 nonce。')
                if receipt.get('ok') is not True:
                    result['error'] = str(receipt.get('error', 'http_probe_failed'))[:100]
                else:
                    content = base64.b64decode(receipt['body_base64'], validate=True)
                    response_headers = receipt.get('headers', {})
                    if (type(receipt.get('status')) is not int or not 100 <= receipt['status'] <= 599
                            or len(content) > MAX_RESPONSE_BYTES or not isinstance(response_headers, dict)
                            or any(k not in ('content-type', 'location') or not isinstance(v, str) or len(v) > 2048
                                   or '\r' in v or '\n' in v for k, v in response_headers.items())):
                        raise RuntimeErrorSafe('Web HTTP 回执内容无效或过大。')
                    result.update(ok=True, status=receipt['status'], headers=response_headers,
                                  body_base64=receipt['body_base64'], body_text=content.decode('utf-8', errors='replace'))
        except asyncio.CancelledError:
            result.update(error='cancelled_completion_unknown', completion_unknown=True)
            cancelled = True
        except (OSError, TimeoutError, ValueError, RuntimeErrorSafe, KeyError) as error:
            result['error'] = str(error) if isinstance(error, RuntimeErrorSafe) else type(error).__name__
        result['duration_ms'] = round((time.monotonic() - begin) * 1000)
        self._event(record, 'http_request', {'request': request, 'response': result})
        if cancelled:
            raise asyncio.CancelledError()
        return result

    async def request(self, identity, method='GET', path='/', body='', headers=None, *, origin='agent_tool'):
        method, path, body, headers = validate_request(method, path, body, headers)
        session = self.sessions.get(identity)
        if not session or session['record']['status'] != 'ready':
            raise RuntimeErrorSafe('预览未运行或已经到期。')
        if time.monotonic() >= session['deadline']:
            await self.stop(identity, reason='expired')
            raise RuntimeErrorSafe('预览已到期。')
        async with session['requests']:
            if session['record']['request_count'] >= MAX_HTTP_REQUESTS:
                await self.stop(identity, reason='request_limit')
                raise RuntimeErrorSafe('预览 HTTP 请求达到 128 次上限，已停止服务。')
            if not self._frozen_files_match(session):
                await self.stop(identity, reason='source_changed')
                raise RuntimeErrorSafe('预览冻结源码已变化或执行器不一致，停止服务。')
            result = await self._probe(session, method, path, body, headers, origin=origin)
            if not self._frozen_files_match(session):
                await self.stop(identity, reason='source_changed')
                raise RuntimeErrorSafe('HTTP 请求期间冻结源码或执行器变化，结果不可用。')
            return result

    def _frozen_files_match(self, session):
        base = self._base(session['record']['id'])
        current = {}
        try:
            for name in session['files']:
                path = base / 'candidate' / name
                AutonomousSandbox._check(path, self.directory)
                if path.stat().st_size > 65536:
                    return False
                current[name] = path.read_text(encoding='utf-8')
            for name, expected in session['record']['runner_sha256'].items():
                path = base / 'runner' / name
                AutonomousSandbox._check(path, self.directory)
                if path.stat().st_size > 65536 or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                    return False
        except (OSError, UnicodeError, RuntimeErrorSafe):
            return False
        return source_digest(current) == session['record']['source_sha256']

    async def _browser(self, session, reader, writer):
        status, content, extra = 400, b'Invalid preview request', {}
        record = session['record']
        session['browser_clients'] += 1
        try:
            if session['browser_clients'] > 8:
                status, content = 429, b'Too many concurrent preview requests'
                return
            raw = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5)
            if len(raw) > 8192:
                raise ValueError('Header too large')
            lines = raw.decode('ascii').split('\r\n')
            method, path, version = lines[0].split(' ')
            headers = {}
            for line in lines[1:]:
                if not line:
                    continue
                name, value = line.split(':', 1)
                name = name.lower()
                if not re.fullmatch(r'[a-z0-9-]+', name) or name in headers:
                    raise ValueError('Invalid/duplicate header')
                headers[name] = value.strip()
            expected_host = f'127.0.0.1:{record["port"]}'
            if (version != 'HTTP/1.1' or headers.get('host') != expected_host
                    or 'transfer-encoding' in headers or 'upgrade' in headers
                    or headers.get('origin', record['origin']) != record['origin']
                    or headers.get('sec-fetch-site') == 'cross-site'):
                raise ValueError('Untrusted HTTP origin/transport')
            length = int(headers.get('content-length', '0'))
            if not 0 <= length <= MAX_REQUEST_BYTES:
                raise ValueError('Body too large')
            body = await asyncio.wait_for(reader.readexactly(length), 5)
            capability = f'/_agentforge/{session["token"]}/'
            cookie = SimpleCookie()
            cookie.load(headers.get('cookie', ''))
            cookie_name = 'af_preview_' + record['id']
            if method == 'GET' and secrets.compare_digest(path, capability):
                status, content = 302, b'Opening isolated preview'
                extra = {'Location': '/', 'Set-Cookie': f'{cookie_name}={session["token"]}; Path=/; HttpOnly; SameSite=Strict; Max-Age={record["ttl_seconds"]}'}
            elif cookie_name not in cookie or not secrets.compare_digest(cookie[cookie_name].value, session['token']):
                status, content = 403, b'Open this preview from the workbench first.'
            else:
                selected = {k: v for k, v in headers.items() if k in ('accept', 'content-type')}
                reply = await asyncio.wait_for(self.request(record['id'], method, path, body, selected, origin='browser'), 18)
                if not reply['ok']:
                    status, content = 502, ('Application transport failed: ' + reply['error']).encode('utf-8')
                else:
                    status = reply['status']
                    content = base64.b64decode(reply['body_base64'])
                    extra = {'Content-Type': reply['headers'].get('content-type', 'application/octet-stream')}
                    location = reply['headers'].get('location')
                    if location:
                        try:
                            validate_request('GET', location)
                        except ValueError:
                            status, content, extra = 502, b'External or unsupported redirect blocked', {}
                        else:
                            extra['Location'] = location
            if method == 'HEAD':
                content = b''
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError, TimeoutError,
                UnicodeError, ValueError, RuntimeErrorSafe, KeyError, CookieError):
            pass
        except asyncio.CancelledError:
            status, content = 503, b'Preview is stopping'
        finally:
            session['browser_clients'] -= 1
            response_headers = {'Content-Type': 'text/plain; charset=utf-8', **extra,
                'Content-Length': str(len(content)), 'Connection': 'close', 'Cache-Control': 'no-store',
                'Content-Security-Policy': CSP, 'X-Content-Type-Options': 'nosniff',
                'Referrer-Policy': 'no-referrer', 'Cross-Origin-Resource-Policy': 'same-site',
                'Permissions-Policy': 'camera=(), microphone=(), geolocation=(), payment=(), usb=()'}
            output = f'HTTP/1.1 {status} Preview\r\n' + ''.join(f'{k}: {v}\r\n' for k,v in response_headers.items()) + '\r\n'
            try:
                writer.write(output.encode('latin-1') + content)
                await writer.drain()
            except (OSError, RuntimeError):
                pass
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def _expire(self, identity, delay):
        try:
            await asyncio.sleep(delay)
            await self.stop(identity, reason='expired')
        except asyncio.CancelledError:
            pass

    async def _cleanup(self, session):
        record = session['record']
        server = session.get('server')
        if server:
            server.close()
            await server.wait_closed()
        if record.get('container_invoked'):
            try:
                code, stdout, stderr = await self.runtime._command(self.runtime._docker_command(
                    'logs', '--tail', '40', record['container_name']), timeout=10)
                record['application_stdout'], record['application_stderr'] = stdout[-12000:], stderr[-12000:]
                record['log_capture_exit_code'] = code
            except (OSError, RuntimeErrorSafe, TimeoutError) as error:
                record['log_capture_error'] = type(error).__name__
            try:
                code, _, _ = await self.runtime._command(self.runtime._docker_command('rm', '-f', record['container_name']), timeout=10)
                record['container_removed'] = code == 0
            except (OSError, RuntimeErrorSafe, TimeoutError) as error:
                record.update(container_removed=False, cleanup_error=type(error).__name__)
        record['stopped_at'] = now()

    async def stop(self, identity, reason='user'):
        session = self.sessions.get(identity)
        if not session:
            previous = self.get(identity)
            if previous is None:
                raise ValueError('预览不存在。')
            return previous
        async with session['lock']:
            timer = session.get('timer')
            if timer and timer is not asyncio.current_task():
                timer.cancel()
            record = session['record']
            record.update(status='expired' if reason == 'expired' else 'stopped', stop_reason=reason)
            await self._cleanup(session)
            self._event(record, record['status'])
            self.sessions.pop(identity, None)
            return json.loads(_json(record))

    async def recover(self):
        """Only remove containers recorded by this exact workspace manager."""
        recovered = []
        for record in self.records():
            retry_cleanup = record.get('container_invoked') and record.get('container_removed') is not True
            if (record['status'] in TERMINAL and not retry_cleanup) or record['id'] in self.sessions:
                continue
            if record.get('container_name') != 'agentforge-web-' + record['id']:
                raise RuntimeErrorSafe('遗留预览容器名称不匹配，未清理未知容器。')
            code, value, _ = await self.runtime._command(self.runtime._docker_command('inspect', '--format',
                '{{json .Config.Labels}}', record['container_name']), timeout=10)
            labels = json.loads(value) if not code else {}
            if labels and (labels.get('agentforge.preview') != record['id'] or labels.get('agentforge.scope') != self._scope):
                raise RuntimeErrorSafe('遗留容器标签不匹配，未清理未知容器。')
            event = 'cleanup_recovery' if record['status'] in TERMINAL else 'interrupted_recovery'
            if record['status'] not in TERMINAL:
                record.update(status='interrupted', reason='工作台重启，旧预览不恢复运行。')
            await self._cleanup({'record': record, 'server': None})
            self._event(record, event)
            recovered.append(record)
        return recovered

    async def close(self):
        for identity in list(self.sessions):
            await self.stop(identity, reason='shutdown')
