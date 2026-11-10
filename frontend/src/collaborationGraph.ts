import type { Event, Run } from './types'
import { projectActivity, projectToolLabel } from './autonomous.ts'

/** Every replay frame is an observed event, except an explicitly limited snapshot. */
export interface ReplayFrame {
  id: string; eventId: string | null; index: number; timestamp: string;
  type: string; label: string; category: string; event?: Event;
  snapshot?: boolean; notice?: string;
}
export interface GraphNode {
  id: string; kind: 'controller' | 'task' | 'tool' | 'review'; label: string; status: string;
  agentId?: string; instructions?: string; writePaths: string[]; dependsOn: string[];
  summary?: string; details: Record<string, unknown>; eventIds: string[];
}
export interface GraphEdge {
  id: string; source: string; target: string;
  kind: 'dispatch' | 'dependency' | 'verification' | 'feedback' | 'review' | 'tool';
  label: string; eventIds: string[];
}
export interface GraphSnapshot {
  nodes: GraphNode[]; edges: GraphEdge[]; status: string; summary: string; cursor: number;
  currentFrame: ReplayFrame | null; eventCount: number; visibleEvents: Event[];
  limited: boolean; notice: string | null;
  metrics: { requests: number; knownTokens: number; formatRetries: number; transportFailures: number };
}

const object = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const list = (value: unknown): unknown[] => Array.isArray(value) ? value : []
const string = (value: unknown): string => typeof value === 'string' ? value : ''
const strings = (value: unknown): string[] => list(value).filter((item): item is string => typeof item === 'string')
const copy = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T
const stable = (value: unknown): string => JSON.stringify(value, (_key, item) => item && typeof item === 'object' && !Array.isArray(item)
  ? Object.fromEntries(Object.entries(item).sort(([a], [b]) => a.localeCompare(b))) : item)
const taskKey = (id: string) => `task:${id}`
const count = (value: unknown) => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : 0
const terminalStates = new Set(['succeeded', 'failed', 'blocked', 'needs_human'])

function validationCategory(data: Record<string, unknown>) {
  if (data.classification === 'imported_baseline') return { category: 'imported_baseline', label: '导入项目基线' }
  if (data.classification === 'input_baseline') return { category: 'baseline', label: '初始项目基线' }
  if (data.classification === 'seeded_defect_baseline' || data.classification === 'seeded_experiment_candidate') return { category: 'seeded_defect', label: '预设缺陷实验' }
  if (data.acceptance_origin === 'model_generated') return { category: 'generated_acceptance', label: 'AI 生成验收的候选检查' }
  return { category: 'candidate_validation', label: '候选验收' }
}
function describe(event: Event): { label: string; category: string } {
  const data = object(event.data)
  const activity = projectActivity(event)
  if (activity) return { label: activity.label + (event.type === 'tool_started' ? ' · 已请求' : event.type === 'tool_interrupted' ? ' · 中断' : event.type === 'tool_result' ? ` · ${['failed', 'blocked'].includes(string(data.status)) ? '未完成' : '已返回'}` : ''), category: event.type === 'main_edit' ? 'direct_edit' : 'project_tool' }
  if (event.type.startsWith('verification')) {
    const value = validationCategory(data)
    return { ...value, label: `${value.label} · ${event.type === 'verification_started' ? '已请求' : event.type === 'verification_interrupted' ? '中断' : data.status === 'passed' ? '通过' : data.status === 'failed' ? '未通过' : '未确认'}` }
  }
  if (event.type === 'model_request' && data.format_retry === true) return { label: '发送格式重试', category: 'format_retry' }
  if (event.type === 'format_error') return { label: data.retry_scheduled ? '输出格式错误，等待重试' : '输出格式错误，停止重试', category: 'protocol_failure' }
  if (event.type === 'model_response' && data.status === 'transport_failed') return { label: '模型传输失败', category: 'transport_failure' }
  if (event.type === 'model_response' && data.status === 'cancelled') return { label: '模型请求中断，完成情况未知', category: 'interruption' }
  const labels: Record<string, [string, string]> = {
    created: ['建立运行', 'lifecycle'], intake_started: ['理解需求', 'planning'], intake_completed: ['冻结接口与验收', 'planning'],
    intake_revision: ['修订验收契约', 'planning'], intake_needs_input: ['需要补充需求', 'planning'],
    intake_rejected: ['生成验收未通过检查', 'planning'], controller_decision: ['主 Agent 作出决定', 'planning'],
    decision_rejected: ['决定被拒绝，未按该决定执行', 'decision_rejected'], plan_revision: ['更新实际任务计划', 'planning'],
    dispatch: ['分配就绪任务', 'dispatch'], task_result: ['子 Agent 提交结果', 'execution'],
    task_interrupted: ['子任务中断', 'interruption'], interrupted_recovery: ['恢复中断记录，未重放请求', 'interruption'],
    integrity_failure: ['候选完整性未确认', 'interruption'], posthoc_review: ['事后核查', 'posthoc'],
    contract_review_started: ['开始契约检查', 'review'], contract_review: ['契约检查结果', 'review'],
    quality_review_started: ['开始交付质量检查', 'review'], quality_review: ['交付质量检查结果', 'review'],
    model_request: ['发送真实模型请求', 'model_request'], model_response: ['收到模型响应', 'model_response'],
    budget_reserve_blocked: ['为最终验证保留预算，未发送本次请求', 'budget'],
    budget_finalizing: ['进入收尾验证', 'budget'], budget_finalization_stopped: ['收尾未完成，需要处理', 'budget'],
    budget_finalized: ['收尾检查完成', 'budget'],
    terminal: ['运行结束', 'lifecycle'], snapshot: ['保存快照', 'snapshot'], error: ['记录错误', 'error'],
  }
  const [label, category] = labels[event.type] ?? [event.message || event.type, 'event']
  if (event.type === 'plan_revision' && strings(data.task_ids).length) return { label: '撤销待执行任务', category: 'cancellation' }
  return { label, category }
}

