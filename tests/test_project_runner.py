"""Explicit fake transport tests. No candidate execution, real Docker or paid calls."""
import asyncio
import base64
from contextlib import closing
import json
from pathlib import Path
import sqlite3
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


def test_state_cas_conflict_records_completed_app_without_overwriting_winner(tmp_path):
    proposed, winner = {'app.json': 'WzFd'}, {'winner.json': 'WzJd'}
    runner, imported, commands, store = setup(tmp_path, state=proposed)
    project_id = imported['project']['id']
    command = runner.runtime._command

    async def concurrent_command(args, timeout=8):
        result = await command(args, timeout)
        if 'run' in args:
            store.commit_execution(project_id, {'kind': 'concurrent-unit-winner'}, winner, digest({}))
        return result

    runner.runtime._command = concurrent_command
    result = asyncio.run(runner.execute(project_id, imported['revision'], 'main.py', []))
    assert result['status'] == 'blocked' and result['application_completed'] is True
    assert result['application_status'] == 'succeeded' and result['exit_code'] == 0
    assert result['data_saved'] is False and result['state_commit_status'] == 'conflict'
    assert result['state_commit_unknown'] is False and result['evidence_recorded'] is True
    assert result['data_before_sha256'] == digest({})
    assert result['data_after_sha256'] == digest(winner)
    assert result['data_after_version'] == 1
    assert store.application_state(project_id)['state'] == winner
    records = store.executions(project_id)
    assert len(records) == 2 and records[-1]['record'] == result
    assert sum('run' in args for args in commands) == 1
    intent = json.loads((runner.directory / result['intent_path']).read_text(encoding='utf-8'))
    assert intent['kind'] == 'application_commit_intent' and intent['status'] == 'pending_commit'
    assert intent['completion_claim'] is False and intent['data_saved'] is False
    assert intent['data_after_sha256'] is None and intent['proposed_data_sha256'] == digest(proposed)


@pytest.mark.parametrize('seed_state', [False, True])
def test_real_sqlite_state_write_failure_rolls_back_and_records_uncertainty(tmp_path, seed_state):
    proposed = {'new.json': 'WzNd'}
    runner, imported, commands, store = setup(tmp_path, state=proposed)
    project_id = imported['project']['id']
    if seed_state:
        store.commit_execution(project_id, {'kind': 'unit-seed'}, {'old.json': 'e30='}, digest({}))
    before = store.application_state(project_id)
    action = 'UPDATE' if seed_state else 'INSERT'
    with closing(sqlite3.connect(store.path)) as db, db:
        db.execute(f"CREATE TRIGGER reject_state BEFORE {action} ON application_states BEGIN SELECT RAISE(ABORT,'unit storage failure'); END")
    result = asyncio.run(runner.execute(project_id, imported['revision'], 'main.py', []))
    assert result['status'] == 'blocked' and result['application_completed'] is True
    assert result['application_status'] == 'succeeded'
    assert result['data_saved'] is False and result['state_commit_unknown'] is True
    assert result['state_commit_status'] == 'unknown' and result['storage_error_type'] == 'IntegrityError'
    assert result['data_after_sha256'] == before['sha256']
    assert store.application_state(project_id) == before
    assert store.executions(project_id)[-1]['record'] == result
    assert (runner.directory / result['intent_path']).is_file()
    assert sum('run' in args for args in commands) == 1


def test_unavailable_storage_returns_unknown_with_intent_and_never_retries_state(tmp_path, monkeypatch):
    runner, imported, commands, store = setup(tmp_path, state={'new.json': 'WzRd'})
    project_id = imported['project']['id']
    before = store.application_state(project_id)
    original_read = store.application_state
    reads, commits = [], []

    def unavailable_commit(project, record, state=None, expected_state_sha256=None):
        commits.append(state)
        raise sqlite3.OperationalError('Explicit unavailable unit database')

    def unavailable_after_initial_read(project):
        reads.append(project)
        if len(reads) > 1:
            raise sqlite3.OperationalError('Explicit unavailable unit database')
        return original_read(project)

    monkeypatch.setattr(store, 'commit_execution', unavailable_commit)
    monkeypatch.setattr(store, 'application_state', unavailable_after_initial_read)
    result = asyncio.run(runner.execute(project_id, imported['revision'], 'main.py', []))
    assert result['status'] == 'blocked' and result['application_completed'] is True
    assert result['data_saved'] is False and result['state_commit_unknown'] is True
    assert result['data_after_sha256'] is None and result['state_observation_unknown'] is True
    assert result['evidence_recorded'] is False and result['evidence_commit_unknown'] is True
    assert len(commits) == 2 and commits[0] == {'new.json': 'WzRd'} and commits[1] is None
    reopened = ProjectStore(store.path)
    assert reopened.application_state(project_id) == before
    assert reopened.executions(project_id) == []
    assert sum('run' in args for args in commands) == 1
    intent_path = runner.directory / result['intent_path']
    assert intent_path.is_file()
    archived = json.loads((intent_path.parent / 'record.json').read_text(encoding='utf-8'))
    assert archived == result and archived['state_commit_unknown'] is True


