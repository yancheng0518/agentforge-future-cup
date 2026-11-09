"""Trusted frozen Web unittest runner, executed only inside Docker."""
import importlib.util
import json
import sys
import unittest

sys.path.insert(0, "/runner")
from web_bridge import start_server, stop_server, server_evidence


def main():
    nonce, entrypoint = sys.argv[1:3]
    # Import only the separately mounted trusted acceptance; never candidate code.
    spec = importlib.util.spec_from_file_location("frozen_web_acceptance", "/trusted/test_acceptance.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    discovered = suite.countTestCases()
    start_server(entrypoint)
    try:
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        evidence = server_evidence()
        passed = (discovered > 0 and result.testsRun == discovered and result.wasSuccessful()
                  and not result.skipped and not result.expectedFailures and not evidence["startup_error"]
                  and evidence["server_exit_code"] is None and evidence["http_requests"] > 0)
        attestation = {"nonce": nonce, "tests": result.testsRun, "discovered": discovered,
                       "passed": passed, "failures": len(result.failures), "errors": len(result.errors),
                       "skipped": len(result.skipped), "expected_failures": len(result.expectedFailures),
                       "verification_kind": "http", **evidence}
        print("AGENTFORGE_AUTONOMOUS=" + json.dumps(attestation, ensure_ascii=True, sort_keys=True))
        return 0 if passed else 1
    finally:
        stop_server()


if __name__ == "__main__":
    raise SystemExit(main())
