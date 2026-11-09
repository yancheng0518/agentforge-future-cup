"""Declared maintainer fixture: real Docker/proxy plumbing, zero model calls."""
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
from agentforge.runtime import Runtime
from agentforge.web_preview import WebPreviewManager

SOURCE = '''from http.server import BaseHTTPRequestHandler, HTTPServer
import json
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        data = json.dumps({'message': 'Declared plumbing fixture', 'path': self.path}).encode()
        self.send_response(404 if self.path == '/missing' else 200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)
HTTPServer(('127.0.0.1', 8080), Handler).serve_forever()
'''


async def main():
    output = Path(__file__).resolve().parent / 'real-docker-smoke.json'
    if output.exists():
        raise RuntimeError('Refusing to overwrite original evidence')
    runtime = Runtime(ROOT, ROOT / '.agentforge/web-preview-platform-20261109')
    code, image, _ = await runtime._command(runtime._docker_command('image','inspect','--format','{{.Id}}', runtime.settings['image']), timeout=15)
    if code:
        raise RuntimeError('Test image missing')
    manager = WebPreviewManager(runtime, runtime.data_dir / 'previews')
    start = await manager.start({'app.py':SOURCE}, image_id=image.strip(), ttl_seconds=120,
                                owner={'kind':'declared_platform_smoke','model_calls':0,'seeded_defect':False})
    result = {'model_calls':0,'seeded_defect':False,'classification':'maintainer_fixture_platform_smoke',
              'start':start,'passed':False}
    try:
        if start['status'] == 'ready':
            result['http_success'] = await manager.request(start['id'], 'GET', '/')
            result['http_expected_missing'] = await manager.request(start['id'], 'GET', '/missing')
            async def http(path, cookie=''):
                reader, writer = await asyncio.open_connection('127.0.0.1', start['port'])
                writer.write(f'GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{start["port"]}\r\n{cookie}\r\n'.encode())
                await writer.drain()
                data = (await reader.read()).decode()
                writer.close()
                await writer.wait_closed()
                return data
            rejected = await http('/')
            opening = await http(start['url'].split(str(start['port']),1)[1])
            cookie = next(line for line in opening.split('\r\n') if line.startswith('Set-Cookie: ')).split(': ',1)[1].split(';',1)[0]
            served = await http('/', 'Cookie: ' + cookie + '\r\n')
            result['browser_proxy_transport'] = {'unauthorized_status':rejected.split('\r\n',1)[0],
                'bootstrap_status':opening.split('\r\n',1)[0], 'authorized_status':served.split('\r\n',1)[0],
                'csp_present':'Content-Security-Policy:' in served, 'fixture_present':'Declared plumbing fixture' in served,
                'method':'raw HTTP transport check; not a browser rendering claim'}
            result['passed'] = (result['http_success'].get('status') == 200
                and result['http_expected_missing'].get('status') == 404 and served.startswith('HTTP/1.1 200')
                and rejected.startswith('HTTP/1.1 403'))
    finally:
        result['stop'] = await manager.stop(start['id'])
        with output.open('x',encoding='utf-8') as stream:
            json.dump(result,stream,ensure_ascii=False,indent=2)
    print(json.dumps({'passed':result['passed'],'preview_id':start['id'],'startup_status':start['status'],
        'failure_kind':start.get('failure_kind'),'reason':start.get('reason'),'model_calls':0}))
    if not result['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    asyncio.run(main())
