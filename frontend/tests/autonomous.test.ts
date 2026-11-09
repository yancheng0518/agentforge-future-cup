import assert from 'node:assert/strict'
import test from 'node:test'
import { acceptanceScope, buildDynamicRequest, currentVerification, displayStatus, dynamicProgress, dynamicResult, dynamicTitle, executionReviews, generatedAcceptance, hasCandidateProject, isAutonomous, isWebRun, mergeHistory, needsReview, normalizeAutonomous, runEndpoint, verificationLabel } from '../src/autonomous.ts'
import type { DynamicRecord, DynamicVerification } from '../src/autonomous.ts'
import type { Run } from '../src/types.ts'

const record = (changes: Partial<DynamicRecord> = {}): DynamicRecord => ({ id: 'shared', engine: 'model-directed-v1', requirement: '创建物流计价工具，按重量计价', status: 'running', mode: 'live', created_at: '2026-11-08T10:00:00Z', task_graph: [], controller_decisions: [], verifications: [], ...changes })
const verification = (changes: Partial<DynamicVerification> = {}): DynamicVerification => ({ index: 1, timestamp: '2026-11-08T10:01:00Z', status: 'passed', isolated: true, exit_code: 0, test_count: 7, candidate_hash: 'hash-1', candidate_origin: 'model', classification: 'natural_candidate', ...changes })
const normalized = (changes: Partial<DynamicRecord> = {}) => normalizeAutonomous(record(changes))

