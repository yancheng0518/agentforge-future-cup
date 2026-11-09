import assert from 'node:assert/strict'
import test from 'node:test'
import { awaitsProjectPublication, buildExecutionRequest, buildProjectRunRequest, executionSummary, parseExecutionArgs, profileCapabilities, readProjectSources, retainRequestIntent, validateProjectPath } from '../src/projects.ts'
import { dynamicProgress, dynamicResult, verificationLabel } from '../src/autonomous.ts'
import { buildReplay, projectFrame } from '../src/collaborationGraph.ts'
import type { BrowserSourceFile } from '../src/projects.ts'
import type { DynamicVerification } from '../src/autonomous.ts'
import type { Run } from '../src/types.ts'

function file(name: string, content: string, relative = ''): BrowserSourceFile & { reads: number } {
  const bytes = new TextEncoder().encode(content)
  return { name, webkitRelativePath: relative, size: bytes.length, reads: 0,
    async arrayBuffer() { this.reads++; return bytes.buffer as ArrayBuffer } }
}

test('folder import preserves package paths and source bytes including BOM and CRLF', async () => {
  const original = '\ufeff# 中文\r\ndef value():\r\n    return 7\r\n'
  const sources = [file('main.py', original, 'my_project/main.py'), file('__init__.py', '', 'my_project/package/__init__.py')]
  const result = await readProjectSources(sources)
  assert.equal(result.files['main.py'], original)
  assert.equal(result.files['package/__init__.py'], '')
  assert.equal(result.suggestedName, 'my_project')
  assert.equal(result.bytes, new TextEncoder().encode(original).length)
  assert.equal(sources[0].reads, 1)
})

test('dependency, hidden, non-Python and credential files are reported but never opened', async () => {
  const ignored = [file('.env', 'SECRET_SENTINEL', 'app/.env'), file('lib.py', 'do not read', 'app/.venv/lib.py'),
    file('cache.py', 'do not read', 'app/__pycache__/cache.py'), file('README.md', 'do not read', 'app/README.md'),
    file('module.py', 'do not read', 'app/node_modules/module.py')]
  const result = await readProjectSources([file('main.py', 'print(1)', 'app/main.py'), ...ignored])
  assert.deepEqual(Object.keys(result.files), ['main.py'])
  assert.equal(result.excluded.length, 5)
  assert.ok(ignored.every(source => source.reads === 0))
  assert.equal(JSON.stringify(result).includes('SECRET_SENTINEL'), false)
})

test('multiple selected files retain names without inventing a server filesystem path', async () => {
  const result = await readProjectSources([file('reader.py', '# reader'), file('report.py', '# report')])
  assert.deepEqual(Object.keys(result.files), ['reader.py', 'report.py'])
  assert.equal(result.suggestedName, 'reader')
})

test('unsafe paths and Windows reserved names are rejected before reading files', async () => {
  for (const path of ['../outside.py', '/absolute.py', 'C:evil.py', 'pkg\\file.py', 'CON.py', 'aux/item.py', 'pkg/name.py:stream', 'a//b.py']) {
    const source = file(path, 'do not read')
    await assert.rejects(readProjectSources([source]))
    assert.equal(source.reads, 0)
  }
  assert.throws(() => validateProjectPath('目录.py'))
})

test('case collisions and duplicate paths cannot silently replace imported source', async () => {
  for (const files of [[file('a.py', 'one'), file('a.py', 'two')], [file('A.py', 'one'), file('a.py', 'two')],
    [file('a.py', 'one', 'root/Pkg/a.py'), file('b.py', 'two', 'root/pkg/b.py')]]) {
    await assert.rejects(readProjectSources(files), /冲突|重复/)
    assert.ok(files.every(source => source.reads === 0))
  }
})

