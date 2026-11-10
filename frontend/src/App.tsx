import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { api, post } from './api'
import type { Event, Health, Run, Step, TaskDefinition } from './types'
import { Management } from './Management'
import { getRunSummary, getTestEvidence, runTitle, runUserMessage } from './conversation'
import { AutonomousDetails, AutonomousThread } from './AutonomousRun'
import { CollaborationGraph } from './CollaborationGraphView'
import { ProjectWorkspace } from './ProjectWorkspace'
import { awaitsProjectPublication } from './projects'
import { buildDynamicRequest, displayStatus, dynamicData, dynamicTitle, isAutonomous, mergeHistory, needsReview, normalizeAutonomous, runEndpoint } from './autonomous'
import type { DynamicExport, DynamicRecord } from './autonomous'

type View = 'tasks' | 'projects' | 'agents' | 'knowledge'
type Panel = 'changes' | 'validation' | 'activity' | 'project' | 'graph' | null
const stateNames: Record<string, string> = { queued: '等待开始', running: '进行中', succeeded: '已完成', failed: '执行失败', blocked: '执行受阻', needs_human: '需要处理', pending: '待执行', skipped: '已跳过', cancelled: '已撤销' }
const eventNames: Record<string, string> = { schedule: '分工', tool: '工具', agent: 'Agent 输出', validation: '验证', feedback: '修正反馈', status: '状态', error: '错误', snapshot: '快照', controller_decision: '主 Agent 决策', plan_revision: '计划调整', dispatch: '子 Agent 分工', task_result: '子 Agent 结果', verification: 'Docker 验收', verification_started: '准备验收', verification_interrupted: '验收中断', model_request: '模型请求', model_response: '模型响应', format_error: '格式重试', intake_started: '整理需求', intake_completed: '需求与验收已冻结', terminal: '执行结束', created: '建立任务', decision_rejected: '决定未执行', integrity_failure: '完整性检查失败', contract_review: '验收契约检查', contract_review_started: '检查验收契约', quality_review: '交付质量检查', quality_review_started: '检查交付质量', posthoc_review: '事后复核', main_edit: '主 Agent 直接修改', tool_started: '请求项目工具', tool_result: '项目工具结果', tool_interrupted: '项目工具中断', budget_reserve_blocked: '保留收尾预算', budget_finalizing: '收尾验证', budget_finalization_stopped: '收尾未完成', budget_finalized: '收尾检查完成' }
const examples = [
  { icon: 'file', text: '编写一个 CSV 销售报表工具，按地区汇总金额，并处理空行、无效金额和带引号的字段。' },
  { icon: 'sort', text: '实现区间合并工具：合并重叠或相邻的整数区间，查询某个点是否在区间内，返回窗口中的空隙。' },
  { icon: 'check', text: '编写文本统计工具，统计单词频率、忽略大小写和标点，按出现次数排序，并测试空文本与并列排序。' },
]
const titleOf = (run: Run) => isAutonomous(run) ? dynamicTitle(run) : runTitle(run)
const object = (value: unknown): Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const printable = (value: unknown) => typeof value === 'string' ? value : JSON.stringify(value, null, 2)
const timestamp = (value?: string) => value ? new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(value)) : ''
const selectedFromLocation = () => new URLSearchParams(window.location.hash.slice(1)).get('task')
const projectFromLocation = () => new URLSearchParams(window.location.hash.slice(1)).get('project')

function Icon({ name, size = 18 }: { name: string; size?: number }) {
  const paths: Record<string, ReactNode> = {
    forge: <path d="M4 7h16l-3 4H7zM9 11v5l-3 4h12l-3-4v-5M9 3h6" />,
    plus: <path d="M12 5v14M5 12h14" />, send: <path d="M12 20V4m-6 6 6-6 6 6" />,
    check: <path d="m5 12 4 4L19 6" />, close: <path d="m6 6 12 12M6 18 18 6" />,
    search: <><circle cx="10" cy="10" r="6" /><path d="m15 15 6 6" /></>,
    settings: <><path d="M4 7h16M4 17h16" /><circle cx="9" cy="7" r="3" /><circle cx="16" cy="17" r="3" /></>,
    folder: <path d="M3 6h7l2 3h9v11H3V6Z" />,
    book: <path d="M3 4h7l2 2 2-2h7v16h-7l-2 1-2-1H3V4Zm9 2v15" />,
    agents: <><circle cx="9" cy="8" r="3" /><path d="M3 21v-3a6 6 0 0 1 12 0v3m2-16a3 3 0 0 1 0 6m1 4a5 5 0 0 1 3 5" /></>,
    panel: <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M9 4v16" /></>,
    chevron: <path d="m9 5 7 7-7 7" />, code: <path d="m8 6-6 6 6 6m8-12 6 6-6 6m-3-14-2 16" />,
    file: <path d="M5 3h9l5 5v13H5V3Zm9 0v6h5M8 13h8M8 17h5" />,
    edit: <path d="m15 4 5 5M4 20l5-1L21 7l-5-5L4 14v6Z" />,
    sort: <path d="M8 4v16m-4-4 4 4 4-4M15 5h6m-6 6h4m-4 6h2" />,
    refresh: <path d="M20 8a8 8 0 1 0 0 8m0-13v5h-5" />,
    clock: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>,
    download: <path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5" />,
    alert: <><circle cx="12" cy="12" r="9" /><path d="M12 7v6m0 4h.01" /></>,
  }
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] ?? paths.file}</svg>
}
function Code({ value, diff = false }: { value: string; diff?: boolean }) {
  return <div className="code-scroll" tabIndex={0} aria-label={diff ? '代码修改差异' : '项目代码'}><pre>{value.split('\n').map((line, i) => <span key={i} className={`code-line ${diff && line.startsWith('+') ? 'addition' : diff && line.startsWith('-') ? 'deletion' : diff && line.startsWith('@@') ? 'hunk' : ''}`}><span className="line-number" aria-hidden="true">{i + 1}</span><code>{line || ' '}</code></span>)}</pre></div>
}
function EvidenceEvent({ event }: { event: Event }) {
  return <article className={`evidence-event ${event.type}`}><div><span>{eventNames[event.type] ?? event.type}</span><time>{timestamp(event.timestamp)}</time></div><p>{event.message}</p>{event.data != null && <details><summary>输入与输出</summary><pre>{printable(event.data)}</pre></details>}</article>
}
function Review({ step }: { step: Step }) {
  const output = object(step.output)
  return <section className="review-section"><div className="section-line"><h3>{step.label || '代码审查'}</h3><span className={output.passed === true ? 'success-text' : 'muted'}>{output.passed === true ? '通过' : output.passed === false ? '发现问题' : stateNames[step.status] ?? step.status}</span></div><p>{typeof output.summary === 'string' ? output.summary : '尚未获得完整审查结果。'}</p>{Array.isArray(output.issues) && output.issues.length > 0 && <ul>{output.issues.map((issue, index) => <li key={index}>{printable(issue)}</li>)}</ul>}<details><summary>完整审查记录</summary><pre>{printable(step)}</pre></details></section>
}

