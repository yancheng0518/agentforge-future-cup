"""Project revisions and application execution around the dynamic kernel."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sqlite3
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from .autonomous_sandbox import _json
from .projects import ProjectStore, ProjectConflictError
from .project_runner import ProjectRunner
from .project_tools import ProjectTools
from .runtime import RuntimeErrorSafe


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ImportInput(Input):
    name: str = Field(min_length=1, max_length=100)
    files: dict[str, str]
    acceptance: str = Field(default='', max_length=65536)
    web: dict | None = None


class Ref(Input):
    id: str = Field(min_length=1, max_length=80)
    version: int = Field(ge=1, strict=True)


class RunInput(Input):
    base_revision_id: str
    requirement: str = Field(min_length=1, max_length=8000)
    budget: dict = Field(default_factory=dict)
    profile_refs: list[Ref] | None = Field(default=None, max_length=16)
    knowledge_refs: list[Ref] | None = Field(default=None, max_length=32)
    idempotency_key: str = Field(min_length=8, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')


class ProfileInput(Input):
    name: str
    description: str = ''
    prompt: str
    capabilities: list[str] = Field(default_factory=list)
    enabled: bool = True


class KnowledgeInput(Input):
    title: str
    content: str
    document_id: str | None = None


class ExecutionInput(Input):
    revision_id: str
    entrypoint: str
    args: list[str] = Field(default_factory=list)
    idempotency_key: str = Field(min_length=8, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')


def mount_projects(app, root, data_dir, engine):
    directory = Path(data_dir) / 'projects'
    projects = ProjectStore(directory / 'projects.sqlite3')
    runner = ProjectRunner(engine.runtime, directory, projects)
    request_path = directory / 'requests.sqlite3'
    with sqlite3.connect(request_path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS requests (project_id TEXT, key TEXT, request_hash TEXT, run_id TEXT UNIQUE, completed INTEGER DEFAULT 0, PRIMARY KEY(project_id,key))')
        db.execute('CREATE UNIQUE INDEX IF NOT EXISTS active_project ON requests(project_id) WHERE completed=0')
        db.execute('CREATE TABLE IF NOT EXISTS execution_requests (project_id TEXT, key TEXT, request_hash TEXT, response TEXT, PRIMARY KEY(project_id,key))')
        # Single-process local server: terminal records left by a previous process
        # cannot still have an in-flight publication callback in this process.
        for (run_id,) in db.execute('SELECT run_id FROM requests WHERE completed=0').fetchall():
            previous = engine.store.run(run_id)
            if not previous or previous.get('status') not in ('queued', 'running'):
                db.execute('UPDATE requests SET completed=1 WHERE run_id=?', (run_id,))
    router = APIRouter(prefix='/api/projects')
    lock = asyncio.Lock()

    def project_or_404(project_id):
        value = projects.get_project(project_id)
        if not value:
            raise HTTPException(404, '项目不存在')
        return value

    def revision_or_404(project_id, revision_id):
        project_or_404(project_id)
        value = projects.revision(project_id, revision_id)
        if not value:
            raise HTTPException(404, '该项目版本不存在')
        return value

    def public_run(value):
        return {key: item for key, item in value.items() if key not in ('bundle', 'project_context', 'profile_snapshot', 'knowledge_snapshot')}

    def project_runs(project_id):
        with engine.store.connect() as db:
            values = [json.loads(row[0]) for row in db.execute('SELECT body FROM runs ORDER BY rowid DESC')]
        return [public_run(run) for run in values if run.get('project_id') == project_id]

    def clean_requests(db):
        for row in db.execute('SELECT run_id FROM requests WHERE completed=0').fetchall():
            run = engine.store.run(row[0])
            terminal = not run or bool(run.get('publication')) or any(
                event['type'] in ('terminal', 'interrupted_recovery') for event in engine.store.events(row[0]))
            if terminal:
                db.execute('UPDATE requests SET completed=1 WHERE run_id=?', (row[0],))

    async def publish_locked(run_id):
        run = engine.store.run(run_id)
        if not run or not run.get('project_id'):
            return
        publication = {'status': 'not_saved', 'reason': '运行未达到保存验证版本的条件'}
        try:
            if run['status'] == 'succeeded':
                files = await engine.sandbox.read(run_id)
                fingerprint = await engine.sandbox.fingerprint(run_id)
                test = run['verifications'][-1] if run['verifications'] else {}
                if fingerprint != run.get('verified_hash'):
                    raise RuntimeErrorSafe('当前候选与已验证哈希不同，未保存新版本。')
                verification = {'status': 'passed', 'candidate_sha256': fingerprint,
                                'verified_hash': run['verified_hash'], 'test': test,
                                'review': run.get('quality_review'), 'source_run': run_id,
                                'coverage_notice': 'AI生成验收与模型审查，不代表所有业务均已验证'}
                result = projects.promote(run['project_id'], run['base_revision_id'], run_id,
                                          files, run['bundle']['acceptance'], verification)
                revision = result['revision']
                publication = {'status': 'saved', 'revision_id': revision['id'], 'number': revision['number'],
                               'source_sha256': revision['source_sha256'], 'original_files_untouched': True}
                if revision.get('web'):
                    from .web_config import web_runtime_sha256
                    publication.update(web_runtime_sha256=web_runtime_sha256(revision['web']),
                                       dependency_id=revision['web']['dependency_id'])
            engine.store.update_run(run_id, publication=publication)
            engine.store.event(run_id, 'project_revision', '项目版本保存结果', data=publication)
        except ProjectConflictError:
            publication = {'status': 'conflict', 'reason': '项目当前版本已变化；候选和验证记录保留，未覆盖新版本。'}
            engine.store.update_run(run_id, publication=publication)
            engine.store.event(run_id, 'project_revision', '验证候选未合入项目', data=publication)
        except (ValueError, RuntimeErrorSafe, OSError) as exc:
            publication = {'status': 'failed', 'reason': str(exc) if isinstance(exc, (ValueError, RuntimeErrorSafe)) else type(exc).__name__}
            engine.store.update_run(run_id, publication=publication)
            engine.store.event(run_id, 'project_revision', '版本保存失败，候选与记录保留', data=publication)
        finally:
            with sqlite3.connect(request_path) as db:
                db.execute('UPDATE requests SET completed=1 WHERE run_id=?', (run_id,))

    async def publish(run_id):
        async with lock:
            await publish_locked(run_id)

    engine.completion_callback = publish
    from .web_api import mount_web
    from .web_tools import WebProjectTools
    previews = mount_web(app, root, data_dir, engine, projects)
    project_tools = WebProjectTools(engine, previews, app.state.validate_web_runtime)
    engine.project_tool_callback = project_tools
    engine.project_cleanup_callback = project_tools.close_run

    @router.get('')
    async def list_projects():
        return projects.list_projects()

    @router.post('/import', status_code=201)
    async def import_project(body: ImportInput):
        try:
            web = None
            if body.web is not None:
                if set(body.web) != {'dependency_id', 'entrypoint'}:
                    raise ValueError('Web 导入只接受已准备的 dependency_id 与 entrypoint')
                web = app.state.web_configuration(body.web['dependency_id'], body.web['entrypoint'], body.files)
            result = projects.import_project(body.name, body.files, body.acceptance, web=web)
            return {**result['project'], 'revision_id': result['revision']['id'],
                    'source_sha256': result['revision']['source_sha256']}
        except (ValueError, RuntimeErrorSafe) as exc:
            raise HTTPException(422, str(exc)) from None

    @router.get('/profiles')
    async def list_profiles():
        return projects.list_profiles()

    @router.post('/profiles', status_code=201)
    async def create_profile(body: ProfileInput):
        try:
            return projects.save_profile(body.model_dump())
        except (ValueError, RuntimeErrorSafe) as exc:
            raise HTTPException(422, str(exc)) from None

    @router.put('/profiles/{profile_id}')
    async def update_profile(profile_id: str, body: ProfileInput):
        if not projects.profile(profile_id):
            raise HTTPException(404, 'Agent 配置不存在')
        try:
            return projects.save_profile(body.model_dump(), profile_id)
        except (ValueError, RuntimeErrorSafe) as exc:
            raise HTTPException(422, str(exc)) from None

    @router.get('/{project_id}')
    async def get_project(project_id: str):
        return {**project_or_404(project_id), 'revisions': projects.revisions(project_id),
                'knowledge': projects.knowledge(project_id), 'executions': [item['record'] for item in projects.executions(project_id)],
                'runs': project_runs(project_id), 'previews': app.state.web_preview_records({'kind': 'project', 'project_id': project_id})}

    @router.get('/{project_id}/revisions')
    async def revisions(project_id: str):
        project_or_404(project_id)
        return projects.revisions(project_id)

    @router.get('/{project_id}/revisions/{revision_id}')
    async def revision(project_id: str, revision_id: str):
        return revision_or_404(project_id, revision_id)

    @router.get('/{project_id}/revisions/{revision_id}/download')
    async def download(project_id: str, revision_id: str):
        revision = revision_or_404(project_id, revision_id)
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            for path, text in revision['files'].items():
                archive.writestr(path, text.encode('utf-8'))
            metadata_name = 'PROJECT_REVISION.json'
            while metadata_name in revision['files']:
                metadata_name = '_' + metadata_name
            archive.writestr(metadata_name, json.dumps(
                {key: value for key, value in revision.items() if key != 'files'}, ensure_ascii=False, indent=2))
            if revision.get('web'):
                web = revision['web']
                app.state.web_configuration(web['dependency_id'], web['entrypoint'], revision['files'])
                lock_path = Path(data_dir) / 'web/dependencies/bundles' / web['dependency_id'] / 'requirements.lock'
                archive.writestr('requirements.txt', '\n'.join(web['requirements']) + '\n')
                archive.writestr('requirements.lock', lock_path.read_bytes())
                archive.writestr('RUNNING.txt', 'Web 工程：通过 AgentForge 准备冻结依赖，再启动隔离预览。入口 '+web['entrypoint']+'，PORT=8080；不要在宿主机执行候选。\n版本与验收来源见 '+metadata_name+'。\n')
            else:
                archive.writestr('RUNNING.txt', '标准库 Python 工程。入口与参数请参阅源码。平台 Docker 运行工作目录和 AGENTFORGE_DATA_DIR 均为 /data。\n导入版本未自动验证；验收状态见 '+metadata_name+'。\n')
        return Response(output.getvalue(), media_type='application/zip', headers={
            'Content-Disposition': f'attachment; filename="project-{project_id}-v{revision["number"]}.zip"', 'Cache-Control': 'no-store'})

    @router.post('/{project_id}/knowledge', status_code=201)
    async def save_knowledge(project_id: str, body: KnowledgeInput):
        project_or_404(project_id)
        try:
            return projects.save_knowledge(project_id, body.title, body.content, body.document_id)
        except KeyError:
            raise HTTPException(404, '资料版本不属于该项目') from None
        except (ValueError, RuntimeErrorSafe) as exc:
            raise HTTPException(422, str(exc)) from None

    @router.post('/{project_id}/runs', status_code=201)
    async def create_run(project_id: str, body: RunInput):
        project_or_404(project_id)
        request_hash = hashlib.sha256(_json(body.model_dump()).encode()).hexdigest()
        async with lock:
            # Check a previous submission before health/version gates: retries never charge again.
            with sqlite3.connect(request_path) as db:
                previous = db.execute('SELECT request_hash,run_id FROM requests WHERE project_id=? AND key=?',
                                      (project_id, body.idempotency_key)).fetchone()
            if previous:
                if previous[0] != request_hash:
                    raise HTTPException(409, '提交标识已经用于不同需求，请刷新后重新提交')
                run = engine.store.run(previous[1])
                if not run:
                    raise HTTPException(409, '原运行记录不可用，未重复发送模型请求')
                return public_run(run)
            health = await engine.runtime.health()
            if not health['model']['configured'] or not health['sandbox']['available']:
                raise HTTPException(409, '需要真实模型和 Docker；不会切换为模拟或主机执行')
            if sum(not task.done() for task in engine.tasks.values()) >= 2:
                raise HTTPException(429, '最多并发两个动态运行')
            revision = revision_or_404(project_id, body.base_revision_id)
            if revision.get('web'):
                try:
                    await app.state.validate_web_runtime(revision['web'])
                except (RuntimeErrorSafe, ValueError) as exc:
                    raise HTTPException(409, str(exc)) from None
            if project_or_404(project_id)['head_revision_id'] != body.base_revision_id:
                raise HTTPException(409, '项目版本已更新，请刷新后在当前版本上继续修改')
            try:
                if body.profile_refs is None:
                    profiles = [value for value in projects.list_profiles() if value['enabled']]
                else:
                    profiles = [projects.profile(ref.id, ref.version) for ref in body.profile_refs]
                    if len({ref.id for ref in body.profile_refs}) != len(body.profile_refs):
                        raise ValueError('不能重复选择同一 Agent 配置')
                if not profiles or any(not value or not value['enabled'] for value in profiles):
                    raise ValueError('至少需要一个有效且启用的 Agent 配置版本')
                refs = ([ref.model_dump() for ref in body.knowledge_refs] if body.knowledge_refs is not None
                        else [{'id': doc['id'], 'version': doc['version']} for doc in projects.knowledge(project_id)])
                knowledge = projects.select_knowledge(project_id, refs)
                with sqlite3.connect(request_path, timeout=15) as db:
                    db.execute('BEGIN IMMEDIATE')
                    clean_requests(db)
                    if db.execute('SELECT 1 FROM requests WHERE project_id=? AND completed=0', (project_id,)).fetchone():
                        raise HTTPException(409, '该项目已有运行，请等待完成后继续修改')
                    run = engine.create_run_from_project(body.requirement, revision, body.budget, profiles, knowledge)
                    db.execute('INSERT INTO requests(project_id,key,request_hash,run_id) VALUES (?,?,?,?)',
                               (project_id, body.idempotency_key, request_hash, run['id']))
                engine.start(run['id'])
                return public_run(run)
            except KeyError:
                raise HTTPException(422, '所选配置或资料版本不存在或不属于本项目') from None
            except (ValueError, RuntimeErrorSafe) as exc:
                raise HTTPException(422, str(exc)) from None

    @router.post('/{project_id}/execute', status_code=201)
    async def execute(project_id: str, body: ExecutionInput):
        revision = revision_or_404(project_id, body.revision_id)
        if revision.get('web'):
            raise HTTPException(422, 'Web 项目使用浏览器预览与 HTTP 工具，不使用短时 CLI 执行。')
        request_hash = hashlib.sha256(_json(body.model_dump()).encode()).hexdigest()
        with sqlite3.connect(request_path, timeout=15) as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT request_hash,response FROM execution_requests WHERE project_id=? AND key=?',
                                  (project_id, body.idempotency_key)).fetchone()
            if previous:
                if previous[0] != request_hash:
                    raise HTTPException(409, '运行标识已用于其他参数，请重新发起一次运行')
                if previous[1] is None:
                    raise HTTPException(409, '该次应用运行仍在执行或完成状态未知；请查看历史，不会重复执行')
                return json.loads(previous[1])
            db.execute('INSERT INTO execution_requests(project_id,key,request_hash) VALUES(?,?,?)',
                       (project_id, body.idempotency_key, request_hash))
        try:
            result = await runner.execute(project_id, revision, body.entrypoint, body.args)
            with sqlite3.connect(request_path) as db:
                db.execute('UPDATE execution_requests SET response=? WHERE project_id=? AND key=?',
                           (_json(result), project_id, body.idempotency_key))
            return result
        except (ValueError, RuntimeErrorSafe) as exc:
            raise HTTPException(422, str(exc)) from None

    app.include_router(router)
    app.state.project_store, app.state.project_runner = projects, runner
    app.state.project_publish = publish