test('import limits are checked before reading large selections', async () => {
  const many = Array.from({ length: 33 }, (_, index) => file(`module_${index}.py`, ''))
  await assert.rejects(readProjectSources(many), /32/)
  assert.ok(many.every(source => source.reads === 0))
  const oversized = file('main.py', 'x'.repeat(65537))
  await assert.rejects(readProjectSources([oversized]), /64 KiB/)
  assert.equal(oversized.reads, 0)
  const total = Array.from({ length: 5 }, (_, index) => file(`m${index}.py`, 'x'.repeat(60000)))
  await assert.rejects(readProjectSources(total), /256 KiB/)
  assert.ok(total.every(source => source.reads === 0))
})

test('invalid UTF-8, NUL and understated actual file sizes are rejected', async () => {
  await assert.rejects(readProjectSources([{ name: 'bad.py', size: 2, async arrayBuffer() { return new Uint8Array([0xc3, 0x28]).buffer } }]), /UTF-8/)
  await assert.rejects(readProjectSources([file('bad.py', 'x\0y')]), /有效/)
  const understated = file('main.py', 'x'.repeat(65537)); understated.size = 1
  await assert.rejects(readProjectSources([understated]), /有界/)
})

test('empty, ignored-only and multiple-root selections do not create misleading projects', async () => {
  await assert.rejects(readProjectSources([]), /先选择/)
  await assert.rejects(readProjectSources([file('notes.txt', 'hello')]), /没有找到/)
  await assert.rejects(readProjectSources([file('a.py', '', 'one/a.py'), file('b.py', '', 'two/b.py')]), /一个项目/)
})

test('project continuation carries the exact base, shared budget and stable caller idempotency key', () => {
  const result = buildProjectRunRequest('revision-2', '保留现有代码，增加新功能', 'request-id')
  assert.equal(result.base_revision_id, 'revision-2')
  assert.equal(result.requirement, '保留现有代码，增加新功能')
  assert.equal(result.idempotency_key, 'request-id')
  assert.deepEqual(result.budget, { max_tokens: 300000, max_calls: 40, max_output_tokens: 8192 })
  assert.equal('role' in result, false); assert.equal('task_id' in result, false); assert.equal('files' in result, false)
  assert.equal('profile_refs' in result, false); assert.equal('knowledge_refs' in result, false)
  assert.deepEqual(buildProjectRunRequest('revision-2', result.requirement, 'request-id'), result)
})

test('invalid continuation input cannot produce a request', () => {
  assert.throws(() => buildProjectRunRequest('', 'do work', 'id'), /版本/)
  assert.throws(() => buildProjectRunRequest('v1', '', 'id'), /需求/)
  assert.throws(() => buildProjectRunRequest('v1', 'x'.repeat(8001), 'id'), /8000/)
  assert.throws(() => buildProjectRunRequest('v1', 'work', ''), /标识/)
})

test('execution arguments are literal lines with no shell expansion or quote processing', () => {
  assert.deepEqual(parseExecutionArgs('--title\r\nMy report\r\n$(do-not-execute)\r\n"literal quotes"\r\n'), ['--title', 'My report', '$(do-not-execute)', '"literal quotes"'])
  assert.deepEqual(parseExecutionArgs(''), [])
  assert.deepEqual(parseExecutionArgs('  meaningful spaces  '), ['  meaningful spaces  '])
  assert.throws(() => parseExecutionArgs('x\0y'))
  assert.throws(() => parseExecutionArgs(Array(33).fill('x').join('\n')))
})

test('uncertain execution retry retains its key; changed input and completed intent receive new keys', () => {
  let keys = 0
  const next = () => `key-${++keys}`
  const signature = JSON.stringify(['project', 'v1', 'main.py', ['add', 'item']])
  const first = retainRequestIntent(null, signature, next)
  const retry = retainRequestIntent(first, signature, next)
  assert.equal(retry.key, first.key); assert.equal(keys, 1)
  const changed = retainRequestIntent(retry, JSON.stringify(['project', 'v2', 'main.py', ['add', 'item']]), next)
  assert.notEqual(changed.key, first.key)
  const newIntent = retainRequestIntent(null, signature, next)
  assert.notEqual(newIntent.key, first.key)
  const body = buildExecutionRequest('v1', 'main.py', ['add', 'item'], retry.key)
  assert.deepEqual(body, { revision_id: 'v1', entrypoint: 'main.py', args: ['add', 'item'], idempotency_key: first.key })
  assert.equal('command' in body, false)
})

