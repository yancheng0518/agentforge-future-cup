"""Web contract/sandbox regressions with fake transports; no candidate execution."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import shutil
from unittest.mock import AsyncMock, Mock

import pytest

from agentforge.autonomous_model import (AutonomousModel, canonical_sha256, validate_web_intake,
                                         validate_project_tool)
from agentforge.autonomous_sandbox import AutonomousSandbox, _sources, _json, _digest, validate_bundle
from agentforge.runtime import Runtime, RuntimeErrorSafe
from agentforge.storage import Store

ROOT = Path(__file__).resolve().parents[1]
WEB = {'schema': 'python-web-v1', 'entrypoint': 'app.py', 'dependency_id': 'b' * 64,
       'image_id': 'sha256:' + 'a' * 64, 'lock_sha256': 'c' * 64, 'requirements': ['Flask==3.1.3']}
FILES = {'app.py': '# A Web baseline fixture; never executed by these unit tests.\n',
         'templates/index.html': '<h1>Existing page</h1>', 'static/site.css': 'body { color: black; }'}
SUITE = '''import unittest
from web_bridge import request
class WebTests(unittest.TestCase):
    def test_page(self):
        self.assertIn('Existing page', request('GET', '/')['text'])
    def test_api(self):
        self.assertEqual(request('GET', '/api/items')['status'], 200)
    def test_invalid(self):
        self.assertEqual(request('POST', '/api/items', json={'title':''})['status'], 400)
'''


def proposal():
    return {'status': 'ready', 'summary': '新增有界 HTTP 功能', 'specification': '页面和 API 保留既有功能',
            'assumptions': [], 'files': {}, 'acceptance': SUITE}


def bundle():
    return {'requirement': '新增真实 Web API', 'files': dict(FILES), 'acceptance': SUITE,
            'provenance': {'kind': 'natural', 'web': deepcopy(WEB)}}


def test_web_sources_accept_assets_without_changing_python_fingerprint():
    legacy = {'module.py': 'def f():\n    return 1\n'}
    # Known legacy fingerprint; adding asset support must not migrate these bytes.
    expected = 'bd8706cc74686182dcc2307612d0e8284a61a5a1e8ab7ee9cd07694aae0e950d'
    assert _digest(_json(_sources(legacy))) == expected
    assets = {**FILES, 'static/app.min.js': 'fetch("/api/items")', 'data/options.json': '{}'}
    assert _sources(assets) == dict(sorted(assets.items()))


@pytest.mark.parametrize('path', ['../x.html', '/x.html', 'C:/x.html', 'a\\x.html', '.env',
                                  'static/NUL.json', 'static/../x.css', 'static//x.js', 'package.json',
                                  'package-lock.json', 'requirements.txt', 'Dockerfile', 'server.sh', 'x.svg'])
def test_web_sources_reject_paths_and_dependency_manifests(path):
    with pytest.raises(RuntimeErrorSafe):
        _sources({path: ''})


def test_web_intake_freezes_real_http_tests_and_empty_new_assets():
    value = proposal()
    value['files'] = {'templates/new.html': '', 'helpers.py': 'def fn():\n    raise NotImplementedError\n'}
    assert validate_web_intake(value, FILES) == value
    assert validate_web_intake({'status':'needs_input','summary':'范围不足','questions':['需要哪个页面？']}, FILES)['status'] == 'needs_input'


@pytest.mark.parametrize('path', [
    'f"/api/items/{created[\'json\'][\'id\']}"',
    '"/api/items/" + str(created["json"]["id"])',
    'item_path',
])
def test_web_intake_accepts_crud_paths_derived_from_real_response_ids(path):
    value = proposal()
    value['acceptance'] = value['acceptance'].replace(
        "self.assertIn('Existing page', request('GET', '/')['text'])",
        'created = request("POST", "/api/items", json={"title":"unique-edit-record"})\n'
        '        item_path = "/api/items/" + str(created["json"]["id"])\n'
        f'        response = request("PATCH", {path}, json={{"title":"updated"}})\n'
        '        self.assertEqual(response["status"], 200)')
    assert validate_web_intake(value, FILES) == value


@pytest.mark.parametrize('change', [
    lambda p: p.update(files={'app.py':'replacement'}),
    lambda p: p.update(files={1:'bad path type'}),
    lambda p: p.update(files={'new.html':'<h1>already implemented</h1>'}),
    lambda p: p.update(files={'helpers.py':'def f():\n    return 1\n'}),
    lambda p: p.update(acceptance=p['acceptance'].replace('from web_bridge import request','import requests')),
    lambda p: p.update(acceptance=p['acceptance'].replace("request('GET', '/')", "request('GET', 'http://outside.invalid/')")),
    lambda p: p.update(acceptance=p['acceptance'].replace("request('GET', '/')", "request('GET', '/../private')")),
    lambda p: p.update(acceptance=p['acceptance'].replace("request('GET', '/')", "request('CONNECT', '/')")),
    lambda p: p.update(acceptance=p['acceptance'].replace("request('GET', '/')", "request(method, '/')")),
    lambda p: p.update(acceptance=p['acceptance'].replace("request('GET', '/')", "request('GET', 123)")),
    lambda p: p.update(acceptance=p['acceptance'].replace("request('GET', '/')", "request('GET', *paths)")),
    lambda p: p.update(acceptance=p['acceptance'].replace("request('GET', '/')", "request('GET', '/', timeout=1)")),
    lambda p: p.update(acceptance=p['acceptance'].replace("self.assertIn('Existing page', request('GET', '/')['text'])", "self.assertTrue(True)")),
    lambda p: p.update(acceptance=p['acceptance'] + '\ndef load_tests(loader, suite, pattern):\n    return unittest.TestSuite()\n'),
])
def test_web_intake_rejects_self_fulfilling_unsafe_or_implemented_contracts(change):
    value = proposal()
    change(value)
    with pytest.raises(ValueError):
        validate_web_intake(value, FILES)


@pytest.mark.parametrize('path', ['https://outside.invalid/', '//outside.invalid/', '/a/../x', '/%2e%2e/x',
                                  '/%252e%252e/x', '/\\outside', '/%2f%2foutside', '/x\r\nHeader: x'])
def test_http_tool_rejects_external_targets_and_traversal(path):
    with pytest.raises(ValueError):
        validate_project_tool('http_request', {'method':'GET','path':path})


def test_http_tool_body_limits_and_asset_read_differ_from_python_entrypoint():
    assert validate_project_tool('http_request', {'method':'POST','path':'/api/items','body':'{"title":"x"}'})['method'] == 'POST'
    with pytest.raises(ValueError):
        validate_project_tool('http_request', {'method':'POST','path':'/','body':'x' * 16385})
    assert validate_project_tool('read_file', {'path':'templates/index.html'})['path'].endswith('.html')
    with pytest.raises(ValueError):
        validate_project_tool('run_application', {'entrypoint':'templates/index.html','args':[]})


def environment(tmp_path, passed=True):
    shutil.copytree(ROOT / 'sandbox/web', tmp_path / 'sandbox/web')
    shutil.copytree(ROOT / 'sandbox/autonomous', tmp_path / 'sandbox/autonomous')
    runtime = Runtime(tmp_path, tmp_path / 'data')
    runtime.settings.update(image='ignored-base-image:unit', docker_backend='native')
    runtime.health = AsyncMock(return_value={'sandbox':{'available':True,'reason':'fake unit transport'}})
    commands = []
    async def command(args, timeout=8):
        commands.append(args)
        if args[1:3] == ['image','inspect']:
            return 0, WEB['image_id'], ''
        if args[1] == 'run':
            value = {'nonce':args[-2], 'tests':3, 'discovered':3, 'passed':passed,
                     'verification_kind':'http', 'http_requests':3,
                     'startup_error':None if passed else 'candidate syntax error', 'server_exit_code': None}
            return (0 if passed else 1), 'AGENTFORGE_AUTONOMOUS=' + json.dumps(value), ''
        return 0, '', ''
    runtime._command = AsyncMock(side_effect=command)
    return runtime, AutonomousSandbox(runtime), commands


def test_web_sandbox_freezes_runtime_and_uses_dependency_image_with_no_network(tmp_path):
    async def check():
        runtime, sandbox, commands = environment(tmp_path)
        manifest = await sandbox.prepare('web', bundle())
        assert manifest['web_runtime_sha256'] == canonical_sha256(WEB)
        assert set(manifest['runner_sha256']) == {'runner.py','web_bridge.py','serve.py'}
        assert manifest['schema'] == 'autonomous-python-web-v1'
        result = await sandbox.test('web')
        assert result['status'] == 'passed' and result['isolated'] and result['test_count'] == 3
        assert result['dependency_id'] == WEB['dependency_id'] and result['image'] == WEB['image_id']
        run_command = next(command for command in commands if command[1] == 'run')
        assert run_command[run_command.index('--network')+1] == 'none'
        assert '--publish' not in run_command and '-p' not in run_command
        assert run_command[-1] == 'app.py'
        assert all(run_command[i+1].endswith(',readonly') for i,arg in enumerate(run_command) if arg == '--mount')
        assert commands[0][-1] == WEB['image_id']
    asyncio.run(check())


def test_web_candidate_startup_failure_is_real_failed_suite_not_infrastructure_block(tmp_path):
    async def check():
        _, sandbox, _ = environment(tmp_path, passed=False)
        await sandbox.prepare('web', bundle())
        result = await sandbox.test('web')
        assert result['status'] == 'failed' and result['isolated'] and result['test_count'] == 3
        assert result['attestation']['startup_error'] == 'candidate syntax error'
    asyncio.run(check())


@pytest.mark.parametrize('field,value', [('http_requests', 0), ('http_requests', 65),
                                        ('verification_kind', 'function'), ('startup_error', 'failed'),
                                        ('server_exit_code', 0)])
def test_web_host_rejects_success_receipt_without_a_healthy_http_server(tmp_path, field, value):
    async def check():
        runtime, sandbox, _ = environment(tmp_path)
        await sandbox.prepare('web', bundle())
        command = runtime._command.side_effect
        async def corrupted(args, timeout=8):
            code, out, error = await command(args, timeout)
            if args[1] == 'run':
                receipt = json.loads(out.split('=', 1)[1])
                receipt[field] = value
                out = 'AGENTFORGE_AUTONOMOUS=' + json.dumps(receipt)
            return code, out, error
        runtime._command = AsyncMock(side_effect=corrupted)
        result = await sandbox.test('web')
        assert result['status'] == 'blocked' and result['isolated'] is False
        assert result['container_invoked'] is True and result['exit_code'] == 0
    asyncio.run(check())


def test_web_manifest_runtime_tamper_is_rejected_before_container(tmp_path):
    async def check():
        runtime, sandbox, commands = environment(tmp_path)
        await sandbox.prepare('web', bundle())
        path = runtime._run_dir('web') / 'autonomous_manifest.json'
        manifest = json.loads(path.read_text())
        manifest['web']['image_id'] = 'sha256:' + 'd' * 64
        path.write_text(json.dumps(manifest))
        with pytest.raises(RuntimeErrorSafe, match='Web'):
            await sandbox.test('web')
        assert commands == []
    asyncio.run(check())


def test_frozen_image_mismatch_blocks_before_candidate_launch(tmp_path):
    async def check():
        runtime, sandbox, commands = environment(tmp_path)
        await sandbox.prepare('web', bundle())
        runtime._command = AsyncMock(return_value=(0, 'sha256:' + 'f' * 64, ''))
        result = await sandbox.test('web')
        assert result['status'] == 'blocked' and result['container_invoked'] is False
        assert all(call.args[0][1] != 'run' for call in runtime._command.call_args_list)
    asyncio.run(check())


def test_actual_model_payload_routes_web_contract_and_runtime_to_all_roles(tmp_path):
    async def check():
        runtime = Runtime(tmp_path, tmp_path / 'data')
        runtime.settings.update(api_key='UNIT-INVALID', model='unit-model',base_url='https://example.invalid/v1',min_request_interval=0)
        store = Store(tmp_path / 'state.sqlite3')
        run = {'id':'web-model','mode':'live','requirement':'新增页面和 API','budget':{'max_calls':10,'max_tokens':100000,'max_output_tokens':4096},
               'project_context':{'id':'revision','project_id':'project','number':1,'source_sha256':canonical_sha256(FILES),
                                  'files':FILES,'acceptance':'','web':WEB},'bundle':{'acceptance':SUITE}}
        store.create_run(run)
        model = AutonomousModel(runtime, store)
        def respond(value):
            runtime._post = Mock(return_value={'choices':[{'message':{'content':json.dumps(value)},'finish_reason':'stop'}], 'usage':{'total_tokens':10}})
        respond(proposal())
        await model.prepare(run)
        payload = runtime._post.call_args.args[0]
        assert 'from web_bridge import request' in payload['messages'][0]['content']
        assert json.loads(payload['messages'][1]['content'])['context']['web'] == WEB
        respond({'passed':True,'issues':[],'summary':'HTTP contract ready'})
        await model.review_contract(run, proposal())
        assert 'real HTTP' in runtime._post.call_args.args[0]['messages'][0]['content']
        respond({'action':'tool','summary':'inspect response','tool':'http_request','arguments':{'method':'GET','path':'/'}})
        await model.decide(run, {'project':FILES})
        assert 'http_request' in runtime._post.call_args.args[0]['messages'][0]['content']
        respond({'summary':'edit template','files':{'templates/index.html':'<h1>New</h1>'}})
        await model.work(run, {'id':'template','instructions':'编辑页面','write_paths':['templates/index.html']}, {'project':FILES})
        assert json.loads(runtime._post.call_args.args[0]['messages'][1]['content'])['context']['web'] == WEB
        assert 'Use Python\'s standard library;' not in runtime._post.call_args.args[0]['messages'][0]['content']
    asyncio.run(check())
