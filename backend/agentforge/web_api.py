"""Dependency preparation and capability-scoped Web preview API.

Project source never enters dependency preparation. Preview state is ephemeral;
immutable evidence and request intents survive browser disconnects/restarts.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .autonomous_sandbox import _json
from .runtime import RuntimeErrorSafe
from .storage import now
from .web_config import validate_web_config, web_runtime_sha256
from .web_dependencies import WebDependencies
from .web_preview import WebPreviewManager


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class PrepareInput(Input):
    requirements: str = Field(min_length=1, max_length=8000)
    idempotency_key: str = Field(min_length=8, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')


class PreviewInput(Input):
    revision_id: str
    idempotency_key: str = Field(min_length=8, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')


def mount_web(app, root, data_dir, engine, projects):
    directory = Path(data_dir) / 'web'
    directory.mkdir(parents=True, exist_ok=True)
    dependencies = WebDependencies(engine.runtime, directory / 'dependencies')
    previews = WebPreviewManager(engine.runtime, directory / 'previews')
    registry = directory / 'requests.sqlite3'
    with sqlite3.connect(registry) as db:
        db.execute('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,key TEXT UNIQUE,payload TEXT,body TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS preview_requests(key TEXT PRIMARY KEY,payload TEXT,preview_id TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS preview_outcomes(key TEXT PRIMARY KEY,identity TEXT UNIQUE,body TEXT)')
        db.execute("CREATE TRIGGER IF NOT EXISTS frozen_preview_outcome_update BEFORE UPDATE ON preview_outcomes BEGIN SELECT RAISE(ABORT,'Immutable preview outcome'); END")
        db.execute("CREATE TRIGGER IF NOT EXISTS frozen_preview_outcome_delete BEFORE DELETE ON preview_outcomes BEGIN SELECT RAISE(ABORT,'Immutable preview outcome'); END")
    jobs = {}
    lock = asyncio.Lock()

    def preview_attempt_id(key):
        return hashlib.sha256(key.encode('utf-8')).hexdigest()[:32]

    def outcome(identity):
        with sqlite3.connect(registry) as db:
            row = db.execute('SELECT body FROM preview_outcomes WHERE identity=?', (identity,)).fetchone()
        return json.loads(row[0]) if row else None

    def store_outcome(key, record):
        """An immutable outcome and idempotency pointer commit in one transaction."""
        folder = directory / 'preview-request-evidence' / preview_attempt_id(key)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / 'outcome.json'
        body = _json(record)
        if path.exists():
            if path.read_text(encoding='utf-8') != body:
                raise RuntimeErrorSafe('预览请求已有不同的冻结结果；不会覆盖或自动重跑。')
        else:
            with path.open('x', encoding='utf-8') as stream:
                stream.write(body)
        with sqlite3.connect(registry) as db:
            previous = db.execute('SELECT identity,body FROM preview_outcomes WHERE key=?', (key,)).fetchone()
            if previous:
                if previous != (record['id'], body):
                    raise RuntimeErrorSafe('预览请求结果不一致，拒绝覆盖。')
            else:
                db.execute('INSERT INTO preview_outcomes VALUES(?,?,?)', (key, record['id'], body))
            db.execute('UPDATE preview_requests SET preview_id=? WHERE key=? AND preview_id IS NULL', (record['id'], key))

    def preview_records(owner=None):
        values = {record['id']: record for record in previews.records(owner)}
        with sqlite3.connect(registry) as db:
            for (body,) in db.execute('SELECT body FROM preview_outcomes'):
                record = json.loads(body)
                if record['id'] not in values and (owner is None or all(record.get('owner', {}).get(k) == v for k,v in owner.items())):
                    # A once-ready container without an event record is unknown,
                    # never reconstructed as an actually running service.
                    if record['status'] == 'ready':
                        record = {**record, 'status':'interrupted', 'url':None,
                                  'reason':'原预览状态不可读取；不会重新启动。'}
                    values[record['id']] = record
        return sorted(values.values(), key=lambda record: record['created_at'], reverse=True)

    def job(identity):
        with sqlite3.connect(registry) as db:
            row = db.execute('SELECT body FROM jobs WHERE id=?', (identity,)).fetchone()
        if not row:
            raise HTTPException(404, '依赖准备记录不存在')
        return json.loads(row[0])

    def store_job(record):
        with sqlite3.connect(registry) as db:
            db.execute('UPDATE jobs SET body=? WHERE id=?', (_json(record), record['id']))
        folder = directory / 'job-evidence' / record['id']
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / (record['status'] + '.json')).open('x', encoding='utf-8') as stream:
            stream.write(_json(record))

    async def prepare(record, requirements):
        try:
            result = await dependencies.prepare(requirements)
            record.update(status='succeeded', result=result, finished_at=now())
        except asyncio.CancelledError:
            record.update(status='interrupted', error='服务停止，依赖准备完成状态未确认；不会自动重放。', finished_at=now())
            raise
        except Exception as exc:
            record.update(status='failed', error=str(exc) if isinstance(exc, (RuntimeErrorSafe, ValueError)) else type(exc).__name__, finished_at=now())
        finally:
            store_job(record)

    def configuration(dependency_id, entrypoint, files):
        value = dependencies.resolve(dependency_id)
        return validate_web_config({'schema': 'python-web-v1', 'entrypoint': entrypoint,
            'dependency_id': value['dependency_id'], 'image_id': value['image_id'],
            'lock_sha256': value['lock_sha256'], 'requirements': value['requirements']}, files)

    async def validate_runtime(web):
        web = validate_web_config(web)
        frozen = configuration(web['dependency_id'], web['entrypoint'], None)
        if frozen != web:
            raise RuntimeErrorSafe('项目冻结的 Web 环境与依赖快照不一致。')
        code, output, _ = await engine.runtime._command(engine.runtime._docker_command(
            'image', 'inspect', '--format', '{{.Id}}', web['image_id']), timeout=15)
        if code or output.strip() != web['image_id']:
            raise RuntimeErrorSafe('冻结的 Web 依赖镜像不可用；未调用模型或运行候选。')
        return web

    router = APIRouter(prefix='/api/web')

    @router.get('/dependencies')
    async def list_dependencies():
        try:
            return [dependencies.resolve(folder.name) for folder in sorted((directory / 'dependencies/bundles').glob('*')) if folder.is_dir()]
        except RuntimeErrorSafe as exc:
            raise HTTPException(409, str(exc)) from None

    @router.post('/dependencies/prepare', status_code=202)
    async def start_dependency(body: PrepareInput):
        payload = _json({'requirements': body.requirements})
        async with lock:
            with sqlite3.connect(registry) as db:
                previous = db.execute('SELECT id,payload FROM jobs WHERE key=?', (body.idempotency_key,)).fetchone()
                if previous:
                    if previous[1] != payload:
                        raise HTTPException(409, '同一提交标识不能用于不同依赖')
                    return job(previous[0])
                if any(not task.done() for task in jobs.values()):
                    raise HTTPException(409, '已有依赖准备正在执行，请等待其结束')
                record = {'id': uuid4().hex, 'status': 'running', 'created_at': now(), 'requirements': body.requirements,
                          'candidate_execution': False, 'model_calls': 0}
                db.execute('INSERT INTO jobs VALUES(?,?,?,?)', (record['id'], body.idempotency_key, payload, _json(record)))
            store_job(record)
            jobs[record['id']] = asyncio.create_task(prepare(dict(record), body.requirements))
            return record

    @router.get('/dependencies/jobs/{identity}')
    async def get_job(identity: str):
        return job(identity)

    @router.get('/previews/{identity}')
    async def get_preview(identity: str):
        try:
            value = previews.get(identity)
        except ValueError:
            raise HTTPException(404, '预览不存在') from None
        if not value:
            value = outcome(identity)
        if not value:
            raise HTTPException(404, '预览不存在')
        if value['status'] == 'ready' and previews.get(identity) is None:
            return {**value, 'status': 'interrupted', 'url': None, 'reason': '原预览状态不可读取；不会重新启动。'}
        return value

    @router.get('/previews')
    async def list_previews(project_id: str | None = None):
        return preview_records({'kind':'project', 'project_id':project_id} if project_id else None)

    @router.post('/previews/{identity}/stop')
    async def stop_preview(identity: str):
        current = await get_preview(identity)
        if previews.get(identity) is None:
            return current
        return await previews.stop(identity)

    @app.post('/api/projects/{project_id}/preview', status_code=201)
    async def start_preview(project_id: str, body: PreviewInput):
        revision = projects.revision(project_id, body.revision_id)
        if not revision:
            raise HTTPException(404, '项目版本不存在')
        if not revision.get('web'):
            raise HTTPException(422, '该版本不是 Web 项目')
        payload = _json({'project_id': project_id, 'revision_id': body.revision_id})
        key = project_id + ':' + body.idempotency_key
        async with lock:
            with sqlite3.connect(registry) as db:
                previous = db.execute('SELECT payload,preview_id FROM preview_requests WHERE key=?', (key,)).fetchone()
                if previous:
                    if previous[0] != payload:
                        raise HTTPException(409, '同一预览标识不能用于其他版本')
                    if previous[1] is None:
                        raise HTTPException(409, '该预览启动中或状态未知，不会自动重启')
                    return await get_preview(previous[1])
                db.execute('INSERT INTO preview_requests VALUES(?,?,NULL)', (key, payload))
            intent = {'id': preview_attempt_id(key), 'status': 'starting', 'created_at': now(),
                'owner': {'kind':'project','project_id':project_id,'revision_id':body.revision_id},
                'source_sha256': revision['source_sha256'], 'image_id': revision['web']['image_id'],
                'entrypoint': revision['web']['entrypoint'], 'url': None, 'container_invoked': False,
                'isolated': False, 'test_claim': False, 'application_data_persistent': False}
            phase = 'validating'
            try:
                folder = directory / 'preview-request-evidence' / intent['id']
                folder.mkdir(parents=True, exist_ok=True)
                with (folder / 'intent.json').open('x', encoding='utf-8') as stream:
                    stream.write(_json(intent))
                web = await validate_runtime(revision['web'])
                phase = 'launching'
                result = await previews.start(revision['files'], image_id=web['image_id'], entrypoint=web['entrypoint'],
                    owner={'kind': 'project', 'project_id': project_id, 'revision_id': body.revision_id,
                           'web_runtime_sha256': web_runtime_sha256(web), 'dependency_id': web['dependency_id']})
                phase = 'persisting'
                store_outcome(key, result)
                return result
            except asyncio.CancelledError:
                result = {**intent, 'status':'interrupted', 'finished_at':now(), 'completion_unknown':True,
                          'container_invoked':None,
                          'failure_kind':'preview_request_cancelled', 'reason':'预览请求中断，启动完成状态未确认；不会自动重放。'}
                store_outcome(key, result)
                raise
            except (RuntimeErrorSafe, ValueError, OSError, TimeoutError) as exc:
                result = {**intent, 'status':'failed', 'finished_at':now(),
                          'failure_kind':'preview_validation_or_platform_failed',
                          'container_invoked':False if phase == 'validating' else None,
                          'completion_unknown':phase != 'validating',
                          'reason':str(exc) if isinstance(exc, (RuntimeErrorSafe,ValueError)) else type(exc).__name__}
                store_outcome(key, result)
                return result

    async def recover():
        with sqlite3.connect(registry) as db:
            pending = [json.loads(row[0]) for row in db.execute('SELECT body FROM jobs') if json.loads(row[0])['status'] == 'running']
        for record in pending:
            record.update(status='interrupted', error='工作台重启，依赖准备状态未确认；不会自动重放。', finished_at=now())
            store_job(record)
        await previews.recover()
        with sqlite3.connect(registry) as db:
            preview_pending = db.execute('SELECT key,payload FROM preview_requests WHERE preview_id IS NULL').fetchall()
        for key, payload in preview_pending:
            folder = directory / 'preview-request-evidence' / preview_attempt_id(key)
            if (folder / 'outcome.json').exists():
                # A crash may occur after immutable file creation but before the
                # registry transaction. Reattach that exact result, never rerun.
                record = json.loads((folder / 'outcome.json').read_text(encoding='utf-8'))
            else:
                request = json.loads(payload)
                record = {'id':preview_attempt_id(key), 'status':'interrupted', 'created_at':now(), 'finished_at':now(),
                          'owner':{'kind':'project', **request}, 'url':None, 'isolated':False,
                          'container_invoked':None, 'completion_unknown':True, 'test_claim':False,
                          'failure_kind':'preview_request_interrupted',
                          'reason':'工作台重启，预览请求完成状态未确认；不会自动重放。'}
            store_outcome(key, record)

    async def close():
        active = [task for task in jobs.values() if not task.done()]
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        await previews.close()

    app.include_router(router)
    app.state.web_dependencies, app.state.web_previews = dependencies, previews
    app.state.web_recover, app.state.web_close = recover, close
    app.state.web_configuration, app.state.validate_web_runtime = configuration, validate_runtime
    app.state.web_preview_records = preview_records
    return previews