def test_storage_error_after_commit_is_unknown_and_reports_observed_current_hash(tmp_path, monkeypatch):
    proposed = {'committed.json': 'WzVd'}
    runner, imported, commands, store = setup(tmp_path, state=proposed)
    project_id = imported['project']['id']
    commit = store.commit_execution
    calls = []

    def ambiguous_commit(project, record, state=None, expected_state_sha256=None):
        calls.append(state)
        saved = commit(project, record, state, expected_state_sha256)
        if state is not None:
            raise OSError('Unit simulated lost confirmation after actual commit')
        return saved

    monkeypatch.setattr(store, 'commit_execution', ambiguous_commit)
    result = asyncio.run(runner.execute(project_id, imported['revision'], 'main.py', []))
    assert result['status'] == 'blocked' and result['state_commit_unknown'] is True
    assert result['data_saved'] is False and result['application_status'] == 'succeeded'
    assert result['data_after_sha256'] == digest(proposed)
    assert store.application_state(project_id)['state'] == proposed
    assert len(calls) == 2 and calls[1] is None
    records = store.executions(project_id)
    assert records[0]['record']['data_saved'] is True  # actual atomic transaction evidence
    assert records[-1]['record']['state_commit_unknown'] is True  # caller could not confirm it
    assert sum('run' in args for args in commands) == 1


def test_intent_archive_failure_prevents_state_commit_and_preserves_completed_app_evidence(tmp_path, monkeypatch):
    runner, imported, commands, store = setup(tmp_path, state={'new.json': 'WzZd'})
    project_id = imported['project']['id']
    before = store.application_state(project_id)
    write = Path.write_text

    def fail_intent(path, *args, **kwargs):
        if path.name == 'record-intent.json':
            raise OSError('Unit archive unavailable')
        return write(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'write_text', fail_intent)
    result = asyncio.run(runner.execute(project_id, imported['revision'], 'main.py', []))
    assert result['status'] == 'blocked' and result['application_completed'] is True
    assert result['data_saved'] is False and result['state_commit_status'] == 'not_attempted'
    assert result['state_commit_unknown'] is False and result['intent_available'] is False
    assert result['data_after_sha256'] == before['sha256']
    assert store.application_state(project_id) == before
    assert store.executions(project_id)[-1]['record'] == result
    assert result['intent_path'].endswith('/record-intent.json')


def test_final_file_export_failure_does_not_relabel_confirmed_database_commit(tmp_path, monkeypatch):
    proposed = {'saved.json': 'Wzdd'}
    runner, imported, _, store = setup(tmp_path, state=proposed)
    project_id = imported['project']['id']
    write = Path.write_text

    def fail_final_export(path, *args, **kwargs):
        if path.name == 'record.json':
            raise OSError('Unit derived export unavailable')
        return write(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'write_text', fail_final_export)
    result = asyncio.run(runner.execute(project_id, imported['revision'], 'main.py', []))
    assert result['status'] == 'succeeded' and result['data_saved'] is True
    assert result['file_export_failed'] is True and result['state_commit_unknown'] is False
    assert store.application_state(project_id)['state'] == proposed
    saved = store.executions(project_id)[-1]['record']
    assert saved['status'] == 'succeeded' and saved['data_saved'] is True
    assert saved['state_commit_status'] == 'committed'


def test_cancelled_application_records_interruption_without_committing_data(tmp_path):
    runner, imported, _, store = setup(tmp_path, state={'new.json': 'Wzhd'})
    project_id = imported['project']['id']
    before = store.application_state(project_id)
    command = runner.runtime._command

    async def cancel_command(args, timeout=8):
        if 'run' in args:
            raise asyncio.CancelledError()
        return await command(args, timeout)

    runner.runtime._command = cancel_command
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runner.execute(project_id, imported['revision'], 'main.py', []))
    assert store.application_state(project_id) == before
    result = store.executions(project_id)[-1]['record']
    assert result['status'] == 'blocked' and result['completion_unknown'] is True
    assert result['application_completed'] is False and result['data_saved'] is False
