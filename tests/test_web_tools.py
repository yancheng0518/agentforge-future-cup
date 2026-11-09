"""Web tool adapter tests with explicit fake transport; no model or candidates."""
import asyncio
from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from agentforge.autonomous_sandbox import SandboxIntegrityError
from agentforge.project_runner import digest
from agentforge.web_config import web_runtime_sha256
from agentforge.web_tools import WebProjectTools

WEB = {'schema':'python-web-v1','entrypoint':'app.py','dependency_id':'d'*64,
       'image_id':'sha256:'+'a'*64,'lock_sha256':'e'*64,'requirements':['Flask==3.1.0']}
FILES = {'app.py':"raise RuntimeError('never execute on host')",'templates/index.html':'<h1>Unit</h1>'}


class FakeSandbox:
    def __init__(self):
        self.files = deepcopy(FILES)
    async def read(self, identity):
        return deepcopy(self.files)
    async def fingerprint(self, identity):
        return digest(self.files)


class FakePreviews:
    def __init__(self):
        self.records, self.started, self.requests, self.stopped = {}, [], [], []
        self.start_overrides, self.reply_overrides = {}, {}
        self.after_start, self.after_request = None, None
    def get(self, identity):
        return deepcopy(self.records.get(identity))
    async def start(self, files, **options):
        identity = str(len(self.started)+1)
        record = {'id':identity,'status':'ready','source_sha256':digest(files),
                  'image_id':options['image_id'],'owner':deepcopy(options['owner']),**self.start_overrides}
        self.records[identity] = record
        self.started.append({'files':deepcopy(files),**options})
        if self.after_start:
            self.after_start()
        return deepcopy(record)
    async def request(self, identity, method, path, body='', headers=None):
        self.requests.append((identity,method,path,body,headers))
        record = self.records[identity]
        result = {'ok':True,'status':200,'body_text':'{"ok":true}','body_base64':'e30=',
                  'source_sha256':record['source_sha256'],'image_id':record['image_id'],**self.reply_overrides}
        if self.after_request:
            self.after_request()
        return result
    async def stop(self, identity, reason='user'):
        self.stopped.append((identity,reason))
        self.records[identity]['status'] = 'stopped'
        return deepcopy(self.records[identity])


def setup():
    sandbox, previews = FakeSandbox(), FakePreviews()
    runs = {identity:{'id':identity,'project_context':{'web':deepcopy(WEB)},'verified_hash':None}
            for identity in ('one','two')}
    engine = SimpleNamespace(sandbox=sandbox,store=SimpleNamespace(run=lambda identity:deepcopy(runs.get(identity))))
    validate = AsyncMock(return_value=WEB)
    return WebProjectTools(engine,previews,validate), sandbox, previews, runs, validate


def test_same_candidate_reuses_only_run_local_preview_and_never_grants_verification():
    async def run():
        adapter,sandbox,previews,runs,validate = setup()
        with patch('agentforge.projects.ProjectStore.application_state',side_effect=AssertionError('No user app data')):
            first = await adapter('one','http_request',{'method':'GET','path':'/'})
            second = await adapter('one','http_request',{'method':'POST','path':'/items','body':'{"name":"a"}'})
            other = await adapter('two','http_request',{'method':'GET','path':'/'})
        assert len(previews.started)==2 and len(previews.requests)==3
        assert first['preview_id']==second['preview_id'] != other['preview_id']
        assert [item['owner']['run_id'] for item in previews.started] == ['one','two']
        assert second['web_runtime_sha256']==web_runtime_sha256(WEB)
        assert not second['test_claim'] and not second['application_data_persistent']
        assert 'body_base64' not in second and second['json']=={'ok':True}
        assert runs['one']['verified_hash'] is None
        assert previews.requests[1][-1]=={'Content-Type':'application/json'}
        await adapter.close_run('one')
        assert 'one' not in adapter.active and 'two' in adapter.active
    asyncio.run(run())


def test_changed_candidate_stops_old_preview_and_starts_clean_snapshot():
    async def run():
        adapter,sandbox,previews,_,_ = setup()
        first=await adapter('one','http_request',{'method':'GET','path':'/'})
        sandbox.files={**FILES,'app.py':'# edited source text'}
        second=await adapter('one','http_request',{'method':'GET','path':'/'})
        assert first['preview_id'] != second['preview_id']
        assert previews.stopped==[(first['preview_id'],'candidate_changed')]
        assert previews.started[1]['files']==sandbox.files
        assert second['candidate_sha256']==digest(sandbox.files)
    asyncio.run(run())


