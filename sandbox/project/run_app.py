"""Trusted container-only application launcher and bounded persistent-state transport.

This file is copied as text by the host and executed only inside Docker.
The receipt describes an application execution, never an acceptance-test result.
"""
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys

MAX_STATE = 65536
MAX_FILES = 32
DATA = Path('/data')


def valid_path(name):
    return isinstance(name, str) and len(name) <= 160 and all(
        re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}', part)
        and part not in ('.', '..') for part in name.split('/')) and len(name.split('/')) <= 5


def main():
    request = json.loads(Path('/input/request.json').read_text())
    for name, value in request['state'].items():
        if not valid_path(name):
            raise ValueError('Invalid seed state path')
        target = DATA / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(value, validate=True))
    launcher = "import runpy,sys;sys.path.insert(0,'/candidate');p=sys.argv[1];sys.argv=sys.argv[1:];runpy.run_path(p,run_name='__main__')"
    result = {'nonce': request['nonce'], 'exit_code': None, 'completed': False,
              'stdout': '', 'stderr': '', 'state': None, 'state_valid': False, 'reason': ''}
    # A tmpfs bounds even a program that writes endless output. No host rw mount.
    with open('/tmp/app.stdout', 'wb') as out, open('/tmp/app.stderr', 'wb') as err:
        proc = subprocess.Popen([sys.executable, '-I', '-B', '-c', launcher,
                                 '/candidate/' + request['entrypoint'], *request['args']],
                                stdin=subprocess.DEVNULL, stdout=out, stderr=err, cwd='/data',
                                env={'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8',
                                     'AGENTFORGE_DATA_DIR': '/data'})
        try:
            result['exit_code'] = proc.wait(timeout=15)
            result['completed'] = True
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            result['timed_out'] = True
            result['reason'] = 'Application exceeded 15 seconds; data changes were not saved'
    for key, limit in (('stdout', 12000), ('stderr', 8000)):
        with open('/tmp/app.' + key, 'rb') as stream:
            value = stream.read(limit + 1)
        result[key] = value[:limit].decode('utf-8', errors='replace')
        result[key + '_truncated'] = len(value) > limit
    try:
        state, total = {}, 0
        # Never follow a candidate-created link when exporting application data.
        for folder, dirs, names in os.walk(DATA, followlinks=False):
            for name in dirs:
                if (Path(folder) / name).is_symlink():
                    raise ValueError('Linked data directory refused')
            for name in names:
                path = Path(folder) / name
                relative = path.relative_to(DATA).as_posix()
                if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1 or not valid_path(relative):
                    raise ValueError('Unsupported data path or link refused')
                total += path.stat().st_size
                if total > MAX_STATE or len(state) >= MAX_FILES:
                    raise ValueError('Application data exceeds 32 files or 64 KiB')
                state[relative] = base64.b64encode(path.read_bytes()).decode('ascii')
        result['state'], result['state_valid'] = state, True
    except (ValueError, OSError) as exc:
        result['reason'] = str(exc)
    encoded = json.dumps(result, ensure_ascii=False, separators=(',', ':'))
    while len(encoded.encode('utf-8')) > 120000 and (result['stdout'] or result['stderr']):
        key = max(('stdout', 'stderr'), key=lambda item: len(result[item]))
        result[key] = result[key][:len(result[key]) // 2]
        result[key + '_truncated'] = True
        encoded = json.dumps(result, ensure_ascii=False, separators=(',', ':'))
    if len(encoded.encode('utf-8')) > 120000:
        result.update(state=None, state_valid=False, reason='State receipt exceeded transport size limit')
        encoded = json.dumps(result, ensure_ascii=False, separators=(',', ':'))
    print('AGENTFORGE_APPLICATION=' + encoded)


if __name__ == '__main__':
    main()
