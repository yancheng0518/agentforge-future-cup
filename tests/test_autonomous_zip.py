"""Candidate ZIP API contract tests; no model, Docker or candidate execution."""
import asyncio
from copy import deepcopy
import hashlib
import io
import json
import zipfile
from unittest.mock import AsyncMock

import pytest

from test_autonomous_api import make_app, client_for, BUNDLE


def fingerprint(files):
    return hashlib.sha256(json.dumps(files, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def test_zip_contains_actual_source_and_manifest_without_tests_or_configuration(tmp_path):
    async def scenario():
        app, engine = make_app(tmp_path)
        bundle = deepcopy(BUNDLE)
        bundle["files"] = {"math_tools/__init__.py": "", "math_tools/totals.py": "# 中文注释\ndef total(values):\n    return sum(values)\n"}
        run = engine.create_run(bundle)
        expected_hash = fingerprint(bundle["files"])
        engine.store.update_run(run["id"], status="succeeded", verified_hash=expected_hash,
                                intake={"status": "completed", "acceptance_origin": "model_generated"})
        engine.sandbox.read = AsyncMock(return_value=deepcopy(bundle["files"]))
        engine.sandbox.fingerprint = AsyncMock(return_value=expected_hash)
        async with client_for(app) as client:
            response = await client.get(f"/api/autonomous/runs/{run['id']}/candidate.zip")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert response.headers["content-disposition"] == f'attachment; filename="{run["id"]}-candidate.zip"'
        assert response.headers["cache-control"] == "no-store"
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert archive.testzip() is None
            assert set(archive.namelist()) == {*bundle["files"], "manifest.json"}
            for path, content in bundle["files"].items():
                assert archive.read(path).decode("utf-8") == content
            manifest = json.loads(archive.read("manifest.json"))
        assert manifest["status"] == "succeeded"
        assert manifest["candidate_sha256"] == expected_hash
        assert manifest["verification_matches_candidate"] is True
        assert manifest["acceptance_origin"] == "model_generated"
        assert manifest["applied_to_source_project"] is False
        assert manifest["business_verification"] == "not_reviewed"
        assert "model_config" not in manifest and "acceptance" not in manifest
        engine.runtime._post.assert_not_called()
        engine.runtime._command.assert_not_called()
    asyncio.run(scenario())


def test_zip_posthoc_semantic_failure_overrides_historical_success_and_test_hash(tmp_path):
    async def scenario():
        app, engine = make_app(tmp_path)
        run = engine.create_run(BUNDLE)
        expected_hash = fingerprint(BUNDLE["files"])
        posthoc = {"passed": False, "issues": ["排序行为违反原始需求"], "summary": "历史测试通过不能证明业务正确",
                   "candidate_hash": expected_hash, "origin": "posthoc_review"}
        engine.store.update_run(run["id"], status="succeeded", verified_hash=expected_hash, posthoc_review=posthoc)
        engine.sandbox.read = AsyncMock(return_value=deepcopy(BUNDLE["files"]))
        engine.sandbox.fingerprint = AsyncMock(return_value=expected_hash)
        async with client_for(app) as client:
            response = await client.get(f"/api/autonomous/runs/{run['id']}/candidate.zip")
        assert response.status_code == 200
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        assert manifest["status"] == "succeeded" and manifest["verification_matches_candidate"] is True
        assert manifest["business_verification"] == "rejected"
        assert manifest["posthoc_review"] == posthoc
    asyncio.run(scenario())


def test_zip_not_prepared_returns_409_and_unknown_run_404(tmp_path):
    async def scenario():
        app, engine = make_app(tmp_path)
        run = engine.create_run(BUNDLE)
        engine.sandbox.read = AsyncMock(side_effect=FileNotFoundError())
        async with client_for(app) as client:
            assert (await client.get(f"/api/autonomous/runs/{run['id']}/candidate.zip")).status_code == 409
            assert (await client.get("/api/autonomous/runs/unknown/candidate.zip")).status_code == 404
    asyncio.run(scenario())


@pytest.mark.parametrize("path", ["../escape.py", "/absolute.py", "pkg/../../escape.py", "C:/escape.py", "pkg\\escape.py", "pkg//escape.py", "file.txt"])
def test_zip_defensively_rejects_unsafe_paths(tmp_path, path):
    async def scenario():
        app, engine = make_app(tmp_path)
        run = engine.create_run(BUNDLE)
        engine.sandbox.read = AsyncMock(return_value={path: "data"})
        engine.sandbox.fingerprint = AsyncMock(return_value="unused")
        async with client_for(app) as client:
            response = await client.get(f"/api/autonomous/runs/{run['id']}/candidate.zip")
        assert response.status_code == 409
        engine.sandbox.fingerprint.assert_not_called()
    asyncio.run(scenario())


def test_zip_does_not_export_torn_candidate_snapshot(tmp_path):
    async def scenario():
        app, engine = make_app(tmp_path)
        run = engine.create_run(BUNDLE)
        engine.sandbox.read = AsyncMock(return_value=BUNDLE["files"])
        engine.sandbox.fingerprint = AsyncMock(return_value="changed-after-read")
        async with client_for(app) as client:
            response = await client.get(f"/api/autonomous/runs/{run['id']}/candidate.zip")
        assert response.status_code == 409
        assert "变化" in response.json()["detail"]
    asyncio.run(scenario())
