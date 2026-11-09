"""Separate API/data store for model-directed runs; legacy records stay untouched."""
import asyncio
import hashlib
import io
import json
import re
import zipfile
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from .autonomous import AutonomousEngine
from .runtime import Runtime
from .autonomous_sandbox import _sources
from .storage import Store


class AutonomousRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bundle: dict
    budget: dict = Field(default_factory=dict)


class AutonomousRequestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requirement: str = Field(min_length=1, max_length=8000)
    budget: dict = Field(default_factory=dict)


def mount_autonomous(app, root, data_dir):
    data_dir = data_dir / "autonomous"
    store = Store(data_dir / "agentforge.sqlite3")
    runtime = Runtime(root, data_dir)
    engine = AutonomousEngine(store, runtime)
    create_lock = asyncio.Lock()
    router = APIRouter(prefix="/api/autonomous")

    def require_run(run_id):
        run = store.run(run_id)
        if not run:
            raise HTTPException(404, "动态运行不存在")
        return run

    @router.post("/requests", status_code=201)
    async def create_request(body: AutonomousRequestInput):
        health = await runtime.health()
        if not health["model"]["configured"] or not health["sandbox"]["available"]:
            raise HTTPException(409, "需要真实模型配置和 Docker 才能启动；不会切换为模拟执行")
        async with create_lock:
            if sum(not task.done() for task in engine.tasks.values()) >= 2:
                raise HTTPException(429, "最多并发 2 个动态运行")
            try:
                run = engine.create_run_from_requirement(body.requirement, body.budget)
            except (ValueError, RuntimeError) as exc:
                raise HTTPException(422, str(exc)) from None
            engine.start(run["id"])
        return {key: value for key, value in run.items() if key != "bundle"}

    @router.post("/runs", status_code=201)
    async def create(body: AutonomousRunInput):
        health = await runtime.health()
        if not health["model"]["configured"] or not health["sandbox"]["available"]:
            raise HTTPException(409, "动态运行需要真实模型配置和 Docker；不会切换模拟或主机执行")
        async with create_lock:
            if sum(not task.done() for task in engine.tasks.values()) >= 2:
                raise HTTPException(429, "最多并发 2 个动态运行")
            try:
                web = (body.bundle.get('provenance') or {}).get('web')
                if web:
                    await app.state.validate_web_runtime(web)
                run = engine.create_run(body.bundle, body.budget)
            except (ValueError, RuntimeError) as exc:
                raise HTTPException(422, str(exc)) from None
            engine.start(run["id"])
        return {k: v for k, v in run.items() if k != "bundle"}

    @router.get("/runs")
    async def list_runs():
        return [{k: v for k, v in run.items() if k != "bundle"} for run in store.runs()]

    @router.get("/runs/{run_id}")
    async def get_run(run_id: str):
        require_run(run_id)
        return engine.export(run_id)

    @router.get("/runs/{run_id}/candidate")
    async def candidate(run_id: str):
        require_run(run_id)
        try:
            return {"files": await engine.sandbox.read(run_id),
                    "sha256": await engine.sandbox.fingerprint(run_id)}
        except FileNotFoundError:
            raise HTTPException(409, "候选目录尚未准备完成") from None
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from None

    @router.get("/runs/{run_id}/candidate.zip")
    async def candidate_zip(run_id: str):
        run = require_run(run_id)
        try:
            files = await engine.sandbox.read(run_id)
            # The sandbox already bounds and validates files. Keep ZIP paths safe
            # independently so a future sandbox adapter cannot create Zip Slip.
            if _sources(files) != files:
                raise HTTPException(409, "候选文件不符合安全导出路径约束")
            fingerprint = hashlib.sha256(json.dumps(files, ensure_ascii=False, sort_keys=True,
                                                      separators=(",", ":")).encode("utf-8")).hexdigest()
            if fingerprint != await engine.sandbox.fingerprint(run_id):
                raise HTTPException(409, "候选正在变化，请稍后重新导出")
        except FileNotFoundError:
            raise HTTPException(409, "候选目录尚未准备完成") from None
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from None
        provenance = (run.get("bundle") or {}).get("provenance", {})
        review_keys = {"passed", "issues", "summary", "origin", "candidate_hash", "timestamp"}
        quality_review = {key: value for key, value in (run.get("quality_review") or {}).items() if key in review_keys}
        posthoc_review = {key: value for key, value in (run.get("posthoc_review") or {}).items() if key in review_keys}
        current_reviews = [review for review in (quality_review, posthoc_review)
                           if review and review.get("candidate_hash", fingerprint) == fingerprint]
        business_verification = ("rejected" if any(review.get("passed") is False for review in current_reviews)
                                 else "model_review_passed" if quality_review.get("passed") is True
                                 and quality_review.get("candidate_hash") == fingerprint else "not_reviewed")
        manifest = {"schema": "agentforge-candidate-export-v1", "run_id": run_id,
                    "status": run["status"], "candidate_sha256": fingerprint,
                    "verified_sha256": run.get("verified_hash"),
                    "verification_matches_candidate": run.get("verified_hash") == fingerprint,
                    "acceptance_origin": run.get("intake", {}).get("acceptance_origin")
                        or provenance.get("acceptance_origin", "maintainer_provided"),
                    "files": sorted(files), "applied_to_source_project": False,
                    "quality_review": quality_review or None, "posthoc_review": posthoc_review or None,
                    "business_verification": business_verification,
                    "notice": "状态和 Docker 测试通过不等于业务语义正确；模型审查也不是人工验证。"}
        if provenance.get('web'):
            manifest.update(web=provenance['web'], verified_runtime_sha256=run.get('verified_runtime_sha256'))
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path, content in sorted(files.items()):
                archive.writestr(path, content.encode("utf-8"))
            metadata_name = 'manifest.json'
            while metadata_name in files:
                metadata_name = '_' + metadata_name
            archive.writestr(metadata_name, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
        filename = (re.sub(r"[^A-Za-z0-9_-]", "", run_id)[:80] or "candidate") + "-candidate.zip"
        return Response(buffer.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})

    app.include_router(router)
    app.state.autonomous_engine = engine
    app.state.autonomous_store = store
    from .projects_api import mount_projects
    mount_projects(app, root, data_dir.parent, engine)
    return engine