function limitedReplay(run: Run, reason: string) {
  return { limited: true, reason, frames: [{ id: 'snapshot:final', eventId: null, index: 0,
    timestamp: run.updated_at || run.created_at, type: 'snapshot', label: '只读最终记录', category: 'snapshot',
    snapshot: true, notice: reason } satisfies ReplayFrame] }
}

export function buildReplay(run: Run): { frames: ReplayFrame[]; limited: boolean; reason?: string } {
  if (run.engine !== 'model-directed-v1' || !run.events?.length) return limitedReplay(run, '这份旧记录缺少动态编排事件，只能查看最终记录，不能完整回放。')
  const keyed = new Map<string, Event>()
  for (const event of run.events) {
    const id = String(event.id)
    if (!/^\d+$/.test(id) || !Number.isSafeInteger(Number(id))) return limitedReplay(run, '事件序号无法可靠排序，只展示最终记录。')
    const key = String(Number(id)), previous = keyed.get(key)
    const normalized = { ...copy(event), id: Number(id) }
    if (previous && stable(previous) !== stable(normalized)) return limitedReplay(run, '同一事件序号包含冲突记录，不能可靠回放。')
    keyed.set(key, normalized)
  }
  const events = [...keyed.values()].sort((a, b) => Number(a.id) - Number(b.id))
  if (events[0]?.type !== 'created') return limitedReplay(run, '记录缺少运行起点，只能查看最终记录，不能完整回放。')
  if (terminalStates.has(run.status) && !events.some(event => event.type === 'terminal' || event.type === 'interrupted_recovery')) {
    return limitedReplay(run, '记录缺少运行终态事件，只能查看最终记录。')
  }
  const established = new Set<string>()
  for (const event of events) {
    const data = object(event.data)
    if (event.type === 'plan_revision') for (const item of list(data.added)) established.add(string(object(item).id))
    if (event.type === 'dispatch') for (const item of list(data.tasks)) established.add(string(object(item).id))
    if ((['task_result', 'task_interrupted'].includes(event.type) || event.type === 'integrity_failure' && !data.tool) && !established.has(string(data.id) || string(event.node_id))) {
      return limitedReplay(run, '任务结果缺少先前的建任务或派发记录，只能查看最终记录。')
    }
  }
  if (list(run.task_graph).some(item => !established.has(string(object(item).id)))) return limitedReplay(run, '任务图与事件记录不完整，只能查看最终记录。')
  if (run.posthoc_review && !events.some(event => event.type === 'posthoc_review')) return limitedReplay(run, '事后核查缺少对应事件，只能查看最终记录。')
  return { limited: false, frames: events.map((event, index) => ({ id: `event:${event.id}`, eventId: String(event.id),
    index, timestamp: event.timestamp, type: event.type, ...describe(event), event })) }
}

