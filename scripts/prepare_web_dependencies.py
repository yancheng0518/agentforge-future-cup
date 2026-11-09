"""Download pinned official-PyPI wheels in Docker and install offline in Docker.

No model calls, project source mounts, host dependency installation or old output
replacement. Downloads and builds may consume network traffic and local disk.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from agentforge.runtime import Runtime, RuntimeErrorSafe
from agentforge.web_dependencies import WebDependencies


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--requirement', action='append', help='Exactly pinned name==version; can repeat')
    parser.add_argument('--requirements-file', type=Path)
    parser.add_argument('--output', type=Path, help='Optional NEW public descriptor JSON')
    args = parser.parse_args()
    if bool(args.requirement) == bool(args.requirements_file):
        parser.error('Choose --requirement or --requirements-file')
    if args.output and args.output.exists():
        parser.error('Output exists; refuse to overwrite')
    pins = args.requirement if args.requirement else args.requirements_file.read_text(encoding='utf-8-sig')
    runtime = Runtime(ROOT, ROOT / '.agentforge/autonomous')
    manager = WebDependencies(runtime, ROOT / '.agentforge/web/dependencies')
    try:
        result = await manager.prepare(pins)
    except RuntimeErrorSafe as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