test('controlled program exit zero is never presented as test or business verification', () => {
  const result = executionSummary({ id: 'e1', revision_id: 'v1', status: 'succeeded', isolated: true, exit_code: 0 })
  assert.equal(result.text, '运行结束 · 退出码 0')
  assert.doesNotMatch(result.text, /测试通过|验收通过|业务/)
  assert.equal(executionSummary({ id: 'e2', revision_id: 'v1', status: 'succeeded', isolated: false, exit_code: 0 }).text, '运行未确认')
  assert.equal(executionSummary({ id: 'e3', revision_id: 'v1', status: 'failed', isolated: true, exit_code: 2 }).text, '运行报错 · 退出码 2')
  assert.equal(executionSummary({ id: 'e4', revision_id: 'v1', status: 'blocked', isolated: true, exit_code: 0 }).text, '运行未确认')
})

test('collaboration capabilities are free descriptions, not a fixed role catalog', () => {
  assert.deepEqual(profileCapabilities('文本边界，XML 解析\n文本边界,数据兼容'), ['文本边界', 'XML 解析', '数据兼容'])
})

test('imported baseline is distinct from scaffold and candidate failure in thread and replay', () => {
  const verification: DynamicVerification = { index: 1, timestamp: '2026-11-09T00:00:00Z', status: 'failed', isolated: true, container_invoked: true, exit_code: 1,
    test_count: 3, candidate_hash: 'hash', candidate_origin: 'imported_baseline', classification: 'imported_baseline', acceptance_origin: 'model_generated' }
  const run = { id: 'r', engine: 'model-directed-v1', mode: 'live', status: 'running', requirement: 'Extend existing project', created_at: verification.timestamp,
    workflow_id: 'autonomous', attempt: 0, task_graph: [], verifications: [verification],
    events: [{ id: 1, type: 'created', timestamp: verification.timestamp, message: 'created' },
      { id: 2, type: 'verification', timestamp: verification.timestamp, message: 'result', data: verification }] } as Run
  assert.match(verificationLabel(verification, true), /导入项目基线/)
  assert.doesNotMatch(dynamicProgress(run), /脚手架|修正/)
  const replay = buildReplay(run)
  assert.equal(replay.frames[1].category, 'imported_baseline')
  assert.match(projectFrame(run, replay.frames, 1).nodes.find(node => node.kind === 'tool')!.summary!, /导入项目基线/)
})

test('a successful run with a publication conflict does not claim the project version was updated', () => {
  const run = { id: 'r', engine: 'model-directed-v1', mode: 'live', status: 'succeeded', requirement: 'work', created_at: '', workflow_id: 'autonomous', attempt: 0,
    publication: { status: 'conflict' } } as Run
  assert.deepEqual(dynamicResult(run), { title: '开发运行已结束，项目版本尚未更新', tone: 'warning' })
})

test('project polling continues through successful execution until publication and terminal evidence arrive', () => {
  assert.equal(awaitsProjectPublication({ project_id: 'p1', status: 'succeeded', events: [] }), true)
  assert.equal(awaitsProjectPublication({ project_id: 'p1', status: 'succeeded', publication: { status: 'saved' }, events: [] }), true)
  assert.equal(awaitsProjectPublication({ project_id: 'p1', status: 'succeeded', publication: { status: 'saved' }, events: [{ type: 'terminal' }] }), false)
  assert.equal(awaitsProjectPublication({ project_id: 'p1', status: 'failed', events: [] }), false)
  assert.equal(awaitsProjectPublication({ status: 'succeeded', events: [] }), false)
})
