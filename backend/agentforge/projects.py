"""Immutable project, profile, knowledge and execution records in SQLite.

Source values are bounded Python *text* using the autonomous sandbox contract.
No source is imported/executed and no server path or Git URL is accepted as an
import source. Promotion is an atomic compare-and-swap of the project head.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from .autonomous_sandbox import _digest, _json, _sources
from .runtime import RuntimeErrorSafe

MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_KNOWLEDGE_BYTES = 65536
MAX_SELECTED_KNOWLEDGE_BYTES = 131072
PROFILE_FIELDS = {"name", "description", "prompt", "capabilities", "enabled"}


class ProjectValidationError(RuntimeErrorSafe, ValueError):
    """A bounded input does not satisfy the storage contract."""


class ProjectConflictError(RuntimeErrorSafe):
    """A stale head or conflicting idempotency key cannot be committed."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _text(value, label, maximum, *, empty=False, strip=False):
    if not isinstance(value, str) or "\x00" in value:
        raise ProjectValidationError(f"{label} 必须为 UTF-8 文本。")
    if strip:
        value = value.strip()
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise ProjectValidationError(f"{label} 包含无效 Unicode。") from None
    if size > maximum or (not empty and not value.strip()):
        raise ProjectValidationError(f"{label} 为空或超过 {maximum} 字节限制。")
    return value


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise ProjectValidationError("记录 ID 无效。")
    return value


def _version(value):
    if type(value) is not int or value < 1:
        raise ProjectValidationError("版本号必须为正整数。")
    return value


def _bounded_json(value, maximum=MAX_JSON_BYTES):
    """Detach bounded JSON, reject coercible/non-finite/non-JSON Python values."""
    nodes = 0

    def inspect(item, depth=0):
        nonlocal nodes
        nodes += 1
        if nodes > 20000 or depth > 20:
            raise ProjectValidationError("JSON 层级或节点数量超过限制。")
        if type(item) is dict:
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ProjectValidationError("JSON 对象键必须为字符串。")
                inspect(key, depth + 1)
                inspect(child, depth + 1)
        elif type(item) is list:
            for child in item:
                inspect(child, depth + 1)
        elif type(item) not in (str, int, float, bool, type(None)) or (type(item) is float and not math.isfinite(item)):
            raise ProjectValidationError("记录仅接受有限的 JSON 值。")

    try:
        inspect(value)
        encoded = _json(value)
        if len(encoded.encode("utf-8")) > maximum:
            raise ProjectValidationError("JSON 记录超过大小限制。")
        return json.loads(encoded)
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, ProjectValidationError):
            raise
        raise ProjectValidationError("记录不是有效有界 JSON。") from None


def _files(value):
    try:
        result = _sources(value)
    except (RuntimeErrorSafe, UnicodeError) as exc:
        raise ProjectValidationError(str(exc) if isinstance(exc, RuntimeErrorSafe) else "源码包含无效 Unicode。") from None
    return _bounded_json(result)


def _profile_body(body):
    if not isinstance(body, dict) or not set(body) <= PROFILE_FIELDS or "name" not in body:
        raise ProjectValidationError("Profile 只接受 name/description/prompt/capabilities/enabled；模型继承当前配置。")
    name = _text(body["name"], "Profile 名称", 512, strip=True)
    if len(name) > 120:
        raise ProjectValidationError("Profile 名称最多 120 字符。")
    labels = body.get("capabilities", [])
    if not isinstance(labels, list) or len(labels) > 32:
        raise ProjectValidationError("能力标签必须为最多 32 项的数组。")
    tags = [_text(tag, "能力标签", 128, strip=True) for tag in labels]
    if any(len(tag) > 64 for tag in tags) or len({tag.casefold() for tag in tags}) != len(tags):
        raise ProjectValidationError("能力标签过长或重复。")
    enabled = body.get("enabled", True)
    if type(enabled) is not bool:
        raise ProjectValidationError("enabled 必须为布尔值。")
    return {"name": name, "description": _text(body.get("description", ""), "Profile 描述", 8192, empty=True),
            "prompt": _text(body.get("prompt", ""), "Profile 提示词", 65536, empty=True),
            "capabilities": tags, "enabled": enabled}


