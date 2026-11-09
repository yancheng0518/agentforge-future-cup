import { buildDynamicRequest } from './autonomous.ts'

export interface ProjectSummary { id: string; name: string; head_revision_id: string; created_at: string; updated_at?: string }
export interface ProjectRevision {
  id: string; project_id: string; number: number; parent_id: string | null;
  files?: Record<string, string>; source_sha256: string; acceptance?: unknown; verification?: unknown;
  run_id?: string | null; created_at: string; web?: ProjectWebConfig | null;
}
export interface WebDependency {
  dependency_id: string; image_id: string; lock_sha256: string; requirements: string[];
  wheels?: Array<{ name: string; version: string; sha256: string }>; [key: string]: unknown;
}
export interface ProjectWebConfig { schema: string; entrypoint: string; dependency_id: string; image_id: string; lock_sha256: string; requirements: string[] }
export interface DependencyJob { id: string; status: 'running' | 'succeeded' | 'failed' | 'interrupted'; result?: WebDependency; error?: string; created_at?: string; [key: string]: unknown }
export interface WebPreview { id: string; status: string; revision_id?: string; owner?: { revision_id?: string; [key: string]: unknown }; url?: string; expires_at?: string; ready_at?: string; ttl_seconds?: number; created_at?: string; reason?: string; isolated?: boolean; [key: string]: unknown }
export interface ProjectProfile {
  id: string; version: number; name: string; description: string; prompt: string;
  capabilities: string[]; sha256?: string; enabled: boolean;
}
export interface ProjectKnowledge {
  id: string; version: number; title: string; content: string; sha256?: string; hash?: string; created_at?: string;
}
export interface ProjectExecution {
  id: string; revision_id: string; status: string; exit_code?: number | null; stdout?: string; stderr?: string;
  isolated?: boolean; duration_ms?: number; source_sha256?: string; entrypoint?: string; args?: string[];
  created_at?: string; reason?: string; [key: string]: unknown;
}
export interface ProjectDetail extends ProjectSummary {
  revisions: ProjectRevision[]; knowledge: ProjectKnowledge[]; executions: ProjectExecution[];
  previews?: WebPreview[];
  runs?: Array<{ id: string; requirement: string; status: string; created_at: string; summary?: string; [key: string]: unknown }>;
}
export interface BrowserSourceFile { name: string; webkitRelativePath?: string; size: number; arrayBuffer(): Promise<ArrayBuffer> }
export interface SourceSelection { files: Record<string, string>; excluded: string[]; suggestedName: string; bytes: number; requirements?: string; hasWebAssets?: boolean }
const ignoredDirectories = new Set(['node_modules', 'venv', '__pycache__', 'dist', 'build'])
const reserved = /^(con|prn|aux|nul|com[1-9]|lpt[1-9])$/i
const encoder = new TextEncoder()
const webExtensions = /\.(py|html|css|js|json)$/

export function validateWebProjectPath(path: string): void {
  const parts = path.split('/')
  if (path.length > 160 || parts.length > 6 || !webExtensions.test(path) || path.includes('\\')
    || parts.some(part => !/^[A-Za-z_][A-Za-z0-9_.-]{0,63}$/.test(part) || part.startsWith('.') || reserved.test(part.split('.')[0]))) {
    throw new Error(`不支持的 Web 文件路径：${path}。请使用项目内的英文相对路径。`)
  }
  if (path.endsWith('.py')) validateProjectPath(path)
}

export function validateProjectPath(path: string): void {
  const parts = path.split('/')
  if (path.length > 160 || parts.length > 8 || !path.endsWith('.py') || path.includes('\\')
    || parts.some((part, index) => {
      const module = index === parts.length - 1 ? part.slice(0, -3) : part
      return !/^[A-Za-z_][A-Za-z0-9_]*$/.test(module) || reserved.test(module)
    })) throw new Error(`不支持的 Python 路径：${path}。请使用英文模块名和项目内相对路径。`)
}

