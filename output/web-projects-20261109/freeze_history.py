"""Freeze historical evidence and SQLite rows before this new Web batch."""
from pathlib import Path
from contextlib import closing
import hashlib
import json
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'scripts'))
from check_chat_preservation import read_database

manifest = {'files': {}, 'databases': {}, 'excluded_mutable_environment': ['.agentforge/wsl'],
            'purpose': 'before-web-projects-20261109'}
for parent in (ROOT / '.agentforge', ROOT / 'output'):
    for path in sorted(parent.rglob('*')):
        relative = path.relative_to(ROOT).as_posix()
        if (not path.is_file() or path.is_relative_to(OUT) or relative.startswith('.agentforge/wsl/')
                or path.suffix in ('.sqlite3', '.sqlite', '.db', '.log', '.pyc')
                or path.name.endswith(('-wal', '-shm', '-journal')) or '__pycache__' in path.parts):
            continue
        manifest['files'][relative] = hashlib.sha256(path.read_bytes()).hexdigest()
for index, relative in enumerate(('.agentforge/agentforge.sqlite3', '.agentforge/autonomous/agentforge.sqlite3',
                                   '.agentforge/autonomous-validation-20261108/agentforge.sqlite3',
                                   '.agentforge/projects/projects.sqlite3', '.agentforge/projects/requests.sqlite3')):
    source = ROOT / relative
    backup = OUT / f'history-before-{index}.sqlite3'
    if backup.exists():
        raise RuntimeError('Never overwrite a history backup')
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro', uri=True)) as db, closing(sqlite3.connect(backup)) as destination:
        db.backup(destination)
        destination.execute('PRAGMA journal_mode=DELETE')
    tables = read_database(backup)
    manifest['databases'][relative] = {'backup': backup.relative_to(ROOT).as_posix(),
        'allow_additions': relative in ('.agentforge/autonomous/agentforge.sqlite3', '.agentforge/projects/projects.sqlite3', '.agentforge/projects/requests.sqlite3'),
        'tables': {name: {**{key: value for key, value in table.items() if key != 'row_hashes'},
                          'records_sha256': table['row_hashes']} for name, table in tables.items()}}
with (OUT / 'preservation-before.json').open('x', encoding='utf-8') as stream:
    json.dump(manifest, stream, indent=2, ensure_ascii=False)
print(json.dumps({'files': len(manifest['files']), 'databases': len(manifest['databases'])}))
