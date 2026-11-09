import test from 'node:test'
import assert from 'node:assert/strict'
import { buildRequirement, getRunSummary, getTestEvidence, resolveIntent, runTitle, runUserMessage } from '../src/conversation.ts'
import type { Event, Run, TaskDefinition } from '../src/types.ts'

const task: TaskDefinition = { id: 'completion', name: '完成状态与筛选', requirement: '# 固定契约\n不修改验收。', expected_tests: 10, acceptance_sha256: 'frozen-suite' }
const baseRun = (changes: Partial<Run> = {}): Run => ({ id: 'fixture', requirement: '给任务增加完成状态', workflow_id: 'development',
  mode: 'live', status: 'succeeded', created_at: '2026-11-08T10:00:00Z', attempt: 0, task_id: 'completion', task, ...changes })
function event(id: number, node: string, data: unknown, type = 'tool'): Event {
  return { id, node_id: node, timestamp: `2026-11-08T10:00:${String(id).padStart(2, '0')}Z`, type, message: 'Synthetic evidence fixture', data }
}
function evidenceRun(): Run {
  const diff = { files: [{ path: 'todo.py', before: '# original', after: '# candidate', diff: 'inert diff' }], unified_diff: 'inert diff' }
  return baseRun({ diff, events: [
    event(1, 'develop', { tool: 'candidate_diff', source: 'runtime', status: 'completed', input: { paths: ['todo.py'] }, output: diff }),
    event(2, 'round-0/verify/test', { tool: 'run_acceptance', source: 'runtime', status: 'completed', output: {
      status: 'passed', isolated: true, exit_code: 0, task_id: 'completion', acceptance_sha256: 'frozen-suite', expected_tests: 10,
      stdout: 'AGENTFORGE_ACCEPTANCE={"tests": 10, "passed": true}\n', stderr: 'Ran 10 tests in 0.1s\n\nOK\n',
    } }),
  ] })
}
function rawTest(run: Run): Record<string, unknown> { return (run.events![1].data as { output: Record<string, unknown> }).output }

