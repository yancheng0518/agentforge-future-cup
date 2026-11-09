"""API contracts use explicit fake dependency/Docker transports, no candidates."""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from unittest.mock import AsyncMock, Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from agentforge.autonomous_sandbox import _json
from agentforge.runtime import RuntimeErrorSafe
from agentforge.web_config import web_runtime_sha256
from agentforge.web_api import PreviewInput
from test_projects_api import setup as project_setup

DEPENDENCY = {'dependency_id':'d'*64,'image_id':'sha256:'+'a'*64,'lock_sha256':'e'*64,'requirements':['Flask==3.1.0']}
FILES = {'app.py':"raise RuntimeError('Never run on host')", 'templates/index.html':'<h1>Fixture</h1>', 'static/app.js':'/* fixture */'}


def setup(tmp_path):
    app, engine, client = project_setup(tmp_path)
    app.state.web_dependencies.resolve = Mock(return_value=deepcopy(DEPENDENCY))
    engine.runtime._command = AsyncMock(return_value=(0, DEPENDENCY['image_id'], ''))
    app.state.web_previews.recover = AsyncMock(return_value=[])
    app.state.web_previews.close = AsyncMock(return_value=None)
    return app, engine, client


async def imported(client, name='Web unit'):
    response = await client.post('/api/projects/import', json={'name':name, 'files':FILES,
        'web':{'dependency_id':DEPENDENCY['dependency_id'],'entrypoint':'app.py'}})
    assert response.status_code == 201, response.text
    return response.json()


def fake_previews(app):
    records = {}
    async def start(files, **options):
        identity = f'{len(records)+1:032x}'
        record = {'id':identity,'status':'ready','created_at':'2026-11-09T00:00:00+00:00',
            'url':'http://127.0.0.1:43210/unit','owner':deepcopy(options['owner']),
            'source_sha256':hashlib.sha256(_json(files).encode()).hexdigest(),'image_id':options['image_id'],
            'isolated':True,'container_invoked':True,'test_claim':False}
        records[identity] = record
        return deepcopy(record)
    async def stop(identity, **options):
        records[identity]['status'] = 'stopped'
        return deepcopy(records[identity])
    app.state.web_previews.start = AsyncMock(side_effect=start)
    app.state.web_previews.stop = AsyncMock(side_effect=stop)
    app.state.web_previews.get = Mock(side_effect=lambda identity:deepcopy(records.get(identity)))
    app.state.web_previews.records = Mock(side_effect=lambda owner=None: [deepcopy(r) for r in records.values()
        if owner is None or all(r['owner'].get(k)==v for k,v in owner.items())])
    return records


def test_dependency_jobs_are_idempotent_keep_failure_and_disallow_conflicting_payload(tmp_path):
    async def run():
        app, engine, client = setup(tmp_path)
        app.state.web_dependencies.prepare = AsyncMock(side_effect=RuntimeErrorSafe('Explicit dependency fixture failure'))
        async with client:
            body = {'requirements':'Flask==3.1.0','idempotency_key':'unit-dependency-key'}
            first = (await client.post('/api/web/dependencies/prepare',json=body)).json()
            await asyncio.sleep(0)
            result = (await client.get('/api/web/dependencies/jobs/'+first['id'])).json()
            assert result['status'] == 'failed' and 'fixture failure' in result['error']
            repeated = await client.post('/api/web/dependencies/prepare',json=body)
            assert repeated.json() == result and app.state.web_dependencies.prepare.await_count == 1
            assert (await client.post('/api/web/dependencies/prepare',json={**body,'requirements':'Flask==3.1.1'})).status_code == 409
            evidence = tmp_path / 'data/web/job-evidence' / first['id']
            assert {p.name for p in evidence.iterdir()} == {'running.json','failed.json'}
            engine.runtime._post.assert_not_called()
    asyncio.run(run())


def test_dependency_success_and_cancellation_are_retained_without_automatic_restart(tmp_path):
    async def run():
        app, engine, client = setup(tmp_path)
        app.state.web_dependencies.prepare = AsyncMock(return_value=deepcopy(DEPENDENCY))
        async with client:
            body = {'requirements':'Flask==3.1.0','idempotency_key':'unit-dependency-success'}
            first = (await client.post('/api/web/dependencies/prepare',json=body)).json()
            await asyncio.sleep(0)
            assert (await client.get('/api/web/dependencies/jobs/'+first['id'])).json()['result'] == DEPENDENCY
            started = asyncio.Event()
            async def hanging(_):
                started.set()
                await asyncio.Event().wait()
            app.state.web_dependencies.prepare = AsyncMock(side_effect=hanging)
            second_body = {**body,'idempotency_key':'unit-dependency-interrupted'}
            second = (await client.post('/api/web/dependencies/prepare',json=second_body)).json()
            await started.wait()
            assert (await client.post('/api/web/dependencies/prepare',json={**body,'idempotency_key':'unit-other-pending'})).status_code == 409
            await app.state.web_close()
            final = (await client.get('/api/web/dependencies/jobs/'+second['id'])).json()
            assert final['status'] == 'interrupted'
            assert (await client.post('/api/web/dependencies/prepare',json=second_body)).json() == final
            assert app.state.web_dependencies.prepare.await_count == 1
    asyncio.run(run())


