import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { api, post } from './api'
import type { DynamicRecord } from './autonomous'
import { buildExecutionRequest, buildProjectRunRequest, buildWebImport, executionSummary, parseExecutionArgs, profileCapabilities, readProjectSources, retainRequestIntent, sameRequirements } from './projects'
import type { ProjectDetail, ProjectExecution, ProjectKnowledge, ProjectProfile, ProjectRevision, ProjectSummary, SourceSelection, WebDependency } from './projects'
import { DependencyPreparation, WebProjectPanel } from './WebProjectPanel'
import { deliveryReviewFailed, projectDelivery } from './delivery'
import './projects.css'

type Props = {
  projectId: string | null; initialRequirement?: string;
  onSelectProject: (id: string | null) => void;
  onOpenRun: (run: DynamicRecord | string) => void;
  canDevelop: boolean; canExecute: boolean; environmentReason?: string;
}
const errorText = (error: unknown) => error instanceof Error ? error.message : String(error)
const date = (value?: string) => value && Number.isFinite(new Date(value).getTime()) ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '时间未记录'
const object = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const stateNames: Record<string, string> = { queued: '等待开始', running: '进行中', succeeded: '已完成', failed: '执行失败', blocked: '执行受阻', needs_human: '需要处理' }
const preview = (value: string, limit: number) => { const characters = Array.from(value.trim()); return characters.length > limit ? characters.slice(0, limit - 1).join('') + '…' : characters.join('') }

export function ProjectWorkspace(props: Props) {
  return props.projectId ? <ProjectSession key={props.projectId} {...props} projectId={props.projectId} />
    : <ProjectList onSelectProject={props.onSelectProject} />
}

function ProjectList({ onSelectProject }: { onSelectProject: (id: string) => void }) {
  const [projects, setProjects] = useState<ProjectSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [showImport, setShowImport] = useState(false)
  const [refresh, setRefresh] = useState(0)
  useEffect(() => {
    let current = true
    setLoading(true); setError('')
    void api<ProjectSummary[]>('/projects').then(result => { if (current) setProjects(result) })
      .catch(exception => { if (current) setError(errorText(exception)) })
      .finally(() => { if (current) setLoading(false) })
    return () => { current = false }
  }, [refresh])
  return <div className="project-workspace">
    <header className="pw-heading"><div><h1>项目</h1><p>导入代码，在同一个项目中逐版改进。</p></div><button className="primary-button" onClick={() => setShowImport(value => !value)}>{showImport ? '收起导入' : '导入项目'}</button></header>
    <p className="pw-scope">支持小型 Python 工具和带固定依赖的 Web 项目。每次修改保存为新版本，原始磁盘文件保持原样。</p>
    {error && <div className="pw-error" role="alert"><p>{error}</p><button className="text-button" onClick={() => setRefresh(value => value + 1)}>重新读取</button></div>}
    {(showImport || (!loading && !projects.length && !error)) && <ProjectImport onImported={project => onSelectProject(project.id)} />}
    {loading ? <div className="skeleton pw-skeleton" aria-label="正在读取项目"><i /><i /><i /></div> : projects.length > 0 && <section className="pw-project-list" aria-label="已导入项目">{projects.map(project => <button key={project.id} onClick={() => onSelectProject(project.id)}><span><strong>{project.name}</strong><small>更新于 {date(project.updated_at || project.created_at)}</small></span><span aria-hidden="true">→</span></button>)}</section>}
  </div>
}

