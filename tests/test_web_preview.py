"""Fake Docker transport and real local proxy tests; never execute candidates."""
import asyncio
import base64
import json
from pathlib import Path
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from agentforge.runtime import Runtime, RuntimeErrorSafe
from agentforge.web_preview import WebPreviewManager, validate_sources, validate_request, CSP

IMAGE = 'sha256:' + 'a' * 64
FILES = {'app.py': "raise RuntimeError('never execute this fixture on host')", 'static/app.js': 'console.log(1)',
         'templates/index.html': '<h1>Unit fixture</h1>'}


def setup(tmp_path, *, probe_error=False, running=True, bad_isolation=False, image_available=True):
    runtime = Runtime(ROOT, tmp_path / 'auto')
    runtime.settings['docker_backend'] = 'native'
    commands = []
    manager = WebPreviewManager(runtime, tmp_path / 'previews')

    async def mount(path):
        return str(path)

    async def command(args, timeout=8):
        commands.append(args)
        if args[1:3] == ['image', 'inspect']:
            return (0, IMAGE, '') if image_available else (1, '', 'missing image')
        if args[1] == 'run':
            return 0, 'b' * 64, ''
        if args[1] == 'logs':
            return 0, 'Explicit fake application output', 'SyntaxError: explicit fake startup failure' if not running else ''
        if args[1] == 'inspect':
            identity = args[-1].removeprefix('agentforge-web-')
            labels = {'agentforge.preview': identity, 'agentforge.scope': manager._scope}
            if args[-2] == '{{json .Config.Labels}}':
                return 0, json.dumps(labels), ''
            if args[-2] == '{{json .State}}':
                return 0, json.dumps({'Running': running, 'ExitCode': 1 if not running else 0}), ''
            value = {'Image': IMAGE, 'Config': {'User':'65534:65534', 'Labels':labels},
                'HostConfig': {'NetworkMode':'bridge' if bad_isolation else 'none', 'ReadonlyRootfs':True,
                    'Privileged':False, 'CapDrop':['ALL'], 'SecurityOpt':['no-new-privileges'],
                    'Memory':268435456, 'MemorySwap':268435456, 'NanoCpus':1000000000, 'PidsLimit':64,
                    'PortBindings':{}},
                'Mounts':[{'Type':'bind','RW':False,'Destination':d} for d in ['/candidate','/runner']]}
            return 0, json.dumps(value), ''
        if args[1] == 'exec':
            request = json.loads(base64.b64decode(args[-1]))
            result = {'nonce':request['nonce'], 'ok':not probe_error}
            if probe_error:
                result['error'] = 'ConnectionRefusedError'
            else:
                result.update(status=200, headers={'content-type':'application/json'},
                    body_base64=base64.b64encode(json.dumps({'path':request['path'],
                        'body':base64.b64decode(request['body_base64']).decode()}).encode()).decode())
            return 0, json.dumps(result), ''
        return 0, '', ''

    runtime._command, runtime._mount_path = command, mount
    return manager, commands


def test_preview_freezes_files_and_inspects_real_policy_with_no_published_ports(tmp_path):
    async def run():
        manager, commands = setup(tmp_path)
        record = await manager.start(FILES, image_id=IMAGE, owner={'kind':'unit','id':'one'})
        try:
            assert record['status'] == 'ready' and record['isolated']
            assert record['url'].startswith('http://127.0.0.1:')
            assert record['isolation']['host_ports_published'] is False
            assert record['test_claim'] is False and record['application_data_persistent'] is False
            command = next(c for c in commands if c[1] == 'run')
            assert '--network' in command and command[command.index('--network')+1] == 'none'
            assert not any(flag in command for flag in ['-p','--publish','--network=host','--privileged'])
            assert all('readonly' in value for value in command if value.startswith('type=bind'))
            assert command[-6:] == [IMAGE,'python','-I','-B','/runner/serve.py','app.py']
            result = await manager.request(record['id'], 'POST', '/api/items', '{"x":1}', {'Content-Type':'application/json'})
            assert result['ok'] and result['status'] == 200
            assert json.loads(result['body_text']) == {'path':'/api/items','body':'{"x":1}'}
            assert result['source_sha256'] == record['source_sha256']
            assert manager.records({'kind':'unit'})[0]['id'] == record['id']
        finally:
            final = await manager.stop(record['id'])
        assert final['status'] == 'stopped' and final['container_removed']
        events = list((manager.directory / record['id'] / 'events').glob('*.json'))
        assert len(events) >= 5 and manager.get(record['id'])['status'] == 'stopped'
        assert manager.sessions == {}
    asyncio.run(run())


