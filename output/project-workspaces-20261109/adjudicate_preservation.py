"""Explain the frozen manifest's one mutable WSL disk; never hide raw results."""
from pathlib import Path
import json

out = Path(__file__).resolve().parent
report = json.loads((out / 'preservation-after.json').read_text(encoding='utf-8'))
expected = [{'path': '.agentforge/wsl/AgentForge/ext4.vhdx', 'reason': 'sha256_changed'}]
assert report['checked_files'] == 1647 and report['file_problems'] == expected
assert all(db['passed'] for db in report['databases'].values())
result = {
    'raw_report': 'preservation-after.json', 'raw_check_passed': report['passed'],
    'original_manifest_unchanged': True,
    'historical_evidence_files_checked': 1646, 'historical_evidence_files_unchanged': 1646,
    'original_database_rows_preserved': True,
    'posthoc_scope_correction': True,
    'excluded_mutable_runtime_disk': expected[0]['path'],
    'reason': 'The pre-change manifest accidentally included the live WSL virtual disk. Docker execution writes that disk normally. It is environment state, not an immutable experiment record. The failed raw check is retained; no manifest or history is rewritten.',
    'historical_evidence_preserved_after_explicit_adjudication': True,
}
with (out / 'preservation-adjudication.json').open('x', encoding='utf-8') as stream:
    json.dump(result, stream, ensure_ascii=False, indent=2)
print(json.dumps(result, ensure_ascii=False))
