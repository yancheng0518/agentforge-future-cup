"""HTTP contract checks with StubRuntime; never real model or isolation evidence."""
import asyncio
from pathlib import Path
import sys

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from agentforge.app import create_app
from test_engine import StubRuntime


def test_live_not_configured_and_agent_versioning(tmp_path):
    async def scenario():
        runtime = StubRuntime()
        app = create_app(tmp_path, tmp_path / "data", runtime)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.post("/api/runs", json={"requirement": "task", "mode": "live"})
            assert response.status_code == 409
            agents = (await client.get("/api/agents")).json()
            agent = agents[0]
            old_version = agent.pop("version")
            agent["prompt"] = "new prompt"
            response = await client.put(f"/api/agents/{agent['id']}", json=agent)
            assert response.status_code == 200
            assert response.json()["version"] == old_version + 1
            versions = (await client.get(f"/api/agents/{agent['id']}/versions")).json()
            assert len(versions) == 2
            assert (await client.post("/api/runs", json={"requirement": " "})).status_code == 422
            assert (await client.post("/api/runs", headers={"Origin": "https://untrusted.invalid"}, json={"requirement": "task"})).status_code == 403
    asyncio.run(scenario())


def test_apply_requires_confirmation_validation_and_unchanged_candidate(tmp_path):
    async def scenario():
        runtime = StubRuntime(blocked=True)
        app = create_app(tmp_path, tmp_path / "data", runtime)
        engine, store = app.state.engine, app.state.store
        run = engine.create_run("complete and filter")
        await engine.execute(run["id"])
        url = f"/api/runs/{run['id']}/apply"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            assert (await client.post(url, json={"confirmed": False})).status_code == 422
            assert (await client.post(url, json={"confirmed": "true"})).status_code == 422
            assert (await client.post(url, json={"confirmed": True})).status_code == 409
            runtime.blocked = False
            await engine.execute(run["id"])
            assert store.run(run["id"])["status"] == "succeeded"
            runtime.tampered = True
            assert (await client.post(url, json={"confirmed": True})).status_code == 409
            runtime.tampered = False
            runtime.conflict = True
            assert (await client.post(url, json={"confirmed": True})).status_code == 409
            runtime.conflict = False
            assert (await client.post(url, json={"confirmed": True})).status_code == 200
            assert runtime.counts["apply"] == 1
            assert (await client.post(url, json={"confirmed": True})).status_code == 409
            detail = (await client.get(f"/api/runs/{run['id']}")).json()
            assert detail["events"] and detail["steps"] and detail["snapshots"]
            assert "context" not in detail
    asyncio.run(scenario())
