import type { Event, Run } from './types'

export interface DynamicTask {
  id: string; title: string; instructions: string; depends_on: string[]; write_paths: string[];
  status: string; agent_id: string; started_at?: string; finished_at?: string;
  profile_id?: string; profile_version?: number; profile_sha256?: string;
  result?: { summary?: string; changed_paths?: string[] }; error?: string;
}
export interface DynamicDecision {
  index: number; timestamp: string; action: string; summary: string;
  tasks?: DynamicTask[]; task_ids?: string[];
}
export interface DynamicVerification {
  index: number; timestamp: string; status: string; isolated: boolean; exit_code: number | null;
  test_count: number; candidate_hash: string; candidate_origin: string; classification: string;
  stdout?: string; stderr?: string; reason?: string;
  verification_kind?: string;
  attestation?: { failures?: number; errors?: number; skipped?: number; passed?: boolean };
  [key: string]: unknown;
}
export interface DynamicIntake {
  status: string; specification?: string; questions?: string[]; acceptance_origin?: string;
  verification_kind?: string; coverage_notice?: string;
  [key: string]: unknown;
}
export interface ExecutionReview {
  key: 'contract_review' | 'quality_review' | 'posthoc_review';
  label: string; passed: boolean | null; summary: string; issues: string[]; raw: Record<string, unknown>;
}
export interface DynamicRecord {
  id: string; engine: string; requirement: string; status: string; mode: 'live';
  created_at: string; updated_at?: string; summary?: string; verified_hash?: string | null;
  intake?: DynamicIntake;
  project_context?: { web?: { schema?: string; [key: string]: unknown }; [key: string]: unknown };
  bundle?: { requirement: string; files: Record<string, string>; acceptance: string; provenance: Record<string, unknown> } | null;
  task_graph?: DynamicTask[]; controller_decisions?: DynamicDecision[]; verifications?: DynamicVerification[];
  model_metrics?: { model_calls?: number; tokens?: number; metrics_complete?: boolean; contract_retries?: number; unknown_usage_calls?: number; in_flight?: number };
  [key: string]: unknown;
}
export interface DynamicExport { run: DynamicRecord; events: Event[]; steps: unknown[]; snapshots: unknown[] }