@pytest.mark.parametrize('status',[200,404,500])
def test_http_status_is_observation_and_cannot_be_turned_into_acceptance(status):
    async def run():
        adapter,_,previews,runs,_=setup()
        previews.reply_overrides={'status':status,'test_claim':True}
        result=await adapter('one','http_request',{'method':'GET','path':'/missing'})
        assert result['status']==status and result['ok'] is True
        assert result['test_claim'] is False and runs['one']['verified_hash'] is None
    asyncio.run(run())


def test_failed_application_start_is_returned_once_with_no_http_request():
    async def run():
        adapter,_,previews,_,_=setup()
        previews.start_overrides={'status':'failed','failure_kind':'application_failed','application_stderr':'Fixture SyntaxError'}
        result=await adapter('one','http_request',{'method':'GET','path':'/'})
        assert result['ok'] is False and result['status']=='failed' and result['test_claim'] is False
        assert result['candidate_sha256']==digest(FILES) and result['application_stderr']=='Fixture SyntaxError'
        assert len(previews.started)==1 and previews.requests==[]
    asyncio.run(run())


@pytest.mark.parametrize('stage,key',[('start','source_sha256'),('start','image_id'),('request','source_sha256'),('request','image_id')])
def test_mismatched_receipt_is_not_relabelled_or_left_running(stage,key):
    async def run():
        adapter,_,previews,_,_=setup()
        target=previews.start_overrides if stage=='start' else previews.reply_overrides
        target[key]='wrong hash'
        with pytest.raises(SandboxIntegrityError):
            await adapter('one','http_request',{'method':'GET','path':'/'})
        assert previews.stopped[-1][1]=='integrity_mismatch' and not adapter.active
        assert len(previews.started)==1
        assert len(previews.requests)==(0 if stage=='start' else 1)
    asyncio.run(run())


@pytest.mark.parametrize('stage',['start','request'])
def test_candidate_change_during_execution_is_rejected(stage):
    async def run():
        adapter,sandbox,previews,_,_=setup()
        def change():
            sandbox.files={'app.py':'# concurrently changed'}
        setattr(previews,'after_'+stage,change)
        with pytest.raises(SandboxIntegrityError):
            await adapter('one','http_request',{'method':'GET','path':'/'})
        assert not adapter.active and previews.stopped[-1][1]=='integrity_mismatch'
    asyncio.run(run())


@pytest.mark.parametrize('args',[{}, {'method':'GET','path':'https://outside.invalid'},
    {'method':'GET','path':'/','headers':{'Authorization':'x'}}, {'method':'GET','path':'/','body':'x'*16385}])
def test_invalid_tools_do_not_prepare_or_start_environment(args):
    async def run():
        adapter,_,previews,_,validate=setup()
        with pytest.raises(ValueError):
            await adapter('one','http_request',args)
        validate.assert_not_called()
        assert previews.started==[]
    asyncio.run(run())


def test_missing_run_non_web_and_legacy_application_guards_are_clear():
    async def run():
        adapter,_,previews,runs,_=setup()
        with pytest.raises(ValueError,match='不存在'):
            await adapter('missing','http_request',{'method':'GET','path':'/'})
        runs['two']['project_context'].pop('web')
        with pytest.raises(ValueError,match='仅用于'):
            await adapter('two','http_request',{'method':'GET','path':'/'})
        with pytest.raises(ValueError,match='Web'):
            await adapter('one','run_application',{'entrypoint':'app.py','args':[]})
        result=await adapter('one','read_file',{'path':'templates/index.html'})
        assert result['content']=='<h1>Unit</h1>' and not result['executed_code']
        assert previews.started==[]
    asyncio.run(run())


def test_model_body_is_bounded_and_original_transport_record_is_unchanged():
    async def run():
        adapter,_,previews,_,_=setup()
        text='x'*20000
        previews.reply_overrides={'body_text':text}
        result=await adapter('one','http_request',{'method':'GET','path':'/'})
        assert result['body_text']=='x'*16000 and result['body_truncated_for_model']
        assert previews.reply_overrides['body_text']==text and 'json' not in result
    asyncio.run(run())