function emptySnapshot(cursor: number): GraphSnapshot {
  return { nodes: [], edges: [], status: 'not_started', summary: '尚未到达任何执行事件', cursor,
    currentFrame: null, eventCount: 0, visibleEvents: [], limited: false, notice: null,
    metrics: { requests: 0, knownTokens: 0, formatRetries: 0, transportFailures: 0 } }
}

/** Reduces ONLY the chosen event prefix. Never reads final task/results/status. */
export function projectFrame(run: Run, frames: ReplayFrame[], cursor: number): GraphSnapshot {
  const position = Number.isFinite(cursor) ? Math.min(frames.length - 1, Math.max(-1, Math.floor(cursor))) : frames.length - 1
  const snapshot = emptySnapshot(position)
  if (position < 0) return snapshot
  if (frames[position]?.snapshot) return finalSnapshot(run, frames[position], position)
  const nodes = new Map<string, GraphNode>(), edges = new Map<string, GraphEdge>()
  const requests = new Map<number, { nodeId: string; role: string }>()
  const activeTools = new Map<string, string[]>()
  let lastDecision: Record<string, unknown> | null = null
  let activeContract = '', activeQuality = ''
  const ensure = (id: string, kind: GraphNode['kind'], label: string): GraphNode => {
    if (!nodes.has(id)) nodes.set(id, { id, kind, label, status: 'pending', writePaths: [], dependsOn: [], details: {}, eventIds: [] })
    return nodes.get(id)!
  }
  const mark = (node: GraphNode, event: Event) => { if (!node.eventIds.includes(String(event.id))) node.eventIds.push(String(event.id)) }
  const edge = (source: string, target: string, kind: GraphEdge['kind'], label: string, event: Event) => {
    if (!nodes.has(source) || !nodes.has(target)) return
    const id = `${kind}:${source}->${target}`, previous = edges.get(id)
    edges.set(id, { id, source, target, kind, label, eventIds: [...new Set([...(previous?.eventIds ?? []), String(event.id)])] })
  }
  const createTask = (data: Record<string, unknown>, event: Event) => {
    const id = string(data.id)
    if (!id) return null
    const node = ensure(taskKey(id), 'task', string(data.title) || id)
    if (typeof data.title === 'string') node.label = data.title
    if (typeof data.instructions === 'string') node.instructions = data.instructions
    if (Array.isArray(data.write_paths)) node.writePaths = strings(data.write_paths)
    if (Array.isArray(data.depends_on)) node.dependsOn = strings(data.depends_on)
    if (typeof data.agent_id === 'string') node.agentId = data.agent_id
    node.details.taskId = id
    if (lastDecision && lastDecision.action === 'dispatch' && !lastDecision.rejected) node.details.decision = copy(lastDecision)
    mark(node, event)
    return node
  }
  for (const frame of frames.slice(0, position + 1)) {
    if (!frame.event) continue
    const event = copy(frame.event), data = object(event.data)
    snapshot.visibleEvents.push(event); snapshot.eventCount++; snapshot.currentFrame = frame
    const main = ensure('main', 'controller', '主 Agent')
    mark(main, event)
    if (event.type === 'created') { main.status = 'pending'; main.details.requirement = run.requirement; snapshot.status = 'queued'; snapshot.summary = event.message }
    if (['intake_started', 'model_request', 'controller_decision', 'dispatch'].includes(event.type)) {
      if (!terminalStates.has(snapshot.status)) snapshot.status = 'running'
      if (!terminalStates.has(main.status)) main.status = 'running'
    }
    if (event.type.startsWith('intake')) {
      main.details.intake = { ...object(main.details.intake), ...data }
      if (data.summary || event.message) main.summary = string(data.summary) || event.message
      if (event.type === 'intake_needs_input' || event.type === 'intake_rejected') main.status = 'needs_human'
    }
    if (event.type === 'controller_decision') {
      lastDecision = { ...data, eventId: String(event.id) }
      main.details.decision = copy(lastDecision); main.summary = string(data.summary)
      snapshot.summary = main.summary || event.message
    }
    if (event.type === 'decision_rejected') {
      const decision = object(data.decision)
      if (lastDecision && lastDecision.index === decision.index) { lastDecision.rejected = true; main.details.decision = copy(lastDecision) }
      main.details.rejectedDecision = data; main.summary = string(data.reason) || event.message
      snapshot.summary = main.summary
    }
    if (event.type === 'main_edit') {
      main.details.edits = [...list(main.details.edits), { ...data, eventId: String(event.id) }]
      main.writePaths = [...new Set([...main.writePaths, ...strings(data.changed_paths)])]
      main.summary = projectActivity(event)?.summary || event.message
      snapshot.summary = main.summary
    }
    if (['tool_started', 'tool_result', 'tool_interrupted'].includes(event.type) || event.type === 'integrity_failure' && data.tool) {
      const scope = stable({ tool: data.tool, arguments: data.arguments, candidate_sha256: data.candidate_sha256 })
      const pending = activeTools.get(scope) ?? []
      const started = event.type === 'tool_started'
      const id = started ? `tool:${event.id}` : pending.pop() || `tool:result:${event.id}`
      const node = ensure(id, 'tool', projectToolLabel(data.tool))
      if (started) pending.push(id)
      activeTools.set(scope, pending)
      const result = object(data.result)
      const confirmed = result.isolated === true && result.container_invoked === true
      node.details = { ...data, category: 'project_tool', startObserved: started || node.details.startObserved === true,
        isolationConfirmed: confirmed, verification_evidence: false }
      node.status = started ? 'requested' : event.type !== 'tool_result' ? 'interrupted'
        : ['failed', 'blocked'].includes(string(data.status)) ? string(data.status)
          : data.tool === 'run_application' && !confirmed ? 'unconfirmed' : 'completed'
      node.summary = projectActivity(event)?.summary || event.message
      mark(node, event)
      edge('main', node.id, 'tool', '请求受控项目工具', event)
      if (!started) edge(node.id, 'main', 'feedback', '工具结果 · 非验收结论', event)
      main.details.lastProjectTool = data
    }
    if (event.type === 'plan_revision') {
      for (const item of list(data.added)) {
        const node = createTask(object(item), event)
        if (node) edge('main', node.id, 'dispatch', '已建立任务，等待派发', event)
      }
      for (const id of strings(data.task_ids)) {
        const node = nodes.get(taskKey(id))
        if (node) { node.status = 'cancelled'; node.summary = string(data.summary) || '主 Agent 撤销待执行任务'; node.details.cancellation = data; mark(node, event) }
      }
    }
    if (event.type === 'dispatch') for (const item of list(data.tasks)) {
      const node = createTask(object(item), event)
      if (node) { node.status = 'running'; node.details.dispatchedAt = event.timestamp; edge('main', node.id, 'dispatch', '派发给子 Agent', event) }
    }
    if (event.type === 'plan_revision' || event.type === 'dispatch') for (const node of nodes.values()) {
      if (node.kind === 'task') for (const dependency of node.dependsOn) edge(taskKey(dependency), node.id, 'dependency', '先完成依赖任务', event)
    }
    if (['task_result', 'task_interrupted', 'integrity_failure'].includes(event.type)) {
      const node = nodes.get(taskKey(string(data.id) || string(event.node_id)))
      if (node) {
        node.status = event.type === 'task_result' ? string(data.status) || 'unknown' : event.type === 'task_interrupted' ? 'interrupted' : 'blocked'
        node.agentId = string(data.agent_id) || node.agentId
        node.summary = string(object(data.result).summary) || string(data.error) || event.message
        node.details.result = data; mark(node, event)
        edge(node.id, 'main', 'feedback', '子任务结果', event)
      }
    }
    if (event.type.startsWith('verification')) {
      const index = count(data.index), id = `test:${index || event.id}`
      const node = ensure(id, 'tool', `Docker 验收 ${index || ''}`.trim())
      node.details = { ...node.details, ...data }; mark(node, event)
      if (event.type === 'verification_started') {
        node.status = 'preparing'; node.summary = '已请求验收，等待容器回执'; node.details.isolationConfirmed = false
      } else if (event.type === 'verification_interrupted') {
        node.status = 'interrupted'; node.summary = string(data.reason) || '验收中断，未获完整回执'; node.details.isolationConfirmed = false
      } else if (event.type === 'verification') {
        const confirmed = data.isolated === true && data.container_invoked === true
        node.status = confirmed ? string(data.status) || 'unknown' : 'blocked'
        // Do not retain the preparation event's container_confirmed=false or
        // stage=preparing alongside a completed runtime receipt.
        node.details = { ...data, isolationConfirmed: confirmed,
          startObserved: node.eventIds.some(idValue => snapshot.visibleEvents.some(item => String(item.id) === idValue && item.type === 'verification_started')) }
        node.summary = `${validationCategory(data).label}：${confirmed ? data.status === 'passed' ? '通过' : '未通过' : '隔离未确认'}${count(data.test_count) ? `，${count(data.test_count)} 项` : ''}`
        edge(node.id, 'main', 'feedback', `${validationCategory(data).label}反馈`, event)
        main.details.lastVerification = data
      }
      edge('main', node.id, 'verification', '运行验收工具', event)
    }
    if (event.type === 'contract_review_started' || event.type === 'contract_review') {
      const index = count(data.proposal_index) || count(data.index) || event.id
      activeContract = `review:contract:${index}`
      const node = ensure(activeContract, 'review', `验收契约检查 ${index}`)
      node.status = event.type.endsWith('_started') ? 'running' : data.passed === true ? 'succeeded' : data.passed === false ? 'failed' : 'unknown'
      node.summary = string(data.summary) || event.message; node.details = { ...node.details, ...data }; mark(node, event)
      edge('main', node.id, 'review', '检查生成的验收契约', event)
      if (!event.type.endsWith('_started')) edge(node.id, 'main', 'feedback', '契约检查结果', event)
    }
    if (event.type === 'quality_review_started' || event.type === 'quality_review') {
      activeQuality = `review:quality:${string(data.candidate_hash) || count(data.index) || event.id}`
      const node = ensure(activeQuality, 'review', '交付质量检查')
      node.status = event.type.endsWith('_started') ? 'running' : data.passed === true ? 'succeeded' : data.passed === false ? 'failed' : 'unknown'
      node.summary = string(data.summary) || event.message; node.details = { ...node.details, ...data }; mark(node, event)
      edge('main', node.id, 'review', '对照原始需求检查', event)
      if (!event.type.endsWith('_started')) edge(node.id, 'main', 'feedback', '交付检查结果', event)
    }
    if (event.type === 'posthoc_review') {
      const node = ensure(`review:posthoc:${event.id}`, 'review', '事后核查')
      node.status = data.passed === true ? 'succeeded' : data.passed === false ? 'failed' : 'unknown'
      node.summary = string(data.summary) || event.message; node.details = data; mark(node, event)
      edge(node.id, 'main', 'feedback', '事后复核结论', event)
      main.details.posthocReview = data
      if (data.passed === false) {
        if (main.details.originalStatus === undefined) main.details.originalStatus = snapshot.status
        if (main.details.originalSummary === undefined) main.details.originalSummary = main.summary || snapshot.summary
        main.status = 'needs_human'; main.summary = node.summary
        snapshot.status = 'needs_human'; snapshot.summary = node.summary
      }
    }
    if (event.type === 'model_request') {
      const role = string(data.role), target = role === 'worker' ? taskKey(string(event.node_id))
        : role === 'contract_review' ? activeContract : role === 'final_review' ? activeQuality : 'main'
      const node = nodes.get(target) || main, sequence = count(data.sequence)
      requests.set(sequence, { nodeId: node.id, role }); snapshot.metrics.requests++
      if (data.format_retry === true) snapshot.metrics.formatRetries++
      node.details.requests = [...list(node.details.requests), { ...data, status: 'running', eventId: String(event.id) }]; mark(node, event)
    }
    if (event.type === 'model_response' || event.type === 'format_error') {
      const request = requests.get(count(data.sequence)), node = nodes.get(request?.nodeId || taskKey(string(event.node_id))) || main
      node.details.requests = list(node.details.requests).map(value => {
        const item = object(value)
        return item.sequence === data.sequence ? { ...item, ...data, status: event.type === 'format_error' ? 'protocol_failed' : data.status } : value
      })
      if (event.type === 'format_error') node.details.formatErrors = [...list(node.details.formatErrors), data]
      if (event.type === 'model_response') {
        if (data.usage_known === true) snapshot.metrics.knownTokens += count(data.tokens)
        if (data.status === 'transport_failed') snapshot.metrics.transportFailures++
      }
      mark(node, event)
    }
    if (event.type === 'terminal') {
      snapshot.status = string(data.status) || 'unknown'; snapshot.summary = string(data.summary) || event.message
      main.status = snapshot.status; main.summary = snapshot.summary
      for (const node of nodes.values()) if (node.id !== 'main' && ['running', 'preparing', 'requested'].includes(node.status)) {
        node.status = 'interrupted'; node.summary = '运行已结束，此节点未获完成确认'
        node.details.completionUnknown = true; mark(node, event)
      }
    }
    if (event.type === 'interrupted_recovery') {
      snapshot.status = 'blocked'; snapshot.summary = event.message; main.status = 'blocked'; main.summary = event.message
      for (const node of nodes.values()) if (node.id !== 'main' && ['running', 'preparing', 'requested'].includes(node.status)) { node.status = 'interrupted'; node.summary = '进程中断，完成情况未确认'; mark(node, event) }
    }
  }
  snapshot.nodes = [...nodes.values()]; snapshot.edges = [...edges.values()]
  return snapshot
}

