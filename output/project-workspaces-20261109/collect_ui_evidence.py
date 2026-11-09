"""Read-only verification after the browser import/run/download checks.

No model or application execution. Creates a new evidence file, never overwrites.
"""
from pathlib import Path
import hashlib
import json
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
BASE = 'http://127.0.0.1:8002'
PROJECT = '4da12061fc7c43fe965b630637b86a2c'
IMPORTED = '354c118a26f14ed4a7b67c98181d2f54'
EXECUTION = '35234b6664df40c592a3be435f2f44a5'


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as response:
        return json.load(response)


project = get('/api/projects/' + PROJECT)
imported = get('/api/projects/' + IMPORTED)
revision = get('/api/projects/' + PROJECT + '/revisions/' + project['head_revision_id'])
receipt = json.loads((ROOT / '.agentforge/projects/executions' / EXECUTION / 'record.json').read_text(encoding='utf-8'))
assert receipt['status'] == 'succeeded' and receipt['isolated'] and receipt['container_invoked']
assert receipt['exit_code'] == 0 and receipt['data_saved']
assert receipt['data_before_sha256'] == receipt['data_after_sha256']
assert json.loads(receipt['stdout']) == [{'id': 2, 'title': 'Bus', 'cents': 300, 'category': 'travel'}]
with zipfile.ZipFile(OUT / 'ui-downloaded-v3.zip') as archive:
    assert archive.testzip() is None
    meta = json.loads(archive.read('PROJECT_REVISION.json'))
    assert meta['id'] == revision['id'] and meta['source_sha256'] == revision['source_sha256']
    assert set(archive.namelist()) == set(revision['files']) | {'PROJECT_REVISION.json', 'RUNNING.txt'}
    assert all(archive.read(name).decode('utf-8') == content for name, content in revision['files'].items())
report = {
    'base_url': BASE, 'project': project, 'ui_import': imported,
    'execution': receipt, 'download_verified': True,
    'download_sha256': hashlib.sha256((OUT / 'ui-downloaded-v3.zip').read_bytes()).hexdigest(),
    'browser_observations': {
        'ui_import_label': '导入版本 · 未验收', 'history_count': 25,
        'mobile_viewport_width': 390, 'document_scroll_width': 390,
        'console_errors_and_warnings': [], 'replay_is_read_only': True,
    },
    'model_calls_by_this_verification': 0,
}
with (OUT / 'ui-delivery-evidence.json').open('x', encoding='utf-8') as stream:
    json.dump(report, stream, ensure_ascii=False, indent=2)
print(json.dumps({'passed': True, 'execution_id': EXECUTION, 'version': revision['number'], 'model_calls': 0}))
