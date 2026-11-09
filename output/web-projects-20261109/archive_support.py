"""Read-only bounded ZIP/wheel verification for the Web reproduction batch."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from export_reproduction import ExportError, MANIFEST, SCHEMA, EXCLUDED_DIRS, reject_secret

MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_ENTRIES = 50000
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_WHEEL_BYTES = 32 * 1024 * 1024
MAX_NESTED_MEMBER_BYTES = 256 * 1024 * 1024
MAX_NESTED_BYTES = 4 * 1024 * 1024 * 1024
MAX_NESTED_ENTRIES = 250000
PRIVATE_NAME = re.compile(r"^(?:credentials?|secrets?|tokens?)(?:[._-]|$)|\.(?:pem|p12|pfx|key)$", re.I)
# These exact historical archives remain sealed. Exceptions never apply to
# newly selected files, changed ZIPs, or current SQLite sidecars.
LEGACY_ARCHIVE_MEMBERS = {
    "d760b5fe49c10425f53cf8529a20505412767731cc6fd66bc300d4279302b708": {
        ".agentforge/autonomous-validation-20261108/agentforge.sqlite3-shm": "fd4c9fda9cd3f9ae7c962b0ddf37232294d55580e1aa165aa06129b8549389eb",
        ".agentforge/autonomous-validation-20261108/agentforge.sqlite3-wal": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    },
    "79beca3a73824807497c81295826ef6cf6cfb0327e711db368588bfe732d604e": {
        "output/collaboration-graph-20261108/frontend-final-build-20261108T141043278Z-d8e88f.log": "173aab8e81e3ee931cb4516fbf79590fa8105d084e5c7c36fa0bf74b14a9a230",
        "output/collaboration-graph-20261108/frontend-final-test-20261108T141043278Z-d8e88f.log": "d4a679753848df0b9064bca936185f11d30d06490c27e64f6d122ed3d6812305",
    },
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def validate_member(item, seen, *, maximum=MAX_FILE_BYTES, directories=False):
    name, parts = item.filename, PurePosixPath(item.filename).parts
    if (not name or not parts or item.orig_filename != name or "\\" in name or ":" in name or PurePosixPath(name).is_absolute()
            or ".." in parts or name.rstrip("/") != PurePosixPath(name).as_posix()
            or stat.S_ISLNK(item.external_attr >> 16) or item.flag_bits & 1
            or name.casefold() in seen or item.file_size > maximum
            or item.is_dir() and not directories):
        raise ExportError("An archive has an unsafe, duplicate, encrypted or oversized member.")
    seen.add(name.casefold())
    return parts


def forbidden_member(parts, *, wheel=False):
    # A wheel is a package ZIP, not a host installation. Its public certificate,
    # secrets.py, native-extension and build modules may be legitimate files;
    # all bytes still receive exactly the same configured-secret scan.
    if any(p.casefold() in {"wsl", ".wsl"} or p.casefold() == ".env"
           or p.casefold().startswith(".env.") and p.casefold() != ".env.example" for p in parts):
        return True
    name = parts[-1].casefold()
    if name.endswith((".vhd", ".vhdx", ".vmdk", ".iso", ".msi", "-wal", "-shm", "-journal", ".pyc", ".pyo")):
        return True
    if wheel:
        return False
    if PRIVATE_NAME.search(parts[-1]) or name.endswith((".pyd", ".log")):
        return True
    for index, part in enumerate(parts[:-1]):
        dependency_build = (part == "build" and index >= 3 and parts[index - 2] == "attempts"
                            and parts[index - 3] == "dependencies")
        if part.casefold() in EXCLUDED_DIRS - {".agentforge"} and not dependency_build:
            return True
    return False


def scan_nested_zip(data, secrets, budget, depth=0, *, wheel=False):
    """Read compressed members recursively; never extract/import/install them."""
    if depth > 8 or wheel and len(data) > MAX_WHEEL_BYTES:
        raise ExportError("Nested archive or wheel exceeds its depth/size budget.")
    exceptions = LEGACY_ARCHIVE_MEMBERS.get(sha(data), {})
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        seen = set()
        if wheel and sum(i.file_size for i in archive.infolist()) > 256 * 1024 * 1024:
            raise ExportError("A wheel exceeds the 256 MiB expanded-content limit.")
        for item in archive.infolist():
            parts = validate_member(item, seen, maximum=MAX_NESTED_MEMBER_BYTES, directories=True)
            reject_secret(item.filename.encode("utf-8"), secrets)
            if forbidden_member(parts, wheel=wheel) and item.filename not in exceptions:
                raise ExportError("A nested archive contains an excluded credential/cache/disk path.")
            budget["entries"] += 1
            budget["bytes"] += item.file_size
            if budget["entries"] > MAX_NESTED_ENTRIES or budget["bytes"] > MAX_NESTED_BYTES:
                raise ExportError("Recursive decompression exceeds its bounded inspection budget.")
            if item.is_dir():
                continue
            content = archive.read(item)
            reject_secret(content, secrets)
            if item.filename in exceptions and sha(content) != exceptions[item.filename]:
                raise ExportError("A sealed historical archive exception changed.")
            nested_wheel = item.filename.casefold().endswith(".whl")
            if nested_wheel or item.filename.casefold().endswith(".zip") or zipfile.is_zipfile(io.BytesIO(content)):
                scan_nested_zip(content, secrets, budget, depth + 1, wheel=nested_wheel)


def verify_archive(path, expected_manifest=None, *, secrets=()):
    """Verify the new bounded manifest without migrating any historical schema."""
    budget = {"entries": 0, "bytes": 0}
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_ENTRIES + 1 or sum(i.file_size for i in infos) > MAX_TOTAL_BYTES:
            raise ExportError("Archive member count or expanded size exceeds its budget.")
        seen = set()
        for item in infos:
            parts = validate_member(item, seen)
            if forbidden_member(parts):
                raise ExportError("The new archive contains an excluded credential/cache/disk path.")
        names = [item.filename for item in infos]
        if MANIFEST not in names or archive.getinfo(MANIFEST).file_size > MAX_MANIFEST_BYTES:
            raise ExportError("Archive lacks a bounded reproduction manifest.")
        metadata = archive.read(MANIFEST)
        reject_secret(metadata, secrets)
        if expected_manifest is not None and metadata != expected_manifest:
            raise ExportError("Written archive manifest changed.")
        manifest = json.loads(metadata)
        if not isinstance(manifest, dict):
            raise ExportError("Unsupported reproduction manifest object.")
        records = manifest.get("files")
        if manifest.get("schema") != SCHEMA or not isinstance(records, dict) or set(names) != set(records) | {MANIFEST}:
            raise ExportError("Archive differs from its supported complete manifest inventory.")
        for name, record in records.items():
            content = archive.read(name)
            reject_secret(name.encode("utf-8"), secrets)
            reject_secret(content, secrets)
            if not isinstance(record, dict) or len(content) != record.get("bytes") or sha(content) != record.get("sha256"):
                raise ExportError("An archive member failed its size/SHA-256 check.")
            if name.casefold().endswith((".whl", ".zip")) or zipfile.is_zipfile(io.BytesIO(content)):
                scan_nested_zip(content, secrets, budget, wheel=name.casefold().endswith(".whl"))
    with Path(path).open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"verified": True, "files": len(records), "zip_sha256": digest,
            "manifest_sha256": sha(metadata), "nested_zip_scan": budget}
