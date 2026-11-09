import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { Run } from './types'
import { buildReplay, projectFrame } from './collaborationGraph'
import type { GraphEdge, GraphNode, ReplayFrame } from './collaborationGraph'
import './collaborationGraph.css'

type Props = { run: Run; expanded: boolean; onToggleExpanded: () => void }
type Point = { x: number; y: number; width: number; height: number }
type Geometry = { width: number; height: number; nodes: Record<string, Point> }
const statusNames: Record<string, string> = {
  queued: '等待开始', pending: '待执行', running: '执行中', preparing: '验收已请求 · 等待回执',
  succeeded: '已完成', passed: '通过', completed: '已完成', failed: '失败', blocked: '受阻',
  cancelled: '已撤销', needs_human: '需要处理', unknown: '尚未确认', unconfirmed: '执行未确认',
  review_required: '需复核', rejected: '未通过', proposed: '已提出', created: '已建立', stopped: '已停止',
  not_started: '尚未开始', interrupted: '已中断 · 完成未确认',
}
const kindNames = { controller: '主 Agent', task: '子 Agent 任务', tool: '工具', review: '检查' }
const pretty = (value: unknown) => typeof value === 'string' ? value : JSON.stringify(value, null, 2)
const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const time = (value?: string) => {
  if (!value || !Number.isFinite(new Date(value).getTime())) return '时间未记录'
  return new Date(value).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
}
function toolCategory(node: GraphNode): string | null {
  if (node.kind !== 'tool') return null
  const data = node.details
  if (data.classification === 'input_baseline') return data.acceptance_origin === 'model_generated' ? '初始脚手架' : '项目基线'
  if (data.classification === 'seeded_defect_baseline' || data.classification === 'seeded_experiment_candidate') return '预设缺陷'
  if (data.acceptance_origin === 'model_generated') return node.status === 'failed' ? 'AI 生成验收 · 原因待核查' : 'AI 生成验收'
  return '候选验收'
}

function Glyph({ name }: { name: 'expand' | 'collapse' | 'previous' | 'next' | 'play' | 'pause' | GraphNode['kind'] }) {
  const paths = {
    expand: <path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5M3 3l6 6m12-6-6 6M3 21l6-6m12 6-6-6" />,
    collapse: <path d="M3 8h5V3m13 5h-5V3M8 21v-5H3m13 5v-5h5M3 3l5 5m13-5-5 5M3 21l5-5m13 5-5-5" />,
    previous: <path d="m14 5-7 7 7 7" />, next: <path d="m10 5 7 7-7 7" />,
    play: <path d="m8 4 12 8-12 8V4Z" />, pause: <path d="M8 4v16M16 4v16" />,
    controller: <><circle cx="12" cy="6" r="3" /><path d="M5 20v-4a7 7 0 0 1 14 0v4M3 12h3m12 0h3" /></>,
    task: <path d="m8 6-5 6 5 6m8-12 5 6-5 6m-3-14-2 16" />,
    tool: <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="m6 8 4 4-4 4m7 0h5" /></>,
    review: <><circle cx="10" cy="10" r="6" /><path d="m15 15 6 6m-14-11 2 2 4-4" /></>,
  }
  return <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>
}

