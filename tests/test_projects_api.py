"""API contracts use fake transports only; these are not live model/Docker evidence."""
import asyncio
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sys
from unittest.mock import AsyncMock, Mock
import zipfile

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from agentforge.app import create_app
from agentforge.autonomous_sandbox import _json
from test_engine import StubRuntime

FILES = {'numbers.py': 'def add(a,b):\n    return a+b\n'}


def setup(tmp_path):
    app = create_app(ROOT, tmp_path / 'data', StubRuntime())
    engine = app.state.autonomous_engine
    engine.runtime.settings.update(api_key='UNIT-FAKE-NEVER-SEND', model='unit-fake', base_url='https://example.invalid/v1')
    engine.runtime.health = AsyncMock(return_value={'model': {'configured': True}, 'sandbox': {'available': True}})
    engine.runtime._post = Mock(side_effect=AssertionError('No model calls allowed'))
    engine.runtime._command = AsyncMock(side_effect=AssertionError('No Docker allowed'))
    engine.start = Mock()
    return app, engine, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver')


def test_import_continue_idempotency_configuration_freeze_and_project_history(tmp_path):
    async def scenario():
        app, engine, client = setup(tmp_path)
        async with client:
            imported = await client.post('/api/projects/import', json={'name': 'Unit project', 'files': FILES})
            assert imported.status_code == 201
            project = imported.json()
            url = '/api/projects/' + project['id']
            doc = (await client.post(url + '/knowledge', json={'title': 'Addition rules', 'content': 'Always preserve add.'})).json()
            body = {'base_revision_id': project['head_revision_id'], 'requirement': 'Add subtract(a,b).', 'idempotency_key': 'unit-submit-001'}
            first = await client.post(url + '/runs', json=body)
            assert first.status_code == 201, first.text
            run = engine.store.run(first.json()['id'])
            assert run['project_context']['files'] == FILES
            assert run['knowledge_snapshot'][0]['id'] == doc['id']
            profile = app.state.project_store.profile('general')
            await client.put('/api/projects/profiles/general', json={**{k: profile[k] for k in ('name','description','prompt','capabilities','enabled')}, 'prompt': 'Changed prompt for later runs'})
            assert engine.store.run(run['id'])['profile_snapshot'][0]['version'] == 1
            second = await client.post(url + '/runs', json=body)
            assert second.status_code == 201 and second.json()['id'] == run['id']
            assert engine.start.call_count == 1
            conflict = await client.post(url + '/runs', json={**body, 'requirement': 'Different request'})
            assert conflict.status_code == 409
            active = await client.post(url + '/runs', json={**body, 'idempotency_key': 'unit-submit-002'})
            assert active.status_code == 409
            detail = (await client.get(url)).json()
            assert len(detail['runs']) == 1 and detail['revisions'][0]['files'] == FILES
            archive = await client.get(url + '/revisions/' + project['head_revision_id'] + '/download')
            with zipfile.ZipFile(io.BytesIO(archive.content)) as data:
                assert data.read('numbers.py').decode() == FILES['numbers.py']
            engine.runtime._post.assert_not_called()
            engine.runtime._command.assert_not_called()
    asyncio.run(scenario())


def test_cross_project_refs_and_mock_override_are_rejected_without_creating_run(tmp_path):
    async def scenario():
        app, engine, client = setup(tmp_path)
        async with client:
            a, b = [(await client.post('/api/projects/import', json={'name': str(i), 'files': FILES})).json() for i in range(2)]
            doc = (await client.post('/api/projects/' + b['id'] + '/knowledge', json={'title': 'Other', 'content': 'other private rules'})).json()
            body = {'base_revision_id': a['head_revision_id'], 'requirement': 'New requirement', 'idempotency_key': 'unit-ref-001'}
            response = await client.post('/api/projects/' + a['id'] + '/runs', json={**body, 'knowledge_refs': [{'id':doc['id'],'version':1}]})
            assert response.status_code == 422
            response = await client.post('/api/projects/' + a['id'] + '/runs', json={**body, 'base_revision_id':b['head_revision_id']})
            assert response.status_code == 404
            response = await client.post('/api/projects/' + a['id'] + '/runs', json={**body, 'mode':'mock'})
            assert response.status_code == 422
            assert engine.store.runs() == []
    asyncio.run(scenario())


