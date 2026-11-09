"""Explicit fake Docker transport. No downloads, installs or candidate execution."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from agentforge.runtime import RuntimeErrorSafe
from agentforge.web_dependencies import WebDependencies, inspect_wheels, normalize_requirements

spec = importlib.util.spec_from_file_location('trusted_download_wheels', ROOT / 'sandbox/web/download_wheels.py')
downloader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(downloader)


def wheel(path, name='Flask', version='3.1.3', requires=()):
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr(f'{name}-{version}.dist-info/METADATA',
                         f'Metadata-Version: 2.3\nName: {name}\nVersion: {version}\n' + ''.join(f'Requires-Dist: {item}\n' for item in requires))
        archive.writestr(name.lower() + '/__init__.py', "raise RuntimeError('MUST NEVER IMPORT WHEEL ON HOST')")


def test_requirements_normalize_exact_pins():
    assert normalize_requirements('# test\nFlask==3.1.3\n Jinja2==3.1.6\n') == ['flask==3.1.3', 'jinja2==3.1.6']


@pytest.mark.parametrize('value', [
    '', ['Flask>=3'], ['Flask==3.*'], ['flask==3.1.3', 'Flask==3.1.3'],
    ['x @ https://example.org/pkg.whl'], ['git+https://example.org/repo'], ['--index-url=x'],
    ['-e .'], ['../x==1'], ['x[extra]==1'], ['x==1;python_version>="3"'], ['x==1\n--index-url=x'],
    ['x===1'], ['flask==3.1.3 # inline'], ['x==1 ' * 5000], {'x': '1'},
])
def test_reject_requirement_language_that_could_add_sources_options_or_ambiguity(value):
    with pytest.raises(RuntimeErrorSafe):
        normalize_requirements(value)


@pytest.mark.parametrize('url', ['https://evil.invalid/x.whl', 'http://pypi.org/simple', 'file:///tmp/x',
                                'https://pypi.org:444/x', 'https://user:pass@pypi.org/x',
                                'https://files.pythonhosted.org.evil.invalid/x', '//pypi.org/x'])
def test_downloader_blocks_unknown_sources_before_any_transport(url):
    observed, sent = set(), []
    class Session:
        def send(self, request, **kwargs):
            sent.append(request.url)
    downloader.install_guard(Session, observed)
    request = type('Request', (), {'url': url})()
    with pytest.raises(RuntimeError):
        Session().send(request)
    assert not observed and not sent


def test_downloader_catches_redirect_send_and_removes_proxies():
    observed, sent = set(), []
    class Session:
        def send(self, request, **kwargs):
            sent.append((request.url, kwargs))
            if request.url.endswith('/redirect'):
                return self.send(type('Request', (), {'url': 'https://external.invalid/package.whl'})())
            return 'ok'
    downloader.install_guard(Session, observed)
    request = type('Request', (), {'url': 'https://pypi.org/redirect'})()
    with pytest.raises(RuntimeError):
        Session().send(request, proxies={'https': 'https://proxy.invalid'})
    assert observed == {'pypi.org'} and len(sent) == 1 and sent[0][1]['proxies'] == {}


@pytest.mark.parametrize('source', ['https://files.pythonhosted.org/package.tar.gz', 'file:///request/file.whl',
                                  'git+https://github.com/example/project'])
def test_transitive_direct_requirement_rejected_before_candidate_preparation(source):
    class Requirement:
        def __init__(self, text):
            self.url = text.split('@', 1)[1].strip() if '@' in text else None
    downloader.install_requirement_guard(Requirement)
    assert Requirement('ordinary>=1').url is None
    with pytest.raises(RuntimeError, match='Direct URLs'):
        Requirement('example @ ' + source)


def test_reading_wheel_metadata_does_not_import_its_python_and_rejects_direct_urls(tmp_path):
    wheel(tmp_path / 'flask-3.1.3-py3-none-any.whl')
    records = inspect_wheels(tmp_path)
    assert records[0]['name'] == 'flask' and len(records[0]['sha256']) == 64
    wheel(tmp_path / 'evil-1-py3-none-any.whl', 'evil', '1', ['dependency @ https://external.invalid/package.whl'])
    with pytest.raises(RuntimeErrorSafe, match='URL'):
        inspect_wheels(tmp_path)


def test_rejects_wheel_zip_traversal_without_extracting(tmp_path):
    path = tmp_path / 'flask-3.1.3-py3-none-any.whl'
    wheel(path)
    with zipfile.ZipFile(path, 'a') as archive:
        archive.writestr('../../escape.txt', 'unsafe')
    with pytest.raises(RuntimeErrorSafe, match='不安全'):
        inspect_wheels(tmp_path)
    assert not (tmp_path.parent / 'escape.txt').exists()


class FakeDocker:
    root = ROOT
    settings = {'image': 'test-base:unit'}
    def __init__(self, directory, failure=None):
        self.directory, self.failure, self.commands = directory, failure, []
    def _docker_command(self, *args):
        return ['docker', *args]
    async def _mount_path(self, path):
        return str(path)
    async def _command(self, command, timeout=8):
        self.commands.append(command)
        if 'inspect' in command:
            is_base = 'test-base:unit' in command or any(value.startswith('agentforge-web-base:') for value in command)
            return 0, 'sha256:' + ('a' if is_base else 'b') * 64, ''
        if 'run' in command:
            if self.failure == 'download':
                return 1, 'Explicit unit fake download refusal', 'fake failure'
            if self.failure == 'timeout':
                raise TimeoutError()
            folder = next(path for path in (self.directory / 'attempts').glob('*/wheels') if not list(path.iterdir()))
            wheel(folder / 'flask-3.1.3-py3-none-any.whl')
            wheel(folder / 'jinja2-3.1.6-py3-none-any.whl', 'Jinja2', '3.1.6')
            (folder / 'transport.json').write_text(json.dumps({'source_policy': 'pip-requests-https-host-allowlist-v1',
                                                             'observed_hosts': ['pypi.org', 'files.pythonhosted.org']}))
            return 0, 'Explicit fake wheel download', ''
        if 'build' in command and self.failure == 'build':
            return 1, 'Explicit fake offline build', 'fake failure'
        return 0, 'Explicit fake Docker success', ''


def test_prepare_pins_full_wheel_lock_and_builds_offline_without_candidate_mount(tmp_path):
    runtime = FakeDocker(tmp_path)
    manager = WebDependencies(runtime, tmp_path)
    result = asyncio.run(manager.prepare('Flask==3.1.3'))
    assert result['image_id'] == 'sha256:' + 'b' * 64
    assert result['base_image_id'] == 'sha256:' + 'a' * 64
    assert result['offline_install'] and not result['host_installation'] and not result['candidate_source_mounted']
    assert result == manager.resolve(result['dependency_id'])
    downloaded = next(command for command in runtime.commands if 'run' in command)
    assert '--read-only' in downloaded and '--cap-drop' in downloaded
    assert not any('/candidate' in arg or '.env' in arg or '/workspace' in arg for arg in downloaded)
    build = next(command for command in runtime.commands if 'build' in command)
    assert '--network=none' in build and '--pull=false' in build
    lock = (tmp_path / 'bundles' / result['dependency_id'] / 'requirements.lock').read_text()
    assert 'flask==3.1.3 --hash=sha256:' in lock and 'jinja2==3.1.6 --hash=sha256:' in lock
    context = Path(build[-1])
    assert set(path.name for path in context.iterdir()) == {'Dockerfile', 'requirements.lock', 'wheels'}
    dockerfile = (context / 'Dockerfile').read_text()
    assert '--require-hashes' in dockerfile and '--no-index' in dockerfile and '--no-deps' in dockerfile


def test_reusing_ready_dependency_still_checks_immutable_files_and_image(tmp_path):
    runtime = FakeDocker(tmp_path)
    manager = WebDependencies(runtime, tmp_path)
    first = asyncio.run(manager.prepare('Flask==3.1.3'))
    assert asyncio.run(manager.prepare('Flask==3.1.3')) == first
    assert len([c for c in runtime.commands if 'run' in c]) == 1
    attempts = [json.loads(path.read_text(encoding='utf-8')) for path in (tmp_path / 'attempts').glob('*/record.json')]
    assert len(attempts) == 2 and {record['reused'] for record in attempts} == {False, True}
    target = tmp_path / 'bundles' / first['dependency_id'] / 'requirements.lock'
    target.write_text('tampered')
    with pytest.raises(RuntimeErrorSafe, match='哈希'):
        manager.resolve(first['dependency_id'])


@pytest.mark.parametrize('failure', ['download', 'build', 'timeout'])
def test_every_failed_attempt_and_step_remains_distinct_without_publishing_bundle(tmp_path, failure):
    runtime = FakeDocker(tmp_path, failure=failure)
    manager = WebDependencies(runtime, tmp_path)
    for _ in range(2):
        with pytest.raises(RuntimeErrorSafe):
            asyncio.run(manager.prepare('Flask==3.1.3'))
    attempts = list((tmp_path / 'attempts').glob('*/record.json'))
    assert len(attempts) == 2
    assert all(json.loads(path.read_text(encoding='utf-8'))['status'] == 'failed' for path in attempts)
    assert not (tmp_path / 'bundles').exists()
    assert len([c for c in runtime.commands if c[1:3] == ['rm', '-f']]) == 2


def test_invalid_user_input_preserves_failure_without_invoking_docker(tmp_path):
    runtime = FakeDocker(tmp_path)
    with pytest.raises(RuntimeErrorSafe):
        asyncio.run(WebDependencies(runtime, tmp_path).prepare('x @ https://external.invalid/x.whl'))
    assert runtime.commands == []
    record = json.loads(next((tmp_path / 'attempts').glob('*/record.json')).read_text(encoding='utf-8'))
    assert record['status'] == 'failed' and 'requirements' not in record