test('unrestricted requirement is posted verbatim without legacy task or role routing', () => {
  const value = buildDynamicRequest('  实现天气文件解析，支持 Unicode 和重复记录  ')
  assert.equal(value.requirement, '实现天气文件解析，支持 Unicode 和重复记录')
  assert.deepEqual(Object.keys(value).sort(), ['budget', 'requirement'])
  assert.equal(value.budget.max_tokens, 300000)
  assert.equal(value.budget.max_output_tokens, 8192)
})
test('empty and oversized input are rejected before submission', () => {
  assert.throws(() => buildDynamicRequest('  '))
  assert.throws(() => buildDynamicRequest('a'.repeat(8001)))
})
test('same raw ID in legacy and new stores keeps both history items', () => {
  const legacy = { id: 'shared', requirement: '历史任务', workflow_id: 'development', mode: 'mock', status: 'succeeded', attempt: 1, created_at: '2026-11-08T09:00:00Z' } as Run
  const merged = mergeHistory([legacy], [record()])
  assert.deepEqual(merged.map(run => run.id), ['auto:shared', 'shared'])
  assert.equal(merged[1], legacy)
})
test('legacy deep links retain their API; new deep links use autonomous API', () => {
  assert.equal(runEndpoint('old-id'), '/runs/old-id')
  assert.equal(runEndpoint('auto:new-id'), '/autonomous/runs/new-id')
})
test('normalization retains actual tasks and raw backend ID', () => {
  const source = record({ task_graph: [{ id: 'anything', title: '任意拆分', instructions: '只分析', depends_on: [], write_paths: [], status: 'pending', agent_id: 'worker-custom' }] })
  const run = normalizeAutonomous(source)
  assert.equal(run.id, 'auto:shared')
  assert.equal(run.autonomous_id, 'shared')
  assert.equal(run.task_graph, source.task_graph)
  assert.equal(isAutonomous(run), true)
  assert.equal(source.id, 'shared')
})
test('only current candidate with actual nonempty isolated verification can be called passed', () => {
  const run = normalized({ verified_hash: 'hash-1', verifications: [verification()] })
  assert.equal(currentVerification(run)?.test_count, 7)
  for (const changes of [{ isolated: false }, { exit_code: 1 }, { test_count: 0 }, { status: 'failed' }, { candidate_hash: 'other' }]) {
    assert.equal(currentVerification(normalized({ verified_hash: 'hash-1', verifications: [verification(changes)] })), null)
  }
})
test('later failure and unverified candidate mutation invalidate displayed completion evidence', () => {
  assert.equal(currentVerification(normalized({ verified_hash: 'hash-1', verifications: [verification(), verification({ index: 2, status: 'failed' })] })), null)
  const changed = { ...normalized({ verified_hash: 'hash-1', verifications: [verification()] }), candidate_current_hash: 'changed' }
  assert.equal(currentVerification(changed), null)
  assert.match(dynamicResult({ ...changed, status: 'succeeded' }).title, /待核对/)
})
test('model summary alone cannot turn an unsuccessful run into success', () => {
  const run = normalized({ status: 'failed', summary: '所有测试通过了' })
  assert.equal(currentVerification(run), null)
  assert.equal(dynamicResult(run).tone, 'warning')
})
test('intake pending and clarification have explicit progress', () => {
  assert.match(dynamicProgress(normalized({ intake: { status: 'pending' } })), /整理需求/)
  assert.match(dynamicProgress(normalized({ intake: { status: 'needs_input', questions: ['输入格式是什么？'] } })), /补充需求/)
})
test('actual arbitrary active task names become progress without fixed stages', () => {
  const run = normalized({ task_graph: [{ id: 'custom', title: '处理 Unicode 边界', instructions: '', depends_on: [], write_paths: ['text.py'], status: 'running', agent_id: 'worker-1' }] })
  assert.equal(dynamicProgress(run), '处理 Unicode 边界')
})
test('verification in flight is derived from controller timestamps', () => {
  const run = normalized({ controller_decisions: [{ index: 1, action: 'verify', timestamp: '2026-11-08T10:02:00Z', summary: '查看真实结果' }], verifications: [verification()] })
  assert.match(dynamicProgress(run), /Docker/)
})
test('input scaffold failure is shown as baseline, not natural model repair', () => {
  const baseline = verification({ status: 'failed', candidate_origin: 'baseline', classification: 'input_baseline' })
  const run = normalized({ intake: { status: 'completed', acceptance_origin: 'model_generated' }, verifications: [baseline] })
  assert.equal(verificationLabel(baseline, true), '初始脚手架验证')
  assert.match(dynamicProgress(run), /脚手架验证已完成/)
  assert.doesNotMatch(dynamicProgress(run), /修正|失败结果调整/)
})
test('natural candidate failure gets an actual feedback progress label', () => {
  assert.match(dynamicProgress(normalized({ verifications: [verification({ status: 'failed' })] })), /测试失败结果调整计划/)
})
test('seeded and blocked verification remain distinct', () => {
  assert.equal(verificationLabel(verification({ classification: 'seeded_defect_baseline' })), '预设缺陷基线')
  assert.equal(verificationLabel(verification({ status: 'blocked', isolated: false })), '隔离验收未获确认')
})
test('AI-generated acceptance is attributed only when provenance says so', () => {
  assert.equal(generatedAcceptance(normalized({ intake: { status: 'completed', acceptance_origin: 'model_generated' } })), true)
  assert.equal(generatedAcceptance(normalized()), false)
})
test('new freeform task titles do not fall back to the legacy Todo catalog', () => {
  assert.equal(dynamicTitle(normalized({ requirement: '为文件名建立自然排序工具' })), '为文件名建立自然排序工具')
})
test('a failed quality review overrides a succeeded status without rewriting raw evidence', () => {
  const run = normalized({ status: 'succeeded', verified_hash: 'hash-1', verifications: [verification()],
    quality_review: { passed: false, summary: '实现迎合错误验收', issues: ['排序不符合用户指定的 Unicode 顺序'] } })
  assert.equal(currentVerification(run), null)
  assert.equal(dynamicResult(run).title, '验收存在问题，需要复核')
  assert.equal(dynamicProgress(run), '验收存在问题，需要复核')
  assert.equal(needsReview(run), true)
  assert.equal(displayStatus(run), 'needs_human')
  assert.equal(run.status, 'succeeded')
  assert.equal((run.verifications as DynamicVerification[])[0].status, 'passed')
})
test('posthoc contradiction takes precedence over a passing AI quality review', () => {
  const run = normalized({ status: 'succeeded', verified_hash: 'hash-1', verifications: [verification()], quality_review: { passed: true, summary: 'AI判断通过', issues: [], candidate_hash: 'hash-1' },
    posthoc_review: { passed: false, summary: '人工检查发现契约错误', issues: ['预期顺序与原始需求相反'], source: 'manual_review' } })
  assert.equal(needsReview(run), true)
  assert.equal(dynamicResult(run).tone, 'warning')
  assert.equal(executionReviews(run).length, 2)
})
test('AI-generated acceptance requires a passing quality check for the same candidate', () => {
  const base = { status: 'succeeded', verified_hash: 'hash-1', verifications: [verification()], intake: { status: 'completed', acceptance_origin: 'model_generated' } }
  assert.equal(currentVerification(normalized(base)), null)
  assert.equal(currentVerification(normalized({ ...base, quality_review: { passed: true, issues: [], summary: '通过', candidate_hash: 'old' } })), null)
  assert.equal(currentVerification(normalized({ ...base, quality_review: { passed: true, issues: [], summary: '通过', candidate_hash: 'hash-1' } }))?.test_count, 7)
})
test('failed AI acceptance does not assert that a natural code defect has been confirmed', () => {
  assert.equal(verificationLabel(verification({ status: 'failed' }), true), 'AI 验收下的候选，原因待核查')
  assert.equal(verificationLabel(verification({ status: 'failed' }), false), '真实模型生成的候选')
})
test('contract review records are surfaced and unknown pass values stay unconfirmed', () => {
  const run = normalized({ intake: { status: 'completed', contract_review: { passed: 'yes', summary: '未知协议', issues: [] } } })
  assert.equal(executionReviews(run)[0].passed, null)
  assert.equal(executionReviews(run)[0].label, '验收契约检查')
})
test('failed review during active worker revision retains real execution progress and history status', () => {
  const run = normalized({ status: 'running', summary: '修订候选', quality_review: { passed: false, issues: ['排序错误'], summary: '需要修订' },
    task_graph: [{ id: 'fix-sort', title: '修正 Unicode 排序', instructions: '', depends_on: [], write_paths: ['report.py'], status: 'running', agent_id: 'worker-sort' }] })
  assert.equal(dynamicProgress(run), '修正 Unicode 排序')
  assert.equal(displayStatus(run), 'running')
  assert.equal(needsReview(run), false)
})
test('ongoing contract revision uses actual backend summary rather than implying a manual pause', () => {
  const run = normalized({ status: 'running', summary: '正在根据契约检查结果重新生成验收', contract_review: { passed: false, issues: ['预期顺序矛盾'], summary: '当前提案不通过' } })
  assert.equal(dynamicProgress(run), '正在根据契约检查结果重新生成验收')
  assert.equal(displayStatus(run), 'running')
  assert.equal(needsReview(run), false)
  const terminal = { ...run, status: 'needs_human' }
  assert.equal(dynamicProgress(terminal), '验收标准尚未通过检查')
  assert.equal(displayStatus(terminal), 'needs_human')
  assert.equal(needsReview(terminal), false)
})
test('protocol termination remains the primary outcome after an earlier rejected contract', () => {
  const run = normalized({ status: 'failed', summary: 'Model output still violates the protocol after one recorded format retry',
    bundle: null, bundle_sha256: null, intake: { status: 'pending', acceptance_origin: 'model_generated' },
    contract_review: { passed: false, summary: '验收提案缺少边界条件', issues: ['全空行定义不清'] } })
  assert.equal(hasCandidateProject(run), false)
  assert.equal(displayStatus(run), 'failed')
  assert.equal(needsReview(run), false)
  assert.equal(dynamicProgress(run), '模型输出格式不符合要求，任务已停止')
  assert.equal(dynamicResult(run).title, '模型输出格式不符合要求，任务已停止')
  assert.equal(executionReviews(run)[0].summary, '验收提案缺少边界条件')
})
test('transport and blocked execution causes are not masked by a previous contract problem', () => {
  const failed = normalized({ status: 'failed', summary: '模型 HTTP 429', contract_review: { passed: false, issues: [], summary: '待修订' } })
  assert.equal(dynamicResult(failed).title, '模型服务限制了请求，任务已停止')
  assert.equal(displayStatus(failed), 'failed')
  const blocked = { ...failed, status: 'blocked', summary: 'Docker unavailable' }
  assert.equal(dynamicResult(blocked).title, '执行条件受阻，任务尚未完成')
  assert.equal(displayStatus(blocked), 'blocked')
})

