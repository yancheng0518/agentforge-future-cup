"""Check staged bytes (including nested ZIP members) without printing credentials."""
from __future__ import annotations

import io
from pathlib import Path, PurePosixPath
import subprocess
import zipfile

from export_reproduction import configured_secrets, reject_secret

ROOT = Path(__file__).resolve().parents[1]


def main():
    secrets = configured_secrets(ROOT)
    paths = subprocess.check_output(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM", "-z"], cwd=ROOT
    ).decode("utf-8").split("\0")
    checked = 0
    expanded = 0

    def inspect(data, name, depth=0):
        nonlocal expanded
        basename = PurePosixPath(name).name.lower()
        if basename == ".env" or (basename.startswith(".env.") and basename != ".env.example"):
            raise ValueError("Private dotenv file is staged")
        reject_secret(data, secrets)
        if name.lower().endswith(".zip"):
            if depth >= 5:
                raise ValueError("Nested ZIP depth exceeds inspection limit")
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for member in archive.infolist():
                    if member.is_dir():
                        continue
                    expanded += member.file_size
                    if expanded > 512 * 1024 * 1024 or member.file_size > 64 * 1024 * 1024:
                        raise ValueError("Expanded ZIP inspection limit exceeded")
                    inspect(archive.read(member), member.filename, depth + 1)

    for path in filter(None, paths):
        data = subprocess.check_output(["git", "show", ":" + path], cwd=ROOT)
        try:
            inspect(data, path)
        except Exception as exc:
            print(f"REFUSED: {path}: {type(exc).__name__}; no credential contents emitted")
            return 1
        checked += 1
    print(f"PASS: {checked} staged files; {expanded} expanded ZIP bytes; configured credential values absent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
