import { buildDynamicRequest } from './autonomous.ts'

export interface ProjectSummary { id: string; name: string; head_revision_id: string; created_at: string; updated_at?: string }
export interface ProjectRevision {
  id: string; project_id: string; number: number; parent_id: string | null;
  files?: Record<string, string>; source_sha256: string; acceptance?: unknown; verification?: unknown;
  run_id?: string | null; created_at: string;
}
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
  runs?: Array<{ id: string; requirement: string; status: string; created_at: string; summary?: string; [key: string]: unknown }>;
}
export interface BrowserSourceFile { name: string; webkitRelativePath?: string; size: number; arrayBuffer(): Promise<ArrayBuffer> }
export interface SourceSelection { files: Record<string, string>; excluded: string[]; suggestedName: string; bytes: number }
const ignoredDirectories = new Set(['node_modules', 'venv', '__pycache__', 'dist', 'build'])
const reserved = /^(con|prn|aux|nul|com[1-9]|lpt[1-9])$/i
const encoder = new TextEncoder()

export function validateProjectPath(path: string): void {
  const parts = path.split('/')
  if (path.length > 160 || parts.length > 8 || !path.endsWith('.py') || path.includes('\\')
    || parts.some((part, index) => {
      const module = index === parts.length - 1 ? part.slice(0, -3) : part
      return !/^[A-Za-z_][A-Za-z0-9_]*$/.test(module) || reserved.test(module)
    })) throw new Error(`不支持的 Python 路径：${path}。请使用英文模块名和项目内相对路径。`)
}

/** Read only selected Python sources; ignored files are never opened. */
export async function readProjectSources(selected: readonly BrowserSourceFile[]): Promise<SourceSelection> {
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
    if (!path.endsWith('.py') || originalParts.some(part => part.startsWith('.') || ignoredDirectories.has(part.toLowerCase()))) {
      excluded.push(path); continue
    }
    validateProjectPath(path)
    const folded = path.toLowerCase()
    if (names.has(folded)) throw new Error(`文件路径重复或大小写冲突：${path}`)
    names.add(folded)
    for (let index = 1; index < originalParts.length; index++) {
      const parent = originalParts.slice(0, index).join('/'), key = parent.toLowerCase()
      if (directories.has(key) && directories.get(key) !== parent) throw new Error(`目录存在大小写冲突：${parent}`)
      directories.set(key, parent)
    }
    if (file.size > 65536) throw new Error(`${path} 超过单文件 64 KiB 限制。`)
    bytes += file.size
    entries.push({ file, path })
  }
  if (roots.size > 1) throw new Error('请一次选择一个项目文件夹。')
  if (!entries.length) throw new Error('没有找到可导入的 Python 源文件；依赖、缓存和隐藏目录会被忽略。')
  if (entries.length > 32 || bytes > 262144) throw new Error('目前支持最多 32 个 Python 文件、合计 256 KiB 的标准库项目。')
  const files: Record<string, string> = Object.create(null)
  bytes = 0
  for (const { file, path } of entries) {
    let content: string
    try { content = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(await file.arrayBuffer()) }
    catch { throw new Error(`无法以 UTF-8 读取 ${path}，请检查文件编码后重试。`) }
    const actualSize = encoder.encode(content).length
    if (content.includes('\0') || actualSize > 65536) throw new Error(`${path} 不是有效的有界 UTF-8 源码。`)
    bytes += actualSize
    if (bytes > 262144) throw new Error('项目源码超过 256 KiB 限制。')
    files[path] = content
  }
  return { files, excluded, bytes, suggestedName: [...roots][0] || entries[0].path.replace(/\.py$/, '') }
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
