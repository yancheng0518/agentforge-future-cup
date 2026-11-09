"""Run the maintainer's initial HTTP regression only inside Docker, no model."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from agentforge.runtime import Runtime
from agentforge.autonomous_sandbox import AutonomousSandbox
from agentforge.web_config import validate_web_config
from agentforge.web_dependencies import WebDependencies


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dependency-id', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    target = Path(args.output).resolve()
    if target.exists():
        raise RuntimeError('Never overwrite a verification result')
    runtime = Runtime(ROOT, ROOT / '.agentforge/web-validation-20261109')
    dependencies = WebDependencies(runtime, ROOT / '.agentforge/web/dependencies')
    descriptor = dependencies.resolve(args.dependency_id)
    web = validate_web_config({'schema': 'python-web-v1', 'entrypoint': 'app.py',
        **{key: descriptor[key] for key in ('dependency_id', 'image_id', 'lock_sha256', 'requirements')}})
    files = {'app.py': (ROOT / 'examples/web_board/app.py').read_text(encoding='utf-8')}
    bundle = {'requirement': 'Maintainer Web baseline health regression; no model calls.', 'files': files,
        'acceptance': (ROOT / 'benchmarks/web-projects/acceptance_baseline.py.txt').read_text(encoding='utf-8'),
        'provenance': {'kind': 'natural', 'input_origin': 'maintainer_working_baseline', 'web': web}}
    identity = 'baseline-' + uuid4().hex
    sandbox = AutonomousSandbox(runtime)
    await sandbox.prepare(identity, bundle)
    result = await sandbox.test(identity)
    record = {'run_id': identity, 'result': result, 'model_calls': 0,
              'classification': 'platform_http_baseline_validation',
              'passed': result['status'] == 'passed' and result['test_count'] == 3 and result['isolated']}
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('x', encoding='utf-8') as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2)
    print(json.dumps({'run_id': identity, 'passed': record['passed'], 'test_count': result['test_count'], 'status': result['status']}))
    return 0 if record['passed'] else 1


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
