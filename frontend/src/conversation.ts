import type { Run, TaskDefinition } from './types'

export type Intent = { taskId: string | null; workflowId: 'development' | 'review'; reason: string }
export type TestEvidence = {
  status: string; testsRun: number | null; failures: number | null; errors: number | null;
  isolated: boolean; round: number; reason: string;
}

const TASK_NAMES: Record<string, string> = {
  completion: '完成状态与筛选', bulk_edit: '批量编辑与原子更新',
  priority_query: '优先级与稳定分页查询', json_merge: 'JSON 导入、导出与原子合并',
}
const REQUEST_HEADER = '# AgentForge 任务请求 v1\n\n用户需求 JSON：'
const CONTRACT_HEADER = '\n\n## 固定验收契约\n'
const record = (value: unknown): Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const text = (value: unknown): string => typeof value === 'string' ? value : ''
const count = (value: unknown): number | null => Number.isSafeInteger(value) && Number(value) >= 0 ? Number(value) : null

/** Small, explicit routing rules for the four supported tasks, not an LLM claim. */
export function resolveIntent(input: string): Intent {
  const value = input.trim().toLowerCase()
  const review = /审查|审阅|评审|检查.*(?:代码|问题|错误)|找.*(?:bug|问题|错误)|\b(?:review|audit|inspect)\b/i.test(value)
  // Explicit read-only instructions win over words that merely describe an
  // existing implementation, e.g. "只审查新增的功能，告诉我如何修复".
  const readOnlyText = value.replace(/(?:不要|不能|不必|无需)只(?:做|进行)?(?:审查|审阅|评审|检查)/g, '')
  const readOnly = /(?:只|仅)(?:做|进行|需要|需)?(?:审查|审阅|评审|检查)|(?:不要|不许|禁止|无需|不需要|不必|不)(?:再|直接|实际|自动|进行)?(?:修改|改动|改写|编辑|修复|修正|写入)(?=(?:任何|现有|原有|项目)?(?:源代码|代码|源码|程序|实现|文件)|[，。；！？,.;!?]|$)|\bread[ -]only\b|\b(?:only (?:review|audit|inspect)|(?:review|audit|inspect) only)\b|\b(?:do not|don't) (?:change|modify|edit|fix)\b/.test(readOnlyText)
  const editingText = value
    .replace(/(?:不要|不需要|无需|禁止|不必)(?:再|进行)?(?:修复|修正|实现|新增|增加|添加)/g, '')
    .replace(/(?:已|已经)?(?:修复|修正|实现|新增|增加|添加)(?:后|过|的)/g, '')
  const editing = /修复|修正|新增|增加|添加|(?:^|请|帮我|并|同时|需要|希望)实现|\b(?:fix|implement|add)\b/.test(editingText)
  const workflowId = readOnly || (review && !editing) ? 'review' : 'development'
  if (!value) return { taskId: null, workflowId, reason: '先描述想完成的任务。' }

  const bulk = /批量|重命名|原子更新|删除(?:任务|待办)|删掉(?:任务|待办)|\b(?:rename|batch_complete|delete)\b/.test(value)
  const priority = /优先级|分页|稳定排序|按.*排序|\b(?:priority|set_priority|min_priority|offset)\b/.test(value)
  const json = /\bjson\b|import_json|export_json|导入|原子合并|导出(?!候选|代码|结果|报告)/.test(value)
  // "批量标记完成" belongs to bulk editing. A separate filtering request does not.
  const completion = /完成状态|已完成.*筛选|未完成.*筛选|筛选.*(?:已完成|未完成|完成)|恢复未完成|\b(?:completed|completion)\b/.test(value)
    || (!bulk && /标记.*完成|任务.*完成|待办.*完成|\bcomplete\s*\(|mark.*(?:done|complete)/.test(value))
  const matches = [completion && 'completion', bulk && 'bulk_edit', priority && 'priority_query', json && 'json_merge'].filter(Boolean) as string[]
  if (matches.length > 1) return { taskId: null, workflowId, reason: '这条需求涉及多个任务，请先选择一个执行范围。' }
  if (matches.length === 1) return { taskId: matches[0], workflowId, reason: `将使用“${TASK_NAMES[matches[0]]}”的独立验收。` }
  if (workflowId === 'review' && /(?:代码|待办|todo)|^(?:请|帮我|帮忙|只)?(?:审查|审阅|检查|review|audit|inspect)$/.test(value)) {
    return { taskId: 'completion', workflowId, reason: '将审查当前待办示例，不改写代码。' }
  }
  return { taskId: null, workflowId, reason: '还没匹配到可验收的任务，请选择一个示例或补充需求。' }
}

/** Keep the user's short request separate from the unchanged acceptance contract. */
export function buildRequirement(input: string, task: TaskDefinition): string {
  const value = input.trim()
  if (!value) throw new Error('请先描述任务。')
  if (!task.requirement?.trim()) throw new Error('任务验收契约尚未加载，请刷新后重试。')
  const requirement = REQUEST_HEADER + JSON.stringify(value) + CONTRACT_HEADER + task.requirement
  if (requirement.length > 8000) throw new Error('需求与验收契约合计超过 8000 字，请缩短需求后重试。')
  return requirement
}

export function runUserMessage(run: Run): string {
  const raw = run.requirement
  if (!raw.startsWith(REQUEST_HEADER)) return raw
  const end = raw.indexOf(CONTRACT_HEADER, REQUEST_HEADER.length)
  if (end < 0) return raw
  try {
    const decoded: unknown = JSON.parse(raw.slice(REQUEST_HEADER.length, end))
    return typeof decoded === 'string' && decoded.trim() ? decoded : raw
  } catch { return raw }
}

export function runTitle(run: Run): string {
  const message = runUserMessage(run).trim()
  const legacyContract = message === run.requirement && (message.startsWith('#') || message.length > 180)
  const fallback = run.task?.name || TASK_NAMES[run.task_id || 'completion'] || '开发任务'
  const line = legacyContract ? fallback : message.split(/\r?\n/).find(value => value.trim())?.trim() || fallback
  const characters = Array.from(line)
  return characters.length > 28 ? characters.slice(0, 28).join('') + '…' : line
}

function candidateFiles(value: unknown, contentField: 'after' | 'content'): string | null {
  const files = record(value).files
  if (!Array.isArray(files) || !files.length) return null
  const normalized = files.map(item => {
    const file = record(item)
    return typeof file.path === 'string' && typeof file[contentField] === 'string'
      ? { path: file.path, content: file[contentField] } : null
  })
  if (normalized.some(item => !item)) return null
  return JSON.stringify(normalized.sort((a, b) => a!.path.localeCompare(b!.path)))
}

function scopeMatches(scope: unknown, round: number, verifiedHash: unknown): boolean {
  if (scope === undefined) return true // Raw runtime tool events do not yet carry a scope wrapper.
  const value = record(scope)
  return value.id === `round-${round}/verify` && value.attempt === round
    && typeof value.candidate_hash === 'string'
    && (typeof verifiedHash !== 'string' || value.candidate_hash === verifiedHash)
}

/** Read trusted runtime evidence for the current candidate, never the model's test opinion. */
export function getTestEvidence(run: Run): TestEvidence | null {
  const round = count(run.attempt) ?? 0
  const events = run.events || []
  const node = `round-${round}/verify/test`
  let testIndex = -1
  for (let index = 0; index < events.length; index++) {
    const event = events[index], data = record(event.data)
    if (event.type === 'tool' && event.node_id === node && data.tool === 'run_acceptance') testIndex = index
  }
  if (testIndex < 0) return null
  const testEvent = events[testIndex], data = record(testEvent.data), output = record(data.output)
  if (data.source !== 'runtime' || !['completed', 'failed', 'blocked'].includes(text(data.status)) || !Object.keys(output).length) return null
  if (run.task_id && output.task_id && output.task_id !== run.task_id) return null
  if (run.task?.acceptance_sha256 && output.acceptance_sha256 !== run.task.acceptance_sha256) return null
  if (!scopeMatches(output.evidence_scope, round, run.status === 'succeeded' ? run.verified_hash : undefined)) return null

  const current = candidateFiles(run.diff, 'after')
  if (current === null) return null
  let origin = -1, original: string | null = null
  for (let index = 0; index < events.length; index++) {
    const event = events[index], item = record(event.data)
    if (event.type !== 'tool' || item.source !== 'runtime' || item.status !== 'completed') continue
    const write = item.tool === 'candidate_diff' && (event.node_id === 'develop' || /^round-\d+\/fix$/.test(event.node_id || ''))
    const baseline = run.workflow_id === 'review' && item.tool === 'read_project' && event.node_id === 'prepare'
    if (write || baseline) {
      origin = index
      original = candidateFiles(item.output, write ? 'after' : 'content')
    }
  }
  if (origin < 0 || origin >= testIndex || original !== current) return null
  const step = run.steps?.find(item => item.id === node)
  const scopedTest = record(record(step?.output).test)
  if (Object.keys(scopedTest).length && !scopeMatches(scopedTest.evidence_scope, round, run.status === 'succeeded' ? run.verified_hash : undefined)) return null

  const isolated = output.isolated === true
  const reason = text(output.reason)
  if (!isolated || output.status === 'blocked') {
    return { status: 'blocked', testsRun: null, failures: null, errors: null, isolated: false, round,
      reason: reason || '隔离验收尚未完成。' }
  }
  const stdout = text(output.stdout), stderr = text(output.stderr)
  const markers = [...stdout.matchAll(/^AGENTFORGE_ACCEPTANCE=(\{[^\r\n]*\})\s*$/gm)]
  let attestation: Record<string, unknown> = {}
  if (markers.length === 1) {
    try { attestation = record(JSON.parse(markers[0][1])) } catch { /* Unknown evidence stays unknown. */ }
  }
  const ran = stderr.match(/(?:^|\n)Ran (\d+) tests? in [^\r\n]+/)
  const reported = ran ? count(Number(ran[1])) : null
  const attested = count(attestation.tests)
  const testsRun = attested ?? reported
  const expected = count(run.task?.expected_tests) ?? count(output.expected_tests)
  const contradictoryCount = reported !== null && attested !== null && reported !== attested
  const passed = output.status === 'passed' && output.exit_code === 0 && attestation.passed === true
    && attested !== null && !contradictoryCount && (expected === null || attested === expected)
  const failedSummary = stderr.match(/(?:^|\n)FAILED \(([^\r\n]*)\)/)
  const failures = failedSummary ? count(Number(failedSummary[1].match(/\bfailures=(\d+)/)?.[1] ?? 0)) : passed ? 0 : null
  const errors = failedSummary ? count(Number(failedSummary[1].match(/\berrors=(\d+)/)?.[1] ?? 0)) : passed ? 0 : null
  return {
    status: passed ? 'passed' : output.status === 'failed' ? 'failed' : 'incomplete',
    testsRun: contradictoryCount ? null : testsRun, failures, errors, isolated, round,
    reason: reason || (contradictoryCount ? '测试记录中的执行数量不一致，暂不确认结果。'
      : output.status === 'passed' && !passed ? '尚未获得完整且匹配的验收结果。' : ''),
  }
}

export function getRunSummary(run: Run): { title: string; description: string; tone: 'success' | 'warning' | 'neutral' } {
  const summary = (run.summary || '').replace(/^执行失败：(?:RuntimeErrorSafe:\s*)?/, '').trim()
  if (run.status === 'queued') return { title: '正在准备任务', description: '任务已保存，等待开始执行。', tone: 'neutral' }
  if (run.status === 'running') {
    const active = (run.steps || []).filter(step => step.status === 'running')
    const leaves = active.filter(step => !active.some(other => other.id.startsWith(step.id + '/')))
    return { title: '正在处理你的需求', description: leaves.length ? `正在${leaves.map(step => step.label).join('、')}。` : summary || '正在读取项目并安排执行。', tone: 'neutral' }
  }
  if (run.status === 'succeeded') {
    const evidence = getTestEvidence(run)
    if (evidence?.status !== 'passed') {
      return { title: run.workflow_id === 'review' ? '当前代码需要重新验证' : '当前候选需要重新验证',
        description: `${run.applied ? '原修改已应用；' : '原运行记录为已通过，但'}当前副本尚无匹配且通过的完整验收证据。请查看验证记录，重新验证后再使用。`,
        tone: 'warning' }
    }
    if (run.applied) return { title: '修改已应用', description: '已经写入示例项目。此次运行的修改和验收记录已保留。', tone: 'success' }
    if (run.mode === 'mock') return { title: '演示已完成', description: summary || '固定模拟输出已完成流程；这不是一次真实模型开发。', tone: 'success' }
    return { title: run.workflow_id === 'review' ? '代码检查完成' : '修改已准备好',
      description: summary || (run.workflow_id === 'review' ? '本次检查已完成。' : '可以查看修改、导出代码，再决定是否应用。'), tone: 'success' }
  }
  if (run.status === 'needs_human') return { title: '需要进一步处理', description: summary || '本次检查仍有待解决的问题，请查看结果后新建任务。', tone: 'warning' }
  if (run.status === 'blocked') return { title: '任务暂时受阻', description: summary || '执行条件尚未满足；已完成的记录会保留。', tone: 'warning' }
  if (run.status === 'failed') {
    const title = /HTTP 429/.test(summary) ? '模型服务返回了限制'
      : /HTTP (?:401|403)/.test(summary) ? '模型请求未获授权'
        : /HTTP 5\d\d/.test(summary) ? '模型服务暂时不可用' : '任务暂未完成'
    return { title, description: summary || '执行中出现错误，请展开过程查看详情。', tone: 'warning' }
  }
  return { title: '任务记录', description: summary || '任务状态尚未确认。', tone: 'neutral' }
}
