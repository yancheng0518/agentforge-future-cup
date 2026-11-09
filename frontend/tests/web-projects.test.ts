import assert from 'node:assert/strict'
import test from 'node:test'
import { buildWebImport, latestRevisionPreview, normalizePinnedRequirements, previewExpiry, previewStatus, readProjectSources, safePreviewUrl, sameRequirements } from '../src/projects.ts'
import type { BrowserSourceFile, WebDependency, WebPreview } from '../src/projects.ts'

function file(name: string, content: string, relative = ''): BrowserSourceFile & { reads: number } {
  const bytes = new TextEncoder().encode(content)
  return { name, webkitRelativePath: relative, size: bytes.length, reads: 0,
    async arrayBuffer() { this.reads++; return bytes.buffer as ArrayBuffer } }
}

test('Web import preserves source bytes, extracts root requirements and never reads private or dependency folders', async () => {
  const ignored = [file('.env', 'NEVER_READ_SECRET', 'web/.env'), file('settings.json', 'NEVER_READ_SECRET', 'web/.private/settings.json'),
    file('helper.js', 'NEVER_READ_DEPENDENCY', 'web/node_modules/helper.js'), file('credentials.json', 'NEVER_READ_SECRET', 'web/credentials.json'),
    file('requirements.txt', 'NEVER_READ_NESTED', 'web/other/requirements.txt')]
  const result = await readProjectSources([file('app.py', '# app', 'web/app.py'), file('requirements.txt', 'Flask==3.1.3\r\n', 'web/requirements.txt'),
    file('index.html', '<h1>预览</h1>\r\n', 'web/templates/index.html'), file('app.css', 'body{}', 'web/static/app.css'),
    file('app.js', 'fetch("/api")', 'web/static/app.js'), file('data.json', '{"value":1}', 'web/data.json'), ...ignored], { web: true })
  assert.deepEqual(Object.keys(result.files), ['app.py', 'templates/index.html', 'static/app.css', 'static/app.js', 'data.json'])
  assert.equal(result.files['templates/index.html'], '<h1>预览</h1>\r\n')
  assert.equal(result.requirements, 'Flask==3.1.3\r\n')
  assert.equal(result.hasWebAssets, true)
  assert.equal(Object.hasOwn(result.files, 'requirements.txt'), false)
  assert.ok(ignored.every(item => item.reads === 0))
  assert.doesNotMatch(JSON.stringify(result), /NEVER_READ/)
})

test('Web source selection rejects invalid paths, collision and dependency limits before reading', async () => {
  for (const path of ['../index.html', 'static\\app.js', 'CON.json', 'a/b/c/d/e/f/g.html', 'bad:name.css']) {
    const item = file(path, 'must not read')
    await assert.rejects(readProjectSources([file('app.py', ''), item], { web: true }))
    assert.equal(item.reads, 0)
  }
  const conflict = [file('app.py', ''), file('x.css', 'file'), file('child.js', 'child', 'root/x.css/child.js')]
  await assert.rejects(readProjectSources(conflict, { web: true }), /冲突/)
  assert.ok(conflict.every(item => item.reads === 0))
  const huge = file('requirements.txt', 'x'.repeat(4097))
  await assert.rejects(readProjectSources([file('app.py', ''), huge], { web: true }), /4 KiB/)
  assert.equal(huge.reads, 0)
  await assert.rejects(readProjectSources([file('index.html', '<h1>Static-only</h1>')], { web: true }), /Python/)
})

test('pinned requirements cannot smuggle resolver options, local paths, ranges or Direct URLs', () => {
  assert.deepEqual(normalizePinnedRequirements('# pinned\nFlask==3.1.3\nJinja2==3.1.6'), ['flask==3.1.3', 'jinja2==3.1.6'])
  for (const text of ['Flask>=3', 'x==1\n--index-url https://evil.invalid', 'x @ https://evil.invalid/x.whl', '-e .',
    'x[extra]==1', 'x==1;python_version>"3"', 'x==1\nX==1', '', 'x===1']) assert.throws(() => normalizePinnedRequirements(text))
})

