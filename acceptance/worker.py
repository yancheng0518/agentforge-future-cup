"""Untrusted-side RPC adapter. Its stdout is captured, never test-runner output."""
import json
import sys

sys.path.insert(0, "/candidate")
load_error = None
try:
    from todo import TodoStore
except Exception as exc:
    # Return syntax/import diagnostics through RPC instead of losing them in
    # candidate stderr. This worker remains the untrusted side of the boundary.
    load_error = exc

stores = {}
references = {}
METHODS = frozenset(("add", "list", "complete", "rename", "delete", "batch_complete",
                     "set_priority", "query", "export_json", "import_json"))


def encode(value):
    if isinstance(value, dict):
        ref = len(references) + 1
        references[ref] = value
        return {"$ref": ref, "items": value}
    if isinstance(value, list):
        return [encode(item) for item in value]
    return value


for line in sys.stdin:
    try:
        request = json.loads(line)
        if load_error is not None:
            raise load_error
        if request["op"] == "new":
            store_id = len(stores) + 1
            stores[store_id] = TodoStore()
            result = store_id
        elif request["op"] == "set":
            references[request["ref"]][request["key"]] = request["value"]
            result = None
        elif request["op"] in METHODS:
            args, kwargs = request.get("args", []), request.get("kwargs", {})
            if not isinstance(args, list) or not isinstance(kwargs, dict):
                raise ValueError("RPC args must be a list and kwargs must be an object")
            result = getattr(stores[request["store"]], request["op"])(*args, **kwargs)
        else:
            raise ValueError("Unsupported operation")
        response = {"ok": True, "value": encode(result)}
    except Exception as exc:
        response = {"ok": False, "type": type(exc).__name__, "message": str(exc)[:1000]}
    print(json.dumps(response), flush=True)
