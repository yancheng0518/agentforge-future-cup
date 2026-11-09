"""Package this UI verification batch without changing or executing evidence."""
from pathlib import Path
import hashlib
import json
import sqlite3
import sys
import zipfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from export_reproduction import collect_files, checked_bytes, configured_secrets, reject_secret


def main():
    outdir = ROOT / 'output/autonomous-chat-20261108'
    archive_path = ROOT / 'output/agentforge-autonomous-chat-20261108.zip'
    if archive_path.exists():
        raise RuntimeError('Choose a new archive name; existing archives are retained')
    snapshot = outdir / 'autonomous-api-final.sqlite3'
    if snapshot.exists():
        raise RuntimeError('Final DB snapshot already exists')
    with sqlite3.connect((ROOT / '.agentforge/autonomous/agentforge.sqlite3').as_uri() + '?mode=ro', uri=True) as source:
        with sqlite3.connect(snapshot) as dest:
            source.backup(dest)
    selected, skipped, _ = collect_files(ROOT, [])
    for relative in ['.agentforge/autonomous/runs', '.agentforge/autonomous-chat-posthoc-20261108',
                     'output/autonomous-chat-20261108']:
        for path in sorted((ROOT / relative).rglob('*')):
            if not path.is_file() or '__pycache__' in path.parts or path.suffix in {'.pyc', '.pyo'}:
                continue
            if path.name.endswith(('-wal', '-shm', '-journal')):
                continue
            selected[path.relative_to(ROOT).as_posix()] = (path, 'evidence')
    secrets = configured_secrets(ROOT)
    records = {}
    for name, (path, kind) in sorted(selected.items()):
        data = checked_bytes(ROOT, path, secrets)
        records[name] = {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data), 'kind': kind}
    manifest = {'schema': 'agentforge-autonomous-chat-reproduction-v1',
                'created_at': datetime.now(timezone.utc).isoformat(), 'files': records,
                'skipped_source_counts': skipped, 'model_calls': 0, 'candidate_execution': False,
                'secret_scan': 'Exact configured credential bytes absent; dotenv excluded',
                'database_snapshot': snapshot.relative_to(ROOT).as_posix(),
                'instructions': 'See docs/autonomous-chat-verification.md. All attempts retained, including rejected success and posthoc script error.'}
    metadata = json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8')
    reject_secret(metadata, secrets)
    with zipfile.ZipFile(archive_path, 'x', zipfile.ZIP_DEFLATED) as archive:
        for name, (path, _) in sorted(selected.items()):
            data = checked_bytes(ROOT, path, secrets)
            if hashlib.sha256(data).hexdigest() != records[name]['sha256']:
                raise RuntimeError('A selected file changed; partial archive retained')
            archive.writestr(name, data)
        archive.writestr('CHAT_REPRODUCTION_MANIFEST.json', metadata)
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.testzip() is None
        for name, record in records.items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == record['sha256']
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    archive_path.with_suffix('.zip.sha256').write_text(digest + '  ' + archive_path.name + '\n', encoding='ascii')
    report = {'verified': True, 'files': len(records), 'bytes': archive_path.stat().st_size,
              'sha256': digest, 'archive': archive_path.relative_to(ROOT).as_posix()}
    (outdir / 'archive-manifest.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