test('supported requests resolve to their bounded task', () => {
  assert.equal(resolveIntent('给待办增加完成状态并筛选已完成任务').taskId, 'completion')
  assert.equal(resolveIntent('给任务增加优先级并支持分页').taskId, 'priority_query')
  assert.equal(resolveIntent('支持 JSON 导入导出和原子合并').taskId, 'json_merge')
  assert.equal(resolveIntent('批量标记任务完成').taskId, 'bulk_edit')
  assert.equal(resolveIntent('重命名和删除任务').taskId, 'bulk_edit')
})
test('unknown and multiple tasks do not silently become completion', () => {
  for (const text of ['', '完成这个项目', '开发一个网站', '导出候选代码', '支持优先级和 JSON 导入']) assert.equal(resolveIntent(text).taskId, null, text)
})
test('review routes explicitly, while requested edits remain development', () => {
  assert.deepEqual(resolveIntent('审查当前代码'), { taskId: 'completion', workflowId: 'review', reason: '将审查当前待办示例，不改写代码。' })
  assert.equal(resolveIntent('审查优先级实现，不要修复').workflowId, 'review')
  assert.equal(resolveIntent('审查并修复优先级实现').workflowId, 'development')
  assert.equal(resolveIntent('审查 JSON 导入').taskId, 'json_merge')
})
test('explicit read-only intent takes precedence over descriptions or repair advice', () => {
  for (const text of [
    '只审查新增的 JSON 导入代码，不要修改',
    '仅检查 JSON 导入，告诉我如何修复问题',
    '不要修改代码，检查新增的优先级功能',
    '不要修改任何代码，帮我定位并说明如何修复 JSON 导入',
    'review JSON import in read-only mode; explain how to fix it',
    'only review JSON import; explain how to fix it',
  ]) assert.equal(resolveIntent(text).workflowId, 'review', text)
})
test('reviewing already added or repaired code does not request another edit', () => {
  assert.equal(resolveIntent('检查新增的 JSON 导入代码').workflowId, 'review')
  assert.equal(resolveIntent('审查修复后的优先级代码').workflowId, 'review')
})
test('positive repair requests are not mistaken for read-only constraints', () => {
  for (const text of [
    '不要只审查，请修复 JSON 导入',
    '检查并修复优先级排序问题',
    '不要修改固定测试，修复 JSON 导入',
    '不修改其它逻辑，只修复 JSON 导入',
    'review and fix JSON import',
  ]) assert.equal(resolveIntent(text).workflowId, 'development', text)
})
test('request format preserves arbitrary user text and unchanged contract', () => {
  const input = '添加“完成”状态\n请保持返回副本。\n\n## 固定验收契约\n这也是用户原话'
  const requirement = buildRequirement(input, task)
  assert.equal(runUserMessage(baseRun({ requirement })), input)
  assert.ok(requirement.endsWith(task.requirement))
  assert.ok(!requirement.includes(input)) // JSON escaped newlines cannot end the user field.
})
test('request rejects empty or oversized combined input before API submission', () => {
  assert.throws(() => buildRequirement(' ', task), /描述/)
  assert.throws(() => buildRequirement('x'.repeat(7990), task), /8000/)
  assert.throws(() => buildRequirement('开发', { ...task, requirement: '' }), /契约/)
})
test('legacy and malformed request strings stay intact', () => {
  for (const requirement of ['老运行原始需求', task.requirement, '# AgentForge 任务请求 v1\n\n用户需求 JSON：invalid\n\n## 固定验收契约\nx']) {
    assert.equal(runUserMessage(baseRun({ requirement })), requirement)
  }
})
test('titles use user words, with task names for historical long contracts', () => {
  assert.equal(runTitle(baseRun({ requirement: buildRequirement('帮我添加完成状态', task) })), '帮我添加完成状态')
  assert.equal(runTitle(baseRun({ requirement: task.requirement })), task.name)
  assert.equal(runTitle(baseRun({ requirement: 'x'.repeat(50) })), 'x'.repeat(28) + '…')
})
test('current trusted test result survives a later tester model failure', () => {
  const run = evidenceRun()
  run.status = 'failed'
  run.steps = [{ id: 'round-0/verify/test', label: '独立验收', status: 'failed', attempt: 0, output: null }]
  run.events!.push(event(3, 'round-0/verify/test', { status: 'failed', error: '模型 HTTP 429', output: null }, 'agent'))
  assert.deepEqual(getTestEvidence(run), { status: 'passed', testsRun: 10, failures: 0, errors: 0, isolated: true, round: 0, reason: '' })
})
test('model claims and wrong-scope or wrong-suite evidence are rejected', () => {
  for (const mutate of [
    (r: Run) => { r.events![1].type = 'agent' },
    (r: Run) => { (r.events![1].data as Record<string, unknown>).source = 'model' },
    (r: Run) => { r.attempt = 1 },
    (r: Run) => { rawTest(r).acceptance_sha256 = 'changed' },
    (r: Run) => { rawTest(r).task_id = 'json_merge' },
    (r: Run) => { rawTest(r).evidence_scope = { id: 'round-0/verify', attempt: 1, candidate_hash: 'x' } },
  ]) {
    const run = evidenceRun(); mutate(run); assert.equal(getTestEvidence(run), null)
  }
})
test('candidate changes or a new test start invalidate the previous displayed result', () => {
  const changed = evidenceRun(); changed.diff!.files[0].after = '# externally changed'
  // Evidence must be a snapshot, not the same mutable object as the current diff.
  changed.events![0] = event(1, 'develop', { tool: 'candidate_diff', source: 'runtime', status: 'completed', output: { files: [{ path: 'todo.py', after: '# candidate' }] } })
  assert.equal(getTestEvidence(changed), null)
  const rewrite = evidenceRun(); rewrite.events!.push(event(3, 'round-1/fix', { tool: 'candidate_diff', source: 'runtime', status: 'completed', output: rewrite.diff }))
  assert.equal(getTestEvidence(rewrite), null)
  const testing = evidenceRun(); testing.events!.push(event(3, 'round-0/verify/test', { tool: 'run_acceptance', status: 'running' }))
  assert.equal(getTestEvidence(testing), null)
})
test('review uses its frozen read baseline and does not need a fabricated edit', () => {
  const run = evidenceRun(); run.workflow_id = 'review'
  run.events![0] = event(1, 'prepare', { tool: 'read_project', source: 'runtime', status: 'completed', output: { files: [{ path: 'todo.py', content: '# candidate' }] } })
  assert.equal(getTestEvidence(run)?.status, 'passed')
})
test('failed tests report counts from actual unittest output', () => {
  const run = evidenceRun(), raw = rawTest(run)
  Object.assign(raw, { status: 'failed', exit_code: 1, stdout: 'AGENTFORGE_ACCEPTANCE={"tests": 10, "passed": false}', stderr: 'Ran 10 tests in 0.2s\nFAILED (failures=2, errors=1)' })
  assert.deepEqual(getTestEvidence(run), { status: 'failed', testsRun: 10, failures: 2, errors: 1, isolated: true, round: 0, reason: '' })
})
test('expected count never fills in missing actual counts or fabricates success', () => {
  const run = evidenceRun(); Object.assign(rawTest(run), { stdout: '', stderr: '' })
  const result = getTestEvidence(run)!
  assert.equal(result.testsRun, null)
  assert.equal(result.failures, null)
  assert.equal(result.errors, null)
  assert.equal(result.status, 'incomplete')
})
test('inconsistent attestation and unittest counts remain unconfirmed', () => {
  const run = evidenceRun(); rawTest(run).stderr = 'Ran 9 tests in 0.1s\nOK'
  const result = getTestEvidence(run)!
  assert.equal(result.status, 'incomplete'); assert.equal(result.testsRun, null)
  assert.match(result.reason, /不一致/)
})
test('blocked isolation never reuses claimed pass counts', () => {
  const run = evidenceRun(); Object.assign(rawTest(run), { status: 'blocked', isolated: false, reason: 'Docker 未启动' })
  assert.deepEqual(getTestEvidence(run), { status: 'blocked', testsRun: null, failures: null, errors: null, isolated: false, round: 0, reason: 'Docker 未启动' })
})
test('an incomplete timeout can report failure without manufacturing test counts', () => {
  const run = evidenceRun(); Object.assign(rawTest(run), { status: 'failed', exit_code: null, reason: '隔离测试超过 25 秒，已终止。', stdout: '', stderr: 'test_something ...' })
  assert.equal(getTestEvidence(run)?.testsRun, null)
  assert.equal(getTestEvidence(run)?.status, 'failed')
})
test('successful output distinguishes application and simulation from model development', () => {
  assert.equal(getRunSummary(evidenceRun()).title, '修改已准备好')
  const applied = getRunSummary({ ...evidenceRun(), applied: true, summary: '候选修改尚未应用。' })
  assert.equal(applied.title, '修改已应用'); assert.doesNotMatch(applied.description, /尚未应用/)
  assert.equal(getRunSummary({ ...evidenceRun(), mode: 'mock' }).title, '演示已完成')
  assert.equal(getRunSummary({ ...evidenceRun(), workflow_id: 'review' }).title, '代码检查完成')
})
test('a historical succeeded status cannot certify a changed or unverified current candidate', () => {
  const run = evidenceRun()
  run.diff = structuredClone(run.diff!)
  run.diff.files[0].after += '\n# changed after verification'
  assert.equal(getTestEvidence(run), null)
  assert.equal(getRunSummary(run).tone, 'warning')
  assert.match(getRunSummary(run).title, /重新验证/)
  assert.equal(getRunSummary(baseRun()).tone, 'warning')
  const failed = evidenceRun(); Object.assign(rawTest(failed), { status: 'failed', exit_code: 1 })
  assert.equal(getRunSummary(failed).tone, 'warning')
  const applied = { ...run, applied: true }
  assert.match(getRunSummary(applied).description, /原修改已应用/)
  assert.equal(getRunSummary(applied).tone, 'warning')
})
test('failure summaries retain actual service and sandbox reasons', () => {
  const failed = getRunSummary(baseRun({ status: 'failed', summary: '执行失败：RuntimeErrorSafe: 模型 HTTP 429；请求限流或额度限制。' }))
  assert.equal(failed.tone, 'warning'); assert.match(failed.description, /HTTP 429/)
  const blocked = getRunSummary(baseRun({ status: 'blocked', summary: 'Docker 容器不可用' }))
  assert.match(blocked.description, /Docker/)
  assert.equal(getRunSummary(baseRun({ status: 'needs_human' })).tone, 'warning')
})
test('running summary reflects actual leaf steps rather than a fabricated percent', () => {
  const run = baseRun({ status: 'running', steps: [
    { id: 'round-0/verify', label: '验证子工作流', status: 'running' },
    { id: 'round-0/verify/test', label: '独立验收测试', status: 'running' },
    { id: 'round-0/verify/inspect', label: '需求与差异检查', status: 'running' },
  ] })
  assert.equal(getRunSummary(run).description, '正在独立验收测试、需求与差异检查。')
})