@pytest.mark.parametrize('kwargs,kind', [({'image_available':False},'docker_unavailable'),
    ({'probe_error':True,'running':False},'application_failed'), ({'bad_isolation':True},'preview_platform_error')])
def test_failed_start_keeps_original_reason_logs_and_cleanup(tmp_path, kwargs, kind):
    async def run():
        manager, commands = setup(tmp_path, **kwargs)
        result = await manager.start(FILES, image_id=IMAGE)
        assert result['status'] == 'failed' and result['failure_kind'] == kind
        assert result['url'] is None and result['test_claim'] is False
        assert manager.get(result['id']) == result and not manager.sessions
        if kind == 'application_failed':
            assert 'SyntaxError' in result['application_stderr'] and result['application_exit_code'] == 1
        if kind == 'docker_unavailable':
            assert not any(c[1] == 'run' for c in commands)
        else:
            assert result['container_removed']
    asyncio.run(run())


@pytest.mark.parametrize('path', ['https://evil.example/', '//evil.example/', '/a/../b', '/%252e%252e/a', '/a\\b', '/a%0d%0aHost:x', '/a#fragment'])
def test_request_never_accepts_ssrf_redirect_or_header_paths(path):
    with pytest.raises(ValueError):
        validate_request('GET', path)


@pytest.mark.parametrize('headers', [{'Authorization':'hidden'}, {'Host':'evil'}, {'Content-Type':'a\r\nb'}, {'Accept':'x','accept':'y'}])
def test_request_headers_cannot_forward_credentials_or_override_transport(headers):
    with pytest.raises(ValueError):
        validate_request('GET', '/', headers=headers)


@pytest.mark.parametrize('files', [{'requirements.txt':'Flask==3'}, {'../x.py':'x'}, {'app.py':'x','APP.py':'x'},
    {'a.py':'x','a.py/b.py':'x'}, {'CON.py':'x'}, {'a.py':'x\x00'}, {'a.html':'x'*65537}])
def test_preview_source_rejects_config_paths_collisions_and_oversize(files):
    with pytest.raises(ValueError):
        validate_sources(files)


def test_expiry_and_source_change_stop_instead_of_reusing_stale_service(tmp_path):
    async def run():
        manager, commands = setup(tmp_path)
        first = await manager.start(FILES, image_id=IMAGE)
        manager.sessions[first['id']]['deadline'] = time.monotonic() - 1
        with pytest.raises(RuntimeErrorSafe, match='到期'):
            await manager.request(first['id'])
        assert manager.get(first['id'])['status'] == 'expired'
        second = await manager.start(FILES, image_id=IMAGE)
        (manager.directory / second['id'] / 'candidate/app.py').write_text('changed')
        with pytest.raises(RuntimeErrorSafe, match='源码已变化'):
            await manager.request(second['id'])
        assert manager.get(second['id'])['stop_reason'] == 'source_changed'
        assert not manager.sessions
    asyncio.run(run())