export default function App() {
  const [view, setView] = useState<View>(() => projectFromLocation() !== null ? 'projects' : 'tasks')
  const [selectedProject, setSelectedProject] = useState<string | null>(() => projectFromLocation() || null)
  const [projectDraft, setProjectDraft] = useState('')
  const [health, setHealth] = useState<Health | null>(null)
  const [tasks, setTasks] = useState<TaskDefinition[]>([])
  const [runs, setRuns] = useState<Run[]>([])
  const [selectedRun, setSelectedRun] = useState<string | null>(selectedFromLocation)
  const [run, setRun] = useState<Run | null>(null)
  const [draft, setDraft] = useState('')
  const [panel, setPanelState] = useState<Panel>(null)
  const [graphExpanded, setGraphExpanded] = useState(false)
  const [sourceMode, setSourceMode] = useState(false)
  const [candidateFiles, setCandidateFiles] = useState<Record<string, string>>({})
  const [candidateLoading, setCandidateLoading] = useState(false)
  const [candidateError, setCandidateError] = useState('')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [pollVersion, setPollVersion] = useState(0)
  const [refreshing, setRefreshing] = useState(false)
  const [search, setSearch] = useState('')
  const [searchOpen, setSearchOpen] = useState(false)
  const [confirmApply, setConfirmApply] = useState(false)
  const [environmentOpen, setEnvironmentOpen] = useState(false)
  const [mobileNav, setMobileNav] = useState(false)
  const [narrow, setNarrow] = useState(() => window.matchMedia('(max-width: 1050px)').matches)
  const [tick, setTick] = useState(Date.now())
  const alive = useRef(false)
  const input = useRef<HTMLTextAreaElement>(null)
  const searchInput = useRef<HTMLInputElement>(null)
  const applyDialog = useRef<HTMLDialogElement>(null)
  const mobileDialog = useRef<HTMLDialogElement>(null)
  const detailPanel = useRef<HTMLElement>(null)
  const panelValue = useRef<Panel>(null)
  const panelOpener = useRef<HTMLElement | null>(null)
  const panelOpenerLabel = useRef('')
  const submitGuard = useRef(false)
  const selection = useRef(selectedRun)
  const navigationVersion = useRef(0)

  const setPanel = useCallback((next: Panel) => {
    // Capture the external opener before React makes the background inert.
    // Expanding or switching an existing panel must not replace that opener.
    if (next !== null && panelValue.current === null) {
      const focused = document.activeElement
      panelOpener.current = focused instanceof HTMLElement && focused !== document.body && !detailPanel.current?.contains(focused) ? focused : null
      panelOpenerLabel.current = panelOpener.current?.getAttribute('aria-label') || panelOpener.current?.textContent?.trim() || ''
    }
    panelValue.current = next
    setPanelState(next)
  }, [])

  const refresh = useCallback(async () => {
    setRefreshing(true)
    try {
      const [nextHealth, nextTasks, legacy, autonomous] = await Promise.allSettled([api<Health>('/health'), api<TaskDefinition[]>('/tasks'), api<Run[]>('/runs'), api<DynamicRecord[]>('/autonomous/runs')])
      if (!alive.current) return
      if (nextHealth.status === 'fulfilled') setHealth(nextHealth.value)
      else setHealth(null)
      if (nextTasks.status === 'fulfilled') setTasks(nextTasks.value)
      setRuns(previous => mergeHistory(legacy.status === 'fulfilled' ? legacy.value : previous.filter(item => !isAutonomous(item)), autonomous.status === 'fulfilled' ? autonomous.value : previous.filter(isAutonomous).map(dynamicData)))
      const failures = [nextHealth, legacy, autonomous].filter(result => result.status === 'rejected')
      setError(failures.map(result => result.status === 'rejected' ? String(result.reason instanceof Error ? result.reason.message : result.reason) : '').join('；'))
    } catch (e) { if (alive.current) { setHealth(null); setError(e instanceof Error ? e.message : String(e)) } }
    finally { if (alive.current) { setLoading(false); setRefreshing(false) } }
  }, [])
  useEffect(() => { alive.current = true; void refresh(); return () => { alive.current = false } }, [refresh])
  useEffect(() => {
    const media = window.matchMedia('(max-width: 1050px)')
    const update = () => setNarrow(media.matches)
    media.addEventListener('change', update)
    return () => media.removeEventListener('change', update)
  }, [])
  const panelOpen = panel !== null
  const modalPanel = narrow || (panel === 'graph' && graphExpanded)
  const modalPanelOpen = panelOpen && modalPanel
  useEffect(() => { setGraphExpanded(false) }, [selectedRun, panel])
  useEffect(() => {
    if (!modalPanelOpen) return
    detailPanel.current?.querySelector<HTMLButtonElement>('button[aria-current="page"], button')?.focus()
  }, [modalPanelOpen])
  useEffect(() => {
    if (panelOpen || !panelOpener.current) return
    const opener = panelOpener.current
    const openerLabel = panelOpenerLabel.current
    panelOpener.current = null
    // Restore only after the close commit has removed every inert attribute.
    const frame = requestAnimationFrame(() => {
      // Live planning can replace the pre-plan graph entry with the task-list
      // entry while the panel is open. Prefer its matching current control.
      const target = opener.isConnected ? opener : [...document.querySelectorAll<HTMLButtonElement>('button')].find(button =>
        (button.getAttribute('aria-label') || button.textContent?.trim()) === openerLabel && button.getClientRects().length && !button.closest('[inert]'))
      if (target && !target.closest('[inert]')) target.focus()
    })
    return () => cancelAnimationFrame(frame)
  }, [panelOpen])
  useEffect(() => {
    const navigate = () => {
      const id = selectedFromLocation(), projectId = projectFromLocation()
      selection.current = id; navigationVersion.current++; setSelectedRun(id); setRun(null)
      setSelectedProject(projectId || null); setProjectDraft(''); setView(projectId !== null ? 'projects' : 'tasks'); setPanel(null)
    }
    window.addEventListener('hashchange', navigate)
    return () => window.removeEventListener('hashchange', navigate)
  }, [])
  useEffect(() => {
    if (!selectedRun) { setRun(null); return }
    let cancelled = false
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        const response = await api<Run | DynamicExport>(runEndpoint(selectedRun))
        const next = selectedRun.startsWith('auto:') ? normalizeAutonomous((response as DynamicExport).run, response as DynamicExport) : response as Run
        if (cancelled) return
        setRun(next)
        setRuns(previous => [next, ...previous.filter(item => item.id !== next.id)].sort((a, b) => b.created_at.localeCompare(a.created_at)))
        if (['queued', 'running'].includes(next.status) || awaitsProjectPublication(next)) timer = setTimeout(poll, 1000)
      } catch (e) { if (!cancelled) { setError(e instanceof Error ? e.message : String(e)); timer = setTimeout(poll, 5000) } }
    }
    setRun(previous => previous?.id === selectedRun ? previous : null)
    void poll()
    return () => { cancelled = true; clearTimeout(timer) }
  }, [selectedRun, pollVersion])
  const running = !!run && ['queued', 'running'].includes(run.status)
  const hasActiveRuns = runs.some(item => ['queued', 'running'].includes(item.status))
  useEffect(() => {
    if (!hasActiveRuns) return
    let cancelled = false
    const timer = setInterval(() => {
      if (!cancelled) void refresh()
    }, 5000)
    return () => { cancelled = true; clearInterval(timer) }
  }, [hasActiveRuns, refresh])
  useEffect(() => { if (!running) return; const timer = setInterval(() => setTick(Date.now()), 1000); return () => clearInterval(timer) }, [running])
  useEffect(() => {
    if (confirmApply && !applyDialog.current?.open) applyDialog.current?.showModal()
    if (!confirmApply && applyDialog.current?.open) applyDialog.current.close()
  }, [confirmApply])
  useEffect(() => {
    if (mobileNav && !mobileDialog.current?.open) mobileDialog.current?.showModal()
    if (!mobileNav && mobileDialog.current?.open) mobileDialog.current.close()
  }, [mobileNav])
  useEffect(() => { if (searchOpen) searchInput.current?.focus() }, [searchOpen, mobileNav])
  useEffect(() => {
    const shortcut = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        if (modalPanelOpen) return
        setSearchOpen(true); if (window.innerWidth <= 760) setMobileNav(true)
      }
      if (event.key === 'Escape') { setPanel(null); setEnvironmentOpen(false) }
    }
    window.addEventListener('keydown', shortcut); return () => window.removeEventListener('keydown', shortcut)
  }, [modalPanelOpen, setPanel])

  const autonomousRun = isAutonomous(run)
  const activeTaskId = run?.task_id ?? 'completion'
  const activeTask = run?.task ?? tasks.find(item => item.id === activeTaskId)
  const events = run?.events ?? []
  const steps = run?.steps ?? []
  const test = run && !autonomousRun ? getTestEvidence(run) : null
  const summary = run && !autonomousRun ? getRunSummary(run) : null
  const userMessage = run ? runUserMessage(run) : ''
  const userPreview = run && userMessage === run.requirement && (userMessage.startsWith('#') || userMessage.length > 180) ? `任务：${runTitle(run)}` : userMessage.length > 500 ? userMessage.slice(0, 500) + '…' : userMessage
  const diffFiles = run?.diff?.files ?? []
  const changedFiles = diffFiles.filter(file => file.before !== file.after)
  const canApply = !autonomousRun && run?.status === 'succeeded' && test?.status === 'passed' && run.workflow_id === 'development' && !run.applied && activeTaskId === 'completion' && changedFiles.length > 0
  const plan = object(steps.find(step => step.id === 'plan' && step.status === 'succeeded')?.output)
  const currentSteps = steps.filter(step => ['queued', 'running'].includes(step.status))
  const reviews = steps.filter(step => (step.id.endsWith('/review') || step.id.endsWith('/inspect') || ['review', 'inspect'].includes(step.id)) && (step.attempt ?? 0) === (run?.attempt ?? 0))
  const activity = events.filter(event => ['agent', 'feedback', 'error'].includes(event.type))
  const recent = runs.filter(item => `${titleOf(item)} ${item.requirement}`.toLowerCase().includes(search.toLowerCase()))
  const liveReady = !!health?.model.configured
  const canSubmit = !!draft.trim() && !!health && !busy && liveReady && !!health.sandbox.available
  const endedAt = events.filter(event => ['status', 'error', 'terminal'].includes(event.type)).at(-1)?.timestamp
  const elapsed = run ? Math.max(0, Math.round(((running ? tick : new Date(endedAt ?? run.updated_at ?? run.created_at).getTime()) - new Date(run.created_at).getTime()) / 1000)) : 0
  useEffect(() => {
    setCandidateFiles({}); setCandidateError('')
    if (panel !== 'changes' || !run || !isAutonomous(run) || !dynamicData(run).bundle) { setCandidateLoading(false); return }
    let cancelled = false; setCandidateLoading(true)
    void api<{ files: Record<string, string>; sha256: string }>(`${runEndpoint(run.id)}/candidate`)
      .then(result => { if (!cancelled) { setCandidateFiles(result.files); setRun(previous => previous?.id === run.id ? { ...previous, candidate_current_hash: result.sha256 } : previous) } })
      .catch(e => { if (!cancelled) setCandidateError(e instanceof Error ? e.message : String(e)) })
      .finally(() => { if (!cancelled) setCandidateLoading(false) })
    return () => { cancelled = true }
  }, [panel, run?.id, run?.updated_at, run?.status])

  function selectRun(id: string | null) {
    selection.current = id; navigationVersion.current++
    setSelectedRun(id); setRun(previous => previous?.id === id ? previous : null); setView('tasks')
    setError(''); setNotice(''); setPanel(null); setMobileNav(false); setConfirmApply(false)
    window.history.replaceState(null, '', id ? `#task=${encodeURIComponent(id)}` : window.location.pathname)
  }
  function newTask() { selectRun(null); requestAnimationFrame(() => input.current?.focus()) }
  function useExample(example: typeof examples[number]) { setDraft(example.text); input.current?.focus() }
  async function startRun(event: FormEvent) {
    event.preventDefault()
    if (!canSubmit || submitGuard.current) return
    const origin = navigationVersion.current
    const submittedDraft = draft
    submitGuard.current = true; setBusy(true); setError(''); setNotice('')
    try {
      const response = await post<DynamicRecord>('/autonomous/requests', buildDynamicRequest(draft))
      const next = normalizeAutonomous(response)
      if (!alive.current) return
      setRuns(previous => [next, ...previous.filter(item => item.id !== next.id)])
      if (navigationVersion.current === origin) { selectRun(next.id); setRun(next); setDraft(current => current === submittedDraft ? '' : current) }
    } catch (e) { if (alive.current && navigationVersion.current === origin) setError(e instanceof Error ? e.message : String(e)) }
    finally { submitGuard.current = false; if (alive.current) setBusy(false) }
  }
  async function resumeRun() {
    if (!run || busy) return
    const id = run.id
    setBusy(true); setError('')
    try {
      await post(`/runs/${id}/resume`)
      if (!alive.current) return
      if (selection.current === id) { setNotice('恢复请求已提交，正在读取最新执行状态。'); setPollVersion(value => value + 1) }
    } catch (e) { if (alive.current && selection.current === id) setError(e instanceof Error ? e.message : String(e)) }
    finally { if (alive.current) setBusy(false) }
  }
  async function applyRun() {
    if (!run || !canApply || busy) return
    const id = run.id
    setBusy(true); setError('')
    try {
      await post(`/runs/${id}/apply`, { confirmed: true })
      if (!alive.current) return
      setConfirmApply(false)
      if (selection.current === id) { setNotice('修改已应用到待办示例项目。'); setPollVersion(value => value + 1) }
      void refresh()
    }
    catch (e) { if (alive.current && selection.current === id) setError(e instanceof Error ? e.message : String(e)) } finally { if (alive.current) setBusy(false) }
  }
  async function exportCandidate() {
    if (!run || busy) return
    if (isAutonomous(run)) {
      const id = run.id
      setBusy(true); setError('')
      try {
        const response = await fetch(`/api${runEndpoint(id)}/candidate.zip`, { signal: AbortSignal.timeout(20000) })
        const contentType = response.headers.get('content-type') || ''
        if (!response.ok) {
          const body = contentType.includes('application/json') ? await response.json() : null
          throw new Error(typeof body?.detail === 'string' ? body.detail : `项目下载失败 (${response.status})`)
        }
        if (!contentType.includes('application/zip')) throw new Error('服务器未返回有效的项目 ZIP，请重新连接后再试。')
        const blob = await response.blob()
        if (!blob.size) throw new Error('项目 ZIP 为空，请重新连接后再试。')
        if (alive.current) saveBlob(`${String(run.autonomous_id)}-candidate.zip`, blob)
      } catch (e) {
        if (alive.current && selection.current === id) setError(e instanceof Error ? e.message : String(e))
      } finally { if (alive.current) setBusy(false) }
      return
    }
    for (const file of changedFiles) {
      const url = URL.createObjectURL(new Blob([file.after], { type: 'text/x-python;charset=utf-8' }))
      const link = document.createElement('a'); link.href = url; link.download = `${activeTaskId}-${run.id.slice(0, 8)}-${file.path}`; link.click()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    }
  }
  function download(name: string, content: string) {
    saveBlob(name, new Blob([content], { type: 'application/json;charset=utf-8' }))
  }
  function saveBlob(name: string, blob: Blob) {
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a'); link.href = url; link.download = name; link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }
  async function exportEvidence() {
    if (!run) return
    try { const evidence = await api<unknown>(runEndpoint(run.id)); download(`${run.id.replace(':', '-')}-evidence.json`, JSON.stringify(evidence, null, 2)) }
    catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }
  function openProject(id: string | null) {
    selection.current = null; navigationVersion.current++; setSelectedRun(null); setRun(null)
    setSelectedProject(id); setView('projects'); setPanel(null); setMobileNav(false); setError(''); setNotice('')
    window.history.replaceState(null, '', `#project=${encodeURIComponent(id || '')}`)
  }
  function openProjectRun(record: DynamicRecord | string) {
    setProjectDraft('')
    if (typeof record === 'string') { selectRun(`auto:${record}`); return }
    const next = normalizeAutonomous(record)
    setRuns(previous => [next, ...previous.filter(item => item.id !== next.id)])
    selectRun(next.id); setRun(next)
  }
  function reviseRequirement() {
    if (run && typeof run.project_id === 'string') { setProjectDraft(run.requirement + '\n\n补充说明：'); openProject(run.project_id); return }
    if (run) setDraft(run.requirement + '\n\n补充说明：')
    newTask()
  }
  function navigate(next: View) { navigationVersion.current++; setView(next); setPanel(null); setMobileNav(false); setError(''); setNotice('') }

  const navigation = <>
    <div className="sidebar-brand"><Icon name="forge" size={23} /><span>AgentForge</span><button className="icon-button mobile-only" aria-label="关闭导航" onClick={() => setMobileNav(false)}><Icon name="close" /></button></div>
    <button className="new-task" onClick={newTask}><Icon name="plus" /><span>新任务</span></button>
    <button className={`nav-row ${view === 'projects' ? 'selected' : ''}`} onClick={() => { setProjectDraft(''); openProject(null) }} aria-current={view === 'projects' ? 'page' : undefined}><Icon name="folder" /><span>项目</span></button>
    <button className="nav-row" onClick={() => setSearchOpen(value => !value)} aria-expanded={searchOpen}><Icon name="search" /><span>搜索任务</span><kbd>Ctrl K</kbd></button>
    {searchOpen && <input ref={searchInput} className="history-search" aria-label="搜索历史任务" placeholder="搜索需求或任务名称" value={search} onChange={event => setSearch(event.target.value)} />}
    <div className="history-label">任务记录 <span>{runs.length}</span></div>
    <nav className="task-history" aria-label="任务记录">{loading ? <div className="skeleton" aria-label="正在读取历史任务"><i /><i /><i /></div> : recent.length ? recent.map(item => <button key={item.id} className={`history-item ${view === 'tasks' && selectedRun === item.id ? 'selected' : ''}`} aria-current={view === 'tasks' && selectedRun === item.id ? 'page' : undefined} onClick={() => selectRun(item.id)} title={titleOf(item)}><span className={`task-dot ${displayStatus(item)}`} /><span className="history-copy"><span>{titleOf(item)}</span><small>{isAutonomous(item) ? '动态 · ' : item.mode === 'mock' ? '历史模拟 · ' : '历史 · '}{needsReview(item) ? '需复核' : stateNames[item.status] ?? item.status} · {timestamp(item.created_at)}</small></span></button>) : <p className="sidebar-empty">{search ? '没有找到匹配的任务。' : '你的任务会保存在这里。'}</p>}</nav>
    <div className="sidebar-bottom"><button className={`nav-row ${view === 'agents' ? 'selected' : ''}`} onClick={() => navigate('agents')}><Icon name="agents" /><span>历史 Agent 设置</span></button><button className={`nav-row ${view === 'knowledge' ? 'selected' : ''}`} onClick={() => navigate('knowledge')}><Icon name="book" /><span>知识资料</span></button><div className="workspace-identity"><span className="workspace-avatar">AF</span><div>本地工作区<small>Python 隔离开发</small></div><span className={`connection-dot ${health ? 'connected' : ''}`} title={health ? '本地服务已连接' : '尚未连接后端'} /></div></div>
  </>

  return <div className={`app-shell ${panel ? 'with-panel' : ''}`}>
    <aside className="sidebar desktop-sidebar" inert={modalPanelOpen}>{navigation}</aside>
    <dialog aria-label="任务导航" className="mobile-navigation" ref={mobileDialog} inert={modalPanelOpen} onCancel={() => setMobileNav(false)} onClose={() => setMobileNav(false)}>{mobileNav && navigation}</dialog>
    <div className="main-shell">
      <header className="workspace-header" inert={modalPanelOpen}><div className="header-leading"><button className="icon-button mobile-only" aria-label="打开任务导航" onClick={() => setMobileNav(true)}><Icon name="panel" /></button><button className="project-label" onClick={() => view === 'projects' ? openProject(null) : setPanel(panel === 'project' ? null : 'project')}><Icon name="folder" size={17} /><span>{run && !autonomousRun ? '历史示例项目' : 'Python 项目'}</span><Icon name="chevron" size={12} /></button><span className="header-divider">/</span><span className="header-title">{view === 'projects' ? '项目工作区' : view === 'agents' ? 'Agent 设置' : view === 'knowledge' ? '知识资料' : run ? titleOf(run) : selectedRun ? '读取任务…' : '新任务'}</span></div><button className={`environment-button ${!health?.sandbox.available ? 'warning-text' : ''}`} onClick={() => setEnvironmentOpen(value => !value)} aria-expanded={environmentOpen}><span className={`connection-dot ${health?.sandbox.available ? 'connected' : ''}`} /><span>{loading ? '连接中' : !health ? '未连接' : health.sandbox.available ? '环境就绪' : '需要启动隔离环境'}</span><Icon name="settings" size={15} /></button></header>
      {environmentOpen && <section className="environment-details" aria-label="运行环境" inert={modalPanelOpen}><div><strong>运行环境</strong><button className="text-button" disabled={refreshing} onClick={() => void refresh()}><Icon name="refresh" size={14} />{refreshing ? '检查中…' : '重新检查'}</button></div><p>{health ? '后端已连接' : '后端未连接'} · {health?.sandbox.available ? 'Docker 隔离环境可用' : health?.sandbox.reason ?? '请启动项目的前后端服务。'}</p><p>{liveReady ? `默认模型：${health?.model.model}。${health?.model.verified ? '当前服务已验证模型调用。' : '已读取本地配置；实际可用性以本次调用结果为准。'}` : '真实模型尚未配置，请在本地 .env 中配置后重新连接。'}</p></section>}
      {error && <div className="message error-message" role="alert" inert={modalPanelOpen}><Icon name="alert" /><span>{error}</span><button className="text-button" onClick={() => { void refresh(); setPollVersion(value => value + 1) }}>重新连接</button><button className="icon-button" aria-label="关闭错误提示" onClick={() => setError('')}><Icon name="close" size={16} /></button></div>}
      {notice && <div className="message notice-message" role="status" inert={modalPanelOpen}><Icon name="check" /><span>{notice}</span><button className="icon-button" aria-label="关闭提示" onClick={() => setNotice('')}><Icon name="close" size={16} /></button></div>}
      <div className="workspace-content">
        <main className={`conversation-main ${!selectedRun && view === 'tasks' ? 'is-new' : ''}`} inert={modalPanelOpen}>
          {view === 'tasks' && run && typeof run.project_id === 'string' && <div className="project-run-return"><button className="text-button" onClick={() => { setProjectDraft(''); openProject(run.project_id as string) }}>← 返回项目与版本</button><span>{object(run.publication).status === 'saved' ? `本次修改已保存为版本 ${object(run.publication).number ?? ''}` : object(run.publication).status === 'conflict' ? '版本冲突，候选与执行记录已保留' : object(run.publication).status === 'failed' ? '版本保存失败，候选与执行记录已保留' : running ? '正在基于项目版本开发' : '查看项目当前版本与本次执行记录'}</span></div>}
          {view === 'projects' ? <ProjectWorkspace projectId={selectedProject} initialRequirement={projectDraft} onSelectProject={id => { setProjectDraft(''); openProject(id) }} onOpenRun={openProjectRun} canDevelop={!!health && liveReady && !!health.sandbox.available} canExecute={!!health?.sandbox.available} environmentReason={!health ? '请先连接后端服务。' : !health.sandbox.available ? '请先启动 Docker 隔离环境。' : !liveReady ? '请先配置真实模型。' : ''} /> : view !== 'tasks' ? <><div className="detail-intro"><p>{view === 'agents' ? '这些设置只用于保留的历史工作流。新任务由主 Agent 按需创建子 Agent，不使用此处的固定角色。' : '这是原有工作区的知识资料；新任务的需求和验收标准会在对应运行中单独保存。'}</p></div><Management view={view} /></> : !selectedRun ? <div className="new-conversation">
            <div className="welcome"><div className="welcome-mark"><Icon name="forge" size={32} /></div><h1>想做些什么？</h1><p>描述需求，主 Agent 会拆分任务、安排协作，并根据执行结果调整计划。</p></div>
            <form className="composer" onSubmit={startRun}>
              <label className="sr-only" htmlFor="task-request">描述你的需求</label><textarea id="task-request" ref={input} value={draft} onChange={event => setDraft(event.target.value)} placeholder="描述想实现的功能、输入输出和需要注意的情况…" rows={3} maxLength={8000} onKeyDown={event => { if ((event.ctrlKey || event.metaKey) && event.key === 'Enter' && !event.nativeEvent.isComposing) { event.preventDefault(); event.currentTarget.form?.requestSubmit() } }} />
              <div className="composer-toolbar"><div className="composer-context"><span><Icon name="folder" size={14} />新建 Python 项目</span><span className="mode-control"><span className="mode-dot live" />真实模型 · Docker</span></div><button className="send-button" type="submit" disabled={!canSubmit} aria-label={busy ? '正在提交任务' : '开始任务'} title="开始任务 · Ctrl+Enter"><Icon name="send" size={19} /><span>{busy ? '提交中' : '开始任务'}</span></button></div>
            </form>
            <div className="composer-guidance" aria-live="polite">{!health && !loading ? <span className="warning-text">请先连接后端服务。<button className="text-button" onClick={() => void refresh()}>重新连接</button></span> : health && !health.sandbox.available ? <span className="warning-text">隔离环境尚未就绪。<button className="text-button" onClick={() => setEnvironmentOpen(true)}>查看原因</button></span> : !liveReady && !loading ? <span className="warning-text">请配置真实模型后再开始。</span> : <span>支持 Python 标准库项目；模型调用会使用平台额度。Ctrl + Enter 发送。</span>}</div>
            {!draft.trim() && <div className="suggestions" aria-label="试试这些需求">{examples.map(example => <button key={example.text} onClick={() => useExample(example)}><Icon name={example.icon} size={19} /><span>{example.text}</span><Icon name="chevron" size={14} /></button>)}</div>}
            <p className="home-footnote">代码先保存在独立副本中，你可以查看执行记录、测试结果并导出文件。</p>
          </div> : !run ? <div className="thread-skeleton skeleton" aria-label="正在读取任务"><i /><i /><i /><p>正在读取任务记录…</p></div> : autonomousRun ? <AutonomousThread run={run} elapsed={elapsed} onPanel={setPanel} onNewTask={newTask} onRevise={reviseRequirement} onProject={typeof run.project_id === 'string' ? () => { setProjectDraft(''); openProject(run.project_id as string) } : undefined} /> : <div className="conversation-thread">
            <div className="thread-topline"><span>{timestamp(run.created_at)}</span><span className={`run-mode ${run.mode}`}>{run.mode === 'mock' ? '模拟演示 · 固定输出' : '真实模型'}{run.fault_injection === true ? ' · 预设缺陷' : ''}</span></div>
            <article className="user-message"><span className="sr-only">你的需求</span><p>{userPreview}</p>{userPreview !== userMessage && <details><summary>查看完整需求</summary><pre>{userMessage}</pre></details>}</article>
            <article className="assistant-message"><div className="assistant-heading"><span className="assistant-mark"><Icon name="forge" size={21} /></span><strong>AgentForge</strong><span>{run.mode === 'mock' ? '模拟执行记录' : '协作执行记录'}</span></div>
              <p className="task-understanding">{run.workflow_id === 'review' ? '检查' : '处理'}「{activeTask?.name ?? '待办示例需求'}」，{run.workflow_id === 'review' ? '保留代码并给出审查结论。' : '在独立副本中完成修改并验证。'}</p>
              {run.mode === 'mock' && <p className="mode-explanation">这是模拟演示：预设首轮遗漏与修正输出，模型调用为 0；测试使用实际 Docker 结果。</p>}
              {typeof plan.summary === 'string' && <details className="model-plan"><summary>{run.mode === 'mock' ? '模拟规划结果' : 'Agent 任务计划'}</summary><p>{plan.summary}</p>{Array.isArray(plan.tasks) && <ul>{plan.tasks.map((item, index) => <li key={index}>{typeof item === 'string' ? item : String(object(item).title ?? object(item).description ?? object(item).task ?? printable(item))}</li>)}</ul>}</details>}
              <div className={`execution-summary ${running ? 'active' : ''}`} role="status" aria-live="polite"><span className={`task-dot ${run.status}`} /><span>{running ? currentSteps.length ? currentSteps.map(step => step.label).join(' · ') : '正在准备任务' : stateNames[run.status] ?? run.status}</span><small>{elapsed < 60 ? `${elapsed} 秒` : `${Math.floor(elapsed / 60)} 分 ${elapsed % 60} 秒`}</small></div>
              <details className="execution-steps"><summary><span>执行步骤</span><span>{steps.filter(step => step.status === 'succeeded').length} 个步骤已完成</span></summary>{steps.length ? <ol>{steps.map(step => <li key={`${step.id}-${step.attempt}`}><span className={`step-indicator ${step.status}`}><Icon name={step.status === 'succeeded' ? 'check' : 'clock'} size={13} /></span><span>{step.label}<small>{step.agent_name ?? ''}{step.attempt ? ` · 第 ${step.attempt} 轮修正` : ''}</small></span><span className="step-status">{stateNames[step.status] ?? step.status}</span></li>)}</ol> : <p className="muted">等待后端记录第一个步骤。</p>}</details>
              {running && activity.length > 0 && <p className="latest-progress">{activity.at(-1)?.message}</p>}
              {!running && summary && <section className={`task-result ${summary.tone}`}><h2>{summary.title}</h2><p>{summary.description}</p>{run.applied && <p className="success-text">修改已应用到待办示例项目。</p>}<div className="result-facts"><button onClick={() => setPanel('validation')}><Icon name="check" size={16} /><span>{test ? test.status === 'passed' ? `独立测试通过${test.testsRun != null ? ` · ${test.testsRun} 项` : ''}` : test.status === 'failed' ? '独立测试未通过' : '测试未完成' : '尚无完整测试结果'}</span></button>{changedFiles.length > 0 && <button onClick={() => setPanel('changes')}><Icon name="file" size={16} /><span>{changedFiles.length} 个文件有修改</span></button>}</div><div className="result-actions">{changedFiles.length > 0 && <button className="primary-button" onClick={() => { setPanel('changes'); setSourceMode(false) }}><Icon name="code" size={16} />查看修改</button>}{['failed', 'blocked'].includes(run.status) && <button className={changedFiles.length ? 'secondary-button' : 'primary-button'} disabled={busy} onClick={() => void resumeRun()}><Icon name="refresh" size={15} />{busy ? '正在恢复' : '恢复原任务'}</button>}<button className="secondary-button" onClick={() => setPanel('validation')}>查看验证结果</button></div>{['failed', 'blocked'].includes(run.status) && <p className="result-note">恢复会继续原需求的未完成步骤，并可能产生新的模型调用。</p>}{changedFiles.length > 0 && <p className="result-note">{activeTaskId === 'completion' ? run.applied ? '应用记录可在执行详情中查看。' : '修改仍保留在副本中，查看后可导出。' : '此任务保留固定基线，修改可导出保存。'}</p>}</section>}
              <button className="details-link" onClick={() => setPanel('activity')}><Icon name="clock" size={15} />查看完整执行记录 <span>{events.length}</span><Icon name="chevron" size={13} /></button>
              <div className="run-footnote">{typeof run.model_calls === 'number' && <span>{run.model_calls} 次模型请求</span>}{typeof run.model_tokens === 'number' && <span>{run.model_tokens.toLocaleString()} tokens{run.model_metrics_complete === false ? '（已知用量）' : ''}</span>}<span>修正 {run.attempt ?? 0} / 2 轮</span></div>
            </article>
            <div className="next-task"><div><strong>{running ? '任务会继续在后台运行' : '开始下一项需求'}</strong><p>新需求使用动态编排创建独立 Python 项目，当前历史记录会保留。</p></div><button className="secondary-button" onClick={newTask}><Icon name="plus" size={16} />新任务</button></div>
          </div>}
        </main>
        {panel && <aside ref={detailPanel} className={`detail-panel ${panel === 'graph' ? 'graph-panel' : ''} ${panel === 'graph' && graphExpanded ? 'graph-panel-expanded' : ''}`} aria-label="任务详情" role={modalPanel ? 'dialog' : undefined} aria-modal={modalPanel ? true : undefined} onKeyDown={event => {
          if (!modalPanel || event.key !== 'Tab') return
          const controls = [...event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), summary, [tabindex="0"]')].filter(element => element.getClientRects().length)
          const first = controls[0], last = controls.at(-1)
          if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
          else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
        }}><div className="detail-header"><nav aria-label="详情视图">{([{ id: 'changes', label: '修改' }, { id: 'validation', label: '验证' }, { id: 'graph', label: '协作图' }, { id: 'activity', label: '过程' }, { id: 'project', label: '项目' }] as const).filter(item => item.id !== 'graph' || autonomousRun).map(item => <button key={item.id} className={panel === item.id ? 'active' : ''} onClick={() => setPanel(item.id)} aria-current={panel === item.id ? 'page' : undefined}>{item.label}</button>)}</nav><button className="icon-button" aria-label="关闭详情" onClick={() => setPanel(null)}><Icon name="close" /></button></div><div className="detail-scroll">
          {autonomousRun && run ? panel === 'graph' ? <CollaborationGraph key={run.id} run={run} expanded={graphExpanded} onToggleExpanded={() => setGraphExpanded(value => !value)} /> : <AutonomousDetails run={run} panel={panel} files={candidateFiles} candidateLoading={candidateLoading} candidateError={candidateError} events={events.map(event => <EvidenceEvent event={event} key={event.id} />)} /> : !run ? <div className="detail-intro"><h2>新建 Python 项目</h2><p>直接描述需求，主 Agent 会生成接口约定和验收标准，再安排子 Agent 执行。</p><p className="muted">此入口支持标准库、多文件和 JSON 函数输入输出。Web 项目请从「项目」导入并准备依赖，再继续开发。每项任务独立创建，不会自动修改本机已有工程。</p></div> : <>
          {panel === 'changes' && <>{changedFiles.length ? <><div className="detail-intro"><h2>{run?.applied ? '已应用的修改' : '待检查的修改'}</h2><p>{run?.status === 'succeeded' && test?.status === 'passed' ? '当前候选的独立测试和完整流程已通过。' : '尚未通过完整流程，请结合验证记录检查。'}{run?.mode === 'mock' ? '这是模拟生成的候选。' : ''}</p></div><div className="code-toggle"><button className={!sourceMode ? 'active' : ''} onClick={() => setSourceMode(false)}>修改差异</button><button className={sourceMode ? 'active' : ''} onClick={() => setSourceMode(true)}>完整代码</button></div>{changedFiles.map(file => <section key={file.path} className="file-section"><h3><Icon name="file" size={15} />{file.path}</h3><Code value={sourceMode ? file.after : file.diff} diff={!sourceMode} /></section>)}</> : <div className="panel-empty"><Icon name="code" size={25} /><h2>暂无代码修改</h2><p>{run?.workflow_id === 'review' ? '本次任务只进行审查，原始代码保持不变。' : running ? '生成的候选修改会显示在这里。' : '从一条开发需求开始，生成的修改将保留在独立副本中。'}</p></div>}</>}
          {panel === 'validation' && <><div className="detail-intro"><h2>验证结果</h2><p>{activeTask?.name ?? '待办示例'} · 固定独立验收</p></div>{test ? <section className="test-section"><div className="section-line"><h3>Docker 独立测试</h3><span className={test.status === 'passed' ? 'success-text' : 'warning-text'}>{test.status === 'passed' ? '通过' : test.status === 'failed' ? '未通过' : '未完成'}</span></div><p>{test.testsRun != null ? `实际执行 ${test.testsRun} 项测试` : '尚无完整测试计数'}{test.failures != null ? ` · ${test.failures} 项失败` : ''}{test.errors != null ? ` · ${test.errors} 项错误` : ''}</p><p className="muted">{test.round === 0 ? '首次候选' : `第 ${test.round} 轮修正`} · {test.isolated ? '隔离执行已确认' : '尚未确认隔离执行'}</p>{test.reason && <p>{test.reason}</p>}</section> : <div className="detail-intro"><p>当前候选尚无可确认的完整测试结果。执行受阻或模型服务失败不等于代码测试失败。</p></div>}{reviews.length > 0 && <p className="detail-intro muted">以下为本轮 Agent 的审查记录；当前候选是否通过，请结合独立测试确认。</p>}{reviews.map(step => <Review key={`${step.id}-${step.attempt}`} step={step} />)}<details className="raw-evidence"><summary>测试原始记录</summary>{events.filter(event => object(event.data).tool === 'run_acceptance' || event.type === 'validation').map(event => <EvidenceEvent key={event.id} event={event} />)}</details>{activeTask && <details className="raw-evidence"><summary>固定验收范围</summary><pre>{activeTask.requirement}</pre></details>}</>}
          {panel === 'activity' && <><div className="detail-intro"><h2>完整执行记录</h2><p>后端保存的分工、工具、验证与反馈记录。</p></div>{events.map(event => <EvidenceEvent event={event} key={event.id} />)}{run && <details className="raw-evidence"><summary>配置与步骤快照</summary><pre>{printable({ model: run.model_config, task: run.task, agents: run.agent_versions, snapshots: run.snapshots })}</pre></details>}{!events.length && <p className="detail-intro muted">任务开始后，真实执行记录会显示在这里。</p>}</>}
          {panel === 'project' && <><div className="detail-intro"><h2>{run ? '本次任务的项目基线' : '已连接的项目'}</h2><p>Python 待办示例 · todo.py</p><p className="muted">当前支持完成状态、批量编辑、优先级查询、JSON 导入导出四类需求。</p></div>{diffFiles.map(file => ({ path: file.path, content: file.before })).map(file => <section className="file-section" key={file.path}><h3><Icon name="file" size={15} />{file.path}</h3><Code value={file.content} /></section>)}{run && !diffFiles.length && <p className="detail-intro muted">本次任务尚未保存可读取的项目副本。</p>}</>}
          </>}
        </div>{autonomousRun && run && (panel === 'changes' || panel === 'activity') && <div className="detail-actions">{panel === 'changes' && <button className="secondary-button" disabled={running || busy || candidateLoading || !Object.keys(candidateFiles).length} onClick={() => void exportCandidate()}><Icon name="download" size={15} />{busy ? '正在下载…' : '下载项目 ZIP'}</button>}<button className="secondary-button" onClick={() => void exportEvidence()}>导出完整记录</button><span>候选保留 · 原项目未改动</span></div>}{!autonomousRun && panel === 'changes' && changedFiles.length > 0 && <div className="detail-actions"><button className="secondary-button" disabled={running} onClick={exportCandidate}><Icon name="download" size={15} />导出代码</button>{canApply && <button className="primary-button" onClick={() => setConfirmApply(true)}>应用到示例</button>}<span>{run?.applied ? '已应用' : activeTaskId === 'completion' ? '应用前需确认' : '基线保留 · 仅导出'}</span></div>}</aside>}
      </div>
    </div>
    <dialog ref={applyDialog} className="confirm-dialog" inert={modalPanelOpen} onCancel={event => { if (busy) event.preventDefault(); else setConfirmApply(false) }} onClose={() => setConfirmApply(false)} aria-labelledby="apply-title"><h2 id="apply-title">应用到待办示例？</h2><p>这会将已验证的候选写入目标文件，之后新任务会读取修改后的示例。</p><code>examples/todo/todo.py</code><p className="muted">若目标基线或候选已变化，系统会拒绝应用。</p><div className="dialog-actions"><button autoFocus className="secondary-button" disabled={busy} onClick={() => setConfirmApply(false)}>先不应用</button><button className="primary-button" disabled={busy || !canApply} onClick={() => void applyRun()}>{busy ? '正在应用…' : '确认应用修改'}</button></div></dialog>
  </div>
}