/** Read source text only. Dependencies are separate; ignored files are never opened. */
export async function readProjectSources(selected: readonly BrowserSourceFile[], options: { web?: boolean } = {}): Promise<SourceSelection> {
  if (!selected.length) throw new Error('请先选择 Python 文件或项目文件夹。')
  const excluded: string[] = [], entries: Array<{ file: BrowserSourceFile; path: string }> = []
  const roots = new Set<string>(), names = new Set<string>(), directories = new Map<string, string>()
  let bytes = 0
  for (const file of selected) {
    const original = file.webkitRelativePath || file.name
    if (original.includes('\\') || original.startsWith('/') || original.split('/').some(part => part === '..' || part === '.')) throw new Error('所选文件包含不安全的相对路径。')
    const originalParts = original.split('/')
    if (file.webkitRelativePath && originalParts.length > 1) roots.add(originalParts.shift()!)
    const path = originalParts.join('/')
    const dependencyFile = options.web && path === 'requirements.txt'
    const supported = options.web ? webExtensions.test(path) || dependencyFile : path.endsWith('.py')
    if (!supported || originalParts.some(part => part.startsWith('.') || ignoredDirectories.has(part.toLowerCase()))
      || /^(credentials?|secrets?|tokens?|service-account)[._-]/i.test(originalParts[originalParts.length - 1])) {
      excluded.push(path); continue
    }
    if (!dependencyFile) (options.web ? validateWebProjectPath : validateProjectPath)(path)
    const folded = path.toLowerCase()
    if (names.has(folded)) throw new Error(`文件路径重复或大小写冲突：${path}`)
    names.add(folded)
    for (let index = 1; index < originalParts.length; index++) {
      const parent = originalParts.slice(0, index).join('/'), key = parent.toLowerCase()
      if (directories.has(key) && directories.get(key) !== parent) throw new Error(`目录存在大小写冲突：${parent}`)
      directories.set(key, parent)
    }
    if (dependencyFile && file.size > 4096) throw new Error('requirements.txt 超过 4 KiB 限制。')
    if (file.size > 65536) throw new Error(`${path} 超过单文件 64 KiB 限制。`)
    bytes += file.size
    entries.push({ file, path })
  }
  if (roots.size > 1) throw new Error('请一次选择一个项目文件夹。')
  if ([...names].some(name => directories.has(name))) throw new Error('所选项目存在文件与目录路径冲突。')
  if (!entries.some(entry => entry.path.endsWith('.py'))) throw new Error('没有找到可导入的 Python 源文件；依赖、缓存和隐藏目录会被忽略。')
  if (entries.filter(entry => entry.path !== 'requirements.txt').length > 32 || bytes > 262144) throw new Error('目前支持最多 32 个源文件、合计 256 KiB 的 Python 项目。')
  const files: Record<string, string> = Object.create(null)
  bytes = 0; let requirements: string | undefined
  for (const { file, path } of entries) {
    let content: string
    try { content = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(await file.arrayBuffer()) }
    catch { throw new Error(`无法以 UTF-8 读取 ${path}，请检查文件编码后重试。`) }
    const actualSize = encoder.encode(content).length
    if (content.includes('\0') || actualSize > (path === 'requirements.txt' ? 4096 : 65536)) throw new Error(`${path} 不是有效的有界 UTF-8 源码。`)
    bytes += actualSize
    if (bytes > 262144) throw new Error('项目源码超过 256 KiB 限制。')
    if (path === 'requirements.txt') requirements = content.replace(/^\ufeff/, '')
    else files[path] = content
  }
  return { files, excluded, bytes, suggestedName: [...roots][0] || entries.find(entry => entry.path.endsWith('.py'))!.path.replace(/\.py$/, ''),
    ...(options.web ? { requirements, hasWebAssets: Object.keys(files).some(path => !path.endsWith('.py')) } : {}) }
}

export function normalizePinnedRequirements(text: string): string[] {
  if (encoder.encode(text).length > 4096 || text.includes('\0')) throw new Error('依赖清单最多 4 KiB。')
  const values = new Map<string, string>()
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim()
    if (!line || line.startsWith('#')) continue
    const match = /^([A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9])?)==([0-9][A-Za-z0-9.!+_-]{0,79})$/.exec(line)
    if (!match) throw new Error('每行填写 name==version；不支持版本范围、下载地址、extras 或安装选项。')
    const name = match[1].toLowerCase().replace(/[-_.]+/g, '-')
    if (values.has(name)) throw new Error('依赖清单包含重复包名。')
    values.set(name, match[2])
  }
  if (!values.size || values.size > 16) throw new Error('请填写 1 至 16 个固定版本依赖。')
  return [...values].sort(([a], [b]) => a.localeCompare(b, 'en')).map(([name, version]) => `${name}==${version}`)
}

export function sameRequirements(text: string, dependency: WebDependency): boolean {
  try { return JSON.stringify(normalizePinnedRequirements(text)) === JSON.stringify([...dependency.requirements].sort()) }
  catch { return false }
}

export function buildWebImport(selection: SourceSelection, dependency: WebDependency, entrypoint: string, text: string) {
  if (!sameRequirements(text, dependency)) throw new Error('依赖内容已变化，请先准备对应的依赖版本。')
  validateProjectPath(entrypoint)
  if (!Object.hasOwn(selection.files, entrypoint)) throw new Error('请选择已导入项目中的 Python 入口文件。')
  if (!/^[0-9a-f]{64}$/.test(dependency.dependency_id)) throw new Error('依赖版本标识无效，请重新准备。')
  return { dependency_id: dependency.dependency_id, entrypoint }
}