def test_web_import_freezes_server_resolved_metadata_and_rejects_client_image_override(tmp_path):
    async def run():
        app, engine, client = setup(tmp_path)
        async with client:
            project = await imported(client)
            url = '/api/projects/'+project['id']
            revision = (await client.get(url+'/revisions/'+project['head_revision_id'])).json()
            assert revision['files'] == FILES
            assert revision['web'] == {'schema':'python-web-v1','entrypoint':'app.py',**DEPENDENCY}
            runtime_hash = web_runtime_sha256(revision['web'])
            await app.state.validate_web_runtime(revision['web'])
            assert len(runtime_hash) == 64
            override = await client.post('/api/projects/import',json={'name':'Invalid','files':FILES,
                'web':{'dependency_id':'d'*64,'entrypoint':'app.py','image_id':'sha256:'+'f'*64}})
            assert override.status_code == 422
            app.state.web_dependencies.resolve.return_value = {**DEPENDENCY,'lock_sha256':'f'*64}
            with pytest.raises(RuntimeErrorSafe,match='不一致'):
                await app.state.validate_web_runtime(revision['web'])
            assert (await client.get(url+'/revisions/'+project['head_revision_id'])).json() == revision
            engine.runtime._post.assert_not_called()
    asyncio.run(run())


def test_preview_validation_failure_is_readable_and_replayed_even_after_environment_fixed(tmp_path):
    async def run():
        app, engine, client = setup(tmp_path)
        app.state.web_previews.start = AsyncMock(side_effect=AssertionError('No start after missing image'))
        async with client:
            project = await imported(client)
            url = '/api/projects/'+project['id']+'/preview'
            body = {'revision_id':project['head_revision_id'],'idempotency_key':'unit-preview-failure'}
            engine.runtime._command.return_value = (1,'','missing image')
            response = await client.post(url,json=body)
            assert response.status_code == 201
            failed = response.json()
            assert failed['status'] == 'failed' and not failed['container_invoked'] and not failed['test_claim']
            assert '镜像不可用' in failed['reason']
            engine.runtime._command.return_value = (0,DEPENDENCY['image_id'],'')
            assert (await client.post(url,json=body)).json() == failed
            assert (await client.get('/api/web/previews/'+failed['id'])).json() == failed
            assert (await client.post('/api/web/previews/'+failed['id']+'/stop')).json() == failed
            assert (await client.get('/api/web/previews',params={'project_id':project['id']})).json() == [failed]
            assert app.state.web_preview_records({'kind':'project','project_id':project['id']}) == [failed]
            app.state.web_previews.start.assert_not_called()
            outcomes = list((tmp_path/'data/web/preview-request-evidence').glob('*/outcome.json'))
            assert len(outcomes)==1 and json.loads(outcomes[0].read_text(encoding='utf-8')) == failed
            with sqlite3.connect(tmp_path/'data/web/requests.sqlite3') as db:
                with pytest.raises(sqlite3.IntegrityError):
                    db.execute("UPDATE preview_outcomes SET body='changed'")
    asyncio.run(run())


def test_preview_replay_returns_current_state_and_cannot_cross_project_or_version(tmp_path):
    async def run():
        app, engine, client = setup(tmp_path)
        records = fake_previews(app)
        async with client:
            first, second = await imported(client,'first'), await imported(client,'second')
            url = '/api/projects/'+first['id']+'/preview'
            body = {'revision_id':first['head_revision_id'],'idempotency_key':'unit-preview-success'}
            preview = (await client.post(url,json=body)).json()
            assert preview['status'] == 'ready'
            assert (await client.post(url,json=body)).json() == preview
            assert app.state.web_previews.start.await_count == 1
            assert (await client.post(url,json={**body,'revision_id':second['head_revision_id']})).status_code == 404
            revision = app.state.project_store.revision(first['id'],first['head_revision_id'])
            fingerprint = revision['source_sha256']
            acceptance = '# Explicit fake acceptance; never run\n'
            runtime_hash = web_runtime_sha256(revision['web'])
            verification = {'status':'passed','candidate_sha256':fingerprint,'verified_hash':fingerprint,
                'test':{'status':'passed','isolated':True,'exit_code':0,'test_count':1,'candidate_sha256':fingerprint,
                        'acceptance_sha256':hashlib.sha256(acceptance.encode()).hexdigest(),
                        'web_runtime_sha256':runtime_hash,'image':DEPENDENCY['image_id'],
                        'dependency_id':DEPENDENCY['dependency_id'],'lock_sha256':DEPENDENCY['lock_sha256']},
                'review':{'passed':True,'candidate_hash':fingerprint,'web_runtime_sha256':runtime_hash}}
            changed = app.state.project_store.promote(first['id'],revision['id'],'unit-run',FILES,acceptance,verification)
            assert (await client.post(url,json={**body,'revision_id':changed['revision']['id']})).status_code == 409
            stopped = (await client.post('/api/web/previews/'+preview['id']+'/stop')).json()
            assert stopped['status'] == 'stopped'
            assert (await client.post(url,json=body)).json()['status'] == 'stopped'
            records.clear()
            unavailable = (await client.get('/api/web/previews/'+preview['id'])).json()
            assert unavailable['status'] == 'interrupted' and unavailable['url'] is None
    asyncio.run(run())


