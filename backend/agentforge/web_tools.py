"""Model HTTP tools use run-local ephemeral containers, never project user state."""
import json

from .autonomous_sandbox import SandboxIntegrityError
from .project_tools import ProjectTools
from .web_config import web_runtime_sha256
from .web_preview import validate_request


class WebProjectTools:
    def __init__(self, engine, previews, validate_runtime):
        self.engine, self.previews, self.validate_runtime = engine, previews, validate_runtime
        self.legacy = ProjectTools(engine)
        self.active = {}

    async def __call__(self, run_id, name, arguments):
        run = self.engine.store.run(run_id)
        if not run:
            raise ValueError('运行不存在，不能执行项目工具。')
        web = (run.get('project_context') or {}).get('web')
        if name != 'http_request':
            if web and name == 'run_application':
                raise ValueError('Web 项目使用 http_request 检查运行服务。')
            return await self.legacy(run_id, name, arguments)
        if not web:
            raise ValueError('HTTP 工具仅用于已冻结依赖的 Web 项目。')
        if not isinstance(arguments, dict) or not {'method','path'} <= set(arguments) or set(arguments) - {'method','path','body'}:
            raise ValueError('HTTP 工具只接受 method/path/body。')
        validate_request(arguments['method'], arguments['path'], arguments.get('body',''))
        files = await self.engine.sandbox.read(run_id)
        fingerprint = await self.engine.sandbox.fingerprint(run_id)
        identity = self.active.get(run_id)
        record = self.previews.get(identity) if identity else None
        if record and (record['status'] != 'ready' or record['source_sha256'] != fingerprint
                       or record.get('image_id') != web['image_id']
                       or record.get('owner', {}).get('run_id') != run_id):
            await self.previews.stop(identity, reason='candidate_changed')
            record = None
        if not record:
            await self.validate_runtime(web)
            record = await self.previews.start(files, image_id=web['image_id'], entrypoint=web['entrypoint'],
                owner={'kind': 'model_tool', 'run_id': run_id, 'web_runtime_sha256': web_runtime_sha256(web),
                       'dependency_id': web['dependency_id']}, ttl_seconds=600)
            self.active[run_id] = record['id']
            if (record.get('source_sha256') != fingerprint or record.get('image_id') != web['image_id']
                    or await self.engine.sandbox.fingerprint(run_id) != fingerprint):
                await self.previews.stop(record['id'], reason='integrity_mismatch')
                self.active.pop(run_id, None)
                raise SandboxIntegrityError('HTTP 预览启动回执与冻结候选或依赖镜像不一致，停止本轮。')
            if record['status'] != 'ready':
                return {**record, 'ok': False, 'candidate_sha256': fingerprint,
                        'web_runtime_sha256': web_runtime_sha256(web), 'dependency_id': web['dependency_id'],
                        'lock_sha256': web['lock_sha256'], 'test_claim': False}
        result = await self.previews.request(record['id'], arguments['method'], arguments['path'],
            body=arguments.get('body', ''), headers={'Content-Type': 'application/json'} if arguments.get('body') else {})
        if (result.get('source_sha256') != fingerprint or result.get('image_id') != web['image_id']
                or await self.engine.sandbox.fingerprint(run_id) != fingerprint):
            await self.previews.stop(record['id'], reason='integrity_mismatch')
            self.active.pop(run_id, None)
            raise SandboxIntegrityError('HTTP 工具源码或依赖镜像与冻结候选不一致，停止本轮。')
        result = {key: value for key, value in result.items() if key != 'body_base64'}
        result.update(candidate_sha256=fingerprint, web_runtime_sha256=web_runtime_sha256(web),
                      dependency_id=web['dependency_id'], lock_sha256=web['lock_sha256'], test_claim=False,
                      preview_id=record['id'], application_data_persistent=False)
        text = result.get('body_text', '')
        result['body_text'] = text[:16000]
        result['body_truncated_for_model'] = len(text) > 16000
        try:
            if len(text) <= 16000:
                result['json'] = json.loads(text)
        except ValueError:
            pass
        return result

    async def close_run(self, run_id):
        identity = self.active.pop(run_id, None)
        if identity:
            await self.previews.stop(identity, reason='model_run_finished')