def _verification(value, files, acceptance):
    if not isinstance(value, dict):
        raise ProjectValidationError("版本推进需要完整验证记录。")
    value = _bounded_json(value)
    fingerprint = _digest(_json(files))
    test, review = value.get("test"), value.get("review")
    if (value.get("status") != "passed" or value.get("candidate_sha256") != fingerprint
            or value.get("verified_hash") != fingerprint or not isinstance(test, dict) or not isinstance(review, dict)
            or test.get("status") != "passed" or test.get("isolated") is not True
            or type(test.get("exit_code")) is not int or test["exit_code"] != 0
            or type(test.get("test_count")) is not int or test["test_count"] <= 0
            or test.get("candidate_sha256") != fingerprint or test.get("acceptance_sha256") != _digest(acceptance)
            or review.get("passed") is not True or review.get("candidate_hash") != fingerprint):
        raise ProjectValidationError("当前候选源码、独立测试和通过审查的哈希必须一致。")
    return value


class ProjectStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS projects(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, head_revision_id TEXT, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS revisions(
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), number INTEGER NOT NULL,
                    parent_id TEXT REFERENCES revisions(id), run_id TEXT UNIQUE, body TEXT NOT NULL,
                    UNIQUE(project_id, number));
                CREATE TABLE IF NOT EXISTS profiles(
                    id TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(id, version));
                CREATE TABLE IF NOT EXISTS knowledge_versions(
                    id TEXT NOT NULL, project_id TEXT NOT NULL REFERENCES projects(id), version INTEGER NOT NULL,
                    body TEXT NOT NULL, PRIMARY KEY(id, version));
                CREATE TABLE IF NOT EXISTS executions(
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS application_states(
                    project_id TEXT PRIMARY KEY REFERENCES projects(id), body TEXT NOT NULL,
                    sha256 TEXT NOT NULL, version INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS revisions_project ON revisions(project_id, number);
                CREATE INDEX IF NOT EXISTS knowledge_project ON knowledge_versions(project_id, id, version);
                CREATE INDEX IF NOT EXISTS executions_project ON executions(project_id);
            """)
            for table in ("revisions", "profiles", "knowledge_versions", "executions"):
                # Names are code constants, never supplied by a caller.
                for action in ("UPDATE", "DELETE"):
                    db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, 'immutable record'); END")
            body = _profile_body({"name": "通用开发助手", "description": "使用当前模型处理项目任务。",
                                  "prompt": "根据项目需求与上下文完成任务，清楚区分事实、验证结果和待确认内容。",
                                  "capabilities": ["general"], "enabled": True})
            default = {"id": "general", "version": 1, **body, "sha256": _digest(_json(body)), "created_at": _now()}
            db.execute("INSERT OR IGNORE INTO profiles(id,version,body) VALUES(?,?,?)", ("general", 1, _json(default)))

    @contextmanager
    def _connect(self, *, write=False):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _project(db, project_id):
        row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        return dict(row) if row else None

    @classmethod
    def _require_project(cls, db, project_id):
        project = cls._project(db, project_id)
        if project is None:
            raise KeyError(project_id)
        return project

    def import_project(self, name, files, acceptance=""):
        name = _text(name, "项目名称", 512, strip=True)
        if len(name) > 120:
            raise ProjectValidationError("项目名称最多 120 字符。")
        files = _files(files)
        acceptance = _text(acceptance, "验收源码", 65536, empty=True)
        project_id, revision_id, created = uuid4().hex, uuid4().hex, _now()
        project = {"id": project_id, "name": name, "head_revision_id": revision_id, "created_at": created}
        revision = {"id": revision_id, "project_id": project_id, "number": 1, "parent_id": None,
                    "files": files, "source_sha256": _digest(_json(files)), "acceptance": acceptance,
                    "verification": {"status": "unverified"}, "created_at": created, "run_id": None}
        with self._connect(write=True) as db:
            db.execute("INSERT INTO projects VALUES(?,?,?,?)", (project_id, name, revision_id, created))
            db.execute("INSERT INTO revisions VALUES(?,?,?,?,?,?)", (revision_id, project_id, 1, None, None, _json(revision)))
        return {"project": project, "revision": revision, "idempotent": False}

    def list_projects(self):
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM projects ORDER BY created_at,id")]

    def get_project(self, project_id):
        _identifier(project_id)
        with self._connect() as db:
            return self._project(db, project_id)

    def revisions(self, project_id):
        _identifier(project_id)
        with self._connect() as db:
            self._require_project(db, project_id)
            return [json.loads(row[0]) for row in db.execute("SELECT body FROM revisions WHERE project_id=? ORDER BY number", (project_id,))]

    def revision(self, project_id, revision_id):
        _identifier(project_id)
        _identifier(revision_id)
        with self._connect() as db:
            row = db.execute("SELECT body FROM revisions WHERE project_id=? AND id=?", (project_id, revision_id)).fetchone()
            return json.loads(row[0]) if row else None

    def promote(self, project_id, base_revision_id, run_id, files, acceptance, verification):
        for identifier in (project_id, base_revision_id, run_id):
            _identifier(identifier)
        files = _files(files)
        acceptance = _text(acceptance, "验收源码", 65536)
        verification = _verification(verification, files, acceptance)
        source_sha256 = _digest(_json(files))
        with self._connect(write=True) as db:
            project = self._require_project(db, project_id)
            existing = db.execute("SELECT body FROM revisions WHERE run_id=?", (run_id,)).fetchone()
            if existing:
                revision = json.loads(existing[0])
                if (revision["project_id"] != project_id or revision["parent_id"] != base_revision_id
                        or revision["files"] != files or revision["acceptance"] != acceptance or revision["verification"] != verification):
                    raise ProjectConflictError("该 run_id 已用于不同的版本推进请求。")
                return {"project": project, "revision": revision, "idempotent": True}
            base = db.execute("SELECT number FROM revisions WHERE id=? AND project_id=?", (base_revision_id, project_id)).fetchone()
            if base is None:
                raise KeyError(base_revision_id)
            if project["head_revision_id"] != base_revision_id:
                raise ProjectConflictError("项目版本已推进；过期运行不能覆盖当前版本。")
            revision = {"id": uuid4().hex, "project_id": project_id, "number": base["number"] + 1,
                        "parent_id": base_revision_id, "files": files, "source_sha256": source_sha256,
                        "acceptance": acceptance, "verification": verification, "created_at": _now(), "run_id": run_id}
            db.execute("INSERT INTO revisions VALUES(?,?,?,?,?,?)", (revision["id"], project_id, revision["number"], base_revision_id, run_id, _json(revision)))
            changed = db.execute("UPDATE projects SET head_revision_id=? WHERE id=? AND head_revision_id=?",
                                 (revision["id"], project_id, base_revision_id)).rowcount
            if changed != 1:
                raise ProjectConflictError("项目头版本比较交换失败。")
            project["head_revision_id"] = revision["id"]
        return {"project": project, "revision": revision, "idempotent": False}

    def save_profile(self, body, profile_id=None):
        body = _profile_body(body)
        if profile_id is not None:
            _identifier(profile_id)
        with self._connect(write=True) as db:
            previous = db.execute("SELECT MAX(version) FROM profiles WHERE id=?", (profile_id,)).fetchone()[0] if profile_id else None
            if profile_id and previous is None:
                raise KeyError(profile_id)
            profile_id = profile_id or uuid4().hex
            value = {"id": profile_id, "version": (previous or 0) + 1, **body,
                     "sha256": _digest(_json(body)), "created_at": _now()}
            db.execute("INSERT INTO profiles VALUES(?,?,?)", (profile_id, value["version"], _json(value)))
        return value

    def list_profiles(self):
        with self._connect() as db:
            rows = db.execute("SELECT p.body FROM profiles p JOIN (SELECT id,MAX(version) version FROM profiles GROUP BY id) latest ON latest.id=p.id AND latest.version=p.version ORDER BY p.id")
            return [json.loads(row[0]) for row in rows]

    def profile(self, profile_id, version=None):
        _identifier(profile_id)
        if version is not None:
            _version(version)
        with self._connect() as db:
            row = (db.execute("SELECT body FROM profiles WHERE id=? ORDER BY version DESC LIMIT 1", (profile_id,)).fetchone()
                   if version is None else db.execute("SELECT body FROM profiles WHERE id=? AND version=?", (profile_id, version)).fetchone())
            return json.loads(row[0]) if row else None

    def save_knowledge(self, project_id, title, content, document_id=None):
        _identifier(project_id)
        if document_id is not None:
            _identifier(document_id)
        title = _text(title, "知识标题", 512, strip=True)
        content = _text(content, "知识正文", MAX_KNOWLEDGE_BYTES)
        with self._connect(write=True) as db:
            self._require_project(db, project_id)
            previous = (db.execute("SELECT project_id,MAX(version) AS version FROM knowledge_versions WHERE id=? GROUP BY project_id", (document_id,)).fetchone()
                        if document_id else None)
            if document_id and (previous is None or previous["project_id"] != project_id):
                raise KeyError(document_id)
            document_id = document_id or uuid4().hex
            value = {"id": document_id, "project_id": project_id, "version": previous["version"] + 1 if previous else 1,
                     "title": title, "content": content, "sha256": _digest(_json({"title": title, "content": content})),
                     "content_sha256": _digest(content), "created_at": _now()}
            db.execute("INSERT INTO knowledge_versions VALUES(?,?,?,?)", (document_id, project_id, value["version"], _json(value)))
        return value

    def knowledge(self, project_id):
        _identifier(project_id)
        with self._connect() as db:
            self._require_project(db, project_id)
            rows = db.execute("SELECT k.body FROM knowledge_versions k JOIN (SELECT id,MAX(version) version FROM knowledge_versions WHERE project_id=? GROUP BY id) latest ON latest.id=k.id AND latest.version=k.version WHERE k.project_id=? ORDER BY k.id", (project_id, project_id))
            return [json.loads(row[0]) for row in rows]

    def get_knowledge(self, project_id, document_id, version):
        _identifier(project_id)
        _identifier(document_id)
        _version(version)
        with self._connect() as db:
            row = db.execute("SELECT body FROM knowledge_versions WHERE project_id=? AND id=? AND version=?", (project_id, document_id, version)).fetchone()
            return json.loads(row[0]) if row else None

    def select_knowledge(self, project_id, selections):
        _identifier(project_id)
        if not isinstance(selections, list) or len(selections) > 32:
            raise ProjectValidationError("知识选择必须为最多 32 项的 id/version 数组。")
        selected, seen = [], set()
        with self._connect() as db:
            self._require_project(db, project_id)
            for item in selections:
                if not isinstance(item, dict) or set(item) != {"id", "version"}:
                    raise ProjectValidationError("每项知识选择需要且仅接受 id/version。")
                document_id, version = _identifier(item["id"]), _version(item["version"])
                if document_id in seen:
                    raise ProjectValidationError("同一知识文档不能重复选择。")
                seen.add(document_id)
                row = db.execute("SELECT body FROM knowledge_versions WHERE project_id=? AND id=? AND version=?", (project_id, document_id, version)).fetchone()
                if row is None:
                    raise KeyError(document_id)
                selected.append(json.loads(row[0]))
            if sum(len(document["content"].encode("utf-8")) for document in selected) > MAX_SELECTED_KNOWLEDGE_BYTES:
                raise ProjectValidationError("选中的知识正文总量超过 128 KiB。")
        return selected

    def save_execution(self, project_id, record):
        return self.commit_execution(project_id, record)

    def application_state(self, project_id):
        """Read a detached state snapshot; an unexecuted project has version 0."""
        _identifier(project_id)
        with self._connect() as db:
            self._require_project(db, project_id)
            row = db.execute("SELECT body,sha256,version FROM application_states WHERE project_id=?", (project_id,)).fetchone()
            return ({"state": json.loads(row["body"]), "sha256": row["sha256"], "version": row["version"]}
                    if row else {"state": {}, "sha256": _digest(_json({})), "version": 0})

    def commit_execution(self, project_id, record, state=None, expected_state_sha256=None):
        """Atomically append evidence and optionally CAS the application state.

The runner validates file paths/base64 before passing state. This boundary also
requires a bounded JSON object, independent of the caller. No state means only
append evidence; it never advances the state version or applies a state CAS.
"""
        _identifier(project_id)
        if not isinstance(record, dict):
            raise ProjectValidationError("执行证据必须为 JSON 对象。")
        record = _bounded_json(record)
        if state is not None:
            if not isinstance(state, dict):
                raise ProjectValidationError("应用数据必须为 JSON 对象。")
            state = _bounded_json(state, 131072)
            if not isinstance(expected_state_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_state_sha256):
                raise ProjectValidationError("保存应用数据必须提供读取时的状态哈希。")
        value = {"id": uuid4().hex, "project_id": project_id, "created_at": _now(), "record": record}
        with self._connect(write=True) as db:
            self._require_project(db, project_id)
            if state is not None:
                previous = db.execute("SELECT sha256,version FROM application_states WHERE project_id=?", (project_id,)).fetchone()
                before = previous["sha256"] if previous else _digest(_json({}))
                if before != expected_state_sha256:
                    raise ProjectConflictError("应用数据已变化；过期执行不能覆盖新状态，未追加本次提交。")
                encoded, fingerprint = _json(state), _digest(_json(state))
                if previous:
                    changed = db.execute("UPDATE application_states SET body=?,sha256=?,version=? WHERE project_id=? AND sha256=?",
                                         (encoded, fingerprint, previous["version"] + 1, project_id, expected_state_sha256)).rowcount
                    if changed != 1:
                        raise ProjectConflictError("应用数据比较交换失败。")
                else:
                    db.execute("INSERT INTO application_states VALUES(?,?,?,?)", (project_id, encoded, fingerprint, 1))
            db.execute("INSERT INTO executions VALUES(?,?,?)", (value["id"], project_id, _json(value)))
        return value

    def executions(self, project_id):
        _identifier(project_id)
        with self._connect() as db:
            self._require_project(db, project_id)
            return [json.loads(row[0]) for row in db.execute("SELECT body FROM executions WHERE project_id=? ORDER BY rowid", (project_id,))]