def test_recovery_records_null_preview_intent_and_running_dependency_as_interrupted(tmp_path):
    async def run():
        app, engine, client = setup(tmp_path)
        async with client:
            project = await imported(client)
            body = {'revision_id':project['head_revision_id'],'idempotency_key':'unit-recovery-null'}
            key = project['id']+':'+body['idempotency_key']
            payload = _json({'project_id':project['id'],'revision_id':project['head_revision_id']})
            with sqlite3.connect(tmp_path/'data/web/requests.sqlite3') as db:
                db.execute('INSERT INTO preview_requests VALUES(?,?,NULL)',(key,payload))
                db.execute('INSERT INTO jobs VALUES(?,?,?,?)',('job-unit','key-unit','{}',_json({'id':'job-unit','status':'running'})))
            await app.state.web_recover()
            first = (await client.post('/api/projects/'+project['id']+'/preview',json=body)).json()
            assert first['status'] == 'interrupted' and first['completion_unknown']
            assert first['container_invoked'] is None
            assert (await client.get('/api/web/dependencies/jobs/job-unit')).json()['status'] == 'interrupted'
            await app.state.web_recover()
            assert (await client.get('/api/web/previews/'+first['id'])).json() == first
            engine.runtime._command.assert_not_called()
    asyncio.run(run())


def test_cancelled_preview_request_retains_unknown_outcome_and_never_restarts(tmp_path):
    async def run():
        app, engine, client = setup(tmp_path)
        app.state.web_previews.start = AsyncMock(side_effect=asyncio.CancelledError())
        async with client:
            project=await imported(client)
            body={'revision_id':project['head_revision_id'],'idempotency_key':'unit-preview-cancelled'}
            url='/api/projects/'+project['id']+'/preview'
            endpoint = next(route.endpoint for route in app.routes if getattr(route,'path',None)=='/api/projects/{project_id}/preview')
            with pytest.raises(asyncio.CancelledError):
                await endpoint(project['id'],PreviewInput(**body))
            saved=(await client.post(url,json=body)).json()
            assert saved['status']=='interrupted' and saved['completion_unknown']
            assert saved['container_invoked'] is None and not saved['test_claim']
            assert app.state.web_previews.start.await_count==1
            assert (await client.get('/api/web/previews/'+saved['id'])).json()==saved
    asyncio.run(run())


def test_recovery_reattaches_previously_written_outcome_without_rewriting_file(tmp_path):
    async def run():
        app, engine, client = setup(tmp_path)
        async with client:
            project=await imported(client)
            key=project['id']+':unit-orphan-result'
            identity=hashlib.sha256(key.encode()).hexdigest()[:32]
            payload=_json({'project_id':project['id'],'revision_id':project['head_revision_id']})
            record={'id':identity,'status':'failed','created_at':'2026-11-09T00:00:00+00:00',
                'owner':{'kind':'project','project_id':project['id'],'revision_id':project['head_revision_id']},
                'reason':'Original exact recorded failure','url':None,'container_invoked':False,'test_claim':False}
            folder=tmp_path/'data/web/preview-request-evidence'/identity
            folder.mkdir(parents=True)
            path=folder/'outcome.json'
            path.write_text(_json(record),encoding='utf-8')
            before=path.read_bytes()
            with sqlite3.connect(tmp_path/'data/web/requests.sqlite3') as db:
                db.execute('INSERT INTO preview_requests VALUES(?,?,NULL)',(key,payload))
            await app.state.web_recover()
            assert path.read_bytes()==before
            assert (await client.get('/api/web/previews/'+identity)).json()==record
            engine.runtime._command.assert_not_called()
    asyncio.run(run())
