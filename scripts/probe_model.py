"""Explicit, small real API probe. Output is redacted evidence, never credentials."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from agentforge.runtime import Runtime, RuntimeErrorSafe


def http_diagnostics(cause):
    """Classify bounded provider errors without retaining any provider text."""
    category = "provider_rejected_request"
    if cause.code == 401:
        category = "credential_or_authorization"
    elif cause.code == 403:
        category = "permission_denied"
    elif cause.code >= 500:
        category = "provider_unavailable"
    try:
        provider = json.loads(cause.read(16384).decode("utf-8", errors="replace"))
        error = provider.get("error", provider)
        message = error.get("message", error.get("msg", "")) if isinstance(error, dict) else error
        message = message.casefold() if isinstance(message, str) else ""
        # The explicit channel error must win over incidental words such as
        # "token" in a model name or diagnostic suffix. It is a routing failure.
        if "no available channel for model" in message:
            category = "model_channel_unavailable"
        elif any(term in message for term in ("insufficient", "balance", "quota", "余额", "欠费", "额度")):
            category = "quota_or_balance"
        elif any(term in message for term in ("not opened", "not enabled", "未开通", "未订购", "没有开通")):
            category = "model_not_enabled"
        elif any(term in message for term in ("api key", "apikey", "token", "unauthorized", "鉴权", "密钥")):
            category = "credential_or_authorization"
        elif any(term in message for term in ("forbidden", "permission", "denied", "权限", "禁止")):
            category = "permission_denied"
    except (ValueError, TypeError, AttributeError, OSError):
        pass
    hints = {
        "model_channel_unavailable": "平台当前没有可供该模型使用的服务渠道；请核查平台模型渠道状态与账户分组。",
        "provider_unavailable": "模型服务暂不可用；请核查平台服务状态后重试。",
        "quota_or_balance": "模型额度或余额不足；请核查平台账户额度。",
        "model_not_enabled": "模型服务尚未开通；请核查平台模型开通状态。",
        "credential_or_authorization": "模型凭据或授权校验失败；请核查平台密钥及其模型权限。",
        "permission_denied": "平台拒绝访问；请核查账户及模型权限。",
        "provider_rejected_request": "平台拒绝该请求；请核查平台接口要求。",
    }
    return {"http_status": cause.code, "error_category": category,
            "error": f"模型 HTTP {cause.code}；{hints[category]}"}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / ".agentforge/model-probe.json")
    args = parser.parse_args()
    runtime = Runtime(ROOT, ROOT / ".agentforge")
    settings = runtime.settings
    evidence = {"timestamp": datetime.now(timezone.utc).isoformat(), "base_url": settings["base_url"], "model": settings["model"],
                "kind": "live_model_probe", "passed": False, "requests": 0, "tool_calls_verified": False, "json_verified": False}
    started = time.monotonic()
    try:
        if not settings["api_key"] or not settings["model"]:
            raise RuntimeErrorSafe("请在本地 .env 填写比赛平台 API Key 和已开通的准确模型 ID。")
        tool = {"type": "function", "function": {"name": "read_project", "description": "Read a controlled example file.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}
        messages = [{"role": "user", "content": "Call read_project once. Then return JSON: {\"file\":\"todo.py\",\"ok\":true}."}]
        # Omniedu documents V4 as thinking-on by default; the bounded protocol
        # check does not need reasoning tokens and explicitly disables it.
        extra = {"thinking": {"type": "disabled"}} if settings["model"].startswith("deepseek-v4") else {}
        payload = {"model": settings["model"], "messages": messages, "tools": [tool], "tool_choice": {"type": "function", "function": {"name": "read_project"}}, "max_tokens": 256, **extra}
        evidence["requests"] += 1
        first = await asyncio.to_thread(runtime._post, payload)
        message = first["choices"][0]["message"]
        calls = message.get("tool_calls") or []
        if len(calls) != 1 or calls[0]["function"]["name"] != "read_project" or json.loads(calls[0]["function"].get("arguments") or "{}") != {}:
            raise RuntimeErrorSafe("服务没有返回预期只读工具调用；不能宣称工具兼容。")
        evidence["tool_calls_verified"] = True
        messages.append({k: v for k, v in message.items() if k in ("role", "content", "tool_calls")})
        project = await runtime.read_project()
        messages.append({"role": "tool", "tool_call_id": calls[0]["id"], "content": json.dumps(project)})
        evidence["requests"] += 1
        second = await asyncio.to_thread(runtime._post, {"model": settings["model"], "messages": messages, "response_format": {"type": "json_object"}, "max_tokens": 256, **extra})
        output = json.loads(second["choices"][0]["message"]["content"])
        evidence["json_verified"] = isinstance(output, dict) and output.get("ok") is True and output.get("file") == "todo.py"
        evidence["passed"] = evidence["json_verified"]
        evidence["total_tokens"] = sum(r.get("usage", {}).get("total_tokens", 0) for r in (first, second))
        if not evidence["passed"]:
            evidence["error"] = "JSON 内容不符合验收契约。"
    except RuntimeErrorSafe as exc:
        evidence["error"] = str(exc)
        # Runtime deliberately hides provider bodies. Record only categorized
        # diagnostics, never the raw response (which can echo credentials).
        cause = exc.__context__
        if isinstance(cause, urllib.error.HTTPError):
            evidence.update(http_diagnostics(cause))
    except Exception as exc:
        evidence["error"] = f"接口响应或协议未通过校验：{type(exc).__name__}"
    evidence["duration_ms"] = round((time.monotonic() - started) * 1000)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0 if evidence["passed"] else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
