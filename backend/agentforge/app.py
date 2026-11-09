"""Local FastAPI API. Start with uvicorn agentforge.app:app --host 127.0.0.1."""
import asyncio
import os
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .definitions import WORKFLOWS
from .engine import Engine, candidate_fingerprint
from .runtime import Runtime
from .storage import Store
from .task_catalog import TASKS, list_tasks, snapshot_task
from .autonomous_api import mount_autonomous


class AgentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,60}$")
    name: str = Field(min_length=1, max_length=100)
    role: Literal["planner", "developer", "tester", "reviewer", "fixer"]
    capabilities: list[Literal["plan", "develop", "test", "review", "fix"]] = Field(min_length=1, max_length=5)
    enabled: bool = True
    prompt: str = Field(default="", max_length=12000)
    model: str = Field(default="default", max_length=100)
    tools: list[Literal["read_project", "knowledge_search", "candidate_diff", "run_acceptance"]] = Field(default_factory=list)


class RunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requirement: str = Field(min_length=1, max_length=8000)
    workflow_id: Literal["development", "review"] = "development"
    mode: Literal["mock", "live"] = "mock"
    task_id: Literal["completion", "bulk_edit", "priority_query", "json_merge"] = "completion"


class ApplyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: StrictBool


def public_run(run):
    return {k: v for k, v in run.items() if k not in ("context", "agent_versions", "workflow", "task_snapshot")}


