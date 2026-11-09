"""Exercise real Docker preview across an idle interval, with no paid model calls."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from agentforge.runtime import Runtime
from agentforge.web_preview import WebPreviewManager


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision-json', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--idle-seconds', type=int, default=105)
    args = parser.parse_args()
    if not 90 <= args.idle_seconds <= 180:
        parser.error('Idle interval must be 90..180 seconds')
    output = Path(args.output).resolve()
    intent = output.with_suffix('.intent.json')
    if output.exists() or intent.exists():
        raise RuntimeError('Do not overwrite an existing validation')
    revision = json.loads(Path(args.revision_json).read_text(encoding='utf-8'))
    runtime = Runtime(ROOT, ROOT / '.agentforge/web-validation-20261109')
    manager = WebPreviewManager(runtime, runtime.data_dir / 'previews')
    declaration = {'classification': 'platform_preview_idle_regression', 'model_calls': 0,
                   'source_sha256': revision['source_sha256'], 'web': revision['web'],
                   'idle_seconds': args.idle_seconds,
                   'implementation_sha256': hashlib.sha256((ROOT / 'backend/agentforge/web_preview.py').read_bytes()).hexdigest()}
    with intent.open('x', encoding='utf-8') as stream:
        json.dump(declaration, stream, ensure_ascii=False, indent=2)

    async def boot_id():
        if runtime.settings.get('docker_backend') != 'wsl':
            return None
        prefix = runtime._docker_command()[:-1]
        code, value, _ = await runtime._command([*prefix, 'cat', '/proc/sys/kernel/random/boot_id'], timeout=15)
        if code:
            raise RuntimeError('WSL boot identity unavailable')
        return value.strip()

    result = dict(declaration)
    try:
        result['start'] = await manager.start(revision['files'], image_id=revision['web']['image_id'],
            entrypoint=revision['web']['entrypoint'], owner={'kind': 'platform_idle_validation'}, ttl_seconds=240)
        if result['start']['status'] != 'ready':
            raise RuntimeError('Preview did not start')
        result['boot_before'] = await boot_id()
        print(json.dumps({'stage': 'idle_started', 'preview_id': result['start']['id'], 'seconds': args.idle_seconds}), flush=True)
        begin = time.monotonic()
        # No Docker/WSL/HTTP call from this verifier during the idle interval.
        await asyncio.sleep(args.idle_seconds)
        result['elapsed_idle_seconds'] = round(time.monotonic() - begin, 3)
        result['boot_after'] = await boot_id()
        result['after_idle'] = await manager.request(result['start']['id'], 'GET', '/health')
        result['passed'] = (result['boot_before'] == result['boot_after']
                            and result['after_idle'].get('ok') is True
                            and result['after_idle'].get('status') == 200
                            and json.loads(result['after_idle'].get('body_text', '{}')) == {'status': 'ok'})
    except Exception as exc:
        result.update(passed=False, exception_type=type(exc).__name__)
    finally:
        try:
            if result.get('start'):
                result['stop'] = await manager.stop(result['start']['id'], reason='validation_complete')
            await manager.close()
            stopped = result.get('stop', {})
            lease = stopped.get('wsl_lease', {})
            result['cleanup_passed'] = (stopped.get('container_removed') is True and
                (runtime.settings.get('docker_backend') != 'wsl' or
                 (lease.get('status') == 'released' and lease.get('client_reaped') is True)))
            result['passed'] = result.get('passed', False) and result['cleanup_passed']
        except Exception as exc:
            result.update(passed=False, cleanup_exception_type=type(exc).__name__)
        with output.open('x', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({'passed': result['passed'], 'preview_id': result.get('start', {}).get('id')}))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
