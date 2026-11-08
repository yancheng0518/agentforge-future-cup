"""Container entry point. Never invoke this against candidates on the host.

Suite selection belongs to the trusted host runtime, not the candidate. Both
permitted roots are mounted read-only; the default preserves legacy runs.
"""
import argparse
import json
import sys
import unittest


def suite_directory(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("/acceptance", "/task_acceptance"), default="/acceptance")
    return parser.parse_args(argv).suite


def main(argv=None):
    directory = suite_directory(argv)
    # Import the trusted facade before discovery adds the selected suite root
    # to sys.path. A suite folder may contain a baseline todo.py for copying,
    # but acceptance must never accidentally import that baseline as the SUT.
    sys.path.insert(0, "/acceptance")
    sys.path.insert(0, "/acceptance/proxy")
    from todo import close_all
    suite = unittest.defaultTestLoader.discover(directory, pattern="test_todo.py")
    try:
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    finally:
        close_all()
    passed = result.wasSuccessful() and result.testsRun == 10
    print("AGENTFORGE_ACCEPTANCE=" + json.dumps({"tests": result.testsRun, "passed": passed}), flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