def test_local_proxy_requires_capability_cookie_and_rejects_cross_origin(tmp_path):
    async def run():
        manager, commands = setup(tmp_path)
        record = await manager.start(FILES, image_id=IMAGE)
        port = record['port']

        async def http(path='/', extra=''):
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            writer.write(f'GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n{extra}\r\n'.encode())
            await writer.drain()
            data = await reader.read()
            writer.close()
            await writer.wait_closed()
            return data.decode()
        try:
            assert (await http()).startswith('HTTP/1.1 403')
            path = record['url'].split(str(port),1)[1]
            opening = await http(path)
            assert opening.startswith('HTTP/1.1 302') and 'HttpOnly; SameSite=Strict' in opening
            cookie = next(line for line in opening.split('\r\n') if line.startswith('Set-Cookie: ')).split(': ',1)[1].split(';',1)[0]
            accepted = await http('/', f'Cookie: {cookie}\r\n')
            assert accepted.startswith('HTTP/1.1 200') and CSP in accepted and 'application/json' in accepted
            denied = await http('/', f'Cookie: {cookie}\r\nOrigin: https://evil.example\r\n')
            assert denied.startswith('HTTP/1.1 400')
            assert 'Access-Control-Allow-Origin' not in accepted
        finally:
            await manager.close()
    asyncio.run(run())


def test_recover_cleans_only_recorded_container_and_appends_without_overwrite(tmp_path):
    async def run():
        manager, commands = setup(tmp_path)
        record = await manager.start(FILES, image_id=IMAGE)
        frozen = {p:p.read_bytes() for p in (manager.directory / record['id'] / 'events').glob('*.json')}
        session = manager.sessions.pop(record['id'])
        session['timer'].cancel()
        session['server'].close()
        await session['server'].wait_closed()
        recovered = await manager.recover()
        assert recovered[0]['status'] == 'interrupted' and recovered[0]['container_removed']
        assert all(p.read_bytes() == value for p,value in frozen.items())
    asyncio.run(run())


def test_changed_trusted_runner_or_request_budget_stops_before_more_exec(tmp_path):
    async def run():
        manager, commands = setup(tmp_path)
        first = await manager.start(FILES, image_id=IMAGE)
        (manager.directory / first['id'] / 'runner/http_probe.py').write_text('tampered')
        before = sum(c[1] == 'exec' for c in commands)
        with pytest.raises(RuntimeErrorSafe, match='执行器'):
            await manager.request(first['id'])
        assert sum(c[1] == 'exec' for c in commands) == before
        second = await manager.start(FILES, image_id=IMAGE)
        manager.sessions[second['id']]['record']['request_count'] = 128
        with pytest.raises(RuntimeErrorSafe, match='128'):
            await manager.request(second['id'])
        assert manager.get(second['id'])['stop_reason'] == 'request_limit'
    asyncio.run(run())


def test_cancelled_http_retains_started_request_and_unknown_completion(tmp_path):
    async def run():
        manager, commands = setup(tmp_path)
        record = await manager.start(FILES, image_id=IMAGE)
        original = manager.runtime._command
        async def cancelled(args, timeout=8):
            if args[1] == 'exec':
                raise asyncio.CancelledError()
            return await original(args, timeout)
        manager.runtime._command = cancelled
        try:
            with pytest.raises(asyncio.CancelledError):
                await manager.request(record['id'], 'POST', '/items', '{}')
            events = [json.loads(p.read_text()) for p in sorted((manager.directory / record['id'] / 'events').glob('*.json'))]
            assert events[-2]['event'] == 'http_request_started'
            response = events[-1]['data']['response']
            assert response['completion_unknown'] and response['error'] == 'cancelled_completion_unknown'
            assert response['request_sequence'] == 2
        finally:
            await manager.close()
    asyncio.run(run())


def test_failed_cleanup_is_retried_only_for_the_recorded_container(tmp_path):
    async def run():
        manager, commands = setup(tmp_path)
        record = await manager.start(FILES, image_id=IMAGE)
        original = manager.runtime._command
        async def fail_remove(args, timeout=8):
            if args[1] == 'rm':
                return 1, '', 'Explicit fake Docker unavailable'
            return await original(args, timeout)
        manager.runtime._command = fail_remove
        final = await manager.stop(record['id'])
        assert final['status'] == 'stopped' and not final['container_removed']
        manager.runtime._command = original
        recovered = await manager.recover()
        assert recovered[0]['container_removed'] and recovered[0]['status'] == 'stopped'
        assert not await manager.recover()
    asyncio.run(run())