function finalSnapshot(run: Run, frame: ReplayFrame, cursor: number): GraphSnapshot {
  const snapshot = emptySnapshot(cursor)
  snapshot.currentFrame = frame; snapshot.limited = true; snapshot.notice = frame.notice || '事件记录不足，不能完整回放。'
  const posthoc = object(run.posthoc_review)
  snapshot.status = posthoc.passed === false ? 'needs_human' : run.status
  snapshot.summary = posthoc.passed === false ? string(posthoc.summary) : run.summary || '仅有最终记录'
  snapshot.nodes.push({ id: 'main', kind: 'controller', label: '主 Agent · 最终记录', status: snapshot.status,
    summary: snapshot.summary, writePaths: [], dependsOn: [], eventIds: [], details: { requirement: run.requirement, originalStatus: run.status, replayUnavailable: true } })
  const tasks = list(run.task_graph).length ? list(run.task_graph) : list(run.steps)
  for (const value of tasks) {
    const task = object(value), id = string(task.id)
    if (!id) continue
    snapshot.nodes.push({ id: taskKey(id), kind: 'task', label: string(task.title) || string(task.label) || id,
      status: string(task.status) || 'unknown', agentId: string(task.agent_id) || undefined,
      instructions: string(task.instructions) || undefined, writePaths: strings(task.write_paths), dependsOn: strings(task.depends_on),
      summary: string(object(task.result).summary) || string(task.error), details: { ...task, replayUnavailable: true }, eventIds: [] })
  }
  for (const value of list(run.verifications)) {
    const verification = object(value)
    const confirmed = verification.isolated === true && verification.container_invoked === true
    snapshot.nodes.push({ id: `test:${verification.index}`, kind: 'tool', label: `Docker 验收 ${verification.index}`,
      status: confirmed ? string(verification.status) || 'unknown' : 'blocked',
      writePaths: [], dependsOn: [], details: { ...verification, isolationConfirmed: confirmed, startObserved: false, replayUnavailable: true },
      eventIds: [], summary: `${validationCategory(verification).label}：${confirmed ? verification.status === 'passed' ? '通过' : '未通过' : '隔离未确认'}` })
  }
  if (Object.keys(posthoc).length) snapshot.nodes.push({ id: 'review:posthoc:final', kind: 'review', label: '事后核查',
    status: posthoc.passed === true ? 'succeeded' : posthoc.passed === false ? 'failed' : 'unknown',
    summary: string(posthoc.summary), writePaths: [], dependsOn: [], details: posthoc, eventIds: [] })
  return snapshot
}
