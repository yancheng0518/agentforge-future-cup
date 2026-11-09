"""Pinned PyPI wheel preparation; no project source or host installation.

Networking is limited to a source-free trusted downloader in Docker. Its pip
transport restricts every HTTPS request/redirect to official PyPI domains.
The second phase installs a fully hashed lock using Docker build --network=none.
Every attempt remains distinct; all unsuccessful attempts are retained.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import time
from uuid import uuid4
import zipfile

from .runtime import RuntimeErrorSafe

SCHEMA = 'agentforge-web-dependencies-v1'
MAX_WHEELS = 48
MAX_WHEEL_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
NAME = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9])?')
VERSION = re.compile(r'[0-9][A-Za-z0-9.!+_-]{0,79}')
IMAGE = re.compile(r'sha256:[0-9a-f]{64}')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def canonical_name(value):
    return re.sub(r'[-_.]+', '-', value).lower()


def normalize_requirements(value):
    """Accept named, exactly pinned packages only. No pip option interpretation."""
    if isinstance(value, str):
        if len(value.encode('utf-8')) > 4096 or '\x00' in value:
            raise RuntimeErrorSafe('依赖清单超过 4 KiB 或包含无效字符。')
        lines = value.splitlines()
    elif isinstance(value, list) and len(value) <= 16 and all(isinstance(item, str) for item in value):
        lines = value
    else:
        raise RuntimeErrorSafe('依赖必须是 requirements.txt 文本或固定版本字符串列表。')
    pins = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        fields = line.split('==')
        if len(fields) != 2 or not NAME.fullmatch(fields[0]) or not VERSION.fullmatch(fields[1]):
            raise RuntimeErrorSafe('仅支持 name==version 固定 PyPI 依赖；不支持 URL、选项、extras 或环境条件。')
        name = canonical_name(fields[0])
        if name in pins:
            raise RuntimeErrorSafe('依赖清单包含重复或冲突的包名。')
        pins[name] = fields[1]
    if not 1 <= len(pins) <= 16:
        raise RuntimeErrorSafe('每个项目需要 1 至 16 个固定版本依赖。')
    return [f'{name}=={version}' for name, version in sorted(pins.items())]


def _safe(path, root):
    root = root.resolve()
    if not path.resolve().is_relative_to(root):
        raise RuntimeErrorSafe('依赖文件路径越界。')
    for item in (path, *path.parents):
        if item.is_symlink() or (hasattr(item, 'is_junction') and item.is_junction()):
            raise RuntimeErrorSafe('依赖目录不接受符号链接或目录联接。')
        if item == root:
            break
    if path.is_file() and path.stat().st_nlink > 1:
        raise RuntimeErrorSafe('依赖文件不接受硬链接。')
    return path


def _write(path, value):
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(value if isinstance(value, str) else _json(value))


def inspect_wheels(directory):
    """Read only bounded ZIP metadata. Never import or extract downloaded code."""
    names, records, total = set(), [], 0
    paths = sorted(directory.glob('*.whl'))
    if not 1 <= len(paths) <= MAX_WHEELS:
        raise RuntimeErrorSafe('依赖下载结果为空或超过 48 个 wheel。')
    for path in paths:
        _safe(path, directory)
        size = path.stat().st_size
        total += size
        if size > MAX_WHEEL_BYTES or total > MAX_TOTAL_BYTES:
            raise RuntimeErrorSafe('依赖 wheel 超过单个 32 MiB/合计 128 MiB 的限制。')
        if not re.fullmatch(r'[A-Za-z0-9_.+!-]+\.whl', path.name):
            raise RuntimeErrorSafe('wheel 文件名无效。')
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 20000 or sum(item.file_size for item in entries) > 256 * 1024 * 1024:
                raise RuntimeErrorSafe('wheel 内容超过大小限制。')
            for entry in entries:
                p = PurePosixPath(entry.filename)
                if (p.is_absolute() or '..' in p.parts or '\\' in entry.filename or ':' in entry.filename
                        or stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1):
                    raise RuntimeErrorSafe('wheel 包含不安全路径、链接或加密内容。')
            metadata = [item for item in entries if re.fullmatch(r'[^/]+\.dist-info/METADATA', item.filename)]
            if len(metadata) != 1 or metadata[0].file_size > 1024 * 1024:
                raise RuntimeErrorSafe('wheel 缺少唯一的有界 METADATA。')
            parsed = BytesParser().parsebytes(archive.read(metadata[0]))
            name, version = parsed.get('Name', ''), parsed.get('Version', '')
            if not NAME.fullmatch(name) or not VERSION.fullmatch(version):
                raise RuntimeErrorSafe('wheel 包名或版本无效。')
            name = canonical_name(name)
            if name in names:
                raise RuntimeErrorSafe('下载结果包含重复包名。')
            names.add(name)
            dependencies = parsed.get_all('Requires-Dist', [])
            if any('@' in dependency or '://' in dependency or '\\' in dependency for dependency in dependencies):
                raise RuntimeErrorSafe('wheel 声明了不支持的直接 URL/路径依赖。')
        records.append({'name': name, 'version': version, 'file': path.name,
                        'sha256': _hash(path.read_bytes()), 'bytes': size})
    if any(path.name not in {record['file'] for record in records} | {'transport.json'} for path in directory.iterdir()):
        raise RuntimeErrorSafe('下载结果包含非 wheel 内容。')
    return sorted(records, key=lambda item: item['name'])


class WebDependencies:
    def __init__(self, runtime, directory):
        self.runtime = runtime
        self.directory = Path(directory).resolve()
        self._lock = asyncio.Lock()

    def resolve(self, dependency_id):
        if not isinstance(dependency_id, str) or not re.fullmatch(r'[0-9a-f]{64}', dependency_id):
            raise RuntimeErrorSafe('依赖版本标识无效。')
        folder = _safe(self.directory / 'bundles' / dependency_id, self.directory)
        path = _safe(folder / 'manifest.json', self.directory)
        try:
            if path.stat().st_size > 128 * 1024:
                raise ValueError()
            result = json.loads(path.read_text(encoding='utf-8'))
            expected_id = _hash(_json({'schema': SCHEMA, 'requirements': result['requirements'],
                                      'base_image_id': result['base_image_id']}).encode())
            if (result['schema'] != SCHEMA or result['dependency_id'] != dependency_id or expected_id != dependency_id
                    or not IMAGE.fullmatch(result['image_id']) or not IMAGE.fullmatch(result['base_image_id'])
                    or result['requirements'] != normalize_requirements(result['requirements'])):
                raise ValueError()
            lock = _safe(folder / 'requirements.lock', self.directory).read_bytes()
            if _hash(lock) != result['lock_sha256']:
                raise ValueError()
            for wheel in result['wheels']:
                if not re.fullmatch(r'[A-Za-z0-9_.+!-]+\.whl', wheel['file']):
                    raise ValueError()
                target = _safe(folder / 'wheels' / wheel['file'], self.directory)
                if target.stat().st_size != wheel['bytes'] or _hash(target.read_bytes()) != wheel['sha256']:
                    raise ValueError()
            return result
        except (OSError, KeyError, TypeError, ValueError, UnicodeError):
            raise RuntimeErrorSafe('依赖快照缺失、无效或哈希不一致；未运行候选。') from None

    async def _step(self, attempt, name, command, timeout):
        _write(attempt / (name + '-command.json'), {'command': command, 'timeout_seconds': timeout})
        started = time.monotonic()
        try:
            code, out, err = await self.runtime._command(command, timeout=timeout)
            _write(attempt / (name + '-stdout.txt'), out)
            _write(attempt / (name + '-stderr.txt'), err)
            _write(attempt / (name + '-result.json'), {'exit_code': code, 'duration_ms': round((time.monotonic() - started) * 1000)})
        except BaseException as exc:
            _write(attempt / (name + '-error.json'), {'error_type': type(exc).__name__, 'duration_ms': round((time.monotonic() - started) * 1000)})
            raise
        if code:
            raise RuntimeErrorSafe(f'依赖准备步骤 {name} 失败，退出码 {code}；完整回执已保留。')
        return out

    async def prepare(self, requirements):
        """Prepare once, retain every attempt, and pin base and dependency image IDs."""
        self.directory.mkdir(parents=True, exist_ok=True)
        attempt = _safe(self.directory / 'attempts' / uuid4().hex, self.directory)
        attempt.mkdir(parents=True, exist_ok=False)
        record = {'schema': SCHEMA, 'attempt_id': attempt.name, 'status': 'failed',
                  'created_at': datetime.now(timezone.utc).isoformat(), 'model_calls': 0,
                  'candidate_source_mounted': False, 'host_installation': False}
        try:
            pins = normalize_requirements(requirements)
            record['requirements'] = pins
            async with self._lock:
                base_image_id = (await self._step(attempt, 'base-image', self.runtime._docker_command(
                    'image', 'inspect', self.runtime.settings['image'], '--format', '{{.Id}}'), 20)).strip()
                if not IMAGE.fullmatch(base_image_id):
                    raise RuntimeErrorSafe('基础 Docker 镜像 ID 无效。')
                dependency_id = _hash(_json({'schema': SCHEMA, 'requirements': pins, 'base_image_id': base_image_id}).encode())
                record.update(dependency_id=dependency_id, base_image_id=base_image_id)
                bundle = _safe(self.directory / 'bundles' / dependency_id, self.directory)
                if (bundle / 'manifest.json').is_file():
                    result = self.resolve(dependency_id)
                    image_id = (await self._step(attempt, 'cached-image', self.runtime._docker_command(
                        'image', 'inspect', result['image_id'], '--format', '{{.Id}}'), 20)).strip()
                    if image_id != result['image_id']:
                        raise RuntimeErrorSafe('已冻结的依赖镜像不可用，未替换为其他镜像。')
                    record.update(status='succeeded', reused=True, image_id=image_id)
                    return result
                request, wheels, trusted = (attempt / name for name in ('request', 'wheels', 'trusted'))
                for folder in (request, wheels, trusted):
                    folder.mkdir()
                # This newly created download-only folder contains no project
                # source or user data; the non-root downloader must write wheels.
                wheels.chmod(0o777)
                _write(request / 'requirements.txt', '\n'.join(pins) + '\n')
                downloader = self.runtime.root / 'sandbox/web/download_wheels.py'
                shutil.copyfile(downloader, trusted / 'download_wheels.py')
                record['downloader_sha256'] = _hash(downloader.read_bytes())
                request_mount = await self.runtime._mount_path(request)
                wheels_mount = await self.runtime._mount_path(wheels)
                trusted_mount = await self.runtime._mount_path(trusted)
                container = 'agentforge-dependencies-' + attempt.name
                try:
                    await self._step(attempt, 'download', self.runtime._docker_command(
                        'run', '--name', container, '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                        '--pids-limit', '96', '--memory', '768m', '--cpus', '1', '--user', '65534:65534',
                        '--tmpfs', '/tmp:rw,noexec,nosuid,size=134217728,mode=1777',
                        '--mount', f'type=bind,src={request_mount},dst=/request,readonly',
                        '--mount', f'type=bind,src={trusted_mount},dst=/trusted,readonly',
                        '--mount', f'type=bind,src={wheels_mount},dst=/wheelhouse',
                        base_image_id, 'python', '-I', '-B', '/trusted/download_wheels.py'), 300)
                finally:
                    try:
                        await self._step(attempt, 'download-cleanup', self.runtime._docker_command('rm', '-f', container), 20)
                    except (RuntimeErrorSafe, OSError, TimeoutError):
                        record['download_cleanup_failed'] = True
                wheel_records = inspect_wheels(wheels)
                actual = {item['name']: item['version'] for item in wheel_records}
                if any(actual.get(pin.split('==')[0]) != pin.split('==')[1] for pin in pins):
                    raise RuntimeErrorSafe('下载结果不匹配显式固定的根依赖。')
                transport = json.loads((wheels / 'transport.json').read_text(encoding='utf-8'))
                if not set(transport['observed_hosts']) <= {'pypi.org', 'files.pythonhosted.org'}:
                    raise RuntimeErrorSafe('依赖下载来源回执无效。')
                lock = '\n'.join(f"{w['name']}=={w['version']} --hash=sha256:{w['sha256']}" for w in wheel_records) + '\n'
                context = attempt / 'build'
                context.mkdir()
                (context / 'wheels').mkdir()
                for wheel in wheel_records:
                    shutil.copyfile(wheels / wheel['file'], context / 'wheels' / wheel['file'])
                _write(context / 'requirements.lock', lock)
                dockerfile = self.runtime.root / 'sandbox/web/Dockerfile'
                shutil.copyfile(dockerfile, context / 'Dockerfile')
                tag = 'agentforge-web:' + dependency_id[:24]
                base_tag = 'agentforge-web-base:' + base_image_id.removeprefix('sha256:')[:24]
                await self._step(attempt, 'pin-base-tag', self.runtime._docker_command(
                    'tag', base_image_id, base_tag), 20)
                tagged_base = (await self._step(attempt, 'tagged-base-image', self.runtime._docker_command(
                    'image', 'inspect', base_tag, '--format', '{{.Id}}'), 20)).strip()
                if tagged_base != base_image_id:
                    raise RuntimeErrorSafe('离线构建使用的基础镜像标签不匹配冻结 ID。')
                context_mount = await self.runtime._mount_path(context)
                await self._step(attempt, 'offline-build', self.runtime._docker_command(
                    'build', '--network=none', '--pull=false', '--build-arg', f'BASE_IMAGE={base_tag}',
                    '--tag', tag, context_mount), 300)
                image_id = (await self._step(attempt, 'built-image', self.runtime._docker_command(
                    'image', 'inspect', tag, '--format', '{{.Id}}'), 20)).strip()
                if not IMAGE.fullmatch(image_id):
                    raise RuntimeErrorSafe('构建的依赖镜像 ID 无效。')
                result = {'schema': SCHEMA, 'dependency_id': dependency_id, 'requirements': pins,
                          'base_image_id': base_image_id, 'image_id': image_id, 'image_tag': tag,
                          'lock_sha256': _hash(lock.encode()), 'wheels': wheel_records,
                          'source_policy': transport['source_policy'], 'observed_hosts': transport['observed_hosts'],
                          'created_at': datetime.now(timezone.utc).isoformat(), 'prepared_by_attempt': attempt.name,
                          'downloader_sha256': record['downloader_sha256'], 'dockerfile_sha256': _hash(dockerfile.read_bytes()),
                          'offline_install': True, 'candidate_source_mounted': False, 'host_installation': False}
                # Do not replace a previous successful manifest, even across processes.
                bundle.mkdir(parents=True, exist_ok=False)
                (bundle / 'wheels').mkdir()
                for wheel in wheel_records:
                    shutil.copyfile(wheels / wheel['file'], bundle / 'wheels' / wheel['file'])
                _write(bundle / 'requirements.lock', lock)
                _write(bundle / 'manifest.json', result)
                record.update(status='succeeded', reused=False, image_id=image_id)
                return result
        except asyncio.CancelledError:
            record.update(status='cancelled', error_type='CancelledError')
            raise
        except (RuntimeErrorSafe, OSError, ValueError, KeyError, TimeoutError, zipfile.BadZipFile) as exc:
            record['error_type'] = type(exc).__name__
            record['reason'] = str(exc) if isinstance(exc, RuntimeErrorSafe) else '依赖准备失败，详见该次尝试回执。'
            raise RuntimeErrorSafe(f"{record['reason']} 尝试 ID：{attempt.name}") from None
        finally:
            record['finished_at'] = datetime.now(timezone.utc).isoformat()
            _write(attempt / 'record.json', record)