export function previewExpiry(record: WebPreview): string | null {
  if (record.expires_at && Number.isFinite(Date.parse(record.expires_at))) return record.expires_at
  if (record.ready_at && Number.isFinite(Date.parse(record.ready_at)) && typeof record.ttl_seconds === 'number' && record.ttl_seconds > 0)
    return new Date(Date.parse(record.ready_at) + record.ttl_seconds * 1000).toISOString()
  return null
}

export function latestRevisionPreview(records: readonly WebPreview[], revisionId: string): WebPreview | null {
  return records.filter(item => (item.revision_id || item.owner?.revision_id) === revisionId)
    .sort((left, right) => (Date.parse(right.created_at || '') || 0) - (Date.parse(left.created_at || '') || 0))[0] || null
}

export function previewStatus(record: WebPreview, now = Date.now()): { label: string; active: boolean; usable: boolean } {
  const states: Record<string, string> = { starting: '正在启动', running: '预览运行中', ready: '预览运行中',
    stopped: '已停止', expired: '已到期', failed: '启动失败', blocked: '启动受阻', interrupted: '服务已中断' }
  const active = ['starting', 'running', 'ready'].includes(record.status)
  const expiry = previewExpiry(record)
  if (active && expiry && Date.parse(expiry) <= now) return { label: '预览时限已到，待确认停止', active: true, usable: false }
  return { label: states[record.status] || '状态待确认', active, usable: ['running', 'ready'].includes(record.status) && record.isolated === true }
}

export function safePreviewUrl(record: WebPreview, workbenchOrigin: string): string | null {
  if (!previewStatus(record).usable || typeof record.url !== 'string') return null
  try {
    const url = new URL(record.url)
    if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port || url.origin === workbenchOrigin
      || url.username || url.password || url.hash || !/^\/_agentforge\/[a-f0-9]{48}\/$/.test(url.pathname) || url.search) return null
    return url.href
  } catch { return null }
}

export function parseExecutionArgs(value: string): string[] {
  // Each line is one literal argument. Quotes and spaces are not shell syntax.
  const args = value.replace(/\r\n/g, '\n').split('\n').filter(line => line.length > 0)
  if (args.length > 32 || args.some(arg => arg.includes('\0') || Array.from(arg).length > 1000)) throw new Error('最多填写 32 个参数，每行一个，单个参数不超过 1000 字符。')
  return args
}

export function buildProjectRunRequest(baseRevisionId: string, requirement: string, idempotencyKey: string) {
  if (!baseRevisionId) throw new Error('请先选择当前项目版本。')
  if (!idempotencyKey) throw new Error('本次提交缺少唯一标识，请重新打开项目后重试。')
  return { base_revision_id: baseRevisionId, ...buildDynamicRequest(requirement), idempotency_key: idempotencyKey }
}

export interface RequestIntent { signature: string; key: string }
export function retainRequestIntent(previous: RequestIntent | null, signature: string, newKey: () => string): RequestIntent {
  return previous?.signature === signature ? previous : { signature, key: newKey() }
}

export function buildExecutionRequest(revisionId: string, entrypoint: string, args: string[], idempotencyKey: string) {
  if (!revisionId || !idempotencyKey) throw new Error('程序运行缺少版本或提交标识，请重新读取项目。')
  validateProjectPath(entrypoint)
  if (args.length > 32 || args.some(arg => arg.includes('\0') || Array.from(arg).length > 1000)) throw new Error('程序参数超出当前运行限制。')
  return { revision_id: revisionId, entrypoint, args: [...args], idempotency_key: idempotencyKey }
}

export function executionSummary(record: ProjectExecution): { text: string; tone: string } {
  if (record.isolated !== true || record.status === 'blocked') return { text: '运行未确认', tone: 'warning' }
  if (record.exit_code === 0) return { text: '运行结束 · 退出码 0', tone: 'neutral' }
  if (typeof record.exit_code === 'number') return { text: `运行报错 · 退出码 ${record.exit_code}`, tone: 'warning' }
  return { text: '尚无完整运行回执', tone: 'warning' }
}

export function awaitsProjectPublication(run: { project_id?: unknown; status: string; publication?: unknown; events?: readonly { type: string }[] }): boolean {
  if (typeof run.project_id !== 'string' || run.status !== 'succeeded') return false
  const publication = run.publication && typeof run.publication === 'object' ? run.publication as Record<string, unknown> : {}
  return typeof publication.status !== 'string' || !(run.events || []).some(event => event.type === 'terminal')
}

export function profileCapabilities(value: string): string[] {
  return [...new Set(value.split(/[,，\n]/).map(item => item.trim()).filter(Boolean))]
}
