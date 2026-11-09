"""Trusted Docker-only launcher. The host must never import candidate modules."""
from pathlib import Path
import os
import re
import runpy
import sys


def main():
    if len(sys.argv) != 2:
        raise ValueError('Expected one relative Python entrypoint')
    entrypoint = sys.argv[1]
    if (not entrypoint.endswith('.py') or len(entrypoint) > 160
            or any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', part)
                   for part in entrypoint[:-3].split('/'))):
        raise ValueError('Invalid entrypoint')
    root = Path('/candidate')
    target = root / entrypoint
    if not target.is_file() or target.is_symlink() or not target.resolve().is_relative_to(root):
        raise ValueError('Entrypoint escapes the frozen candidate')
    os.chdir(root)
    os.environ['PORT'] = '8080'
    os.environ['FLASK_DEBUG'] = '0'
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    sys.path.insert(0, str(root))
    sys.argv = [str(target)]
    runpy.run_path(str(target), run_name='__main__')


if __name__ == '__main__':
    main()