def create_app(root=None, data_dir=None, runtime=None):
    root = Path(root or Path(__file__).resolve().parents[2])
    data_dir = Path(data_dir or os.environ.get("AGENTFORGE_DATA_DIR", root / ".agentforge"))
    store = Store(data_dir / "agentforge.sqlite3")
    runtime = runtime or Runtime(root, data_dir)
    engine = Engine(store, runtime)
    apply_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        store.recover_interrupted()
        app.state.autonomous_engine.recover_interrupted()
        yield
        active = [t for runner in (engine, app.state.autonomous_engine)
                  for t in runner.tasks.values() if not t.done()]
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)

    app = FastAPI(title="AgentForge local workbench", version="0.1.0", lifespan=lifespan)
    app.state.store, app.state.engine, app.state.runtime = store, engine, runtime
    mount_autonomous(app, root, data_dir)
    origins = ["http://127.0.0.1:5173", "http://localhost:5173", "http://127.0.0.1:8000", "http://localhost:8000"]
    ui_port = int(os.environ.get("AGENTFORGE_UI_PORT", "8000"))
    if not 1024 <= ui_port <= 65535:
        raise ValueError("AGENTFORGE_UI_PORT must be between 1024 and 65535")
    origins.extend([f"http://127.0.0.1:{ui_port}", f"http://localhost:{ui_port}"])
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST", "PUT"], allow_headers=["Content-Type"])
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def restrict_browser_writes(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin not in origins:
                return JSONResponse(status_code=403, content={"detail": "仅允许本地工作台发起写操作"})
        return await call_next(request)

    def require_run(run_id):
        run = store.run(run_id)
        if not run:
            raise HTTPException(404, "运行不存在")
        return run

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "version": "0.1.0", **await runtime.health()}

    @app.get("/api/agents")
    async def agents():
        return store.agents()

    @app.get("/api/agents/{agent_id}/versions")
    async def agent_versions(agent_id: str):
        return store.agent_versions(agent_id)

    @app.post("/api/agents", status_code=201)
    async def add_agent(body: AgentInput):
        if store.agent_versions(body.id):
            raise HTTPException(409, "Agent 已存在，请使用更新接口")
        return store.save_agent(body.model_dump())

    @app.put("/api/agents/{agent_id}")
    async def edit_agent(agent_id: str, body: AgentInput):
        if agent_id != body.id:
            raise HTTPException(422, "Agent ID 不一致")
        if not store.agent_versions(agent_id):
            raise HTTPException(404, "Agent 不存在")
        return store.save_agent(body.model_dump())

    @app.get("/api/workflows")
    async def workflows():
        return deepcopy(list(WORKFLOWS.values()))

    @app.get("/api/tasks")
    async def tasks():
        return list_tasks(root)

    @app.get("/api/knowledge")
    async def knowledge(q: str = "需求 完成 筛选 规范 测试"):
        return await runtime.retrieve(q[:8000])

    @app.get("/api/project")
    async def project(task_id: str = "completion"):
        if task_id not in TASKS:
            raise HTTPException(422, "未知受控任务")
        if task_id != "completion":
            task = snapshot_task(root, task_id)
            return {"files": [{"path": "todo.py", "content": task["baseline"]}], "task_id": task_id}
        # Read only the controlled source fixture, never arbitrary filesystem paths.
        sample = root / "examples" / "todo"
        files = []
        if sample.is_dir():
            for path in sorted(sample.glob("*")):
                if path.is_file() and path.suffix in (".py", ".md", ".txt", ".json") and not path.is_symlink():
                    files.append({"path": path.name, "content": path.read_text(encoding="utf-8")})
        return {"files": files}

    @app.get("/api/runs")
    async def runs():
        return [public_run(run) for run in store.runs()]

    @app.post("/api/runs", status_code=201)
    async def create_run(body: RunInput):
        if not body.requirement.strip():
            raise HTTPException(422, "需求不能为空")
        if sum(not task.done() for task in engine.tasks.values()) >= 4:
            raise HTTPException(429, "最多并发 4 个运行，请等待已有运行完成")
        if body.mode == "live":
            state = await runtime.health()
            if not state.get("model", {}).get("configured"):
                raise HTTPException(409, "真实模型未配置；请在服务端本地配置密钥")
        try:
            run = engine.create_run(body.requirement.strip(), body.workflow_id, body.mode, body.task_id)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(422, str(exc)) from None
        engine.start(run["id"])
        return public_run(run)

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str):
        run = require_run(run_id)
        try:
            diff = await runtime.diff(run_id)
        except (FileNotFoundError, ValueError, KeyError):
            diff = {"files": [], "unified_diff": ""}
        except RuntimeError as exc:
            diff = {"files": [], "unified_diff": "", "error": str(exc)}
        return {**public_run(run), "events": store.events(run_id), "steps": store.steps(run_id),
                "snapshots": store.snapshots(run_id), "diff": diff,
                "agent_versions": run["agent_versions"], "workflow": run["workflow"]}

    @app.post("/api/runs/{run_id}/resume")
    async def resume(run_id: str):
        run = require_run(run_id)
        if run["status"] not in ("blocked", "failed"):
            raise HTTPException(409, "仅能恢复阻塞或失败的运行；修正上限后需新建运行")
        if run_id in engine.running:
            raise HTTPException(409, "运行正在执行")
        if sum(not task.done() for task in engine.tasks.values()) >= 4:
            raise HTTPException(429, "最多并发 4 个运行，请等待已有运行完成")
        store.event(run_id, "status", "收到恢复请求；保留成功步骤和当时配置版本。")
        store.update_run(run_id, status="queued", summary="等待恢复")
        engine.start(run_id)
        return public_run(store.run(run_id))

    @app.post("/api/runs/{run_id}/apply")
    async def apply(run_id: str, body: ApplyInput):
        if not body.confirmed:
            raise HTTPException(422, "应用修改需要明确确认")
        async with apply_lock:
            run = require_run(run_id)
            if run["status"] != "succeeded" or run["workflow_id"] != "development":
                raise HTTPException(409, "仅能应用独立隔离验收和审查通过的开发候选")
            if run.get("applied"):
                raise HTTPException(409, "该候选已经应用")
            try:
                if candidate_fingerprint(await runtime.diff(run_id)) != run.get("verified_hash"):
                    raise HTTPException(409, "候选内容在验证后发生改变，拒绝应用；请新建运行重新验收")
                result = await runtime.apply(run_id)
            except (ValueError, RuntimeError) as exc:
                raise HTTPException(409, str(exc)) from exc
            store.update_run(run_id, applied=True)
            store.event(run_id, "tool", "用户确认后应用候选修改", data={"tool": "apply_candidate_to_target", "output": result})
            return result

    # A built UI can share the API's localhost port. This avoids Windows/WSL
    # reserved Vite ports without widening origins or binding to the LAN.
    frontend = root / "frontend" / "dist"
    if (frontend / "index.html").is_file():
        @app.get("/", include_in_schema=False)
        async def workbench():
            return FileResponse(frontend / "index.html", headers={"Cache-Control": "no-store"})

        if (frontend / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=frontend / "assets"), name="workbench-assets")
    return app


app = create_app()