function ProjectImport({ onImported }: { onImported: (project: ProjectSummary) => void }) {
  const [name, setName] = useState(''), [selection, setSelection] = useState<SourceSelection | null>(null)
  const [reading, setReading] = useState(false), [saving, setSaving] = useState(false), [error, setError] = useState('')
  const [web, setWeb] = useState(false), [requirements, setRequirements] = useState(''), [entrypoint, setEntrypoint] = useState('')
  const [prepared, setPrepared] = useState<WebDependency | null>(null), [selectionVersion, setSelectionVersion] = useState(0)
  const generation = useRef(0), alive = useRef(true), submitting = useRef(false)
  useEffect(() => { alive.current = true; return () => { alive.current = false; generation.current++ } }, [])
  async function choose(files: File[]) {
    const token = ++generation.current
    setReading(true); setError(''); setSelection(null); setPrepared(null); setSelectionVersion(token)
    try {
      const result = await readProjectSources(files, { web: true })
      if (!alive.current || token !== generation.current) return
      setSelection(result); setName(previous => previous || result.suggestedName)
      setRequirements(result.requirements || ''); setWeb(!!result.requirements || !!result.hasWebAssets)
      const paths = Object.keys(result.files).filter(path => path.endsWith('.py'))
      setEntrypoint(paths.includes('app.py') ? 'app.py' : paths.includes('main.py') ? 'main.py' : paths[0] || '')
    } catch (exception) { if (alive.current && token === generation.current) setError(errorText(exception)) }
    finally { if (alive.current && token === generation.current) setReading(false) }
  }
  async function save(event: FormEvent) {
    event.preventDefault()
    if (!selection || !name.trim() || submitting.current) return
    submitting.current = true; setSaving(true); setError('')
    try {
      if (!web && selection.hasWebAssets) throw new Error('所选项目包含网页文件，请选择 Web 项目并准备依赖。')
      if (web && !prepared) throw new Error('请先准备项目的第三方依赖。')
      const webConfig = web ? buildWebImport(selection, prepared!, entrypoint, requirements) : undefined
      const project = await post<ProjectSummary>('/projects/import', { name: name.trim(), files: selection.files, ...(webConfig ? { web: webConfig } : {}) })
      if (alive.current) onImported(project)
    } catch (exception) { if (alive.current) setError(errorText(exception)) }
    finally { submitting.current = false; if (alive.current) setSaving(false) }
  }
  return <section className="pw-import"><h2>从你的代码开始</h2><p>选择文件后先检查清单。导入只保存版本 1，不执行代码，也不调用模型。</p>
    <form onSubmit={save}>
      <div className="pw-file-choices"><label>选择项目文件<input type="file" accept=".py,.html,.css,.js,.json,.txt" multiple disabled={saving} onChange={event => void choose([...event.currentTarget.files || []])} /></label><label>或选择项目文件夹<input type="file" multiple ref={element => element?.setAttribute('webkitdirectory', '')} disabled={saving} onChange={event => void choose([...event.currentTarget.files || []])} /></label></div>
      <p className="pw-help">最多 32 个 .py / .html / .css / .js / .json 源文件，单文件 64 KiB、合计 256 KiB。根目录 requirements.txt 单独作为依赖清单；缓存、隐藏目录和凭据文件不读取。</p>
      {reading && <p role="status">正在读取所选源码…</p>}
      {selection && <div className="pw-import-review"><p>{Object.keys(selection.files).length} 个源文件 · {(selection.bytes / 1024).toFixed(1)} KiB</p><ul>{Object.keys(selection.files).map(path => <li key={path}><code>{path}</code></li>)}</ul>{selection.excluded.length > 0 && <details><summary>未导入 {selection.excluded.length} 个其他文件</summary><ul>{selection.excluded.slice(0, 100).map((path, index) => <li key={`${path}-${index}`}><code>{path}</code></li>)}</ul>{selection.excluded.length > 100 && <p>其余文件也已忽略。</p>}</details>}</div>}
      <label className="pw-field">项目名称<input value={name} onChange={event => setName(event.target.value)} maxLength={100} required disabled={saving} placeholder="例如：我的文本处理工具" /></label>
      {selection && <><label className="pw-field">项目运行方式<select value={web ? 'web' : 'python'} disabled={saving} onChange={event => { setWeb(event.target.value === 'web'); setError('') }}><option value="python">Python 标准库工具</option><option value="web">Web 项目 · 第三方依赖与预览</option></select></label>
        {web && <><label className="pw-field">Web 入口文件<select value={entrypoint} disabled={saving} onChange={event => setEntrypoint(event.target.value)}>{Object.keys(selection.files).filter(path => path.endsWith('.py')).map(path => <option key={path}>{path}</option>)}</select></label>
          <DependencyPreparation key={selectionVersion} value={requirements} onChange={setRequirements} prepared={prepared} onPrepared={setPrepared} disabled={saving} /></>}
        {!web && selection.hasWebAssets && <p className="pw-help warning-text">所选文件包含网页资源，请选择 Web 项目运行方式。</p>}</>}
      {error && <p className="pw-error" role="alert">{error}</p>}
      <button className="primary-button" disabled={reading || saving || !selection || !name.trim() || (!web && !!selection?.hasWebAssets) || (web && (!prepared || !sameRequirements(requirements, prepared) || !entrypoint))}>{saving ? '正在导入…' : '导入为版本 1'}</button>
    </form>
  </section>
}

