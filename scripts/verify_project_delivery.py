"""Verify the declared expense CLI in Docker through the project API.

This intentionally changes ONLY the selected example project's application data.
It makes no model calls. Persisted request keys let an interrupted observer resume
without replaying an already submitted command. Use after the two declared rounds.
"""
import argparse
import json
from pathlib import Path
import urllib.error
import urllib.request
from uuid import uuid4


def request(base, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(base + path, data=data, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=120) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8001')
    parser.add_argument('--project-id', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    project = request(args.base_url, '/api/projects/' + args.project_id)
    revision = project['head_revision_id']
    bus = {'id': 2, 'title': 'Bus', 'cents': 300, 'category': 'travel'}
    cases = [
        (['report'], {'total_cents':1500,'categories':[{'category':'food','total_cents':1200,'count':1}, {'category':'travel','total_cents':300,'count':1}]}, False),
        (['delete','--id','1'], {'deleted_id':1,'remaining_count':1}, False),
        (['report'], {'total_cents':300,'categories':[{'category':'travel','total_cents':300,'count':1}]}, False),
        (['delete','--id','999'], None, True),
        (['list'], [bus], False),
    ]
    records = []
    for index, (command, expected, negative) in enumerate(cases, 1):
        target, intent = out / f'check-{index}.json', out / f'intent-{index}.json'
        if target.exists():
            record = json.loads(target.read_text(encoding='utf-8'))
        else:
            if intent.exists():
                body = json.loads(intent.read_text(encoding='utf-8'))
                if body['revision_id'] != revision or body['args'] != command:
                    raise RuntimeError('Existing verification intent belongs to a different revision')
            else:
                body = {'revision_id':revision,'entrypoint':'main.py','args':command,'idempotency_key':'delivery-'+uuid4().hex}
                with intent.open('x',encoding='utf-8') as stream:
                    json.dump(body,stream,ensure_ascii=False,indent=2)
            result = request(args.base_url,'/api/projects/'+args.project_id+'/execute',body)
            parsed = None
            if not negative:
                try:
                    parsed = json.loads(result['stdout'])
                except ValueError:
                    pass
            passed = (result.get('isolated') is True and result.get('container_invoked') is True
                      and ((negative and result.get('status') == 'failed' and result.get('exit_code') != 0 and not result.get('data_saved')
                            and result.get('data_before_sha256') == result.get('data_after_sha256'))
                           or (not negative and result.get('status') == 'succeeded' and result.get('exit_code') == 0 and parsed == expected)))
            if command[0] in ('report','list'):
                passed = passed and result.get('data_before_sha256') == result.get('data_after_sha256')
            record = {'command':command,'expected':expected,'expected_nonzero':negative,'passed':passed,
                      'classification':'expected_validation_error' if negative else 'application_behavior_check',
                      'execution':result,'model_calls':0}
            with target.open('x',encoding='utf-8') as stream:
                json.dump(record,stream,ensure_ascii=False,indent=2)
        records.append(record)
        print(json.dumps({'check':index,'command':command,'passed':record['passed'],
                          'exit_code':record['execution'].get('exit_code'),'execution_id':record['execution']['id']}),flush=True)
        if not record['passed']:
            raise RuntimeError('Application check failed; all evidence retained. No automatic retry.')
    report = {'project_id':args.project_id,'revision_id':revision,'passed':all(r['passed'] for r in records),
              'checks':len(records),'model_calls':0,'negative_cases_are_not_code_failures':True}
    (out/'summary.json').write_text(json.dumps(report,indent=2),encoding='utf-8')


if __name__ == '__main__':
    main()
