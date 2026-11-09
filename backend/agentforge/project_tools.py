"""Bounded project tools for a model-directed observe/edit/run feedback loop."""
from __future__ import annotations

import json
from pathlib import Path

from .autonomous_sandbox import AutonomousSandbox, _json
from .project_runner import ProjectRunner, digest
from .projects import ProjectStore
from .runtime import RuntimeErrorSafe


class ProjectTools:
    def __init__(self, engine):
        self.engine = engine

    async def __call__(self, run_id, name, arguments):
        run = self.engine.store.run(run_id)
        if not run or not run.get('project_context'):
            raise ValueError('这些工具仅用于动态项目工作区。')
        if not isinstance(arguments, dict):
            raise ValueError('工具参数必须为对象。')
        files = await self.engine.sandbox.read(run_id)
        fingerprint = digest(files)
        if name == 'read_file':
            if set(arguments) - {'path', 'start_line', 'end_line'}:
                raise ValueError('read_file 接受 path/start_line/end_line。')
            path = arguments.get('path')
            if path not in files:
                raise ValueError('文件不在当前候选项目中。')
            start, end = arguments.get('start_line', 1), arguments.get('end_line', 400)
            if type(start) is not int or type(end) is not int or not 1 <= start <= end or end - start > 399:
                raise ValueError('一次最多读取 400 行，行号从 1 开始。')
            lines = files[path].splitlines()
            content = '\n'.join(lines[start - 1:end])
            return {'path': path, 'start_line': start, 'end_line': min(end, len(lines)),
                    'content': content[:16000], 'truncated': len(content) > 16000 or end < len(lines),
                    'candidate_sha256': fingerprint, 'executed_code': False}
        if name == 'search_project':
            query = arguments.get('query')
            if set(arguments) != {'query'} or not isinstance(query, str) or not 1 <= len(query) <= 200:
                raise ValueError('search_project 需要 1 至 200 字符 query。')
            matches = []
            for path, content in files.items():
                for index, line in enumerate(content.splitlines(), 1):
                    if query.casefold() in line.casefold():
                        matches.append({'path': path, 'line': index, 'text': line[:320]})
            return {'matches': matches[:30], 'truncated': len(matches) > 30,
                    'candidate_sha256': fingerprint, 'executed_code': False}
        if name != 'run_application' or set(arguments) != {'entrypoint', 'args'}:
            raise ValueError('run_application 需要 entrypoint 和 args；不支持任意主机命令。')
        base = self.engine.runtime._run_dir(run_id) / 'application-tools'
        AutonomousSandbox._check(base, self.engine.runtime.data_dir)
        registry = ProjectStore(base / 'scratch.sqlite3')
        # Every invocation freezes its own candidate source. Only this run's
        # scratch data carries forward; actual user project data is never opened.
        marker = base / 'last-project.json'
        AutonomousSandbox._check(marker, base)
        previous = json.loads(marker.read_text(encoding='utf-8')) if marker.exists() else None
        imported = registry.import_project('Temporary tool candidate', files)
        project_id = imported['project']['id']
        if previous:
            prior = registry.application_state(previous['project_id'])
            registry.commit_execution(project_id, {'kind': 'scratch_state_seed', 'source_project_id': previous['project_id'],
                                                     'test_claim': False, 'executed_code': False},
                                      state=prior['state'], expected_state_sha256=digest({}))
        runner = ProjectRunner(self.engine.runtime, base, registry)
        result = await runner.execute(project_id, imported['revision'], arguments['entrypoint'], arguments['args'])
        # Even a failed application has a registered unchanged state. The next
        # tool call continues from that state, not from user's actual /data.
        marker.write_text(_json({'project_id': project_id}), encoding='utf-8')
        result['data_scope'] = 'per_run_scratch_only'
        result['candidate_sha256'] = fingerprint
        if result.get('timed_out'):
            raise TimeoutError('Docker 应用工具超时，本轮停止；执行记录保留。')
        if await self.engine.sandbox.fingerprint(run_id) != fingerprint:
            raise RuntimeErrorSafe('工具运行期间候选变化；结果不可用于当前候选。')
        return result