test('Web import sends only a prepared dependency reference and chosen source entrypoint', async () => {
  const selection = await readProjectSources([file('app.py', '# source'), file('requirements.txt', 'Flask==3.1.3')], { web: true })
  const dependency: WebDependency = { dependency_id: 'a'.repeat(64), image_id: 'sha256:' + 'b'.repeat(64), lock_sha256: 'c'.repeat(64), requirements: ['flask==3.1.3'] }
  assert.equal(sameRequirements('Flask==3.1.3\n', dependency), true)
  assert.deepEqual(buildWebImport(selection, dependency, 'app.py', 'Flask==3.1.3'), { dependency_id: 'a'.repeat(64), entrypoint: 'app.py' })
  assert.throws(() => buildWebImport(selection, dependency, 'app.py', 'Flask==3.1.2'), /变化/)
  assert.throws(() => buildWebImport(selection, dependency, 'outside.py', 'Flask==3.1.3'), /入口/)
  assert.throws(() => buildWebImport(selection, dependency, '../outside.py', 'Flask==3.1.3'))
})

const ready: WebPreview = { id: 'preview', status: 'ready', owner: { revision_id: 'v1' }, isolated: true,
  url: 'http://127.0.0.1:32123/_agentforge/' + 'e'.repeat(48) + '/', ready_at: '2099-01-01T00:00:00Z', ttl_seconds: 600 }

test('iframe URL must be a ready isolated capability on a separate loopback origin', () => {
  assert.equal(safePreviewUrl(ready, 'http://127.0.0.1:8002'), ready.url)
  assert.equal(safePreviewUrl(ready, 'http://127.0.0.1:32123'), null)
  for (const status of ['starting', 'failed', 'expired', 'stopped', 'interrupted']) assert.equal(safePreviewUrl({ ...ready, status }, 'http://127.0.0.1:8002'), null)
  assert.equal(safePreviewUrl({ ...ready, isolated: false }, 'http://127.0.0.1:8002'), null)
  for (const url of ['https://example.com/', 'javascript:alert(1)', 'http://localhost:32123/', 'http://127.0.0.1:32123/',
    ready.url + '?redirect=elsewhere', ready.url!.replace('127.0.0.1', 'user:pass@127.0.0.1')]) {
    assert.equal(safePreviewUrl({ ...ready, url }, 'http://127.0.0.1:8002'), null)
  }
})

test('an expired preview is hidden without claiming the backend container has stopped', () => {
  assert.equal(previewExpiry(ready), '2099-01-01T00:10:00.000Z')
  const status = previewStatus(ready, Date.parse('2099-01-01T00:10:01Z'))
  assert.deepEqual(status, { label: '预览时限已到，待确认停止', active: true, usable: false })
  assert.doesNotMatch(previewStatus(ready).label, /测试通过|验收通过/)
  assert.equal(previewStatus({ ...ready, status: 'stopped' }).active, false)
})

test('preview reload selects the newest record belonging to the selected immutable revision', () => {
  const old = { ...ready, id: 'old', created_at: '2026-11-09T01:00:00Z', status: 'stopped' }
  const newest = { ...ready, id: 'new', created_at: '2026-11-09T03:00:00Z' }
  const other = { ...ready, id: 'other', owner: { revision_id: 'v2' }, created_at: '2026-11-09T04:00:00Z' }
  const records = [newest, other, old]
  assert.equal(latestRevisionPreview(records, 'v1')?.id, 'new')
  assert.equal(latestRevisionPreview(records, 'v2')?.id, 'other')
  assert.equal(latestRevisionPreview(records, 'missing'), null)
  assert.deepEqual(records.map(item => item.id), ['new', 'other', 'old'])
})
