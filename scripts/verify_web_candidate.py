"""Independently check frozen Web source in Docker; never import it on the host."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from agentforge.runtime import Runtime
from agentforge.autonomous_sandbox import AutonomousSandbox


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate-json', required=True)
    parser.add_argument('--web-json', required=True)
    parser.add_argument('--acceptance', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--label', default='independent_http_acceptance')
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if output.exists():
        raise RuntimeError('Never overwrite a verification result')
    candidate = json.loads(Path(args.candidate_json).read_text(encoding='utf-8'))
    web = json.loads(Path(args.web_json).read_text(encoding='utf-8'))
    web = web.get('web', web)
    acceptance = Path(args.acceptance).read_text(encoding='utf-8')
    bundle = {'requirement': args.label, 'files': candidate['files'], 'acceptance': acceptance,
              'provenance': {'kind': 'natural', 'input_origin': 'independent_frozen_candidate',
                             'acceptance_origin': 'maintainer_provided', 'web': web}}
    runtime = Runtime(ROOT, ROOT / '.agentforge/web-validation-20261109')
    sandbox = AutonomousSandbox(runtime)
    identity = 'external-' + uuid4().hex
    await sandbox.prepare(identity, bundle)
    result = await sandbox.test(identity)
    record = {'run_id': identity, 'classification': args.label, 'model_calls': 0,
              'candidate_sha256': await sandbox.fingerprint(identity),
              'acceptance_sha256': hashlib.sha256(acceptance.encode()).hexdigest(),
              'result': result,
              'passed': result['status'] == 'passed' and result.get('test_count', 0) > 0
                        and result.get('isolated') is True and result.get('exit_code') == 0}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2)
    print(json.dumps({key: record[key] for key in ('run_id', 'classification', 'passed')}))
    return 0 if record['passed'] else 1


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