def test_only_matching_verified_candidate_is_published_as_new_revision(tmp_path):
    async def scenario():
        app, engine, client = setup(tmp_path)
        async with client:
            project = (await client.post('/api/projects/import', json={'name':'Test', 'files':FILES})).json()
            body = {'base_revision_id':project['head_revision_id'], 'requirement':'Add feature', 'idempotency_key':'unit-publish-001'}
            run = (await client.post('/api/projects/' + project['id'] + '/runs', json=body)).json()
            fingerprint = hashlib.sha256(_json(FILES).encode()).hexdigest()
            acceptance = 'FAKE UNIT SUITE TEXT NOT EXECUTED'
            test = dict(status='passed', isolated=True, exit_code=0, test_count=3, candidate_sha256=fingerprint,
                        candidate_hash=fingerprint, acceptance_sha256=hashlib.sha256(acceptance.encode()).hexdigest())
            engine.sandbox.read = AsyncMock(return_value=deepcopy(FILES))
            engine.sandbox.fingerprint = AsyncMock(return_value=fingerprint)
            engine.store.update_run(run['id'], status='succeeded', verified_hash=fingerprint, verifications=[test],
                                    bundle={'acceptance':acceptance}, quality_review={'passed':True,'candidate_hash':fingerprint})
            await app.state.project_publish(run['id'])
            saved = engine.store.run(run['id'])['publication']
            assert saved['status'] == 'saved' and saved['number'] == 2
            assert len(app.state.project_store.revisions(project['id'])) == 2
            # Repeating publication is idempotent; no extra revision.
            await app.state.project_publish(run['id'])
            assert len(app.state.project_store.revisions(project['id'])) == 2
            assert app.state.project_store.revision(project['id'], project['head_revision_id'])['verification']['status'] == 'unverified'
    asyncio.run(scenario())


def test_application_request_retry_returns_original_receipt_without_reexecution(tmp_path):
    async def scenario():
        app, _, client = setup(tmp_path)
        async with client:
            project = (await client.post('/api/projects/import', json={'name':'App', 'files':FILES})).json()
            app.state.project_runner.execute = AsyncMock(return_value={'id':'fake-receipt', 'status':'succeeded', 'test_claim':False})
            body = {'revision_id':project['head_revision_id'], 'entrypoint':'numbers.py', 'args':[], 'idempotency_key':'unit-execute-001'}
            url = '/api/projects/' + project['id'] + '/execute'
            first = await client.post(url, json=body)
            second = await client.post(url, json=body)
            assert first.status_code == second.status_code == 201
            assert first.json() == second.json()
            assert app.state.project_runner.execute.await_count == 1
            conflict = await client.post(url, json={**body,'args':['different']})
            assert conflict.status_code == 409
    asyncio.run(scenario())


def test_built_ui_shares_local_api_port_without_exposing_source_or_shadowing_api(tmp_path):
    async def scenario():
        frontend = tmp_path / 'frontend/dist'
        (frontend / 'assets').mkdir(parents=True)
        (frontend / 'index.html').write_text('<h1>Unit UI</h1>')
        (frontend / 'assets/app.js').write_text('console.log("unit")')
        (tmp_path / '.env').write_text('PRIVATE=never-serve')
        app = create_app(tmp_path, tmp_path / 'data', StubRuntime())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
            assert (await client.get('/')).text == '<h1>Unit UI</h1>'
            assert (await client.get('/assets/app.js')).status_code == 200
            assert (await client.get('/.env')).status_code == 404
            assert (await client.post('/api/unknown', json={})).status_code == 404
            assert (await client.get('/api/projects')).json() == []
    asyncio.run(scenario())
