"""Mock WSL/ Docker lifecycle failures; candidates and subprocesses never run."""
import asyncio
import json

import pytest

from test_web_preview import FILES, IMAGE, setup
from agentforge.runtime import RuntimeErrorSafe


class LeaseProcess:
    def __init__(self):
        self.pid = 12345
        self.returncode = None
        self.exited = asyncio.Event()
        self.killed = 0
        self.reaped = 0

    def exit(self, code):
        self.returncode = code
        self.exited.set()

    def kill(self):
        self.killed += 1
        self.exit(-9)

    async def wait(self):
        await self.exited.wait()
        self.reaped += 1
        return self.returncode


def events(manager, identity):
    return [json.loads(path.read_text(encoding='utf-8'))
            for path in sorted((manager.directory / identity / 'events').glob('*.json'))]


def wsl_setup(tmp_path, monkeypatch, **kwargs):
    manager, commands = setup(tmp_path, **kwargs)
    runtime = manager.runtime
    runtime.settings.update(docker_backend='wsl', wsl_distro='AgentForge-unit')
    original = runtime._command

    async def command(args, timeout=8):
        assert args[:6] == ['wsl', '--distribution', 'AgentForge-unit', '--user', 'root', '--exec']
        assert args[6] == 'docker'
        return await original(args[6:], timeout)

    runtime._command = command
    process = LeaseProcess()
    spawned = []

    async def spawn(*args, **options):
        spawned.append((args, options))
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    return manager, commands, process, spawned


@pytest.mark.parametrize('reason,expected', [('user', 'stopped'), ('expired', 'expired')])
def test_wsl_lease_is_bounded_trusted_and_reaped_on_stop(tmp_path, monkeypatch, reason, expected):
    async def run():
        manager, commands, process, spawned = wsl_setup(tmp_path, monkeypatch)
        record = await manager.start(FILES, image_id=IMAGE, ttl_seconds=600)
        assert record['status'] == 'ready' and process.returncode is None
        argv, options = spawned[0]
        assert argv == ('wsl', '--distribution', 'AgentForge-unit', '--user', 'root', '--exec', 'sleep', '780')
        assert all(options[name] == asyncio.subprocess.DEVNULL for name in ('stdin', 'stdout', 'stderr'))
        assert 'shell' not in options and len(spawned) == 1
        assert record['wsl_lease']['candidate_executed'] is False
        assert not process.killed
        final = await manager.stop(record['id'], reason)
        assert final['status'] == expected and final['container_removed']
        assert final['wsl_lease']['status'] == 'released' and final['wsl_lease']['client_reaped']
        assert process.killed == 1 and process.reaped >= 1
        assert not manager.sessions
        assert await manager.stop(record['id']) == final
        assert process.killed == 1
    asyncio.run(run())


