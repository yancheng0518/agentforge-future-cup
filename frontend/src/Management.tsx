import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent, JSX } from 'react'
import { api } from './api'
import type { Agent, Knowledge } from './types'

const roles: Record<string, string> = {
  planner: '规划', developer: '开发', tester: '测试', reviewer: '审查', fixer: '修正',
}
const capabilities = ['plan', 'develop', 'test', 'review', 'fix']
const tools = ['read_project', 'knowledge_search', 'candidate_diff', 'run_acceptance']
const errorText = (error: unknown) => error instanceof Error ? error.message : String(error)
const commaList = (value: string) => [...new Set(value.split(/[,，]/).map(item => item.trim()).filter(Boolean))]

export function Management({ view }: { view: 'agents' | 'knowledge' }): JSX.Element {
  return view === 'agents' ? <AgentSettings /> : <KnowledgeSearch />
}

export default Management

function AgentSettings() {
  const [agents, setAgents] = useState<Agent[]>([])
  const [draft, setDraft] = useState<Agent | null>(null)
  const [capabilityText, setCapabilityText] = useState('')
  const [toolText, setToolText] = useState('')
  const [creating, setCreating] = useState(false)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const active = useRef(true)
  const loadRequest = useRef(0)

  const load = useCallback(async () => {
    const request = ++loadRequest.current
    setLoading(true)
    setError('')
    try {
      const value = await api<Agent[]>('/agents')
      if (active.current && request === loadRequest.current) setAgents(value)
    } catch (exception) {
      if (active.current && request === loadRequest.current) setError(errorText(exception))
    } finally {
      if (active.current && request === loadRequest.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    active.current = true
    void load()
    return () => { active.current = false; loadRequest.current += 1 }
  }, [load])

  function select(agent: Agent, isNew = false) {
    setDraft({ ...agent, model: agent.model === 'default' ? '' : agent.model, capabilities: [...agent.capabilities], tools: [...agent.tools] })
    setCapabilityText(agent.capabilities.join(', '))
    setToolText(agent.tools.join(', '))
    setCreating(isNew)
    setError('')
    setNotice('')
  }

  function create() {
    select({
      id: `agent-${crypto.randomUUID().slice(0, 8)}`, name: '', role: 'developer',
      capabilities: ['develop'], enabled: true, version: 1, prompt: '', model: '',
      tools: ['read_project', 'knowledge_search', 'candidate_diff'],
    }, true)
  }

  async function save(event: FormEvent) {
    event.preventDefault()
    if (!draft || saving) return
    const selectedCapabilities = commaList(capabilityText)
    const selectedTools = commaList(toolText)
    if (!selectedCapabilities.length || selectedCapabilities.some(value => !capabilities.includes(value))) {
      setError('请填写至少一种有效能力：plan、develop、test、review 或 fix。')
      return
    }
    if (selectedTools.some(value => !tools.includes(value))) {
      setError('工具名称不在允许范围内，请使用下方列出的名称。')
      return
    }
    setSaving(true)
    loadRequest.current += 1
    setLoading(false)
    setError('')
    setNotice('')
    try {
      const payload = {
        id: draft.id, name: draft.name.trim(), role: draft.role,
        capabilities: selectedCapabilities, enabled: draft.enabled, prompt: draft.prompt,
        model: draft.model.trim() || 'default', tools: selectedTools,
      }
      const saved = await api<Agent>(creating ? '/agents' : `/agents/${encodeURIComponent(draft.id)}`, {
        method: creating ? 'POST' : 'PUT', body: JSON.stringify(payload),
      })
      if (!active.current) return
      // The mutation response is authoritative. Do not call a second endpoint
      // whose failure could incorrectly label an already saved version failed.
      setAgents(previous => previous.some(agent => agent.id === saved.id)
        ? previous.map(agent => agent.id === saved.id ? saved : agent)
        : [...previous, saved])
      select(saved)
      setNotice(`已保存 ${saved.name} · v${saved.version}。新的任务会使用此配置。`)
    } catch (exception) {
      if (active.current) setError(errorText(exception))
    } finally {
      if (active.current) setSaving(false)
    }
  }

  return <section className="management-page" aria-labelledby="agents-title">
    <div className="section-heading">
      <div><h1 id="agents-title">协作成员</h1><p>管理 Agent 的职责与权限。已有任务保留创建时的配置版本。</p></div>
      <button className="primary-button" type="button" onClick={create} disabled={saving}>新增 Agent</button>
    </div>
    {error && <div className="message error-message" role="alert"><span>{error}</span><button type="button" aria-label="关闭错误提示" onClick={() => setError('')}>×</button></div>}
    {notice && <div className="message success-message" role="status"><span>{notice}</span></div>}
    <div className="agent-layout">
      <div className="agent-list" aria-label="协作成员列表" aria-busy={loading}>
        <div className="form-actions"><span>{loading ? '正在读取配置…' : `${agents.length} 位协作成员`}</span><button className="text-button" type="button" onClick={() => void load()} disabled={loading || saving}>刷新列表</button></div>
        {loading && !agents.length && <div className="skeleton-list" aria-label="正在读取协作成员"><i /><i /><i /></div>}
        {!loading && !agents.length && !error && <div className="empty"><strong>还没有协作成员</strong><p>点击“新增 Agent”，设置它的职责和允许使用的工具。</p></div>}
        {agents.map(agent => <button key={agent.id} type="button" disabled={saving}
          className={`agent-row ${draft?.id === agent.id && !creating ? 'selected' : ''}`}
          aria-pressed={draft?.id === agent.id && !creating} onClick={() => select(agent)}>
          <span className="agent-avatar" aria-hidden="true">{(roles[agent.role] || agent.name).slice(0, 1)}</span>
          <span className="agent-info"><strong>{agent.name}<span>v{agent.version}</span></strong><small>{roles[agent.role] || agent.role} · {agent.capabilities.join(' · ')}</small></span>
          <span className={`agent-enabled ${agent.enabled ? 'yes' : ''}`}>{agent.enabled ? '已启用' : '已停用'}</span>
        </button>)}
      </div>
      {draft ? <form className="agent-form" onSubmit={save} aria-label={creating ? '新增 Agent 配置' : '编辑 Agent 配置'}>
        <div className="form-title"><h2>{creating ? '新增 Agent' : draft.name}</h2><label className="checkbox-label"><input type="checkbox" checked={draft.enabled} disabled={saving} onChange={event => setDraft({ ...draft, enabled: event.target.checked })} />允许参与新任务</label></div>
        <label>Agent ID<span>字母、数字、下划线或连字符；创建后不可更改。</span><input required pattern="[a-zA-Z0-9_-]{1,60}" maxLength={60} disabled={!creating || saving} value={draft.id} onChange={event => setDraft({ ...draft, id: event.target.value })} /></label>
        <div className="form-grid">
          <label>名称<input required maxLength={100} disabled={saving} value={draft.name} onChange={event => setDraft({ ...draft, name: event.target.value })} /></label>
          <label>角色<select value={draft.role} disabled={saving} onChange={event => setDraft({ ...draft, role: event.target.value })}>{Object.entries(roles).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
        </div>
        <label>能力标签<span>可选 plan、develop、test、review、fix，用逗号分隔。</span><input required disabled={saving} value={capabilityText} onChange={event => setCapabilityText(event.target.value)} /></label>
        <label>模型<span>留空或填写 default，使用工作区配置的模型。</span><input maxLength={100} disabled={saving} value={draft.model} placeholder="工作区默认模型" onChange={event => setDraft({ ...draft, model: event.target.value })} /></label>
        <label>允许使用的工具<span>read_project、knowledge_search、candidate_diff、run_acceptance，用逗号分隔；留空表示不开放工具。</span><input disabled={saving} value={toolText} onChange={event => setToolText(event.target.value)} /></label>
        <label>角色提示词<textarea maxLength={12000} disabled={saving} value={draft.prompt} onChange={event => setDraft({ ...draft, prompt: event.target.value })} rows={8} /></label>
        <div className="form-actions"><span>{creating ? '将创建 v1' : `将保存为 v${draft.version + 1}，历史版本保留`}</span><button className="primary-button" disabled={saving || !draft.name.trim()}>{saving ? '正在保存…' : '保存配置'}</button></div>
      </form> : <div className="agent-form"><div className="empty"><strong>选择一位协作成员</strong><p>查看和编辑它的职责、提示词、模型与工具权限。日常使用可以保留现有配置。</p></div></div>}
    </div>
  </section>
}

function KnowledgeSearch() {
  const [query, setQuery] = useState('')
  const [matches, setMatches] = useState<Knowledge[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [lastQuery, setLastQuery] = useState<string | null>(null)
  const active = useRef(true)
  const requestId = useRef(0)

  const search = useCallback(async (value?: string) => {
    const currentRequest = ++requestId.current
    setLoading(true)
    setError('')
    try {
      const response = await api<{ matches: Knowledge[] }>(value?.trim() ? `/knowledge?q=${encodeURIComponent(value.trim())}` : '/knowledge')
      if (!active.current || currentRequest !== requestId.current) return
      setMatches(response.matches)
      setLastQuery(value?.trim() || '')
    } catch (exception) {
      if (active.current && currentRequest === requestId.current) setError(errorText(exception))
    } finally {
      if (active.current && currentRequest === requestId.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    active.current = true
    void search()
    return () => { active.current = false; requestId.current += 1 }
  }, [search])

  function submit(event: FormEvent) {
    event.preventDefault()
    if (!loading) void search(query)
  }

  return <section className="management-page knowledge-page" aria-labelledby="knowledge-title">
    <div className="section-heading"><div><h1 id="knowledge-title">项目资料</h1><p>检索需求、接口约定和开发规范，查看执行任务时可引用的原始内容。</p></div><span className="local-badge">本地关键词检索</span></div>
    <form className="search-form" onSubmit={submit}>
      <label className="sr-only" htmlFor="knowledge-query">检索项目资料</label>
      <input id="knowledge-query" value={query} maxLength={8000} onChange={event => setQuery(event.target.value)} placeholder="搜索需求、接口或测试说明" />
      <button className="secondary-button" disabled={loading}>{loading ? '正在检索…' : '搜索'}</button>
    </form>
    <p className="task-note">资料来自工作区 knowledge 目录。此处展示匹配片段，不会启动模型任务。</p>
    {error && <div className="message error-message" role="alert"><span>{error}</span><button type="button" className="text-button" onClick={() => void search(query)} disabled={loading}>重新加载</button></div>}
    <div className="knowledge-results" aria-busy={loading} aria-live="polite">
      {loading ? <div className="skeleton-list" aria-label="正在检索项目资料"><i /><i /><i /></div> : matches.length ? <>
        <p className="task-note">{lastQuery ? `“${lastQuery}”的匹配资料` : '需求与规范相关资料'} · {matches.length} 条{error ? '（保留上次结果）' : ''}</p>
        {matches.map((item, index) => <article className="knowledge-entry" key={`${item.path}-${index}`}>
          <div><h2>{item.path}</h2><span>匹配分数 {item.score}</span></div>
          <pre>{item.excerpt}</pre><span className="source-label">引用来源：{item.path}</span>
        </article>)}
      </> : !error && <div className="empty"><strong>没有找到匹配资料</strong><p>试试“完成状态”“接口”或“测试”等关键词，也可以清空搜索框查看默认资料。</p></div>}
    </div>
  </section>
}