function NodeFacts({ node }: { node: GraphNode }) {
  const details = node.details
  const attestation = record(details.attestation)
  const taskResult = record(record(details.result).result)
  const requests = Array.isArray(details.requests) ? details.requests.map(record) : []
  const formatErrors = Array.isArray(details.formatErrors) ? details.formatErrors.length : 0
  const transportFailures = requests.filter(request => request.status === 'transport_failed').length
  const issues = Array.isArray(details.issues) ? details.issues.filter((item): item is string => typeof item === 'string') : []
  const count = typeof details.test_count === 'number' && details.test_count > 0 ? details.test_count : null
  const failures = typeof attestation.failures === 'number' ? attestation.failures : details.failures
  const errors = typeof attestation.errors === 'number' ? attestation.errors : details.errors
  const category = details.classification === 'input_baseline' ? '初始项目基线；未实现脚手架的失败不计为模型代码失败。'
    : details.classification === 'seeded_defect_baseline' || details.classification === 'seeded_experiment_candidate' ? '公开预设缺陷实验，与自然失败分开记录。'
      : details.acceptance_origin === 'model_generated' ? 'AI 生成验收；测试不通过的原因仍需核查，不能仅凭失败计数认定代码有错。'
        : details.classification === 'natural_candidate' ? '真实模型候选，未注入预设缺陷。' : null
  return <div className="cg-node-facts">
    {node.kind === 'tool' && <>
      <p className={details.isolationConfirmed === true ? 'cg-confirmed' : 'cg-unconfirmed'}>{details.isolationConfirmed === true ? '已收到真实 Docker 隔离执行回执。' : '容器执行尚未获得完整确认；准备或派发不等于已经运行测试。'}</p>
      <dl><div><dt>实际测试</dt><dd>{count === null ? '暂无完整计数' : `${count} 项`}</dd></div>{typeof failures === 'number' && <div><dt>失败</dt><dd>{failures} 项</dd></div>}{typeof errors === 'number' && <div><dt>错误</dt><dd>{errors} 项</dd></div>}{typeof details.exit_code === 'number' && <div><dt>退出状态</dt><dd>{details.exit_code}</dd></div>}</dl>
      {category && <p>{category}</p>}{typeof details.reason === 'string' && details.reason && <p className="cg-unconfirmed">{details.reason}</p>}
    </>}
    {node.kind === 'review' && <><p>{details.passed === true ? '本次检查记录为通过；仍需结合对应测试和后续复核判断。' : details.passed === false ? '检查发现问题，不能用此前的通过记录覆盖这次结果。' : '检查还没有给出明确结果。'}</p>{issues.length > 0 && <ul>{issues.map((issue, index) => <li key={index}>{issue}</li>)}</ul>}</>}
    {typeof taskResult.summary === 'string' && taskResult.summary !== node.summary && <p>{taskResult.summary}</p>}
    {Array.isArray(taskResult.changed_paths) && taskResult.changed_paths.length > 0 && <p>产出文件：{taskResult.changed_paths.join('、')}</p>}
    {(formatErrors > 0 || transportFailures > 0) && <p className="cg-unconfirmed">{formatErrors > 0 ? `${formatErrors} 条模型输出格式错误记录。` : ''}{transportFailures > 0 ? `${transportFailures} 次模型请求传输失败。` : ''}这些与代码测试失败、代码修正分别记录。</p>}
  </div>
}

/** Layout follows recorded relations. It never manufactures stages or roles. */
function layersFor(nodes: GraphNode[], edges: GraphEdge[]): GraphNode[][] {
  const byId = new Map(nodes.map(node => [node.id, node]))
  const rank = new Map(nodes.map(node => [node.id, 0]))
  const forward = edges.filter(edge => edge.kind !== 'feedback' && byId.has(edge.source)
    && byId.has(edge.target) && byId.get(edge.target)?.kind !== 'controller')
  // Valid tasks form a DAG. Bound iteration for incomplete historical records.
  for (let round = 0; round < nodes.length; round++) {
    let changed = false
    for (const edge of forward) {
      const next = Math.min(nodes.length, (rank.get(edge.source) ?? 0) + 1)
      if (next > (rank.get(edge.target) ?? 0)) { rank.set(edge.target, next); changed = true }
    }
    if (!changed) break
  }
  const groups = new Map<number, GraphNode[]>()
  for (const node of nodes) {
    const level = rank.get(node.id) ?? 0
    groups.set(level, [...(groups.get(level) ?? []), node])
  }
  return [...groups.entries()].sort((a, b) => a[0] - b[0]).map(([, group]) => group)
}

function edgePath(edge: GraphEdge, geometry: Geometry, ordinal: number): string | null {
  const start = geometry.nodes[edge.source], end = geometry.nodes[edge.target]
  if (!start || !end) return null
  const x1 = start.x + start.width / 2, y1 = start.y + start.height
  const x2 = end.x + end.width / 2, y2 = end.y
  const backward = edge.kind === 'feedback' || end.y <= start.y
  const crossesRows = Object.entries(geometry.nodes).some(([id, box]) => id !== edge.source && id !== edge.target
    && box.y < end.y && box.y + box.height > y1)
  if (backward || crossesRows) {
    // Route long links through the outside gutter and row gaps, never across
    // the contents of intervening cards in wrapped parallel layers.
    const rowBottom = Math.max(y1, ...Object.values(geometry.nodes).filter(box => Math.abs(box.y - start.y) < 2).map(box => box.y + box.height))
    const rail = backward ? 5 + ordinal % 3 * 4 : geometry.width - 5 - ordinal % 3 * 4
    return `M ${x1} ${y1} L ${x1} ${rowBottom + 8} L ${rail} ${rowBottom + 8} L ${rail} ${y2 - 8} L ${x2} ${y2 - 8} L ${x2} ${y2}`
  }
  const midpoint = y1 + (y2 - y1) / 2
  return `M ${x1} ${y1} C ${x1} ${midpoint}, ${x2} ${midpoint}, ${x2} ${y2}`
}

