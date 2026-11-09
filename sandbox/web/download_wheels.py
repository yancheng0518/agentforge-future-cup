"""Trusted, source-free wheel downloader executed only inside Docker.

The allowlist is an application-level restriction on pip's Requests transport,
not an OS firewall. Only the base image's pip is imported; downloaded packages
are never installed or imported here. Redirects also pass Session.send.
"""
from pathlib import Path
import json
import os
import sys
from urllib.parse import urlsplit

ALLOWED_HOSTS = frozenset({'pypi.org', 'files.pythonhosted.org'})


def validate_url(value):
    try:
        url = urlsplit(value)
        if (url.scheme != 'https' or url.hostname not in ALLOWED_HOSTS
                or url.port not in (None, 443) or url.username is not None
                or url.password is not None):
            raise ValueError()
    except (ValueError, TypeError):
        raise RuntimeError('Dependency transport blocked a non-official HTTPS source') from None
    return url.hostname


def install_guard(session_class, observed):
    original = session_class.send

    def guarded(self, request, **kwargs):
        host = validate_url(request.url)
        observed.add(host)
        # Do not inherit proxy credentials or let a proxy select the upstream.
        kwargs['proxies'] = {}
        return original(self, request, **kwargs)

    session_class.send = guarded


def install_requirement_guard(requirement_class):
    """Reject all metadata Direct URLs before pip can prepare their candidates.

This also blocks file: and VCS dependencies which do not use Requests and avoids
direct source archives bypassing index wheel selection.
"""
    original = requirement_class.__init__

    def guarded(self, requirement_string):
        original(self, requirement_string)
        if self.url is not None:
            raise RuntimeError('Dependency metadata Direct URLs and local paths are unsupported')

    requirement_class.__init__ = guarded


def main():
    for key in list(os.environ):
        if key.upper().endswith('_PROXY') or key.startswith('PIP_'):
            os.environ.pop(key, None)
    from pip._vendor.requests.sessions import Session
    from pip._vendor.packaging.requirements import Requirement
    from pip._internal.cli.main import main as pip_main
    observed = set()
    install_guard(Session, observed)
    install_requirement_guard(Requirement)
    # pip copies POSIX mode bits with shutil.copy. Windows/WSL bind mounts do
    # not support that for this non-root UID. Download on Linux tmpfs, then
    # export bounded wheel bytes without changing ownership or host mode bits.
    downloaded = Path('/tmp/downloaded-wheels')
    downloaded.mkdir()
    try:
        status = pip_main([
            '--isolated', '--disable-pip-version-check', '--no-cache-dir',
            'download', '--only-binary=:all:', '--index-url', 'https://pypi.org/simple',
            '--dest', str(downloaded), '--retries', '0', '--timeout', '25',
            '--progress-bar', 'off', '-r', '/request/requirements.txt',
        ])
        if status == 0:
            for wheel in downloaded.glob('*.whl'):
                if wheel.is_symlink() or not wheel.is_file() or wheel.stat().st_size > 32 * 1024 * 1024:
                    raise RuntimeError('Downloaded wheel exceeds bounded export policy')
                with (Path('/wheelhouse') / wheel.name).open('xb') as stream:
                    stream.write(wheel.read_bytes())
    finally:
        Path('/wheelhouse/transport.json').write_text(json.dumps({
            'source_policy': 'pip-requests-https-host-allowlist-v1',
            'direct_metadata_urls': 'rejected-before-candidate-preparation',
            'allowed_hosts': sorted(ALLOWED_HOSTS), 'observed_hosts': sorted(observed),
            'candidate_source_mounted': False, 'packages_installed': False,
        }, sort_keys=True), encoding='utf-8')
    return status


if __name__ == '__main__':
    sys.exit(main())
