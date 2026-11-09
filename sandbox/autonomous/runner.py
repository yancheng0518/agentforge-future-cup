"""Trusted acceptance parent; deliberately never adds /candidate to sys.path."""
import importlib.util
import json
import sys
import unittest

sys.path.insert(0, "/runner")


def main():
    nonce = sys.argv[1]
    spec = importlib.util.spec_from_file_location("frozen_acceptance", "/trusted/test_acceptance.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    discovered = suite.countTestCases()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    passed = discovered > 0 and result.testsRun == discovered and result.wasSuccessful() and not result.skipped and not result.expectedFailures
    attestation = {"nonce": nonce, "tests": result.testsRun, "discovered": discovered, "passed": passed,
                   "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped),
                   "expected_failures": len(result.expectedFailures)}
    print("AGENTFORGE_AUTONOMOUS=" + json.dumps(attestation, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
