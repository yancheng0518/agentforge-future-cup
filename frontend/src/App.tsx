import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { api, post } from './api'
import type { Agent, Event, Health, Knowledge, ProjectFile, Run, Step, TaskDefinition, Workflow } from './types'

type View = 'workbench' | 'agents' | 'knowledge'
type LogTab = 'events' | 'tests' | 'review' | 'snapshots'
const labels: Record<string, string> = {
  queued: '排队中', pending: '待执行', running: '执行中', blocked: '执行受阻', failed: '失败',
  needs_human: '待人工处理', succeeded: '已通过', passed: '通过', completed: '已完成', skipped: '已跳过',
  schedule: '调度', tool: '工具', agent: 'Agent', validation: '验证', feedback: '反馈',
  snapshot: '快照', error: '错误', status: '状态', planning: '规划', developer: '开发', reviewer: '审查',
}
const roleLabels: Record<string, string> = { planner: '规划', developer: '开发', tester: '测试', reviewer: '审查', fixer: '修正' }
const matchesNode = (id: string | undefined, node: string) => !!id && (id === node || id.split('/').includes(node))
const format = (value: unknown) => typeof value === 'string' ? value : JSON.stringify(value, null, 2)
const date = (value: string | undefined, long = false) => value ? new Intl.DateTimeFormat('zh-CN', {
  month: long ? '2-digit' : undefined, day: long ? '2-digit' : undefined,
  hour: '2-digit', minute: '2-digit', second: long ? undefined : '2-digit', hour12: false,
}).format(new Date(value)) : '—'

function Icon({ name, size = 18 }: { name: string; size?: number }) {
  const paths: Record<string, ReactNode> = {
    forge: <><path d="M4 7h16l-3 4H7zM9 11v5l-3 4h12l-3-4v-5M9 3h6" /></>,
    layers: <><path d="m12 3 9 5-9 5-9-5 9-5ZM3 12l9 5 9-5M3 16l9 5 9-5" /></>,
    play: <path d="m8 5 11 7-11 7V5Z" />,
    branch: <><circle cx="6" cy="5" r="2" /><circle cx="6" cy="19" r="2" /><circle cx="18" cy="6" r="2" /><path d="M6 7v10M6 14h5a7 7 0 0 0 7-6" /></>,
    code: <><path d="m8 6-6 6 6 6m8-12 6 6-6 6m-3-14-2 16" /></>,
    agents: <><circle cx="9" cy="8" r="3" /><path d="M3 21v-3a6 6 0 0 1 12 0v3m2-16a3 3 0 0 1 0 6m1 4a5 5 0 0 1 3 5" /></>,
    book: <><path d="M3 4h7l2 2 2-2h7v16h-7l-2 1-2-1H3V4Zm9 2v15" /></>,
    file: <><path d="M5 3h9l5 5v13H5V3Zm9 0v6h5M8 13h8M8 17h5" /></>,
    folder: <path d="M3 6h7l2 3h9v11H3V6Z" />,
    terminal: <><path d="m4 6 5 6-5 6m9 0h7" /></>,
    refresh: <><path d="M20 8a8 8 0 1 0 0 8m0-13v5h-5" /></>,
    plus: <path d="M12 5v14M5 12h14" />,
    arrow: <path d="M4 12h16m-6-6 6 6-6 6" />,
    check: <path d="m5 12 4 4L19 6" />,
    alert: <><path d="m12 3 10 18H2L12 3Zm0 6v5m0 3h.01" /></>,
    clock: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>,
    settings: <><path d="M4 7h16M4 17h16" /><circle cx="9" cy="7" r="3" /><circle cx="16" cy="17" r="3" /></>,
    search: <><circle cx="10" cy="10" r="6" /><path d="m15 15 6 6" /></>,
  }
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] || paths.file}</svg>
}

function Status({ value, text }: { value: string; text?: string }) { return <span className={`status status-${value}`}><span className="status-dot" />{text ?? labels[value] ?? value}</span> }
function Empty({ icon = 'terminal', title, children }: { icon?: string; title: string; children: ReactNode }) {
  return <div className="empty"><span className="empty-icon"><Icon name={icon} size={26} /></span><strong>{title}</strong><p>{children}</p></div>
}
function Code({ value, diff = false }: { value: string; diff?: boolean }) {
  return <div className="code-scroll" tabIndex={0} aria-label={diff ? '候选代码差异' : '代码文件'}><pre className="code">{value.split('\n').map((line, i) => <span key={i} className={`code-line ${diff ? line.startsWith('+') ? 'addition' : line.startsWith('-') ? 'deletion' : line.startsWith('@@') ? 'hunk' : '' : ''}`}><span className="line-number" aria-hidden="true">{i + 1}</span><code>{line || ' '}</code></span>)}</pre></div>
}