export function isAutonomous(run: Run | null | undefined): boolean {
  return run?.engine === 'model-directed-v1'
}
export function normalizeAutonomous(record: DynamicRecord, evidence?: DynamicExport): Run {
  return { ...record, id: `auto:${record.id}`, autonomous_id: record.id, workflow_id: 'autonomous', attempt: 0,
    events: evidence?.events ?? [], snapshots: evidence?.snapshots ?? [], steps: [] }
}
export function runEndpoint(selection: string): string {
  return selection.startsWith('auto:') ? `/autonomous/runs/${encodeURIComponent(selection.slice(5))}` : `/runs/${encodeURIComponent(selection)}`
}
export function dynamicData(run: Run): DynamicRecord {
  return { ...run, id: String(run.autonomous_id ?? run.id), mode: 'live', engine: String(run.engine) } as DynamicRecord
}
export function mergeHistory(legacy: Run[], autonomous: DynamicRecord[]): Run[] {
  return [...legacy, ...autonomous.map(record => normalizeAutonomous(record))]
    .sort((a, b) => b.created_at.localeCompare(a.created_at))
}
export function dynamicTitle(run: Run): string {
  const title = run.requirement.trim().split(/\r?\n/).find(Boolean) ?? '开发任务'
  return Array.from(title).length > 28 ? Array.from(title).slice(0, 28).join('') + '…' : title
}
export function buildDynamicRequest(requirement: string) {
  const text = requirement.trim()
  if (!text) throw new Error('请先描述你的需求。')
  if (text.length > 8000) throw new Error('需求请控制在 8000 字以内。')
  return { requirement: text, budget: { max_tokens: 300000, max_calls: 40, max_output_tokens: 8192 } }
}
export function currentVerification(run: Run): DynamicVerification | null {
  const value = dynamicData(run)
  const latest = value.verifications?.at(-1)
  if (reviewProblems(run).length) return null
  if (generatedAcceptance(run) && !executionReviews(run).some(review => ['quality_review', 'posthoc_review'].includes(review.key)
      && review.passed === true && review.raw.candidate_hash === value.verified_hash)) return null
  if (!latest || latest.status !== 'passed' || latest.isolated !== true || latest.exit_code !== 0
      || !Number.isSafeInteger(latest.test_count) || latest.test_count <= 0
      || !value.verified_hash || latest.candidate_hash !== value.verified_hash
      || (typeof run.candidate_current_hash === 'string' && run.candidate_current_hash !== value.verified_hash)) return null
  return latest
}
export function generatedAcceptance(run: Run): boolean {
  const value = dynamicData(run)
  return value.intake?.acceptance_origin === 'model_generated' || value.bundle?.provenance.acceptance_origin === 'model_generated'
}
export function isWebRun(run: Run): boolean {
  const value = dynamicData(run)
  const frozenWeb = value.project_context?.web ?? value.bundle?.provenance.web
  return value.intake?.verification_kind === 'http'
    || (!!frozenWeb && typeof frozenWeb === 'object' && !Array.isArray(frozenWeb)
      && (frozenWeb as Record<string, unknown>).schema === 'python-web-v1')
}
export function acceptanceScope(run: Run): string {
  return isWebRun(run)
    ? 'HTTP 验收在冻结依赖的 Docker 中执行；浏览器预览和页面交互需另行检查。测试通过不代表所有需求均已验证。'
    : '测试覆盖范围受本次验收标准限制，不等于已经证明所有需求正确。'
}
export function executionReviews(run: Run): ExecutionReview[] {
  const value = dynamicData(run)
  const fields = [['contract_review', '验收契约检查'], ['quality_review', '交付质量检查'], ['posthoc_review', '事后复核']] as const
  return fields.flatMap(([key, label]) => {
    const raw = value[key] ?? (key === 'contract_review' ? value.intake?.contract_review : undefined)
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return []
    const review = raw as Record<string, unknown>
    return [{ key, label, passed: typeof review.passed === 'boolean' ? review.passed : null,
      summary: typeof review.summary === 'string' ? review.summary : '',
      issues: Array.isArray(review.issues) ? review.issues.map(issue => typeof issue === 'string' ? issue : JSON.stringify(issue)) : [], raw: review }]
  })
}
export function reviewProblems(run: Run): ExecutionReview[] {
  return executionReviews(run).filter(review => review.passed === false)
}
export function displayStatus(run: Run): string {
  return needsReview(run) ? 'needs_human' : run.status
}
export function hasCandidateProject(run: Run): boolean {
  const value = dynamicData(run)
  return !!value.bundle || (typeof value.bundle_sha256 === 'string' && value.bundle_sha256.length > 0)
    || !!value.task_graph?.length || !!value.verifications?.length
}
export function failureTitle(run: Run): string {
  if (run.status === 'blocked') return '执行条件受阻，任务尚未完成'
  const summary = run.summary ?? ''
  if (/protocol|JSON|输出.*格式|输出.*契约|格式.*重试/i.test(summary)) return '模型输出格式不符合要求，任务已停止'
  if (/HTTP 429/i.test(summary)) return '模型服务限制了请求，任务已停止'
  if (/HTTP (401|403)/i.test(summary)) return '模型请求未获授权，任务已停止'
  return '执行失败，任务已停止'
}
export function needsReview(run: Run): boolean {
  return isAutonomous(run) && ['succeeded', 'needs_human'].includes(run.status)
    && hasCandidateProject(run) && reviewProblems(run).length > 0
}
export function dynamicProgress(run: Run): string {
  const value = dynamicData(run)
  const ongoing = ['queued', 'running'].includes(run.status)
  const active = value.task_graph?.filter(task => task.status === 'running') ?? []
  if (ongoing && active.length) return active.map(task => task.title).join(' · ')
  if (['failed', 'blocked'].includes(run.status)) return failureTitle(run)
  if (ongoing) {
    let review = ''
    for (const event of run.events ?? []) {
      if (event.type === 'quality_review_started') review = 'final_review'
      else if (event.type === 'contract_review_started') review = 'contract_review'
      else if (event.type === 'model_request') review = String((event.data as { role?: string } | null)?.role || '')
      else if (['quality_review', 'contract_review', 'controller_decision', 'terminal'].includes(event.type)) review = ''
    }
    if (review === 'final_review') return '正在检查交付质量'
    if (review === 'contract_review') return '正在检查验收契约'
    const finishing = value.finishing_state
    if (finishing && typeof finishing === 'object' && !Array.isArray(finishing)
        && (finishing as Record<string, unknown>).phase === 'finalizing') return '正在收尾，核对当前版本的验收与审查'
  }
  if (reviewProblems(run).length) return ongoing ? run.summary?.trim() || '检查发现问题，主 Agent 正在处理'
    : hasCandidateProject(run) ? '验收存在问题，需要复核' : '验收标准尚未通过检查'
  if (value.intake?.status === 'needs_input') return '需要补充需求'
  if (!ongoing) return run.status === 'succeeded' ? '本次执行已结束' : '执行已暂停，记录已保留'
  if (value.intake && value.intake.status !== 'completed') return '正在整理需求、接口和验收标准'
  const decision = value.controller_decisions?.at(-1)
  const lastVerification = value.verifications?.at(-1)
  const lastActivity = [...(run.events ?? [])].reverse().find(event => ['main_edit', 'tool_started', 'tool_result', 'tool_interrupted'].includes(event.type))
  if (decision?.action === 'edit') return lastActivity?.type === 'main_edit' ? '主 Agent 已修改代码，正在判断下一步' : '主 Agent 正在准备直接修改代码'
  if (decision?.action === 'tool') return lastActivity?.type === 'tool_started' ? '已请求项目工具，等待实际结果' : '主 Agent 正在根据工具结果判断下一步'
  if (decision?.action === 'verify' && (!lastVerification || lastVerification.timestamp < decision.timestamp)) return isWebRun(run) ? '正在 Docker 中执行 HTTP 验收测试' : '正在 Docker 中执行验收测试'
  if (lastVerification?.status === 'failed') {
    if (lastVerification.classification === 'imported_baseline' && !value.task_graph?.length) return '项目基线检查已记录，主 Agent 正在规划本次修改'
    if (lastVerification.classification === 'input_baseline' && !value.task_graph?.length && value.intake) return '脚手架验证已完成，主 Agent 正在拆分任务'
    return '主 Agent 正在根据测试失败结果调整计划'
  }
  if (value.task_graph?.length) return '主 Agent 正在评估执行结果和下一步计划'
  return run.status === 'queued' ? '等待主 Agent 开始' : '主 Agent 正在判断下一步'
}
export function dynamicResult(run: Run) {
  const value = dynamicData(run)
  if (['failed', 'blocked'].includes(run.status)) return { title: failureTitle(run), tone: 'warning' }
  if (reviewProblems(run).length) return { title: hasCandidateProject(run) ? '验收存在问题，需要复核' : '验收标准尚未通过检查', tone: 'warning' }
  if (value.intake?.status === 'needs_input') return { title: '补充一些信息后继续', tone: 'warning' }
  if (run.status === 'succeeded' && ['conflict', 'failed'].includes(String((value.publication as { status?: string } | undefined)?.status))) return { title: '开发运行已结束，项目版本尚未更新', tone: 'warning' }
  if (run.status === 'succeeded' && currentVerification(run)) return { title: '已完成，并通过本次验收', tone: 'success' }
  if (run.status === 'succeeded') return { title: '执行已结束，验收证据待核对', tone: 'warning' }
  if (run.status === 'needs_human') return { title: '需要你处理', tone: 'warning' }
  if (run.status === 'blocked') return { title: '执行条件暂时受阻', tone: 'warning' }
  return { title: '本次任务尚未完成', tone: 'warning' }
}
export const actionNames: Record<string, string> = {
  dispatch: '分配任务', continue: '继续执行', cancel: '调整任务', verify: '运行验收', finish: '完成检查', stop: '停止并说明原因', edit: '直接修改代码', tool: '使用项目工具',
}
export function projectToolLabel(name: unknown): string {
  return ({ read_file: '读取源码', search_project: '搜索项目', run_application: '运行候选程序' } as Record<string, string>)[String(name)] ?? '项目工具'
}
export function projectActivity(event: Event): { label: string; summary: string } | null {
  const data = event.data && typeof event.data === 'object' && !Array.isArray(event.data) ? event.data as Record<string, unknown> : {}
  if (event.type === 'main_edit') {
    const changed = Array.isArray(data.changed_paths) ? data.changed_paths.filter(path => typeof path === 'string') : []
    return { label: '主 Agent 直接修改', summary: changed.length ? `已修改 ${changed.join('、')}；尚需后续验收。` : '未产生源码变化。' }
  }
  if (!['tool_started', 'tool_result', 'tool_interrupted'].includes(event.type)) return null
  const label = projectToolLabel(data.tool)
  if (event.type === 'tool_started') return { label, summary: '已请求工具，等待实际结果；派发不代表执行完成。' }
  if (event.type === 'tool_interrupted') return { label, summary: '工具已中断，完成情况未确认。' }
  const result = data.result && typeof data.result === 'object' && !Array.isArray(data.result) ? data.result as Record<string, unknown> : {}
  if (['blocked', 'failed'].includes(String(data.status))) return { label, summary: typeof data.error === 'string' ? data.error : typeof result.reason === 'string' ? result.reason : '工具未完成，错误已反馈给主 Agent。' }
  if (data.tool === 'run_application') return { label, summary: result.isolated === true && result.container_invoked === true
    ? `已收到 Docker 回执${typeof result.exit_code === 'number' ? `，退出码 ${result.exit_code}` : ''}；程序运行不等于验收通过。`
    : '尚无完整 Docker 隔离回执；不能认定程序执行完成。' }
  return { label, summary: '已返回只读查询结果，未运行候选代码。' }
}
export const dynamicStateNames: Record<string, string> = {
  queued: '等待开始', running: '执行中', pending: '待执行', succeeded: '已完成', failed: '失败', cancelled: '已撤销', blocked: '受阻', needs_human: '需要处理',
}
export function verificationLabel(verification: DynamicVerification, intake = false): string {
  if (verification.status === 'blocked' || verification.isolated !== true) return '隔离验收未获确认'
  if (verification.classification === 'seeded_defect_baseline') return '预设缺陷基线'
  if (verification.classification === 'seeded_experiment_candidate') return '预设缺陷实验候选'
  if (verification.classification === 'imported_baseline') return '导入项目基线 · 修改前的实际代码'
  if (verification.classification === 'input_baseline') return intake ? '初始脚手架验证' : '修改前的项目基线'
  if (intake && verification.classification === 'natural_candidate' && verification.status === 'failed') return 'AI 验收下的候选，原因待核查'
  return '真实模型生成的候选'
}
