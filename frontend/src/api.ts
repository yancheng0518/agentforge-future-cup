export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
    signal: init?.signal ?? AbortSignal.timeout(20000),
  })
  const contentType = response.headers.get('content-type') || ''
  if (!contentType.includes('application/json')) throw new Error('无法连接后端，请确认 FastAPI 已在 8000 端口启动。')
  const body = await response.json()
  if (!response.ok) {
    const detail = body.detail ?? body.message ?? `请求失败 (${response.status})`
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  return body as T
}

export function post<T>(path: string, body: unknown = {}) {
  return api<T>(path, { method: 'POST', body: JSON.stringify(body) })
}