export function CollaborationGraph(props: Props) {
  return <GraphSession key={props.run.id} {...props} />
}

function GraphSession({ run, expanded, onToggleExpanded }: Props) {
  const replay = useMemo(() => buildReplay(run), [run])
  const frames = replay.frames
  const [cursorId, setCursorId] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [view, setView] = useState<'graph' | 'records'>('graph')
  const [playing, setPlaying] = useState(false)
  const [pauseReason, setPauseReason] = useState('')
  const [reducedMotion, setReducedMotion] = useState(() => window.matchMedia('(prefers-reduced-motion: reduce)').matches)
  const [pageVisible, setPageVisible] = useState(() => document.visibilityState !== 'hidden')
  const framesRef = useRef<ReplayFrame[]>(frames)
  const cursorRef = useRef(cursorId)
  const lastReplayIndex = useRef(0)
  framesRef.current = frames
  cursorRef.current = cursorId
  const located = cursorId === null ? frames.length - 1 : frames.findIndex(frame => frame.id === cursorId)
  const cursor = cursorId === null ? frames.length - 1 : located >= 0 ? located : Math.min(lastReplayIndex.current, frames.length - 1)
  if (cursorId !== null && cursor >= 0) lastReplayIndex.current = cursor
  const live = cursorId === null
  const snapshot = useMemo(() => projectFrame(run, frames, cursor), [run, frames, cursor])
  const groups = useMemo(() => layersFor(snapshot.nodes, snapshot.edges), [snapshot.nodes, snapshot.edges])
  const selected = selectedId === null ? snapshot.nodes.find(node => node.kind === 'controller') ?? snapshot.nodes[0]
    : snapshot.nodes.find(node => node.id === selectedId)
  const [geometry, setGeometry] = useState<Geometry>({ width: 0, height: 0, nodes: {} })
  const scene = useRef<HTMLDivElement>(null)
  const nodeElements = useRef(new Map<string, HTMLButtonElement>())
  const markerId = useId().replace(/:/g, '')

  useEffect(() => {
    const media = window.matchMedia('(prefers-reduced-motion: reduce)')
    const updateMotion = () => {
      setReducedMotion(media.matches)
      if (media.matches) { setPlaying(false); setPauseReason('动态效果偏好已变化，回放已暂停；可以重新播放。') }
    }
    const updateVisibility = () => {
      const visible = document.visibilityState !== 'hidden'
      setPageVisible(visible)
      if (!visible) { setPlaying(false); setPauseReason('页面离开前台，回放已暂停。') }
    }
    media.addEventListener('change', updateMotion)
    document.addEventListener('visibilitychange', updateVisibility)
    return () => { media.removeEventListener('change', updateMotion); document.removeEventListener('visibilitychange', updateVisibility) }
  }, [])

  useEffect(() => {
    if (!playing || !pageVisible || replay.limited) return
    const timer = window.setInterval(() => {
      const currentFrames = framesRef.current
      const currentIndex = cursorRef.current === null ? currentFrames.length - 1 : currentFrames.findIndex(frame => frame.id === cursorRef.current)
      if (currentIndex >= currentFrames.length - 1 || !currentFrames.length) {
        setPlaying(false); setPauseReason('已播放到当前记录末尾。新事件到来后不会自动重播。'); return
      }
      const next = currentFrames[currentIndex + 1].id
      cursorRef.current = next; setCursorId(next)
    }, 1000)
    return () => window.clearInterval(timer)
  }, [playing, reducedMotion, pageVisible, replay.limited])

  useLayoutEffect(() => {
    const element = scene.current
    if (!element || view !== 'graph') return
    let animationFrame = 0
    const measure = () => {
      const bounds = element.getBoundingClientRect()
      const nodes: Record<string, Point> = {}
      nodeElements.current.forEach((node, id) => {
        const rect = node.getBoundingClientRect()
        nodes[id] = { x: rect.left - bounds.left, y: rect.top - bounds.top, width: rect.width, height: rect.height }
      })
      const next = { width: bounds.width, height: bounds.height, nodes }
      setGeometry(previous => JSON.stringify(previous) === JSON.stringify(next) ? previous : next)
    }
    const queueMeasure = () => { cancelAnimationFrame(animationFrame); animationFrame = requestAnimationFrame(measure) }
    const observer = new ResizeObserver(queueMeasure)
    observer.observe(element)
    nodeElements.current.forEach(node => observer.observe(node))
    measure()
    return () => { observer.disconnect(); cancelAnimationFrame(animationFrame) }
  }, [snapshot.nodes, expanded, view])

  function moveTo(index: number) {
    setPlaying(false); setPauseReason('')
    const id = frames[Math.max(0, Math.min(frames.length - 1, index))]?.id ?? null
    cursorRef.current = id; setCursorId(id)
  }
  function returnLive() {
    setPlaying(false); setPauseReason(''); cursorRef.current = null; setCursorId(null)
  }
  function togglePlayback() {
    if (playing) { setPlaying(false); setPauseReason('回放已暂停。'); return }
    if (frames.length < 2 || replay.limited || !pageVisible) return
    if (live || cursor >= frames.length - 1) {
      cursorRef.current = frames[0].id; setCursorId(frames[0].id)
    }
    setPauseReason(''); setPlaying(true)
  }
  const relatedEdges = selected ? snapshot.edges.filter(edge => edge.source === selected.id || edge.target === selected.id) : []
  const sourceEvents = selected ? snapshot.visibleEvents.filter(event => selected.eventIds.includes(String(event.id))) : []
  const selectedMissing = selectedId !== null && !selected
  const activeFrame = snapshot.currentFrame
  const canPlay = frames.length > 1 && !replay.limited && pageVisible

  return <section className={`collaboration-graph ${expanded ? 'cg-expanded' : ''}`} aria-label="真实执行协作图">
    <header className="cg-heading"><div><h2>任务如何推进</h2><p>从已保存的执行事件查看分工、依赖与反馈。</p></div><button className="secondary-button cg-expand" onClick={onToggleExpanded}><Glyph name={expanded ? 'collapse' : 'expand'} />{expanded ? '收起视图' : '放大查看'}</button></header>
    <div className="cg-toolbar"><div className="cg-view-toggle" role="group" aria-label="协作视图"><button aria-pressed={view === 'graph'} onClick={() => setView('graph')}>协作图</button><button aria-pressed={view === 'records'} onClick={() => setView('records')}>事件记录</button></div><span className={`cg-live-label ${live ? 'is-live' : ''}`}>{live ? '实时记录' : '历史回放'}{playing ? ' · 播放中' : ''}</span></div>
    <div className="cg-replay" aria-label="事件回放控制">
      <label className="cg-slider-label" htmlFor={`${markerId}-cursor`}><span>{frames.length ? `事件 ${cursor + 1} / ${frames.length}` : '尚无执行事件'}</span><time>{time(activeFrame?.timestamp)}</time></label>
      <input id={`${markerId}-cursor`} aria-label="选择回放事件" type="range" min={0} max={Math.max(0, frames.length - 1)} value={Math.max(0, cursor)} disabled={frames.length < 2 || replay.limited} onChange={event => moveTo(Number(event.target.value))} />
      <div className="cg-playback-buttons"><button className="secondary-button" aria-label="上一个事件" disabled={cursor <= 0 || replay.limited} onClick={() => moveTo(cursor - 1)}><Glyph name="previous" /><span>上一步</span></button><button className="secondary-button" aria-label={playing ? '暂停事件回放' : '播放事件回放'} disabled={!playing && !canPlay} onClick={togglePlayback}><Glyph name={playing ? 'pause' : 'play'} />{playing ? '暂停' : '播放'}</button><button className="secondary-button" aria-label="下一个事件" disabled={cursor >= frames.length - 1 || replay.limited} onClick={() => moveTo(cursor + 1)}><span>下一步</span><Glyph name="next" /></button><button className="text-button cg-live-button" disabled={live} onClick={returnLive}>回到实时</button></div>
      <p className="cg-replay-note">{reducedMotion && '减少动态效果已启用，回放逐条切换，无过渡动画。'}{pauseReason || '播放仅浏览已有事件，每秒一条，不会重试任务或调用模型。'}</p>
    </div>
    {(replay.limited || snapshot.notice) && <p className="cg-notice" role="note">{snapshot.notice || replay.reason || '记录不足，仅能查看已保存的状态，无法还原完整过程。'}</p>}
    <div className="cg-current-frame" aria-live={playing ? 'off' : 'polite'}><span>{activeFrame?.label || '等待第一个执行事件'}</span>{activeFrame?.eventId && <small>来源事件 #{activeFrame.eventId}</small>}</div>
    {view === 'graph' ? <div className="cg-workspace"><div className="cg-map-area"><div className="cg-map-scroll" tabIndex={0} aria-label="任务关系图，可滚动查看"><div className="cg-scene" ref={scene}>
      {geometry.width > 0 && <svg className="cg-edges" width={geometry.width} height={geometry.height} viewBox={`0 0 ${geometry.width} ${geometry.height}`} aria-hidden="true"><defs><marker id={`${markerId}-arrow`} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M1 1 7 4 1 7" fill="none" stroke="context-stroke" strokeWidth="1.2" /></marker></defs>{snapshot.edges.map((edge, index) => {
        const path = edgePath(edge, geometry, index)
        const highlighted = !!selected && (edge.source === selected.id || edge.target === selected.id)
        return path ? <path key={edge.id} d={path} className={`cg-edge ${edge.kind} ${highlighted ? 'is-highlighted' : ''}`} markerEnd={`url(#${markerId}-arrow)`} /> : null
      })}</svg>}
      {groups.map((group, index) => <div className="cg-layer" key={index}>{group.map(node => <button key={node.id} ref={element => { if (element) nodeElements.current.set(node.id, element); else nodeElements.current.delete(node.id) }} className={`cg-node ${selected?.id === node.id ? 'is-selected' : ''}`} data-status={node.status} aria-pressed={selected?.id === node.id} onClick={() => setSelectedId(node.id)} title={`${node.label} · ${statusNames[node.status] ?? node.status}`}><span className="cg-node-type"><Glyph name={node.kind} />{kindNames[node.kind]}</span><strong>{node.label}</strong>{toolCategory(node) && <span className="cg-node-category">{toolCategory(node)}</span>}<span className="cg-node-status"><span className="cg-status-dot" />{statusNames[node.status] ?? node.status}</span>{node.agentId && <small>{node.agentId}</small>}</button>)}</div>)}
      {!snapshot.nodes.length && <div className="cg-empty"><h3>这个时点还没有任务节点</h3><p>节点只会在真实派发、工具或检查事件出现后加入。</p></div>}
    </div></div><p className="cg-map-caption">箭头表示派发、依赖、工具或反馈关系。位置不代表同时执行，也不是 Agent 私聊记录。</p></div>
      <aside className="cg-inspector" aria-label="选中节点详情">{selected ? <><div className="cg-inspector-heading"><span>{kindNames[selected.kind]}</span><strong>{selected.label}</strong><span className="cg-state-text">{statusNames[selected.status] ?? selected.status}</span></div>{selected.agentId && <p className="cg-mono">{selected.agentId}</p>}{selected.summary && <p className="cg-node-summary">{selected.summary}</p>}<NodeFacts node={selected} />{selected.instructions && <details open><summary>任务指令</summary><p>{selected.instructions}</p></details>}{selected.writePaths.length > 0 && <div className="cg-files"><h3>文件范围</h3><ul>{selected.writePaths.map(path => <li key={path}><code>{path}</code></li>)}</ul></div>}{selected.dependsOn.length > 0 && <p className="cg-dependencies">依赖：{selected.dependsOn.join('、')}</p>}{relatedEdges.length > 0 && <details><summary>与其他节点的关系 · {relatedEdges.length}</summary><ul className="cg-relations">{relatedEdges.map(edge => <li key={edge.id}>{snapshot.nodes.find(node => node.id === edge.source)?.label ?? edge.source}<span> → {edge.label} → </span>{snapshot.nodes.find(node => node.id === edge.target)?.label ?? edge.target}</li>)}</ul></details>}<details><summary>完整节点数据</summary><pre>{pretty(selected.details)}</pre></details><details className="cg-source-events"><summary>来源事件 · {selected.eventIds.length}</summary>{sourceEvents.length ? sourceEvents.map(event => <article key={event.id}><div><strong>#{event.id} · {event.type}</strong><time>{time(event.timestamp)}</time></div><p>{event.message}</p><pre>{pretty(event.data)}</pre></article>) : <p>{snapshot.limited ? '此节点来自最终状态快照，没有可核对的逐事件来源。' : '该时点没有可展开的来源事件。'}</p>}</details></> : <div className="cg-empty"><h3>{selectedMissing ? '这个节点尚未出现' : '选择一个节点'}</h3><p>{selectedMissing ? '选择已保留；回放到对应派发事件后可继续查看。' : '查看真实任务指令、文件范围和执行依据。'}</p></div>}</aside>
    </div> : <div className="cg-event-records" aria-label="当前时点的事件记录">{snapshot.visibleEvents.length ? snapshot.visibleEvents.map(event => <details className={String(event.id) === activeFrame?.eventId ? 'is-current' : ''} key={event.id}><summary><span>#{event.id} · {event.message}</span><time>{time(event.timestamp)}</time></summary><p className="cg-event-type">{event.type}{event.node_id ? ` · ${event.node_id}` : ''}</p><pre>{pretty(event.data)}</pre></details>) : <div className="cg-empty"><h3>没有逐事件记录</h3><p>{replay.reason || '此时点尚未发生可显示的执行事件。'}</p></div>}</div>}
  </section>
}
