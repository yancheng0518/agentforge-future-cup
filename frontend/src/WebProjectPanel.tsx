import { useEffect, useRef, useState } from 'react'
import { api, post } from './api'
import { latestRevisionPreview, normalizePinnedRequirements, previewExpiry, previewStatus, retainRequestIntent, safePreviewUrl, sameRequirements } from './projects'
import type { DependencyJob, ProjectRevision, RequestIntent, WebDependency, WebPreview } from './projects'
import './web-projects.css'

const errorText = (value: unknown) => value instanceof Error ? value.message : String(value)
const date = (value?: string) => value && Number.isFinite(new Date(value).getTime()) ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '未记录'

export function DependencyPreparation({ value, onChange, prepared, onPrepared, disabled = false }: {
  value: string; onChange: (text: string) => void; prepared: WebDependency | null;
  onPrepared: (dependency: WebDependency | null) => void; disabled?: boolean;
}) {
  const [available, setAvailable] = useState<WebDependency[]>([]), [job, setJob] = useState<DependencyJob | null>(null)
  const [history, setHistory] = useState<DependencyJob[]>([]), [error, setError] = useState(''), [posting, setPosting] = useState(false)
  const alive = useRef(true), intent = useRef<RequestIntent | null>(null), busy = useRef(false)
  const preparedCallback = useRef(onPrepared); preparedCallback.current = onPrepared
  const currentValue = useRef(value); currentValue.current = value
  const running = posting || job?.status === 'running'
  useEffect(() => {
    alive.current = true
    void api<WebDependency[]>('/web/dependencies').then(result => { if (alive.current) setAvailable(result) })
      .catch(exception => { if (alive.current) setError(errorText(exception)) })
    return () => { alive.current = false }
  }, [])
  useEffect(() => {
    if (!job || job.status !== 'running') return
    let current = true, timer: ReturnType<typeof setTimeout>
    const refresh = async () => {
      try {
        const result = await api<DependencyJob>(`/web/dependencies/jobs/${encodeURIComponent(job.id)}`)
        if (!current || !alive.current) return
        setJob(result); setError('')
        setHistory(previous => [result, ...previous.filter(item => item.id !== result.id)])
        if (result.status === 'succeeded' && result.result && sameRequirements(currentValue.current, result.result)) {
          preparedCallback.current(result.result)
          setAvailable(previous => [result.result!, ...previous.filter(item => item.dependency_id !== result.result!.dependency_id)])
          intent.current = null
        } else if (result.status === 'failed' || result.status === 'interrupted') intent.current = null
        if (result.status === 'running') timer = setTimeout(() => void refresh(), 2000)
      } catch (exception) {
        if (!current || !alive.current) return
        setError(`${errorText(exception)} 已保留本次准备标识；恢复连接后继续查询。`)
        timer = setTimeout(() => void refresh(), 4000)
      }
    }
    timer = setTimeout(() => void refresh(), 1000)
    return () => { current = false; clearTimeout(timer) }
  }, [job?.id, job?.status])

  async function prepare() {
    if (busy.current || disabled || running) return
    setError('')
    try {
      const pins = normalizePinnedRequirements(value)
      intent.current = retainRequestIntent(intent.current, JSON.stringify(pins), () => crypto.randomUUID())
      busy.current = true; setPosting(true); onPrepared(null)
      const result = await post<DependencyJob>('/web/dependencies/prepare', { requirements: pins.join('\n'), idempotency_key: intent.current.key })
      if (!alive.current) return
      setJob(result); setHistory(previous => [result, ...previous.filter(item => item.id !== result.id)])
      if (result.status === 'succeeded' && result.result && sameRequirements(value, result.result)) {
        onPrepared(result.result); intent.current = null
        setAvailable(previous => [result.result!, ...previous.filter(item => item.dependency_id !== result.result!.dependency_id)])
      } else if (result.status === 'failed' || result.status === 'interrupted') intent.current = null
    } catch (exception) { if (alive.current) setError(errorText(exception)) }
    finally { busy.current = false; if (alive.current) setPosting(false) }
  }

  return <section className="web-dependency-preparation" aria-label="Web 项目依赖">
    <h3>准备项目依赖</h3>
    <p className="pw-help">填写固定版本，或使用已准备的依赖。准备只下载并安装依赖，不执行项目代码，也不调用模型。</p>
    <label>第三方依赖<textarea value={value} onChange={event => { onChange(event.target.value); onPrepared(null); setError('') }}
      rows={3} maxLength={4096} disabled={disabled || running} placeholder={'例如：\nFlask==3.1.3'} spellCheck={false} /></label>
    {available.length > 0 && <label>已准备的依赖<select disabled={disabled || running} value={prepared?.dependency_id || ''} onChange={event => {
      const selected = available.find(item => item.dependency_id === event.target.value)
      if (selected) { onChange(selected.requirements.join('\n')); onPrepared(selected); setError('') }
    }}><option value="">选择已准备的版本…</option>{available.map(item => <option key={item.dependency_id} value={item.dependency_id}>{item.requirements.join(' · ')}</option>)}</select></label>}
    <div className="web-dependency-action"><button className="secondary-button" type="button" disabled={disabled || running || !value.trim()} onClick={() => void prepare()}>
      {running ? '正在准备依赖…' : job?.status === 'failed' || job?.status === 'interrupted' ? '重新准备依赖' : '准备依赖'}
    </button><span role="status">{running ? '下载、锁定版本并离线安装；可在此等待结果。' : prepared && sameRequirements(value, prepared) ? '依赖已准备，可导入项目。' : '准备完成后即可导入。'}</span></div>
    {error && <p className="pw-error" role="alert">{error}</p>}
    {(job?.status === 'failed' || job?.status === 'interrupted') && <p className="pw-error" role="alert">{job.error || '依赖准备未完成。原尝试已保存，重新准备会创建新的记录。'}</p>}
    {prepared && sameRequirements(value, prepared) && <details className="web-dependency-details"><summary>依赖锁与镜像</summary>
      <p className="pw-hash">依赖版本：{prepared.dependency_id}</p><p className="pw-hash">锁 SHA-256：{prepared.lock_sha256}</p><p className="pw-hash">镜像：{prepared.image_id}</p>
      {prepared.wheels && <ul>{prepared.wheels.map(item => <li key={item.name}>{item.name} · {item.version}</li>)}</ul>}</details>}
    {history.length > 0 && <details className="web-dependency-details"><summary>本次页面的准备记录（{history.length}）</summary><ol>{history.map(item => <li key={item.id}>
      <span>{item.status === 'running' ? '进行中' : item.status === 'succeeded' ? '已准备' : item.status === 'interrupted' ? '已中断' : '失败'}</span> · <code>{item.id}</code>{item.error && <p>{item.error}</p>}
    </li>)}</ol><p className="pw-help">离开页面不会删除服务端保存的尝试。</p></details>}
  </section>
}

