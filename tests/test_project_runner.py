"""Explicit fake transport tests. No candidate execution, real Docker or paid calls."""
import asyncio
import base64
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from agentforge.project_runner import ProjectRunner, digest, validate_state
from agentforge.projects import ProjectStore
from agentforge.runtime import Runtime, RuntimeErrorSafe


def setup(tmp_path, *, app_code=0, state=None, receipt=True, timeout=False):
    directory = tmp_path / 'projects'
    store = ProjectStore(directory / 'projects.sqlite3')
    imported = store.import_project('Unit app', {'main.py': "raise RuntimeError('NEVER EXECUTE ON HOST')\n"})
    runtime = Runtime(ROOT, tmp_path / 'auto')
    commands = []

    async def health():
        return {'sandbox': {'available': True}}

    async def mount(path):
        return str(path)

    async def command(args, timeout=8):
        commands.append(args)
        if 'inspect' in args:
            return 0, 'sha256:' + 'a' * 64, ''
        if 'run' in args:
            if setup_timeout:
                raise TimeoutError()
            request_file = next((directory / 'executions').glob('*/input/request.json'))
            request = json.loads(request_file.read_text())
            output = dict(nonce=request['nonce'], completed=True, exit_code=app_code,
                          stdout='Explicit fake unit result', stderr='', state=state or {}, state_valid=True)
            return 0, 'AGENTFORGE_APPLICATION=' + json.dumps(output) if receipt else 'no receipt', ''
        return 0, '', ''

    setup_timeout = timeout
    runtime.health, runtime._mount_path, runtime._command = health, mount, command
    runtime.settings['docker_backend'] = 'native'
    runner = ProjectRunner(runtime, directory, store)
    return runner, imported, commands, store


def test_success_persists_only_bounded_receipted_state_and_uses_readonly_mounts(tmp_path):
    state = {'items.json': base64.b64encode(b'[1]').decode()}
    runner, imported, commands, store = setup(tmp_path, state=state)
    result = asyncio.run(runner.execute(imported['project']['id'], imported['revision'], 'main.py', ['--safe', 'x;echo no']))
    assert result['status'] == 'succeeded' and result['isolated'] and result['data_saved']
    assert result['test_claim'] is False
    command = next(args for args in commands if 'run' in args)
    for flag in ('--read-only', '--network', '--cap-drop', '--pids-limit', '--user'):
        assert flag in command
    assert all('readonly' in value for value in command if value.startswith('type=bind'))
    assert store.application_state(imported['project']['id'])['state'] == state
    assert store.executions(imported['project']['id'])[0]['record']['source_sha256'] == imported['revision']['source_sha256']
    assert commands[-1][-3:-1] == ['rm', '-f']


@pytest.mark.parametrize('kwargs', [{'app_code': 1}, {'receipt': False}, {'timeout': True}, {'state': {'../escape': 'YQ=='}}])
def test_failed_or_invalid_execution_never_commits_application_data(tmp_path, kwargs):
    runner, imported, commands, store = setup(tmp_path, **kwargs)
    store.commit_execution(imported['project']['id'], {'id':'seed-unit', 'kind':'unit_state_seed'}, state={'old.json':'e30='},
                           expected_state_sha256=digest({}))
    before = store.application_state(imported['project']['id'])
    result = asyncio.run(runner.execute(imported['project']['id'], imported['revision'], 'main.py', []))
    assert not result['data_saved'] and result['status'] != 'succeeded'
    assert store.application_state(imported['project']['id']) == before
    assert len(store.executions(imported['project']['id'])) == 2


@pytest.mark.parametrize('state', [{'x': 'not base64!'}, {'X': '', 'x': ''}, {'a': 'YQ==' * 50000}, {'../a': ''}, {'a\\b': ''}])
def test_state_transport_rejects_escape_collision_bad_encoding_and_size(state):
    with pytest.raises(RuntimeErrorSafe):
        validate_state(state)


def test_unknown_entrypoint_and_oversized_args_never_start_docker(tmp_path):
    runner, imported, commands, _ = setup(tmp_path)
    for entry, args in [('../main.py', []), ('main.py', ['x'] * 33), ('main.py', ['\x00'])]:
        with pytest.raises(ValueError):
            asyncio.run(runner.execute(imported['project']['id'], imported['revision'], entry, args))
    assert not commands


def test_runner_rejects_cross_project_revision_even_without_api_guard(tmp_path):
    runner, imported, commands, store = setup(tmp_path)
    other = store.import_project('Another', {'main.py':'print(1)'})
    with pytest.raises(ValueError):
        asyncio.run(runner.execute(other['project']['id'], imported['revision'], 'main.py', []))
    modified = {**imported['revision'], 'files':{'main.py':'different'}}
    with pytest.raises(ValueError):
        asyncio.run(runner.execute(imported['project']['id'], modified, 'main.py', []))
    assert not commands
