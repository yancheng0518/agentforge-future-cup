"""Untrusted candidate execution entry point; invoked only inside Docker."""
import contextlib
import importlib
import json
import re
import sys


def main():
    request = json.loads(sys.stdin.buffer.read(65537))
    module, function = request["module"], request["function"]
    if not isinstance(module, str) or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", module):
        raise ValueError("Invalid module")
    if not isinstance(function, str) or not re.fullmatch(r"[A-Za-z_]\w*", function):
        raise ValueError("Invalid function")
    sys.path.insert(0, "/candidate")
    try:
        with contextlib.redirect_stdout(sys.stderr):
            target = getattr(importlib.import_module(module), function)
            result = target(*request["args"], **request["kwargs"])
        response = {"ok": True, "value": result}
        encoded = json.dumps(response, ensure_ascii=False, allow_nan=False)
    except BaseException as error:
        encoded = json.dumps({"ok": False, "type": type(error).__name__, "message": str(error)[:2000]}, ensure_ascii=False)
    sys.stdout.write(encoded)


if __name__ == "__main__":
    main()