export default function App() {
  const [view, setView] = useState<View>('workbench')
  const [health, setHealth] = useState<Health | null>(null)
  const [agents, setAgents] = useState<Agent[]>([])
  const [workflows, setWorkflows] = useState<Workflow[]>([])
  const [tasks, setTasks] = useState<TaskDefinition[]>([])
  const [taskId, setTaskId] = useState('completion')
  const [projectVersion, setProjectVersion] = useState(0)
  const [projectLoading, setProjectLoading] = useState(true)
  const [files, setFiles] = useState<ProjectFile[]>([])
  const [knowledge, setKnowledge] = useState<Knowledge[]>([])
  const [runs, setRuns] = useState<Run[]>([])
  const [selectedRun, setSelectedRun] = useState<string | null>(null)
  const [pollVersion, setPollVersion] = useState(0)
  const [run, setRun] = useState<Run | null>(null)
  const [workflowId, setWorkflowId] = useState('development')
  const [requirement, setRequirement] = useState('增加任务完成状态，并支持按照完成状态筛选任务。')
  const [mode, setMode] = useState<'mock' | 'live'>('mock')
  const [selectedFile, setSelectedFile] = useState('todo.py')
  const [codeTab, setCodeTab] = useState<'diff' | 'source'>('source')
  const [logTab, setLogTab] = useState<LogTab>('events')
  const [selectedNode, setSelectedNode] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [confirmApply, setConfirmApply] = useState(false)
  const [query, setQuery] = useState('')
  const [searching, setSearching] = useState(false)
  const [agentEdit, setAgentEdit] = useState<Agent | null>(null)
  const [newAgent, setNewAgent] = useState(false)
  const alive = useRef(true)
  const catalogInitialized = useRef(false)
  const applyDialog = useRef<HTMLDialogElement>(null)

  useEffect(() => {
    if (confirmApply && applyDialog.current && !applyDialog.current.open) applyDialog.current.showModal()
    if (!confirmApply && applyDialog.current?.open) applyDialog.current.close()
  }, [confirmApply])

  const refresh = useCallback(async () => {
    setError('')
    try {
      const [h, a, w, t, k, r] = await Promise.all([
        api<Health>('/health'), api<Agent[]>('/agents'), api<Workflow[]>('/workflows'),
        api<TaskDefinition[]>('/tasks'), api<{ matches: Knowledge[] }>('/knowledge'), api<Run[]>('/runs'),
      ])
      if (!alive.current) return
      setHealth(h); setAgents(a); setWorkflows(w); setTasks(t); setKnowledge(k.matches); setRuns(r)
      if (!catalogInitialized.current) {
        const initial = t.find(task => task.id === 'completion')
        if (initial) setRequirement(initial.requirement)
        catalogInitialized.current = true
      }
      setProjectVersion(version => version + 1)
    } catch (e) { if (alive.current) setError(e instanceof Error ? e.message : String(e)) }
    finally { if (alive.current) setLoading(false) }
  }, [])

  useEffect(() => { alive.current = true; void refresh(); return () => { alive.current = false } }, [refresh])
  useEffect(() => {
    if (loading) return
    let cancelled = false
    const controller = new AbortController()
    setFiles([]); setProjectLoading(true)
    void api<{ files: ProjectFile[] }>(`/project?task_id=${encodeURIComponent(taskId)}`, { signal: controller.signal })
      .then(project => {
        if (cancelled) return
        setFiles(project.files)
        setSelectedFile(current => project.files.some(file => file.path === current) ? current : project.files[0]?.path ?? '')
      })
      .catch(e => { if (!cancelled) setError(e instanceof Error ? e.message : String(e)) })
      .finally(() => { if (!cancelled) setProjectLoading(false) })
    return () => { cancelled = true; controller.abort() }
  }, [taskId, projectVersion, loading])
  useEffect(() => {
    if (!selectedRun) { setRun(null); return }
    let cancelled = false
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        const value = await api<Run>(`/runs/${selectedRun}`)
        if (cancelled) return
        setRun(value)
        setRuns(previous => [value, ...previous.filter(r => r.id !== value.id)].sort((a, b) => b.created_at.localeCompare(a.created_at)))
        if (['queued', 'running'].includes(value.status)) timer = setTimeout(poll, 1000)
      } catch (e) { if (!cancelled) { setError(e instanceof Error ? e.message : String(e)); timer = setTimeout(poll, 4000) } }
    }
    setRun(previous => previous?.id === selectedRun ? previous : null)
    void poll()
    return () => { cancelled = true; clearTimeout(timer) }
  }, [selectedRun, pollVersion])

  const workflow = run?.workflow ?? workflows.find(w => w.id === (run?.workflow_id ?? workflowId))
  const diffFiles = run?.diff?.files ?? []
  const diffFile = diffFiles.find(f => f.path === selectedFile)
  // Historical code always comes from the run's frozen baseline, never from
  // the currently selected task or a refreshed target project.
  const visibleFiles = run ? diffFiles.map(file => ({ path: file.path, content: file.before })) : selectedRun ? [] : files
  const currentFile = visibleFiles.find(file => file.path === selectedFile)
  const activeTaskId = run ? run.task_id ?? 'completion' : selectedRun ? runs.find(item => item.id === selectedRun)?.task_id ?? 'completion' : taskId
  const taskName = (id: string) => tasks.find(task => task.id === id)?.name ?? (id === 'completion' ? '完成状态与筛选' : id)
  const activeTask = run?.task ?? tasks.find(task => task.id === activeTaskId)
  const activeTaskName = activeTask?.name ?? taskName(activeTaskId)
  const canApply = run?.status === 'succeeded' && run.workflow_id === 'development' && !run.applied && activeTaskId === 'completion'
  const candidateOrigin = run?.mode === 'mock' ? '候选来源：固定模拟输出 · 预设遗漏'
    : run?.fault_injection === true ? '候选来源：已标注的预设缺陷'
      : run?.candidate_origin === 'live_model_development' && run.fault_injection === false ? '候选来源：真实模型开发 · 未注入缺陷'
        : '候选来源：历史记录未单独标注'
  const events = run?.events ?? []
  const steps = run?.steps ?? []
  const running = !!run && ['running', 'queued'].includes(run.status)
  const nodeSteps = selectedNode ? steps.filter(s => s.node_id === selectedNode || matchesNode(s.id, selectedNode)) : []
  const selectedStep = nodeSteps.at(-1)
  const filteredEvents = events.filter(e => !selectedNode || matchesNode(e.node_id, selectedNode))
  const testEvents = events.filter(e => e.node_id?.includes('test') || e.type === 'validation')
  const reviewSteps = steps.filter(s => s.node_id === 'review' || s.node_id === 'inspect' || matchesNode(s.id, 'review') || matchesNode(s.id, 'inspect'))

  async function startRun(e: FormEvent) {
    e.preventDefault(); setBusy(true); setError(''); setNotice(''); setSelectedNode(null)
    try {
      const next = await post<Run>('/runs', { requirement, workflow_id: workflowId, mode, task_id: taskId })
      setSelectedRun(next.id); setRun(next); setCodeTab(workflowId === 'development' ? 'diff' : 'source'); setLogTab('events')
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) } finally { setBusy(false) }
  }
  async function resumeRun() {
    if (!run) return
    setBusy(true); setError('')
    try {
      await post<Run>(`/runs/${run.id}/resume`)
      // A resume response is only a summary. Keep existing evidence until the
      // detail fetch returns, and explicitly restart terminal-state polling.
      setRun(await api<Run>(`/runs/${run.id}`))
      setPollVersion(version => version + 1)
      await refresh()
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) } finally { setBusy(false) }
  }
  async function applyRun() {
    if (!run || !canApply) return
    setBusy(true); setError('')
    try {
      await post(`/runs/${run.id}/apply`, { confirmed: true })
      setConfirmApply(false); setNotice('候选修改已应用到示例目标项目。'); setRun(await api<Run>(`/runs/${run.id}`)); await refresh()
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) } finally { setBusy(false) }
  }
  function selectTask(id: string) {
    const task = tasks.find(item => item.id === id)
    if (!task) return
    setTaskId(id); setRequirement(task.requirement); setSelectedRun(null); setRun(null)
    setSelectedNode(null); setConfirmApply(false); setCodeTab('source'); setSelectedFile('todo.py')
    setError(''); setNotice('')
    if (id !== 'completion') setMode('live')
  }
  function newTask() {
    const id = run ? run.task_id ?? 'completion' : taskId
    selectTask(id)
  }
  function exportCandidate() {
    if (!run || !diffFile) return
    const url = URL.createObjectURL(new Blob([diffFile.after], { type: 'text/x-python;charset=utf-8' }))
    const link = document.createElement('a')
    link.href = url; link.download = `${activeTaskId}-${run.id.slice(0, 8)}-todo.py`
    link.click()
    setTimeout(() => URL.revokeObjectURL(url), 0)
  }
  async function saveAgent(e: FormEvent) {
    e.preventDefault(); if (!agentEdit) return
    setBusy(true); setError('')
    try {
      const payload = { id: agentEdit.id, name: agentEdit.name, role: agentEdit.role, capabilities: agentEdit.capabilities.filter(Boolean), enabled: agentEdit.enabled, prompt: agentEdit.prompt, model: agentEdit.model || 'default', tools: agentEdit.tools.filter(Boolean) }
      const saved = await api<Agent>(newAgent ? '/agents' : `/agents/${agentEdit.id}`, { method: newAgent ? 'POST' : 'PUT', body: JSON.stringify(payload) })
      setAgents(await api<Agent[]>('/agents')); setAgentEdit(saved); setNewAgent(false); setNotice(`已保存 ${saved.name} · v${saved.version}`)
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) } finally { setBusy(false) }
  }
  async function searchKnowledge(e: FormEvent) {
    e.preventDefault(); setSearching(true); setError('')
    try { setKnowledge((await api<{ matches: Knowledge[] }>(`/knowledge?q=${encodeURIComponent(query)}`)).matches) }
    catch (e) { setError(e instanceof Error ? e.message : String(e)) } finally { setSearching(false) }
  }
  const navigate = (next: View) => { setView(next); setError(''); setNotice('') }

  return <div className="app-shell">
    <header className="topbar">
      <a className="brand" href="#" onClick={e => { e.preventDefault(); navigate('workbench') }}><span className="brand-mark"><Icon name="forge" size={23} /></span><span>AgentForge<span className="brand-sub">开发工作台</span></span></a>
      <nav className="main-nav" aria-label="主导航">
        {([{ id: 'workbench', text: '工作台', icon: 'layers' }, { id: 'agents', text: 'Agents', icon: 'agents' }, { id: 'knowledge', text: '知识资料', icon: 'book' }] as const).map(item => <button key={item.id} className={view === item.id ? 'active' : ''} aria-current={view === item.id ? 'page' : undefined} onClick={() => navigate(item.id)}><Icon name={item.icon} />{item.text}</button>)}
      </nav>
      <div className="topbar-end"><span className="local-badge">本地工作区</span><button className="icon-button" title="刷新环境和配置" aria-label="刷新环境和配置" onClick={() => void refresh()} disabled={busy}><Icon name="refresh" /></button></div>
    </header>

    <div className="workspace-body">
      <aside className="sidebar">
        <div className="project-selector"><span className="project-monogram">Py</span><div><strong>Todo 示例项目</strong><span>{activeTaskName}</span></div></div>
        <section className="side-section">
          <h2>{selectedRun ? '运行基线' : '项目文件'} <span>{visibleFiles.length || '—'}</span></h2>
          <div className="folder-row"><Icon name="folder" size={15} /><span>todo /</span></div>
          {loading || (!selectedRun && projectLoading) ? <div className="skeleton-list" aria-label="正在读取项目"><i /><i /><i /></div> : visibleFiles.map(file => <button className={`file-button ${selectedFile === file.path ? 'selected' : ''}`} key={file.path} onClick={() => { setSelectedFile(file.path); setCodeTab('source'); navigate('workbench') }}><Icon name="file" size={15} /><span>{file.path}</span></button>)}
          {!loading && !projectLoading && visibleFiles.length === 0 && <p className="side-empty">{selectedRun ? '等待本次运行的基线副本。' : '连接后端后读取示例项目。'}</p>}
        </section>
        <section className="side-section templates-section"><h2>工作流模板</h2>
          {workflows.map(w => <button key={w.id} className={`template-button ${workflowId === w.id ? 'selected' : ''}`} onClick={() => { setWorkflowId(w.id); newTask(); navigate('workbench') }}><Icon name={w.id === 'development' ? 'branch' : 'code'} size={16} /><span>{w.name}</span>{workflowId === w.id && <span className="selection-dot" />}</button>)}
        </section>
        <section className="side-section history-section"><h2>运行记录 <span>{runs.length}</span></h2>
          {!runs.length && <p className="side-empty">启动任务后，执行记录会保存在这里。</p>}
          <div className="history-list">{runs.map(r => <button key={r.id} className={`history-button ${selectedRun === r.id ? 'selected' : ''}`} title={r.requirement} onClick={() => { setSelectedRun(r.id); setRun(previous => previous?.id === r.id ? previous : null); setSelectedNode(null); setConfirmApply(false); setSelectedFile('todo.py'); navigate('workbench') }}><div><span className={`history-dot ${r.status}`} /><span className="history-title">{r.task?.name ?? taskName(r.task_id ?? 'completion')}</span></div><small>{date(r.created_at, true)}<span>{r.mode === 'mock' ? '模拟' : '真实模型'} · {labels[r.status] ?? r.status}</span></small></button>)}</div>
        </section>
        <div className="sidebar-foot"><span className="version">预览版 0.1</span><span>配置 · 执行 · 验证</span></div>
      </aside>

      <main className="main-content">
        {error && <div className="message error-message" role="alert"><Icon name="alert" /><span>{error}</span><button onClick={() => setError('')} aria-label="关闭错误提示">×</button></div>}
        {notice && <div className="message success-message" role="status"><Icon name="check" /><span>{notice}</span><button onClick={() => setNotice('')} aria-label="关闭通知">×</button></div>}
        <div className="environment-strip">
          <span className="environment-label">运行环境</span>
          <span className={`environment-item ${health ? 'ready' : 'unknown'}`}><span />{health ? '后端已连接' : loading ? '连接中' : '后端未连接'}</span>
          <span className={`environment-item ${health?.model.configured && health.model.verified === true ? 'ready' : 'warning'}`} title={`模型 ID：${health?.model.model || '未设置'}\n服务地址：${health?.model.base_url || '未设置'}\n模型调用验证不代表开发闭环已通过验收。`}><span />{health?.model.configured ? health.model.verified === true ? '模型调用已验证' : '模型已配置 · 待验证' : '真实模型未配置'}{health?.model.model && <code className="environment-model">{health.model.model}</code>}</span>
          <span className={`environment-item ${health?.sandbox.available ? 'ready' : 'warning'}`} title={health?.sandbox.reason}><span />{health?.sandbox.available ? '隔离测试可用' : '隔离测试不可用'}</span>
        </div>

        {view === 'workbench' && <>
          <section className="task-section">
            <div className="section-heading"><div><div className="breadcrumb">{activeTaskName} <span>/</span> {workflow?.name ?? '软件需求实现'}</div><h1>{run ? '任务执行记录' : '开始一次协作开发'}</h1></div>{run && <div className="run-heading-state"><span className="mono">#{run.id.slice(0, 8)}</span><Status value={run.status} /></div>}</div>
            {!run && !selectedRun ? <form className="composer" onSubmit={startRun}>
              <label className="sr-only" htmlFor="requirement">开发需求或审查说明</label>
              <textarea id="requirement" value={requirement} onChange={e => setRequirement(e.target.value)} rows={taskId === 'completion' ? 3 : 5} maxLength={8000} placeholder="选择开发任务，或填写需要审查的要求……" required />
              <div className="composer-bottom"><div className="composer-options"><label><Icon name="book" size={14} /><select aria-label="开发任务" value={taskId} onChange={e => selectTask(e.target.value)} disabled={!tasks.length || busy}>{tasks.map(task => <option value={task.id} key={task.id}>{task.name}</option>)}</select></label><label><Icon name="branch" size={14} /><select aria-label="工作流模板" value={workflowId} onChange={e => setWorkflowId(e.target.value)}>{workflows.map(w => <option value={w.id} key={w.id}>{w.name}</option>)}</select></label><label><span className={`mode-dot ${mode}`} /><select aria-label="模型运行模式" value={mode} onChange={e => setMode(e.target.value as 'mock' | 'live')}><option value="mock" disabled={taskId !== 'completion'}>模拟模型{taskId !== 'completion' ? ' · 此任务不支持' : ''}</option><option value="live" disabled={!health?.model.configured}>真实模型{!health?.model.configured ? ' · 未配置' : ''}</option></select></label></div><button className="primary-button" disabled={busy || !health || !tasks.length || projectLoading || !requirement.trim() || (taskId !== 'completion' && mode !== 'live') || (mode === 'live' && !health.model.configured)}><Icon name="play" size={15} />{busy ? '正在启动' : '启动工作流'}</button></div>
            </form> : selectedRun && !run ? <div className="run-loading skeleton-list" aria-label="正在加载运行记录"><i /><i /></div> : <div className="run-task"><p>{activeTaskName}</p>{run && run.requirement.length > 240 ? <details className="snapshot"><summary>完整运行需求</summary><pre>{run.requirement}</pre></details> : <p>{run?.requirement}</p>}<div className="run-meta"><span className={`mode-label ${run?.mode}`}>{run?.mode === 'mock' ? '模拟模型 · 固定演示输出' : '真实模型运行'}</span><span>默认模型：{run?.mode === 'mock' ? '固定模拟适配器' : run?.model_config?.model ?? '旧记录未保存型号'}</span><span>修正轮次 {run?.attempt ?? 0} / 2</span><span>启动于 {date(run?.created_at, true)}</span><button className="text-button" onClick={newTask}><Icon name="plus" size={14} />新任务</button></div><p className="task-note">{candidateOrigin}</p></div>}
            <p className="task-note">{(run?.mode ?? mode) === 'mock' ? '模拟模式使用固定模型输出，并预设首轮遗漏以演示反馈；文件与调度记录真实生成，测试仅在容器可用时执行。' : activeTaskId !== 'completion' ? '此评测任务仅使用真实模型与独立容器验收。基线保持冻结，候选保留并可导出。' : '任务调用服务端配置的云端模型；候选代码只写入示例副本，独立验收通过后才能确认应用。'}</p>
            {run?.status === 'blocked' && <div className="blocker"><Icon name="alert" /><div><strong>执行受阻，尚未通过验收</strong><p>{run.summary || health?.sandbox.reason || '运行前置条件未满足，请查看事件记录。'}</p></div><button className="secondary-button" onClick={() => void resumeRun()} disabled={busy}>检查并恢复</button></div>}
            {run && ['failed', 'needs_human'].includes(run.status) && <div className="blocker"><Icon name="alert" /><div><strong>{labels[run.status]}</strong><p>{run.summary || '请查看日志与验证结果，修正后可恢复未完成节点。'}</p></div>{run.status === 'failed' ? <button className="secondary-button" onClick={() => void resumeRun()} disabled={busy}>重试未完成节点</button> : <button className="secondary-button" onClick={newTask}>新建任务</button>}</div>}
          </section>

          <section className="workflow-section" aria-label="工作流">
            <div className="panel-heading"><h2><Icon name="branch" size={16} />工作流</h2><span>按依赖执行 · 验证可并行 · 最多修正两轮</span></div>
            <div className="workflow-track">{(workflow?.nodes ?? []).map((node, index) => {
              const matching = steps.filter(s => s.node_id === node.id || s.id === node.id || s.id.endsWith(`/${node.id}`))
              const step = matching.at(-1)
              const nodeStatus = step?.status ?? (run?.status === 'succeeded' && node.conditional ? 'skipped' : 'pending')
              return <div className="workflow-node-wrap" key={node.id}>{index > 0 && <span className="workflow-connector" aria-hidden="true">→</span>}<button className={`workflow-node ${selectedNode === node.id ? 'selected' : ''} ${nodeStatus}`} onClick={() => { setSelectedNode(selectedNode === node.id ? null : node.id); setLogTab('events') }}><span className="node-top"><span className="node-icon"><Icon name={node.kind === 'subworkflow' ? 'layers' : node.capability?.includes('test') ? 'terminal' : node.capability?.includes('review') ? 'code' : 'branch'} size={15} /></span><span>{node.label}</span>{nodeStatus === 'succeeded' && <Icon name="check" size={14} />}</span><span className="node-bottom">{step?.agent_name ?? (node.kind === 'subworkflow' ? '验证子工作流' : node.capability ?? node.kind ?? '等待调度')}<span className={`node-state ${nodeStatus}`}>{nodeStatus === 'succeeded' ? '已完成' : labels[nodeStatus] ?? nodeStatus}</span></span></button></div>
            })}</div>
            {workflow?.nodes.some(n => n.children?.length) && <div className="validation-branch"><span className="branch-label">验证子工作流</span>{workflow.nodes.flatMap(n => n.children ?? []).map(node => {
              const step = steps.filter(s => s.node_id === node.id || s.id.endsWith(`/${node.id}`)).at(-1)
              return <button key={node.id} className={selectedNode === node.id ? 'selected' : ''} onClick={() => { setSelectedNode(selectedNode === node.id ? null : node.id); setLogTab(node.id === 'test' ? 'tests' : 'review') }}><Icon name={node.id === 'test' ? 'terminal' : 'code'} size={13} /><span>{node.label}</span><small>{node.depends_on?.length ? '等待测试与初审' : '并行'}</small>{step && <Status value={step.status} text={step.status === 'succeeded' ? '步骤已完成' : undefined} />}</button>
            })}</div>}
            {selectedNode && <div className="node-detail"><span>{selectedStep ? `${selectedStep.label} · ${selectedStep.agent_name ?? selectedStep.agent_id ?? '系统'}` : `节点 ${selectedNode} 尚未执行`}</span><button className="text-button" onClick={() => setSelectedNode(null)}>显示全部事件</button></div>}
          </section>

          <div className="work-panels">
            <section className="editor-panel">
              <div className="editor-toolbar"><div className="tabs" role="tablist" aria-label="代码视图"><button role="tab" aria-selected={codeTab === 'source'} className={codeTab === 'source' ? 'active' : ''} onClick={() => setCodeTab('source')}><Icon name="file" size={14} />{selectedRun ? '运行基线' : '项目文件'}</button><button role="tab" aria-selected={codeTab === 'diff'} className={codeTab === 'diff' ? 'active' : ''} onClick={() => setCodeTab('diff')}><Icon name="code" size={14} />候选差异{diffFiles.length > 0 && <span className="count">{diffFiles.length}</span>}</button></div><div className="composer-options">{run && diffFile && !running && <button className="text-button" onClick={exportCandidate}>导出候选</button>}{canApply && <button className="text-button apply-button" onClick={() => setConfirmApply(true)}>确认应用 <Icon name="arrow" size={13} /></button>}</div></div>
              <div className="file-path"><span><Icon name="file" size={13} />{selectedFile || '选择文件'}</span><span>{codeTab === 'diff' ? activeTaskId === 'completion' ? '候选副本 → 目标项目' : '候选副本 · 仅导出' : selectedRun ? '本次运行冻结基线' : '任务基线 · 只读'}</span></div>
              {codeTab === 'source' ? currentFile ? <Code value={currentFile.content} /> : <Empty icon="folder" title={selectedRun ? '等待运行基线' : '等待读取任务基线'}>{selectedRun ? '只展示本次运行保存的基线副本。' : '选择任务后，对应基线文件会显示在这里。'}</Empty> : diffFile?.diff ? <Code value={diffFile.diff} diff /> : run?.diff?.unified_diff ? <Code value={run.diff.unified_diff} diff /> : <Empty icon="code" title={running ? '正在准备候选修改' : '还没有候选差异'}>{run?.workflow_id === 'review' ? '代码审查模板只读取项目，不生成修改。' : '开发 Agent 生成候选后，可以在此查看每一处修改。'}</Empty>}
              <div className="panel-footer"><span>{codeTab === 'diff' ? '差异由真实文件生成' : 'Python'}</span><span>UTF-8</span></div>
            </section>

            <section className="execution-panel">
              <div className="editor-toolbar"><div className="tabs log-tabs" role="tablist" aria-label="运行视图">{([{ id: 'events', label: '执行事件' }, { id: 'tests', label: '测试' }, { id: 'review', label: '审查' }, { id: 'snapshots', label: '快照' }] as const).map(t => <button role="tab" key={t.id} aria-selected={logTab === t.id} className={logTab === t.id ? 'active' : ''} onClick={() => setLogTab(t.id)}>{t.label}{t.id === 'events' && events.length > 0 && <span className="count">{events.length}</span>}</button>)}</div>{running && <span className="live-indicator">实时</span>}</div>
              <div className="execution-scroll" tabIndex={0}>
                {logTab === 'events' && (filteredEvents.length ? <div className="event-list">{filteredEvents.map(e => <EventRow key={e.id} event={e} />)}</div> : <Empty title={selectedNode ? '此节点暂无事件' : '执行记录从这里开始'}>{selectedNode ? '节点运行后会显示调度、工具与输出记录。' : '启动工作流后，查看 Agent 分配依据、真实工具调用和反馈过程。'}</Empty>)}
                {logTab === 'tests' && <><div className={`verification-note ${health?.sandbox.available ? '' : 'warning'}`}><Icon name="terminal" size={17} /><div><strong>{health?.sandbox.available ? '独立验收 · 容器执行' : '隔离环境尚不可用'}</strong><p>{health?.sandbox.available ? '验收测试保存在候选修改范围之外。以下仅展示后端记录的实际结果。' : health?.sandbox.reason || '不会在宿主机执行候选代码，也不会将模拟结果标为测试通过。'}</p></div></div>{testEvents.length ? testEvents.map(e => <EventRow key={e.id} event={e} expanded />) : <Empty title="尚无测试结果">测试执行后会保留退出码、标准输出与错误日志。</Empty>}</>}
                {logTab === 'review' && (reviewSteps.length ? reviewSteps.map(step => <ReviewStep key={`${step.id}-${step.attempt}`} step={step} />) : <Empty icon="code" title="等待审查结果">审查 Agent 将对照需求、项目代码和工具结果给出意见。</Empty>)}
                {logTab === 'snapshots' && <>{run?.task && <details className="snapshot"><summary><Icon name="book" size={15} />本次任务与独立验收版本</summary><pre>{format(run.task)}</pre></details>}{run?.model_config && <details className="snapshot"><summary><Icon name="settings" size={15} />本次模型配置</summary><pre>{format(run.model_config)}</pre></details>}{run?.agent_versions != null && <details className="snapshot"><summary><Icon name="agents" size={15} />本次运行的 Agent 配置版本</summary><pre>{format(run.agent_versions)}</pre></details>}{run?.snapshots?.length ? run.snapshots.map((snapshot, index) => <details className="snapshot" key={index}><summary><Icon name="clock" size={15} />步骤快照 {index + 1}</summary><pre>{format(snapshot)}</pre></details>) : <Empty icon="clock" title="尚未保存步骤快照">已完成步骤会持久保存；恢复执行会继续未完成节点。</Empty>}</>}
              </div>
              <div className="panel-footer"><span>{run ? `${events.length} 条事件 · ${steps.length} 个步骤记录` : '等待启动任务'}</span><span>{run?.mode === 'mock' ? '模拟模型' : run ? '云端模型' : 'SQLite 持久化'}</span></div>
            </section>
          </div>
        </>}

        {view === 'agents' && <section className="management-page">
          <div className="section-heading"><div><div className="breadcrumb">工作区配置 <span>/</span> 注册与发现</div><h1>Agent 管理</h1><p>配置能力与工具；每次保存创建新版本，历史运行保留原配置。</p></div><button className="primary-button" onClick={() => { setNewAgent(true); setAgentEdit({ id: `agent-${Date.now().toString(36)}`, name: '', role: 'developer', capabilities: ['develop'], enabled: true, version: 1, prompt: '', model: '', tools: ['read_project', 'knowledge_search', 'candidate_diff'] }) }}><Icon name="plus" size={15} />新增 Agent</button></div>
          <div className="agent-layout"><div className="agent-list">{agents.map(agent => <button key={agent.id} className={`agent-row ${agentEdit?.id === agent.id && !newAgent ? 'selected' : ''}`} onClick={() => { setNewAgent(false); setAgentEdit({ ...agent }) }}><span className="agent-avatar">{(roleLabels[agent.role] || agent.name).slice(0, 1)}</span><span className="agent-info"><strong>{agent.name}<span>v{agent.version}</span></strong><small>{agent.capabilities.join(' · ')}</small></span><span className={`agent-enabled ${agent.enabled ? 'yes' : ''}`}>{agent.enabled ? '启用' : '停用'}</span></button>)}</div>
            {agentEdit ? <form className="agent-form" onSubmit={saveAgent}><div className="form-title"><h2>{newAgent ? '注册 Agent' : agentEdit.name}</h2><label className="checkbox-label"><input type="checkbox" checked={agentEdit.enabled} onChange={e => setAgentEdit({ ...agentEdit, enabled: e.target.checked })} />允许调度</label></div><label>Agent ID <span>字母、数字、下划线或连字符；保存后不可更改</span><input required pattern="[a-zA-Z0-9_-]{1,60}" disabled={!newAgent} value={agentEdit.id} onChange={e => setAgentEdit({ ...agentEdit, id: e.target.value })} /></label><div className="form-grid"><label>名称<input required value={agentEdit.name} onChange={e => setAgentEdit({ ...agentEdit, name: e.target.value })} /></label><label>角色<select value={agentEdit.role} onChange={e => setAgentEdit({ ...agentEdit, role: e.target.value })}>{Object.entries(roleLabels).map(([key, value]) => <option key={key} value={key}>{value} · {key}</option>)}</select></label></div><label>能力标签 <span>可选 plan, develop, test, review, fix；英文逗号分隔</span><input required value={agentEdit.capabilities.join(', ')} onChange={e => setAgentEdit({ ...agentEdit, capabilities: e.target.value.split(',').map(v => v.trim()) })} /></label><label>模型 <span>留空时使用服务端默认模型</span><input value={agentEdit.model ?? ''} onChange={e => setAgentEdit({ ...agentEdit, model: e.target.value })} placeholder="服务端默认模型" /></label><label>允许使用的工具 <span>read_project, knowledge_search, candidate_diff, run_acceptance</span><input value={agentEdit.tools.join(', ')} onChange={e => setAgentEdit({ ...agentEdit, tools: e.target.value.split(',').map(v => v.trim()).filter(Boolean) })} /></label><label>角色提示词<textarea value={agentEdit.prompt} onChange={e => setAgentEdit({ ...agentEdit, prompt: e.target.value })} rows={7} /></label><div className="form-actions"><span>{newAgent ? '初始版本 v1' : `保存为新版本 v${agentEdit.version + 1}`}</span><button className="primary-button" disabled={busy || !agentEdit.name.trim()}>{busy ? '保存中' : '保存配置版本'}</button></div></form> : <div className="agent-form"><Empty icon="agents" title="选择一个 Agent">查看角色提示词、能力标签、工具权限与配置版本。</Empty></div>}
          </div>
        </section>}

        {view === 'knowledge' && <section className="management-page knowledge-page"><div className="section-heading"><div><div className="breadcrumb">项目上下文 <span>/</span> 本地文本检索</div><h1>知识资料</h1><p>需求、开发规范与测试说明，为 Agent 提供可追溯的文件引用。</p></div><span className="local-badge">文本检索 · 非向量 RAG</span></div><form className="search-form" onSubmit={searchKnowledge}><Icon name="search" /><label className="sr-only" htmlFor="knowledge-query">检索知识资料</label><input id="knowledge-query" value={query} onChange={e => setQuery(e.target.value)} placeholder="检索需求、接口约定或测试说明" /><button className="secondary-button" disabled={searching}>{searching ? '检索中' : '检索'}</button></form><div className="knowledge-results">{knowledge.length ? knowledge.map((item, index) => <article className="knowledge-entry" key={`${item.path}-${index}`}><div><Icon name="book" size={18} /><h2>{item.path}</h2><span>匹配分数 {item.score}</span></div><pre>{item.excerpt}</pre><span className="source-label">引用来源：{item.path}</span></article>) : <Empty icon="book" title="没有匹配的资料">尝试缩短关键词，或检查后端的本地知识资料目录。</Empty>}</div></section>}
      </main>
    </div>
    <footer className="statusbar"><span><span className={`status-dot ${health ? 'connected' : ''}`} />{health ? '本地服务已连接' : '等待本地服务'}</span><span>{running ? '工作流正在后台运行' : '所有状态均来自实际执行记录'}<span className="footer-separator">·</span>AgentForge</span></footer>
    <dialog ref={applyDialog} className="confirm-dialog" aria-labelledby="apply-title" onCancel={e => { if (busy) e.preventDefault(); else setConfirmApply(false) }} onClose={() => setConfirmApply(false)}><Icon name="code" size={24} /><h2 id="apply-title">将候选修改应用到示例项目？</h2><p>运行 #{run?.id.slice(0, 8)} 已通过验证。确认后，候选文件将写入目标项目；如果基线已变化，后端会拒绝应用。</p><ul>{diffFiles.map(file => <li key={file.path}>{file.path}</li>)}</ul><div className="dialog-actions"><button autoFocus className="secondary-button" onClick={() => setConfirmApply(false)} disabled={busy}>返回检查</button><button className="primary-button" onClick={() => void applyRun()} disabled={busy || !canApply}>{busy ? '应用中' : '确认应用修改'}</button></div></dialog>
  </div>
}