test('HTTP scope comes from frozen Web context or explicit intake metadata, never requirement wording', () => {
  assert.equal(isWebRun(normalized({ requirement: '创建 HTTP Web JSON 函数', project_context: {} })), false)
  assert.equal(isWebRun(normalized({ project_context: { web: { schema: 'python-web-v1' } } })), true)
  assert.equal(isWebRun(normalized({ bundle: { requirement: 'Web', files: {}, acceptance: '', provenance: { web: { schema: 'python-web-v1' } } } })), true)
  assert.equal(isWebRun(normalized({ intake: { status: 'completed', verification_kind: 'http' } })), true)
  assert.equal(isWebRun(normalized({ bundle: { requirement: '', files: {}, acceptance: '', provenance: { web: 'http' } } })), false)
})

test('Web acceptance identifies HTTP and separates it from browser preview without altering old scope', () => {
  const web = normalized({ project_context: { web: { schema: 'python-web-v1' } } })
  assert.match(acceptanceScope(web), /HTTP.*冻结依赖.*Docker/)
  assert.match(acceptanceScope(web), /浏览器预览和页面交互需另行检查/)
  assert.doesNotMatch(acceptanceScope(web), /JSON.*函数/)
  assert.equal(acceptanceScope(normalized()), '测试覆盖范围受本次验收标准限制，不等于已经证明所有需求正确。')
})

test('HTTP metadata labels verification progress but cannot establish successful execution', () => {
  const run = normalized({ project_context: { web: { schema: 'python-web-v1' } },
    controller_decisions: [{ index: 1, action: 'verify', timestamp: '2026-11-09T10:02:00Z', summary: '验证 HTTP' }] })
  assert.equal(dynamicProgress(run), '正在 Docker 中执行 HTTP 验收测试')
  assert.equal(currentVerification(run), null)
  assert.equal(currentVerification(normalized({ ...record(), verified_hash: 'hash-1',
    verifications: [verification({ verification_kind: 'http', isolated: false })] })), null)
})
