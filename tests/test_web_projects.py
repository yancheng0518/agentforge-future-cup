"""Immutable Web promotion tests using explicit fake verification records.

No Docker, models, candidate imports or acceptance execution occur here.
"""
from copy import deepcopy
import hashlib
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from agentforge.autonomous_sandbox import _json
from agentforge.projects import ProjectStore, ProjectValidationError, ProjectConflictError
from agentforge.web_config import web_runtime_sha256

FILES = {'app.py':"raise RuntimeError('Never execute this candidate on host')\n",
         'other.py':'# second entrypoint text\n','templates/index.html':'<h1>Original</h1>'}
ACCEPTANCE = '# Frozen fake HTTP acceptance text; not executed by these storage tests.\n'
WEB = {'schema':'python-web-v1','entrypoint':'app.py','dependency_id':'d'*64,
       'image_id':'sha256:'+'a'*64,'lock_sha256':'e'*64,'requirements':['Flask==3.1.0']}


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def verification(files=FILES, web=WEB):
    source_hash = digest(_json(files))
    runtime_hash = web_runtime_sha256(web)
    return {'status':'passed','candidate_sha256':source_hash,'verified_hash':source_hash,
        'test':{'status':'passed','exit_code':0,'isolated':True,'container_invoked':True,'test_count':3,
                'candidate_sha256':source_hash,'acceptance_sha256':digest(ACCEPTANCE),
                'web_runtime_sha256':runtime_hash,'dependency_id':web['dependency_id'],
                'image':web['image_id'],'lock_sha256':web['lock_sha256'],'stdout':'Explicit fake transport'},
        'review':{'passed':True,'candidate_hash':source_hash,'web_runtime_sha256':runtime_hash,
                  'summary':'Explicit fake review; no model request'}}


@pytest.fixture
def project(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Storage must never start a candidate or Docker')
    monkeypatch.setattr('subprocess.Popen', forbidden)
    monkeypatch.setattr('asyncio.create_subprocess_exec', forbidden)
    store = ProjectStore(tmp_path / 'projects.sqlite3')
    imported = store.import_project('Web binding fixture', FILES, ACCEPTANCE, web=WEB)
    return store, imported


def assert_unchanged(store, imported):
    project_id = imported['project']['id']
    assert store.get_project(project_id) == imported['project']
    assert store.revisions(project_id) == [imported['revision']]
    assert store.executions(project_id) == []


@pytest.mark.parametrize('section,field,replacement', [
    ('test','image','sha256:'+'b'*64),
    ('test','lock_sha256','b'*64),
    ('test','dependency_id','b'*64),
    ('test','web_runtime_sha256','b'*64),
    ('review','web_runtime_sha256','b'*64),
    ('review','passed',False),
    ('review','candidate_hash','b'*64),
    ('test','candidate_sha256','b'*64),
    ('test','acceptance_sha256','b'*64),
])
def test_same_source_rejects_mismatched_environment_review_or_verification(project, section, field, replacement):
    store, imported = project
    result = verification()
    result[section][field] = replacement
    with pytest.raises(ProjectValidationError):
        store.promote(imported['project']['id'],imported['revision']['id'],'unit-mismatched',FILES,ACCEPTANCE,result)
    assert_unchanged(store, imported)


@pytest.mark.parametrize('section,field', [('test','image'),('test','lock_sha256'),
    ('test','dependency_id'),('test','web_runtime_sha256'),('review','web_runtime_sha256')])
def test_missing_web_binding_never_defaults_to_current_environment(project, section, field):
    store, imported = project
    result = verification()
    result[section].pop(field)
    with pytest.raises(ProjectValidationError):
        store.promote(imported['project']['id'],imported['revision']['id'],'unit-missing',FILES,ACCEPTANCE,result)
    assert_unchanged(store, imported)


@pytest.mark.parametrize('field,replacement', [('image_id','sha256:'+'f'*64),('lock_sha256','f'*64),
    ('dependency_id','f'*64),('entrypoint','other.py'),('requirements',['Flask==3.1.1'])])
def test_same_source_verification_from_other_runtime_cannot_promote_original_project(project, field, replacement):
    store, imported = project
    other_web = {**WEB,field:replacement}
    other = store.import_project('Same source different environment',FILES,ACCEPTANCE,web=other_web)
    assert other['revision']['source_sha256'] == imported['revision']['source_sha256']
    result = verification(web=other_web)
    with pytest.raises(ProjectValidationError):
        store.promote(imported['project']['id'],imported['revision']['id'],'unit-cross-runtime',FILES,ACCEPTANCE,result)
    assert_unchanged(store, imported)
    # This receipt belongs to the other frozen runtime and is valid only there.
    promoted = store.promote(other['project']['id'],other['revision']['id'],'unit-right-runtime',FILES,ACCEPTANCE,result)
    assert promoted['revision']['web'] == other_web and promoted['revision']['number'] == 2


def test_success_preserves_frozen_runtime_and_all_assets_across_versions_and_replay(project):
    store, imported = project
    changed = {**FILES,'templates/index.html':'<h1>Updated</h1>','static/app.js':'/* explicit changed asset */'}
    result = verification(changed)
    first = store.promote(imported['project']['id'],imported['revision']['id'],'unit-success',changed,ACCEPTANCE,result)
    repeated = store.promote(imported['project']['id'],imported['revision']['id'],'unit-success',changed,ACCEPTANCE,result)
    assert repeated['idempotent'] and repeated['revision']==first['revision']
    assert first['revision']['web']==WEB and first['revision']['files']==changed
    # Caller mutation never mutates frozen source/environment/verification.
    first['revision']['web']['image_id']='caller modification'
    result['review']['web_runtime_sha256']='caller modification'
    saved=store.revision(imported['project']['id'],repeated['revision']['id'])
    assert saved['web']==WEB and saved['verification']==verification(changed)
    newer={**changed,'static/app.js':'/* next asset revision */'}
    last=store.promote(imported['project']['id'],saved['id'],'unit-next',newer,ACCEPTANCE,verification(newer))
    assert last['revision']['number']==3 and last['revision']['parent_id']==saved['id']
    assert last['revision']['web']==WEB
    assert store.revisions(imported['project']['id'])[0]==imported['revision']


def test_invalid_web_receipt_cannot_bypass_gates_by_reusing_published_run_id(project):
    store, imported = project
    valid=verification()
    first=store.promote(imported['project']['id'],imported['revision']['id'],'unit-replayed',FILES,ACCEPTANCE,valid)
    invalid=deepcopy(valid)
    invalid['test']['image']='sha256:'+'b'*64
    with pytest.raises(ProjectValidationError):
        store.promote(imported['project']['id'],imported['revision']['id'],'unit-replayed',FILES,ACCEPTANCE,invalid)
    assert store.get_project(imported['project']['id'])['head_revision_id']==first['revision']['id']
    assert len(store.revisions(imported['project']['id']))==2


def test_stale_web_base_cannot_overwrite_already_published_version(project):
    store, imported = project
    first=store.promote(imported['project']['id'],imported['revision']['id'],'unit-winner',FILES,ACCEPTANCE,verification())
    with pytest.raises(ProjectConflictError):
        store.promote(imported['project']['id'],imported['revision']['id'],'unit-stale',FILES,ACCEPTANCE,verification())
    assert store.get_project(imported['project']['id'])['head_revision_id']==first['revision']['id']
    assert len(store.revisions(imported['project']['id']))==2
