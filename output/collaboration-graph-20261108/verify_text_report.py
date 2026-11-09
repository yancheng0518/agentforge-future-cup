"""Predeclared independent checks; candidate execution occurs only in Docker."""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
from agentforge.runtime import Runtime
from agentforge.autonomous_sandbox import AutonomousSandbox

SUITE = r'''import unittest
from bridge import call

class TextReportChecks(unittest.TestCase):
    def test_tokenize_exact_ascii_rules(self):
        self.assertEqual(call('text_tools', 'tokenize', 'Hi, HI! a2B 中文 cafe'), ['hi', 'hi', 'a', 'b', 'cafe'])

    def test_tokenize_empty_and_unicode_separators(self):
        self.assertEqual(call('text_tools', 'tokenize', ''), [])
        self.assertEqual(call('text_tools', 'tokenize', 'café中文X_42Y'), ['caf', 'x', 'y'])

    def test_counts_and_ties(self):
        self.assertEqual(call('word_report', 'summarize', 'red blue red green blue'), [
            {'word': 'blue', 'count': 2}, {'word': 'red', 'count': 2}, {'word': 'green', 'count': 1}])

    def test_limits(self):
        self.assertEqual(call('word_report', 'summarize', 'b a b a c', limit=1), [{'word': 'a', 'count': 2}])
        self.assertEqual(call('word_report', 'summarize', 'x x y', limit=0), [])
        self.assertEqual(call('word_report', 'summarize', 'x', limit=9), [{'word': 'x', 'count': 1}])

    def test_invalid_limits(self):
        for limit in [-1, True, False, 1.5, '2']:
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    call('word_report', 'summarize', 'x', limit=limit)
                with self.assertRaises(ValueError):
                    call('word_report', 'build_report', 'x', limit=limit)

    def test_report_totals_before_limit(self):
        self.assertEqual(call('word_report', 'build_report', 'red blue red green blue', limit=1), {
            'total_words': 5, 'unique_words': 3, 'items': [{'word': 'blue', 'count': 2}]})

    def test_report_zero_limit_keeps_totals(self):
        self.assertEqual(call('word_report', 'build_report', 'Hi hi world', limit=0), {
            'total_words': 3, 'unique_words': 2, 'items': []})

    def test_empty_report(self):
        self.assertEqual(call('word_report', 'summarize', ''), [])
        self.assertEqual(call('word_report', 'build_report', ''), {
            'total_words': 0, 'unique_words': 0, 'items': []})
'''


def sha(data):
    return hashlib.sha256(data).hexdigest()


async def main(run_id):
    if not re.fullmatch('[0-9a-f]{32}', run_id):
        raise ValueError('Expected a run UUID')
    source = ROOT / '.agentforge/autonomous/runs' / run_id
    evidence = json.loads((source / 'evidence.json').read_text(encoding='utf-8'))
    requirement = Path(__file__).with_name('ui-request.txt').read_text(encoding='utf-8').strip()
    assert evidence['run']['requirement'] == requirement
    files = {p.relative_to(source / 'candidate').as_posix(): p.read_text(encoding='utf-8')
             for p in (source / 'candidate').rglob('*.py')}
    candidate_hash = sha(json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())
    assert candidate_hash == evidence['run']['verified_hash']
    watched = [p for p in source.rglob('*') if p.is_file()]
    before = {p.relative_to(source).as_posix(): sha(p.read_bytes()) for p in watched}
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    destination = ROOT / '.agentforge/collaboration-graph-checks-20261108' / stamp
    destination.mkdir(parents=True, exist_ok=False)
    (destination / 'verify_text_report.py').write_bytes(Path(__file__).read_bytes())
    runtime = Runtime(ROOT, destination)
    sandbox = AutonomousSandbox(runtime)
    check_id = 'predeclared_text_checks'
    await sandbox.prepare(check_id, {'requirement': requirement, 'files': files, 'acceptance': SUITE,
        'provenance': {'kind': 'natural', 'acceptance_origin': 'maintainer_predeclared',
                       'origin_run_id': run_id, 'origin_candidate_sha256': candidate_hash,
                       'description': 'Fixed before model run; no model calls, no seeded defects.'}})
    result = await sandbox.test(check_id)
    after = {p.relative_to(source).as_posix(): sha(p.read_bytes()) for p in watched}
    report = {'source_run_id': run_id, 'candidate_hash': candidate_hash, 'model_calls': 0,
              'suite_sha256': sha(SUITE.encode()), 'result': result, 'source_unchanged': before == after,
              'source_hashes_before': before, 'source_hashes_after': after}
    (destination / 'evidence.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'evidence': str(destination / 'evidence.json'), 'status': result['status'],
                      'test_count': result['test_count'], 'attestation': result.get('attestation'),
                      'isolated': result['isolated'], 'source_unchanged': before == after}, ensure_ascii=True))
    return 0 if result['status'] == 'passed' and result['isolated'] and before == after else 2


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    raise SystemExit(asyncio.run(main(parser.parse_args().run_id)))