function EventRow({ event, expanded = false }: { event: Event; expanded?: boolean }) {
  return <article className={`event-row event-${event.type}`}><div className="event-meta"><time dateTime={event.timestamp}>{date(event.timestamp)}</time><span className="event-type">{labels[event.type] ?? event.type}</span>{event.node_id && <span className="event-node">{event.node_id}</span>}</div><p>{event.message}</p>{event.data != null && <details open={expanded || undefined}><summary>查看输入与输出</summary><pre>{format(event.data)}</pre></details>}</article>
}

function ReviewStep({ step }: { step: Step }) {
  const output = step.output && typeof step.output === 'object' ? step.output as Record<string, unknown> : {}
  const issues = Array.isArray(output.issues) ? output.issues : []
  return <article className="review-result"><div><strong>{step.label}</strong><Status value={step.status} text={step.status === 'succeeded' ? '步骤已完成' : undefined} /></div><p>{step.agent_name ?? step.agent_id} · 第 {step.attempt ?? 0} 轮</p>{typeof output.passed === 'boolean' && <div className={`review-verdict ${output.passed ? 'passed' : 'failed'}`}><Icon name={output.passed ? 'check' : 'alert'} size={15} />审查结论：{output.passed ? '通过' : '未通过'}</div>}{typeof output.summary === 'string' && <p className="review-summary">{output.summary}</p>}{issues.length > 0 && <ul className="review-issues">{issues.map((issue, i) => <li key={i}>{format(issue)}</li>)}</ul>}<details><summary>完整审查输出</summary><pre>{format(step.output ?? '暂无输出')}</pre></details></article>
}
