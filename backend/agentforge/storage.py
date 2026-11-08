"""SQLite stores immutable agent versions and append-only run evidence."""
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .definitions import AGENTS


def now():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript("""
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS agents(id TEXT, version INTEGER, body TEXT NOT NULL,
                PRIMARY KEY(id,version));
              CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, body TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL, body TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS steps(run_id TEXT, id TEXT, body TEXT NOT NULL,
                PRIMARY KEY(run_id,id));
              CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL, body TEXT NOT NULL);
            """)
            for agent in AGENTS:
                db.execute("INSERT OR IGNORE INTO agents VALUES(?,?,?)", (agent["id"], 1, encode(agent)))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def agents(self):
        with self.connect() as db:
            rows = db.execute("SELECT a.body FROM agents a JOIN (SELECT id,MAX(version) v FROM agents GROUP BY id) b ON a.id=b.id AND a.version=b.v ORDER BY a.id")
            return [json.loads(row[0]) for row in rows]

    def agent_versions(self, id):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT body FROM agents WHERE id=? ORDER BY version DESC", (id,))]

    def save_agent(self, agent):
        with self.lock, self.connect() as db:
            version = db.execute("SELECT COALESCE(MAX(version),0)+1 FROM agents WHERE id=?", (agent["id"],)).fetchone()[0]
            value = {**agent, "version": version}
            db.execute("INSERT INTO agents VALUES(?,?,?)", (value["id"], version, encode(value)))
        return value

    def create_run(self, value):
        with self.connect() as db:
            db.execute("INSERT INTO runs VALUES(?,?)", (value["id"], encode(value)))

    def run(self, id):
        with self.connect() as db:
            row = db.execute("SELECT body FROM runs WHERE id=?", (id,)).fetchone()
            return json.loads(row[0]) if row else None

    def update_run(self, id, **changes):
        with self.lock, self.connect() as db:
            row = db.execute("SELECT body FROM runs WHERE id=?", (id,)).fetchone()
            if row is None:
                raise KeyError(id)
            value = json.loads(row[0])
            value.update(changes, updated_at=now())
            db.execute("UPDATE runs SET body=? WHERE id=?", (encode(value), id))
        return value

    def runs(self):
        with self.connect() as db:
            rows = db.execute("SELECT body FROM runs ORDER BY rowid DESC LIMIT 100")
            return [json.loads(r[0]) for r in rows]

    def event(self, run_id, type, message, node_id=None, data=None):
        event = dict(timestamp=now(), type=type, node_id=node_id, message=message, data=data or {})
        with self.connect() as db:
            cursor = db.execute("INSERT INTO events(run_id,body) VALUES(?,?)", (run_id, encode(event)))
            event["id"] = cursor.lastrowid
        return event

    def events(self, run_id):
        with self.connect() as db:
            return [{**json.loads(r["body"]), "id": r["id"]} for r in db.execute("SELECT id,body FROM events WHERE run_id=? ORDER BY id", (run_id,))]

    def step(self, run_id, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO steps VALUES(?,?,?)", (run_id, value["id"], encode(value)))

    def steps(self, run_id):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT body FROM steps WHERE run_id=? ORDER BY rowid", (run_id,))]

    def snapshot(self, run_id, node_id, context):
        body = dict(timestamp=now(), node_id=node_id, context=context)
        with self.connect() as db:
            cursor = db.execute("INSERT INTO snapshots(run_id,body) VALUES(?,?)", (run_id, encode(body)))
        self.event(run_id, "snapshot", "节点快照已保存", node_id, {"snapshot_id": cursor.lastrowid})

    def complete_step(self, run_id, step, context):
        """Commit completion and its recovery context as one SQLite transaction."""
        with self.lock, self.connect() as db:
            row = db.execute("SELECT body FROM runs WHERE id=?", (run_id,)).fetchone()
            run = json.loads(row[0])
            run.update(context=context, updated_at=now())
            db.execute("UPDATE runs SET body=? WHERE id=?", (encode(run), run_id))
            db.execute("INSERT OR REPLACE INTO steps VALUES(?,?,?)", (run_id, step["id"], encode(step)))
            snapshot = dict(timestamp=now(), node_id=step["id"], context=context)
            cursor = db.execute("INSERT INTO snapshots(run_id,body) VALUES(?,?)", (run_id, encode(snapshot)))
            event = dict(timestamp=now(), type="snapshot", node_id=step["id"], message="节点和恢复上下文已原子保存", data={"snapshot_id": cursor.lastrowid})
            db.execute("INSERT INTO events(run_id,body) VALUES(?,?)", (run_id, encode(event)))

    def invalidate_steps(self, run_id, prefix):
        for step in self.steps(run_id):
            if step["id"] == prefix or step["id"].startswith(prefix + "/"):
                self.step(run_id, {**step, "status": "stale", "error": "候选内容改变，历史验证不再适用"})

    def snapshots(self, run_id, include_context=False):
        with self.connect() as db:
            values = [{**json.loads(r["body"]), "id": r["id"]} for r in db.execute("SELECT id,body FROM snapshots WHERE run_id=? ORDER BY id", (run_id,))]
        if not include_context:
            for value in values:
                value.pop("context", None)
        return values

    def recover_interrupted(self):
        with self.connect() as db:
            runs = [json.loads(r[0]) for r in db.execute("SELECT body FROM runs")]
        for run in runs:
            if run["status"] in ("running", "queued"):
                self.update_run(run["id"], status="blocked", summary="服务重启中断执行，可从已保存节点恢复。")
                for step in self.steps(run["id"]):
                    if step["status"] == "running":
                        self.step(run["id"], {**step, "status": "blocked", "error": "服务重启中断节点"})
                self.event(run["id"], "status", "服务重启；已保留成功节点，等待恢复。")