def test_native_preview_never_spawns_a_wsl_lease(tmp_path, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError('Native preview must not spawn WSL')

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', forbidden)

    async def run():
        manager, _ = setup(tmp_path)
        result = await manager.start(FILES, image_id=IMAGE)
        assert result['status'] == 'ready' and 'wsl_lease' not in result
        await manager.close()
    asyncio.run(run())


def test_invalid_distribution_does_not_spawn_or_launch_candidate(tmp_path, monkeypatch):
    async def run():
        manager, commands, process, spawned = wsl_setup(tmp_path, monkeypatch)
        manager.runtime.settings['wsl_distro'] = 'AgentForge; untrusted'
        record = await manager.start(FILES, image_id=IMAGE)
        assert record['status'] == 'failed' and not record['container_invoked']
        assert not spawned and not commands and not manager.sessions
    asyncio.run(run())


def test_failed_start_releases_existing_lease_without_starting_candidate(tmp_path, monkeypatch):
    async def run():
        manager, commands, process, _ = wsl_setup(tmp_path, monkeypatch, image_available=False)
        record = await manager.start(FILES, image_id=IMAGE)
        assert record['status'] == 'failed' and record['failure_kind'] == 'docker_unavailable'
        assert not record['container_invoked'] and process.killed == 1 and process.reaped >= 1
        assert record['wsl_lease']['client_reaped'] and not manager.sessions
        assert not any(command[1] == 'run' for command in commands)
    asyncio.run(run())


def test_cancel_during_lease_creation_still_obtains_handle_and_reaps(tmp_path, monkeypatch):
    async def run():
        manager, commands, process, _ = wsl_setup(tmp_path, monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_spawn(*args, **kwargs):
            entered.set()
            await release.wait()
            return process

        monkeypatch.setattr(asyncio, 'create_subprocess_exec', slow_spawn)
        task = asyncio.create_task(manager.start(FILES, image_id=IMAGE))
        await entered.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        record = manager.records()[0]
        assert record['status'] == 'interrupted' and not record['container_invoked']
        assert record['wsl_lease']['client_reaped'] and process.killed == 1 and process.reaped >= 1
        assert not manager.sessions and not commands
    asyncio.run(run())


def test_cancel_during_docker_start_keeps_unknown_attempt_and_reaps_lease(tmp_path, monkeypatch):
    async def run():
        manager, _, process, _ = wsl_setup(tmp_path, monkeypatch)
        original = manager.runtime._command

        async def cancel_start(args, timeout=8):
            if args[7] == 'run':
                raise asyncio.CancelledError()
            return await original(args, timeout)

        manager.runtime._command = cancel_start
        with pytest.raises(asyncio.CancelledError):
            await manager.start(FILES, image_id=IMAGE)
        record = manager.records()[0]
        assert record['status'] == 'interrupted' and record['container_invoked'] and record['container_removed']
        assert process.killed == 1 and process.reaped >= 1 and not manager.sessions
    asyncio.run(run())


def test_early_lease_exit_fails_preview_and_cleans_without_deadlock(tmp_path, monkeypatch):
    async def run():
        manager, _, process, _ = wsl_setup(tmp_path, monkeypatch)
        record = await manager.start(FILES, image_id=IMAGE)
        watcher = manager.sessions[record['id']]['lease_watcher']
        process.exit(255)
        await asyncio.wait_for(watcher, 2)
        final = manager.get(record['id'])
        assert final['status'] == 'failed' and final['failure_kind'] == 'wsl_lease_lost'
        assert final['url'] is None and final['container_removed'] and not manager.sessions
        assert final['wsl_lease']['unexpected_exit_code'] == 255 and final['wsl_lease']['client_reaped']
        assert not process.killed
    asyncio.run(run())


@pytest.mark.parametrize('state', [
    {'Running': False, 'ExitCode': 255, 'OOMKilled': False, 'Status': 'exited', 'FinishedAt': 'unit-time'},
    {'Running': True, 'ExitCode': 0, 'OOMKilled': False, 'Status': 'running'},
    None,
])
def test_http_failure_keeps_original_receipt_and_uses_actual_container_state(tmp_path, monkeypatch, state):
    async def run():
        manager, _, process, _ = wsl_setup(tmp_path, monkeypatch)
        record = await manager.start(FILES, image_id=IMAGE)
        original = manager.runtime._command

        async def fail_http(args, timeout=8):
            if args[7] == 'exec':
                return 1, '', 'Explicit fake: container is not running'
            if args[7] == 'inspect' and args[-2] == '{{json .State}}':
                return (1, '', 'Explicit fake: daemon unavailable') if state is None else (0, json.dumps(state), '')
            return await original(args, timeout)

        manager.runtime._command = fail_http
        response = await asyncio.wait_for(manager.request(record['id'], path='/after-idle'), 2)
        assert response['ok'] is False and response['error'] == 'docker_exec_failed'
        assert response['transport_stderr'] == 'Explicit fake: container is not running'
        history = events(manager, record['id'])
        failed_http = next(event for event in history if event['event'] == 'http_request'
                           and event['data']['response']['id'] == response['id'])
        assert failed_http['data']['response'] == response
        assert next(event for event in history if event['event'] == 'container_state_after_http_failure')['data']['request_id'] == response['id']
        final = manager.get(record['id'])
        if state and state['Running']:
            assert final['status'] == 'ready' and not process.killed
            await manager.close()
        else:
            assert final['status'] == 'failed' and final['url'] is None
            assert final['container_removed'] and final['wsl_lease']['client_reaped']
            assert final['failure_kind'] == ('container_state_unknown' if state is None else 'application_exited')
            assert final['application_exit_code'] == (None if state is None else 255)
            if state:
                assert final['container_state'] == state
            assert process.killed == 1 and not manager.sessions
            assert await manager.stop(record['id']) == final
            with pytest.raises(RuntimeErrorSafe, match='预览未运行'):
                await manager.request(record['id'])
    asyncio.run(run())


def test_cleanup_cancellation_still_reaps_lease_and_preserves_unknown_cleanup(tmp_path, monkeypatch):
    async def run():
        manager, _, process, _ = wsl_setup(tmp_path, monkeypatch)
        record = await manager.start(FILES, image_id=IMAGE)
        original = manager.runtime._command

        async def cancel_logs(args, timeout=8):
            if args[7] == 'logs':
                raise asyncio.CancelledError()
            return await original(args, timeout)

        manager.runtime._command = cancel_logs
        with pytest.raises(asyncio.CancelledError):
            await manager.stop(record['id'])
        final = manager.get(record['id'])
        assert final['status'] == 'stopped' and final['cleanup_completion_unknown']
        assert final['container_removed'] is False and final['wsl_lease']['client_reaped']
        assert process.killed == 1 and process.reaped >= 1 and not manager.sessions
    asyncio.run(run())


def test_cancellation_while_reaping_waits_for_handle_and_records_release(tmp_path, monkeypatch):
    async def run():
        manager, _, process, _ = wsl_setup(tmp_path, monkeypatch)
        record = await manager.start(FILES, image_id=IMAGE)
        # Let the watcher begin its original wait before changing the reaper.
        await asyncio.sleep(0)
        entered, finish = asyncio.Event(), asyncio.Event()
        original_wait = process.wait

        async def slow_reap():
            entered.set()
            await finish.wait()
            return await original_wait()

        process.wait = slow_reap
        stop = asyncio.create_task(manager.stop(record['id']))
        await entered.wait()
        stop.cancel()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await stop
        final = manager.get(record['id'])
        assert final['container_removed'] and final['wsl_lease']['client_reaped']
        assert final['wsl_lease']['status'] == 'released'
        assert process.killed == 1 and process.reaped >= 1 and not manager.sessions
    asyncio.run(run())


def test_already_exited_lease_is_failure_before_any_container_invocation(tmp_path, monkeypatch):
    async def run():
        manager, commands, process, _ = wsl_setup(tmp_path, monkeypatch)
        process.exit(1)
        result = await manager.start(FILES, image_id=IMAGE)
        assert result['status'] == 'failed' and result['failure_kind'] == 'wsl_lease_lost'
        assert not result['container_invoked'] and not commands
        assert result['wsl_lease']['client_reaped'] and not process.killed
        assert not manager.sessions
    asyncio.run(run())


def test_windows_crlf_sources_retain_original_hash_and_can_be_requested(tmp_path):
    async def run():
        manager, _ = setup(tmp_path)
        files = {'app.py': "# Windows CRLF fixture\r\nraise RuntimeError('never execute on host')\r\n",
                 'templates/index.html': '<h1>CRLF fixture</h1>\r\n<p>Preserve bytes</p>\r\n'}
        from agentforge.web_preview import source_digest
        record = await manager.start(files, image_id=IMAGE)
        try:
            assert record['status'] == 'ready' and record['source_sha256'] == source_digest(files)
            for name, content in files.items():
                assert (manager.directory / record['id'] / 'candidate' / name).read_bytes() == content.encode('utf-8')
            result = await manager.request(record['id'])
            assert result['ok'] and result['source_sha256'] == source_digest(files)
            assert manager.get(record['id'])['status'] == 'ready'
        finally:
            await manager.close()
    asyncio.run(run())


@pytest.mark.parametrize('original,newline', [('\r\n', '\n'), ('\n', '\r\n')])
def test_changing_only_source_newlines_is_detected_before_docker_exec(tmp_path, original, newline):
    async def run():
        manager, commands = setup(tmp_path)
        text = original.join(["# Only newline bytes change", "raise RuntimeError('never execute on host')", ''])
        record = await manager.start({'app.py': text}, image_id=IMAGE)
        before = sum(command[1] == 'exec' for command in commands)
        (manager.directory / record['id'] / 'candidate/app.py').write_bytes(text.replace(original, newline).encode('utf-8'))
        with pytest.raises(RuntimeErrorSafe, match='源码已变化'):
            await manager.request(record['id'])
        final = manager.get(record['id'])
        assert final['stop_reason'] == 'source_changed' and final['container_removed']
        assert sum(command[1] == 'exec' for command in commands) == before
        assert not manager.sessions
    asyncio.run(run())
