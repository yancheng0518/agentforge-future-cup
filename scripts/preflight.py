"""Read-only readiness check; never runs candidate code or sends model requests."""
import asyncio
import json
from pathlib import Path
import platform
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from agentforge.runtime import Runtime


async def main():
    runtime = Runtime(ROOT, ROOT / ".agentforge")
    health = await runtime.health()
    print(json.dumps({"python": platform.python_version(), "root": str(ROOT), **health}, ensure_ascii=False, indent=2))
    return 0 if health["model"]["configured"] and health["sandbox"]["available"] else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