export function WebProjectPanel({ projectId, revision, previews, canExecute }: {
  projectId: string; revision: ProjectRevision; previews: WebPreview[]; canExecute: boolean;
}) {
  const [record, setRecord] = useState<WebPreview | null>(() => latestRevisionPreview(previews, revision.id))
  const [history, setHistory] = useState<WebPreview[]>(previews), [error, setError] = useState(''), [busy, setBusy] = useState('')
  const [frameVersion, setFrameVersion] = useState(0), [, setClock] = useState(Date.now())
  const alive = useRef(true), intent = useRef<RequestIntent | null>(null), posting = useRef(false)
  const status = record ? previewStatus(record) : null
  const safeUrl = record ? safePreviewUrl(record, window.location.origin) : null
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  function apply(result: WebPreview) {
    setRecord(result); setHistory(previous => [result, ...previous.filter(item => item.id !== result.id)])
  }
  useEffect(() => {
    if (!record || !status?.active) return
    let current = true, timer: ReturnType<typeof setTimeout>
    const refresh = async () => {
      setClock(Date.now())
      try {
        const result = await api<WebPreview>(`/web/previews/${encodeURIComponent(record.id)}`)
        if (!current || !alive.current) return
        apply(result); setError('')
        if (previewStatus(result).active) timer = setTimeout(() => void refresh(), 3000)
      } catch (exception) {
        if (!current || !alive.current) return
        setError(`${errorText(exception)} 预览状态暂时无法确认，请恢复连接后刷新。`)
        timer = setTimeout(() => void refresh(), 4000)
      }
    }
    timer = setTimeout(() => void refresh(), 1500)
    return () => { current = false; clearTimeout(timer) }
  }, [record?.id, record?.status, status?.active])
  async function start() {
    if (!canExecute || posting.current || status?.active) return
    posting.current = true; setBusy('start'); setError('')
    try {
      intent.current = retainRequestIntent(intent.current, revision.id, () => crypto.randomUUID())
      const result = await api<WebPreview>(`/projects/${encodeURIComponent(projectId)}/preview`, {
        method: 'POST', body: JSON.stringify({ revision_id: revision.id, idempotency_key: intent.current.key }), signal: AbortSignal.timeout(120000),
      })
      if (!alive.current) return
      apply(result); intent.current = null; setFrameVersion(previous => previous + 1)
    } catch (exception) { if (alive.current) setError(`${errorText(exception)} 本次启动标识已保留，重试不会重复创建同一预览。`) }
    finally { posting.current = false; if (alive.current) setBusy('') }
  }
  async function stop() {
    if (!record || posting.current) return
    posting.current = true; setBusy('stop'); setError('')
    try {
      const result = await post<WebPreview>(`/web/previews/${encodeURIComponent(record.id)}/stop`)
      if (alive.current) apply(result)
    } catch (exception) { if (alive.current) setError(errorText(exception)) }
    finally { posting.current = false; if (alive.current) setBusy('') }
  }
  async function refresh() {
    if (!record || posting.current) return
    setError('')
    try { const result = await api<WebPreview>(`/web/previews/${encodeURIComponent(record.id)}`); if (alive.current) apply(result) }
    catch (exception) { if (alive.current) setError(errorText(exception)) }
  }
  return <section className="pw-section web-project-panel" aria-label="Web 浏览器预览">
    <div className="web-preview-heading"><div><h2>试用这个版本</h2><p>版本 {revision.number} · 浏览器预览</p></div>
      <div className="pw-actions">{status?.active ? <button className="secondary-button" disabled={!!busy} onClick={() => void stop()}>{busy === 'stop' ? '正在停止…' : '停止预览'}</button>
        : <button className="primary-button" disabled={!!busy || !canExecute} onClick={() => void start()}>{busy === 'start' ? '正在启动…' : '启动预览'}</button>}
        {record && <button className="text-button" disabled={!!busy} onClick={() => void refresh()}>刷新状态</button>}</div>
    </div>
    <p className="pw-help">直接操作实际页面，检查交互和显示效果。预览运行状态与开发验收分别记录。</p>
    {!canExecute && <p className="pw-help warning-text">Docker 当前不可用，请先检查运行环境。</p>}
    {error && <p className="pw-error" role="alert">{error}</p>}
    {record && <div className="web-preview-status" role="status"><span>{status?.label}</span>{previewExpiry(record) && status?.active && <span>到期时间：{date(previewExpiry(record)!)}</span>}{record.reason && <p>{record.reason}</p>}</div>}
    {safeUrl ? <div className="web-preview-frame"><div className="web-preview-toolbar"><span>版本 {revision.number} 的实际页面</span><button className="text-button" onClick={() => setFrameVersion(previous => previous + 1)}>重新加载页面</button></div>
      <iframe key={`${record!.id}-${frameVersion}`} src={safeUrl} title={`项目版本 ${revision.number} 的隔离 Web 预览`} sandbox="allow-scripts allow-forms allow-same-origin" referrerPolicy="no-referrer" />
    </div> : <div className="web-preview-empty"><h3>{busy === 'start' || record?.status === 'starting' ? '正在准备实际页面' : '页面将在这里打开'}</h3><p>{busy === 'start' || record?.status === 'starting' ? '正在等待 Web 服务启动回执…' : status?.usable ? '预览地址未通过本地隔离检查，页面尚未加载。' : record ? '当前预览已停止或尚未就绪。点击“启动预览”重新试用。' : '点击“启动预览”，试用所选版本。'}</p></div>}
    <details className="web-dependency-details"><summary>运行范围、入口与冻结依赖</summary><p>预览在 Docker 中运行，到期自动停止。预览产生的数据是临时的，停止或重启后不会保留。</p><p>入口：<code>{revision.web?.entrypoint}</code></p><p>{revision.web?.requirements.join(' · ')}</p><p className="pw-hash">依赖锁：{revision.web?.lock_sha256}</p><p className="pw-hash">镜像：{revision.web?.image_id}</p></details>
    {history.length > 0 && <details className="web-dependency-details"><summary>预览运行记录（{history.length}）</summary><ol className="web-preview-history">{history.map(item => <li key={item.id}>
      <div><strong>{previewStatus(item).label}</strong><small>{date(item.created_at)} · 记录 {item.id}</small>{item.reason && <p>{item.reason}</p>}</div>
      <details><summary>查看完整回执</summary><pre className="pw-json">{JSON.stringify(item, null, 2)}</pre></details>
    </li>)}</ol></details>}
  </section>
}