function ProjectSession({ projectId, initialRequirement = '', onSelectProject, onOpenRun, canDevelop, canExecute, environmentReason }: Props & { projectId: string }) {
  const [project, setProject] = useState<ProjectDetail | null>(null), [revisionId, setRevisionId] = useState('')
  const [loadedRevision, setRevision] = useState<ProjectRevision | null>(null), [file, setFile] = useState('')
  const revision = loadedRevision?.id === revisionId ? loadedRevision : null
  const [loading, setLoading] = useState(true), [revisionLoading, setRevisionLoading] = useState(false)
  const [error, setError] = useState(''), [revisionError, setRevisionError] = useState(''), [notice, setNotice] = useState('')
  const [projectReadError, setProjectReadError] = useState(''), [revisionRefresh, setRevisionRefresh] = useState(0)
  const [requirement, setRequirement] = useState(initialRequirement), [submitting, setSubmitting] = useState(false)
  const [entrypoint, setEntrypoint] = useState(''), [args, setArgs] = useState(''), [executing, setExecuting] = useState(false)
  const [downloadBusy, setDownloadBusy] = useState(false)
  const alive = useRef(true), readVersion = useRef(0), submitGuard = useRef(false), executeGuard = useRef(false), lastHead = useRef('')
  const lastSubmission = useRef<{ signature: string; key: string } | null>(null)
  const lastExecution = useRef<{ signature: string; key: string } | null>(null)
  const requirementInput = useRef<HTMLTextAreaElement>(null)
  const endpoint = `/projects/${encodeURIComponent(projectId)}`
  const load = useCallback(async (keepError = false) => {
    const token = ++readVersion.current
    setLoading(true); if (!keepError) setError('')
    try {
      const result = await api<ProjectDetail>(`/projects/${encodeURIComponent(projectId)}`)
      if (!alive.current || token !== readVersion.current) return
      const previousHead = lastHead.current
      lastHead.current = result.head_revision_id
      setProject(result); setProjectReadError(''); setRevisionId(previous => !previous || previous === previousHead ? result.head_revision_id : previous)
    } catch (exception) { if (alive.current && token === readVersion.current) { setError(errorText(exception)); setProjectReadError(errorText(exception)) } }
    finally { if (alive.current && token === readVersion.current) setLoading(false) }
  }, [projectId])
  useEffect(() => { alive.current = true; void load(); return () => { alive.current = false; readVersion.current++ } }, [load])
  const hasActiveRun = project?.runs?.some(item => ['queued', 'running'].includes(item.status)
    || (item.status === 'succeeded' && typeof object(item.publication).status !== 'string'))
  useEffect(() => {
    if (!hasActiveRun && !revision?.run_id) return
    const timer = setInterval(() => { void load(true) }, hasActiveRun ? 3000 : 15000)
    return () => clearInterval(timer)
  }, [hasActiveRun, revision?.run_id, load])
  useEffect(() => {
    if (!revisionId) return
    let current = true
    // Keep the same immutable revision mounted during refresh, including any
    // locally started preview. The revisionId guard hides a different version.
    setRevision(previous => previous?.id === revisionId ? previous : null); setRevisionError(''); setRevisionLoading(true)
    void api<ProjectRevision>(`${endpoint}/revisions/${encodeURIComponent(revisionId)}`).then(result => {
      if (!current) return
      if (result.id !== revisionId || result.project_id !== projectId) throw new Error('返回的版本与所选版本不一致，请重新读取。')
      setRevision(result)
      const paths = Object.keys(result.files || {})
      setFile(previous => paths.includes(previous) ? previous : paths[0] || '')
      setEntrypoint(paths.includes('main.py') ? 'main.py' : paths[0] || '')
    }).catch(exception => { if (current) setRevisionError(errorText(exception)) })
      .finally(() => { if (current) setRevisionLoading(false) })
    return () => { current = false }
  }, [endpoint, projectId, revisionId, revisionRefresh])
  const atHead = !!project && revisionId === project.head_revision_id
  const revisionNumber = revision?.number ?? project?.revisions.find(item => item.id === revisionId)?.number
  const delivery = project ? projectDelivery(project, revision, { loading: revisionLoading || loading, error: revisionError || projectReadError }) : null
  function refreshProject() { setRevisionRefresh(value => value + 1); void load() }
  async function start(event: FormEvent) {
    event.preventDefault()
    if (!project || !revision || revisionLoading || projectReadError || !atHead || !canDevelop || submitGuard.current) return
    setError(''); setNotice('')
    try {
      const signature = JSON.stringify([projectId, revisionId, requirement.trim()])
      lastSubmission.current = retainRequestIntent(lastSubmission.current, signature, () => crypto.randomUUID())
      const body = buildProjectRunRequest(revisionId, requirement, lastSubmission.current.key)
      submitGuard.current = true; setSubmitting(true)
      const run = await post<DynamicRecord>(`${endpoint}/runs`, body)
      if (alive.current) onOpenRun(run)
    } catch (exception) { if (alive.current) setError(errorText(exception)) }
    finally { submitGuard.current = false; if (alive.current) setSubmitting(false) }
  }
  async function execute(event: FormEvent) {
    event.preventDefault()
    if (!revision || !canExecute || !entrypoint || executeGuard.current) return
    setError(''); setNotice('')
    let executionArgs: string[]
    try { executionArgs = parseExecutionArgs(args) } catch (exception) { setError(errorText(exception)); return }
    try {
      const signature = JSON.stringify([projectId, revision.id, entrypoint, executionArgs])
      lastExecution.current = retainRequestIntent(lastExecution.current, signature, () => crypto.randomUUID())
      const body = buildExecutionRequest(revision.id, entrypoint, executionArgs, lastExecution.current.key)
      executeGuard.current = true; setExecuting(true)
      const result = await api<ProjectExecution>(`${endpoint}/execute`, { method: 'POST', body: JSON.stringify(body), signal: AbortSignal.timeout(120000) })
      // A received terminal receipt completes this user intent. A later click
      // may deliberately run the same command again; uncertain requests retain
      // the key so retry cannot repeat a persistent-data write.
      lastExecution.current = null
      if (!alive.current) return
      readVersion.current++; setLoading(false)
      setProject(previous => previous ? { ...previous, executions: [result, ...previous.executions.filter(item => item.id !== result.id)] } : previous)
      setNotice('运行回执已保存，可在下方查看输出。退出成功不等于通过开发验收。')
    } catch (exception) {
      if (alive.current) { setError(`${errorText(exception)} 若未收到回执，请先刷新运行记录确认，不要直接重复执行。`); void load(true) }
    } finally { executeGuard.current = false; if (alive.current) setExecuting(false) }
  }
  async function download() {
    if (!revision || downloadBusy) return
    setDownloadBusy(true); setError('')
    try {
      const response = await fetch(`/api${endpoint}/revisions/${encodeURIComponent(revision.id)}/download`, { signal: AbortSignal.timeout(20000) })
      if (!response.ok || !(response.headers.get('content-type') || '').includes('application/zip')) throw new Error('版本下载失败，请重新读取项目后再试。')
      const blob = await response.blob()
      if (!blob.size) throw new Error('收到的版本压缩包为空。')
      if (!alive.current) return
      const url = URL.createObjectURL(blob), link = document.createElement('a')
      link.href = url; link.download = `${project?.name || 'project'}-v${revision.number}.zip`; link.click()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (exception) { if (alive.current) setError(errorText(exception)) }
    finally { if (alive.current) setDownloadBusy(false) }
  }
  if (!project) return <div className="project-workspace"><button className="text-button" onClick={() => onSelectProject(null)}>← 所有项目</button>{loading ? <div className="skeleton pw-skeleton" aria-label="正在读取项目"><i /><i /><i /></div> : <div className="pw-error" role="alert"><p>{error || '项目暂时不可读取。'}</p><button className="secondary-button" onClick={() => void load()}>重新读取</button></div>}</div>
  return <div className="project-workspace">
    <button className="text-button pw-back" onClick={() => onSelectProject(null)}>← 所有项目</button>
    <header className="pw-heading"><div><h1>{project.name}</h1><p>查看交付版本，试用后继续改进。</p></div><button className="secondary-button" disabled={loading || revisionLoading} onClick={refreshProject}>{loading || revisionLoading ? '读取中…' : '刷新项目'}</button></header>
    {error && <p className="pw-error" role="alert">{error}</p>}{notice && <p className="pw-notice" role="status">{notice}</p>}
    <section className="pw-delivery" aria-label="所选版本交付概况">
      <div className="pw-version-bar"><label>查看版本<select value={revisionId} disabled={submitting || executing} onChange={event => setRevisionId(event.target.value)}>{project.revisions.map(item => <option key={item.id} value={item.id}>版本 {item.number}{item.id === project.head_revision_id ? ' · 当前' : ' · 历史'}</option>)}</select></label><span>{atHead ? '当前交付版本' : '历史版本 · 可查看与下载'}</span>{!atHead && <button className="text-button" onClick={() => setRevisionId(project.head_revision_id)}>切回当前版本</button>}</div>
      <div className="pw-delivery-content">
        <div className="pw-delivery-status" role="status" aria-live="polite"><h2 className={`pw-delivery-${delivery!.status}`}>{delivery!.label}</h2><p>{delivery!.detail}</p></div>
        {delivery!.requirement && <details className="pw-delivery-request"><summary>本版需求<span>{preview(delivery!.requirement, 48)}</span></summary><p>{delivery!.requirement}</p></details>}
        <dl className="pw-delivery-facts"><div><dt>版本来源</dt><dd>{delivery!.source}</dd></div><div><dt>源码</dt><dd>{revision ? `${Object.keys(revision.files || {}).length} 个文件` : '正在读取'}</dd></div><div><dt>验收记录</dt><dd>{delivery!.status === 'passed' ? `${delivery!.testCount} 项通过 · 模型审查通过` : delivery!.status === 'unverified' ? '尚无开发验收' : '等待核对'}</dd></div></dl>
        <div className="pw-delivery-actions"><button className="secondary-button" disabled={!revision || downloadBusy} onClick={() => void download()}>{downloadBusy ? '准备下载…' : `下载版本 ${revisionNumber ?? ''}`}</button>{delivery!.runId && <button className="text-button" onClick={() => onOpenRun(delivery!.runId!)}>查看开发过程</button>}<button className="text-button" onClick={() => { requirementInput.current?.focus({ preventScroll: true }); requirementInput.current?.scrollIntoView({ behavior: 'instant', block: 'center' }) }}>继续改进</button></div>
        {delivery!.status === 'passed' && <p className="pw-help">验收由 AI 生成并在开发前冻结；通过范围以本版测试为准。</p>}
        {revisionError && <div className="pw-error" role="alert"><p>{revisionError}</p><button className="text-button" onClick={refreshProject}>重新读取版本</button></div>}
      </div>
    </section>
    {revision?.web && <WebProjectPanel key={revision.id} projectId={projectId} revision={revision} previews={project.previews || []} canExecute={canExecute} />}
    {revisionLoading && <div className="skeleton pw-skeleton" aria-label="正在读取所选版本"><i /><i /></div>}
    {revision && !revision.web && <details className="pw-section pw-product-run"><summary>试用版本 {revision.number} · 运行程序</summary><p>选择入口与参数，在 Docker 中查看这个版本的实际输出。</p><form onSubmit={execute}><div className="pw-execution-inputs"><label>入口文件<select value={entrypoint} onChange={event => setEntrypoint(event.target.value)} disabled={executing}>{Object.keys(revision.files || {}).filter(path => path.endsWith('.py')).map(path => <option key={path}>{path}</option>)}</select></label><label>程序参数<textarea value={args} onChange={event => setArgs(event.target.value)} rows={3} disabled={executing} placeholder={'每行一个参数，例如：\n--name\n我的报告'} /></label></div><p className="pw-help">每行一个参数，原样传入；留空表示无参数。</p><button className="primary-button" disabled={!canExecute || !entrypoint || executing}>{executing ? '等待运行回执…' : `运行版本 ${revision.number}`}</button>{!canExecute && <p className="pw-help warning-text">Docker 当前不可用，请先检查运行环境。</p>}</form><details className="pw-runtime-details"><summary>运行范围与数据保存</summary><p>最长运行 15 秒，不调用模型。退出成功不代表通过开发验收。应用可在 /data 保存最多 64 KiB 数据，仅成功运行后保存，后续版本共享；验收不读取这份数据。</p></details></details>}
    <section className="pw-compose" id="project-next-step"><h2>下一步，继续改进</h2><p>{atHead ? `基于版本${revisionNumber ? ` ${revisionNumber}` : ''}提出新需求，保留已有功能与回归验收。` : '当前正在查看历史版本。继续开发前，请切回当前版本。'}</p>{!atHead && <button className="text-button" onClick={() => setRevisionId(project.head_revision_id)}>切回当前版本</button>}
      <form onSubmit={start}><label className="sr-only" htmlFor="project-request">描述项目的新需求</label><textarea id="project-request" ref={requirementInput} value={requirement} onChange={event => setRequirement(event.target.value)} rows={4} maxLength={8000} placeholder="例如：保留现有功能，增加导出报告，并检查边界情况…" disabled={submitting} onKeyDown={event => { if ((event.ctrlKey || event.metaKey) && event.key === 'Enter' && !event.nativeEvent.isComposing) { event.preventDefault(); event.currentTarget.form?.requestSubmit() } }} /><div className="pw-form-footer"><span>真实模型 · Docker 验收 · 配置与资料冻结</span><button className="primary-button" disabled={!atHead || !revision || revisionLoading || !!projectReadError || !requirement.trim() || !canDevelop || submitting}>{submitting ? '正在创建任务…' : '继续开发'}</button></div></form>
      {!canDevelop && <p className="pw-help warning-text">{environmentReason || '请先连接真实模型和 Docker 环境。'}</p>}
    </section>
    <details className="pw-section"><summary>查看版本 {revisionNumber ?? '…'} 的源码</summary>{revisionLoading && <p role="status">正在读取冻结源码…</p>}{revisionError && <p className="pw-error" role="alert">{revisionError}</p>}{revision && <><div className="pw-source-toolbar"><label>文件<select value={file} onChange={event => setFile(event.target.value)}>{Object.keys(revision.files || {}).map(path => <option key={path}>{path}</option>)}</select></label><span>{Object.keys(revision.files || {}).length} 个文件</span></div><div className="pw-source" tabIndex={0} aria-label="不可变版本源码"><pre><code>{revision.files?.[file] ?? '此版本没有可显示的源码。'}</code></pre></div><p className="pw-hash">源码 SHA-256：{revision.source_sha256}</p><details><summary>版本与验收记录</summary><pre className="pw-json">{JSON.stringify({ id: revision.id, parent_id: revision.parent_id, created_at: revision.created_at, run_id: revision.run_id, verification: revision.verification, acceptance: revision.acceptance }, null, 2)}</pre></details></>}</details>
    <CollaborationSettings projectId={projectId} documents={project.knowledge || []} onKnowledgeSaved={document => { readVersion.current++; setLoading(false); setProject(previous => previous ? { ...previous, knowledge: [document, ...previous.knowledge.filter(item => item.id !== document.id)] } : previous) }} />
    <section className="pw-section"><h2>开发记录</h2>{project.runs?.length ? <ol className="pw-history">{project.runs.map(item => { const publication = object(item.publication); const denied = deliveryReviewFailed(item); return <li key={item.id}><div><strong title={item.requirement}>{preview(item.requirement.trim().split(/\r?\n/)[0] || '项目开发任务', 80)}</strong><small>{date(item.created_at)} · {denied ? '复核未通过 · 需要处理' : stateNames[item.status] || item.status}{publication.status === 'saved' ? ` · 已保存版本 ${publication.number ?? ''}` : publication.status === 'conflict' ? ' · 版本存在冲突，未合入' : ''}</small>{denied && <p>后续复核否决了原结论，请查看开发过程中的问题与回执。</p>}{item.summary && !denied && <><p>{preview(item.summary, 140)}</p>{Array.from(item.summary.trim()).length > 140 && <details><summary>展开完整结论</summary><p>{item.summary}</p></details>}</>}</div><button className="text-button" onClick={() => onOpenRun(item.id)}>查看过程</button></li> })}</ol> : <p className="pw-help">第一条需求会从当前版本开始，完整保存成功、失败与受阻记录。</p>}</section>
    {((revision && !revision.web) || !!project.executions?.length) && <section className="pw-section"><h2>程序运行记录</h2>{project.executions?.length ? <div className="pw-executions">{project.executions.map(record => <ExecutionRecord key={record.id} record={record} number={project.revisions.find(item => item.id === record.revision_id)?.number} />)}</div> : <p className="pw-help">尚未运行程序。上方可选择版本入口，查看实际输出。</p>}</section>}
  </div>
}

function ExecutionRecord({ record, number }: { record: ProjectExecution; number?: number }) {
  const summary = executionSummary(record)
  return <details className="pw-execution-record"><summary><span>版本 {number ?? '未知'} · {record.entrypoint || 'Python 入口'}</span><span className={summary.tone === 'warning' ? 'warning-text' : ''}>{summary.text}</span></summary><p>{record.isolated === true ? 'Docker 隔离执行回执已确认' : '隔离执行尚未确认'} · {date(record.created_at)}{typeof record.duration_ms === 'number' ? ` · ${record.duration_ms} ms` : ''}</p>{record.reason && <p>{record.reason}</p>}{typeof record.data_saved === 'boolean' && <p>{record.data_saved ? '应用数据已保存，后续运行可继续使用。' : '本次没有保存新的应用数据，此前的数据保留。'}</p>}<h3>标准输出</h3><pre className="pw-output">{record.stdout || '（无输出）'}</pre>{record.stderr && <><h3>错误输出</h3><pre className="pw-output">{record.stderr}</pre></>}<p className="pw-hash">源码 SHA-256：{record.source_sha256 || '未记录'}</p><details><summary>完整运行回执</summary><pre className="pw-json">{JSON.stringify(record, null, 2)}</pre></details></details>
}

function CollaborationSettings({ projectId, documents, onKnowledgeSaved }: { projectId: string; documents: ProjectKnowledge[]; onKnowledgeSaved: (document: ProjectKnowledge) => void }) {
  const [profiles, setProfiles] = useState<ProjectProfile[]>([]), [loading, setLoading] = useState(true)
  const [profileDraft, setProfileDraft] = useState<Partial<ProjectProfile> | null>(null), [capabilities, setCapabilities] = useState('')
  const [documentDraft, setDocumentDraft] = useState<{ id?: string; title: string; content: string } | null>(null)
  const [saving, setSaving] = useState(''), [error, setError] = useState(''), [notice, setNotice] = useState('')
  const alive = useRef(true), saveGuard = useRef(false), uploadVersion = useRef(0), profileReadVersion = useRef(0)
  useEffect(() => {
    alive.current = true
    const token = ++profileReadVersion.current
    void api<ProjectProfile[]>('/projects/profiles').then(result => { if (alive.current && token === profileReadVersion.current) setProfiles(result) })
      .catch(exception => { if (alive.current && token === profileReadVersion.current) setError(errorText(exception)) }).finally(() => { if (alive.current && token === profileReadVersion.current) setLoading(false) })
    return () => { alive.current = false; uploadVersion.current++; profileReadVersion.current++ }
  }, [])
  function editProfile(profile?: ProjectProfile) { setProfileDraft(profile ? { ...profile } : { name: '', description: '', prompt: '', enabled: true }); setCapabilities(profile?.capabilities.join('，') || ''); setError(''); setNotice('') }
  async function saveProfile(event: FormEvent) {
    event.preventDefault()
    if (!profileDraft || saveGuard.current) return
    const values = profileCapabilities(capabilities)
    if (!values.length) { setError('请描述至少一种能力，例如“处理文本”或“检查边界条件”。'); return }
    saveGuard.current = true; setSaving('profile'); setError(''); setNotice('')
    try {
      const result = await api<ProjectProfile>(profileDraft.id ? `/projects/profiles/${encodeURIComponent(profileDraft.id)}` : '/projects/profiles', { method: profileDraft.id ? 'PUT' : 'POST', body: JSON.stringify({ name: profileDraft.name?.trim(), description: profileDraft.description || '', prompt: profileDraft.prompt || '', capabilities: values, enabled: profileDraft.enabled !== false }) })
      if (!alive.current) return
      profileReadVersion.current++; setLoading(false)
      setProfiles(previous => [result, ...previous.filter(item => item.id !== result.id)]); setProfileDraft(null)
      setNotice(`已保存 ${result.name} · v${result.version}。正在进行的任务继续使用原版本。`)
    } catch (exception) { if (alive.current) setError(errorText(exception)) }
    finally { saveGuard.current = false; if (alive.current) setSaving('') }
  }
  async function readDocument(file?: File) {
    if (!file) return
    const token = ++uploadVersion.current
    setError('')
    if (file.size > 65536) { setError('资料文件请控制在 64 KiB 以内。'); return }
    try {
      const content = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer())
      if (alive.current && token === uploadVersion.current) setDocumentDraft({ title: file.name, content })
    } catch { if (alive.current && token === uploadVersion.current) setError('无法读取资料，请选择 UTF-8 文本文件。') }
  }
  async function saveDocument(event: FormEvent) {
    event.preventDefault()
    if (!documentDraft || saveGuard.current) return
    saveGuard.current = true; setSaving('knowledge'); setError(''); setNotice('')
    try {
      const result = await post<ProjectKnowledge>(`/projects/${encodeURIComponent(projectId)}/knowledge`, { title: documentDraft.title.trim(), content: documentDraft.content, ...(documentDraft.id ? { document_id: documentDraft.id } : {}) })
      if (!alive.current) return
      onKnowledgeSaved(result); setDocumentDraft(null); setNotice(`资料“${result.title}”已保存为 v${result.version}。`)
    } catch (exception) { if (alive.current) setError(errorText(exception)) }
    finally { saveGuard.current = false; if (alive.current) setSaving('') }
  }
  return <details className="pw-section pw-settings"><summary>协作设置与项目资料</summary><p>新任务冻结当前启用的协作配置和本项目各资料的最新版本。主 Agent 按需求分工；这里不预设执行顺序或固定角色。</p>
    {error && <p className="pw-error" role="alert">{error}</p>}{notice && <p className="pw-notice" role="status">{notice}</p>}
    <section><div className="pw-section-heading"><h3>协作配置</h3><button className="text-button" disabled={!!saving} onClick={() => editProfile()}>新增配置</button></div><p className="pw-help">配置可被多个项目使用。修改会新增版本，不改变已有运行。</p>{loading ? <p role="status">读取配置中…</p> : profiles.length ? <ul className="pw-settings-list">{profiles.map(profile => <li key={`${profile.id}-${profile.version}`}><div><strong>{profile.name}</strong><small>v{profile.version} · {profile.enabled ? '已启用' : '已停用'} · {profile.capabilities.join('、')}</small><p>{profile.description}</p></div><button className="text-button" disabled={!!saving} onClick={() => editProfile(profile)}>编辑</button></li>)}</ul> : <p className="pw-help">暂无可读取的协作配置。</p>}
      {profileDraft && <form className="pw-settings-form" onSubmit={saveProfile}><label>名称<input value={profileDraft.name || ''} onChange={event => setProfileDraft(previous => ({ ...previous, name: event.target.value }))} maxLength={120} required disabled={!!saving} /></label><label>能力描述<input value={capabilities} onChange={event => setCapabilities(event.target.value)} placeholder="自由填写，以逗号分隔" required disabled={!!saving} /></label><label>用途说明<input value={profileDraft.description || ''} onChange={event => setProfileDraft(previous => ({ ...previous, description: event.target.value }))} disabled={!!saving} /></label><label>协作提示<textarea value={profileDraft.prompt || ''} onChange={event => setProfileDraft(previous => ({ ...previous, prompt: event.target.value }))} rows={4} disabled={!!saving} /></label><label className="pw-checkbox"><input type="checkbox" checked={profileDraft.enabled !== false} onChange={event => setProfileDraft(previous => ({ ...previous, enabled: event.target.checked }))} disabled={!!saving} />在新任务中启用</label><div className="pw-actions"><button className="secondary-button" disabled={!!saving}>{saving === 'profile' ? '正在保存…' : '保存新版本'}</button><button className="text-button" type="button" disabled={!!saving} onClick={() => setProfileDraft(null)}>取消编辑</button></div></form>}
    </section>
    <section><div className="pw-section-heading"><h3>项目资料</h3><button className="text-button" disabled={!!saving} onClick={() => { uploadVersion.current++; setDocumentDraft({ title: '', content: '' }) }}>新增资料</button></div><p className="pw-help">可放入需求约束、接口说明或编码约定；资料会作为带来源的参考内容。</p>{documents.length > 0 && <ul className="pw-settings-list">{documents.map(document => <li key={`${document.id}-${document.version}`}><div><strong>{document.title}</strong><small>v{document.version}</small><details><summary>查看内容与来源</summary><pre className="pw-json">{document.content}</pre><p className="pw-hash">SHA-256：{document.sha256 || document.hash || '未记录'}</p></details></div><button className="text-button" disabled={!!saving} onClick={() => { uploadVersion.current++; setDocumentDraft({ id: document.id, title: document.title, content: document.content }) }}>更新资料</button></li>)}</ul>}
      <label className="pw-upload-label">从文本文件添加<input type="file" accept=".md,.txt,text/plain,text/markdown" disabled={!!saving} onChange={event => void readDocument(event.currentTarget.files?.[0])} /></label>
      {documentDraft && <form className="pw-settings-form" onSubmit={saveDocument}><label>资料标题<input value={documentDraft.title} onChange={event => setDocumentDraft(previous => previous ? { ...previous, title: event.target.value } : previous)} required maxLength={200} disabled={!!saving} /></label><label>资料内容<textarea value={documentDraft.content} onChange={event => setDocumentDraft(previous => previous ? { ...previous, content: event.target.value } : previous)} rows={6} required disabled={!!saving} /></label><div className="pw-actions"><button className="secondary-button" disabled={!!saving}>{saving === 'knowledge' ? '正在保存…' : '保存资料版本'}</button><button className="text-button" type="button" disabled={!!saving} onClick={() => setDocumentDraft(null)}>取消编辑</button></div></form>}
    </section>
  </details>
}
