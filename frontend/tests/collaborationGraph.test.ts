import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { buildReplay, projectFrame } from '../src/collaborationGraph.ts'
import type { Event, Run } from '../src/types.ts'

const event = (id: number, type: string, data: unknown = {}, node_id?: string): Event => ({
  id, type, data, node_id, message: `Observed ${type}`, timestamp: `2026-11-08T13:00:${String(id % 60).padStart(2, '0')}Z`,
})
const task = (id: string, depends_on: string[] = []) => ({ id, title: `Task ${id}`, instructions: `Implement ${id}`,
  depends_on, write_paths: [`${id}.py`] })
const run = (events: Event[], extra: Partial<Run> = {}): Run => ({
  id: 'auto:unit', engine: 'model-directed-v1', workflow_id: 'autonomous', requirement: 'Original request',
  mode: 'live', created_at: '2026-11-08T13:00:00Z', status: 'running', attempt: 0, events, ...extra,
})
const at = (value: Run, eventId: number) => {
  const replay = buildReplay(value)
  assert.equal(replay.limited, false)
  return projectFrame(value, replay.frames, replay.frames.findIndex(frame => frame.eventId === String(eventId)))
}
const verification = (index: number, status: 'passed' | 'failed', extra = {}) => ({ index, status, test_count: 3,
  candidate_hash: `hash-${index}`, candidate_origin: 'model', classification: 'natural_candidate',
  isolated: true, container_invoked: true, exit_code: status === 'passed' ? 0 : 1, ...extra })

test('event prefixes never expose final tasks, results, summary, metrics or later tests', () => {
  const value = run([
    event(1, 'created'), event(2, 'controller_decision', { index: 1, action: 'dispatch', tasks: [task('alpha')], summary: 'Plan A' }),
    event(3, 'plan_revision', { added: [task('alpha')] }), event(4, 'dispatch', { tasks: [{ id: 'alpha', agent_id: 'worker-live' }] }),
    event(5, 'task_result', { ...task('alpha'), agent_id: 'worker-live', status: 'succeeded', result: { summary: 'FUTURE_RESULT' } }, 'alpha'),
    event(6, 'verification', verification(1, 'passed', { stdout: 'FUTURE_TEST_OUTPUT' })),
    event(7, 'terminal', { status: 'succeeded', summary: 'FUTURE_FINAL_SUMMARY' }),
  ], { status: 'succeeded', summary: 'FUTURE_FINAL_SUMMARY', model_metrics: { model_calls: 987 },
       task_graph: [{ ...task('alpha'), agent_id: 'FUTURE_AGENT', status: 'succeeded', result: { summary: 'FUTURE_RESULT' } }],
       verifications: [verification(1, 'passed', { stdout: 'FUTURE_TEST_OUTPUT' })] })
  const first = at(value, 1)
  assert.equal(first.status, 'queued')
  assert.deepEqual(first.nodes.map(node => node.id), ['main'])
  assert.equal(first.metrics.requests, 0)
  assert.equal(JSON.stringify(first).includes('FUTURE_'), false)
  assert.equal(at(value, 2).nodes.some(node => node.kind === 'task'), false)
  const planned = at(value, 3).nodes.find(node => node.id === 'task:alpha')!
  assert.equal(planned.status, 'pending'); assert.equal(planned.agentId, undefined)
  assert.equal(planned.details.result, undefined)
  assert.equal(at(value, 4).nodes.find(node => node.id === 'task:alpha')?.status, 'running')
  assert.equal(JSON.stringify(at(value, 4)).includes('FUTURE_'), false)
  assert.equal(at(value, 5).nodes.find(node => node.id === 'task:alpha')?.summary, 'FUTURE_RESULT')
  assert.equal(at(value, 5).nodes.some(node => node.kind === 'tool'), false)
  assert.equal(at(value, 7).status, 'succeeded')
})

test('numeric event ids determine stable ordering; duplicates do not replay twice and global gaps are normal', () => {
  const ordered = [event(1, 'created'), event(8, 'model_request', { sequence: 1, role: 'main' }),
    event(10, 'model_response', { sequence: 1, status: 'responded', usage_known: true, tokens: 42 })]
  const a = buildReplay(run(ordered)), b = buildReplay(run([ordered[2], ordered[1], { ...ordered[1], id: '8' }, ordered[0]]))
  assert.equal(b.limited, false)
  assert.deepEqual(b.frames.map(frame => frame.eventId), ['1', '8', '10'])
  assert.deepEqual(projectFrame(run(ordered), a.frames, 2), projectFrame(run(ordered), b.frames, 2))
  assert.equal(projectFrame(run(ordered), b.frames, 2).metrics.knownTokens, 42)
})

test('conflicting duplicate ids and missing lifecycle evidence become a labelled read-only snapshot', () => {
  for (const value of [run([event(1, 'created'), event(1, 'created', { different: true })]),
    run([event(2, 'dispatch', { tasks: [task('alpha')] })]),
    run([event(1, 'created')], { status: 'succeeded' })]) {
    const replay = buildReplay(value)
    assert.equal(replay.limited, true); assert.equal(replay.frames.length, 1)
    assert.equal(replay.frames[0].snapshot, true)
    const graph = projectFrame(value, replay.frames, 0)
    assert.equal(graph.limited, true); assert.ok(graph.notice)
    assert.equal(graph.visibleEvents.length, 0)
  }
})

test('legacy records have no invented chronological replay or communication edges', () => {
  const value = run([], { engine: undefined, status: 'succeeded', steps: [
    { id: 'legacy-develop', label: 'Historical node', status: 'succeeded', agent_id: 'legacy-developer' },
  ] })
  const replay = buildReplay(value), snapshot = projectFrame(value, replay.frames, 0)
  assert.equal(replay.limited, true)
  assert.equal(snapshot.nodes.find(node => node.id === 'task:legacy-develop')?.label, 'Historical node')
  assert.deepEqual(snapshot.edges, [])
})

test('rejected dispatch and cancel decisions never instantiate or cancel execution nodes', () => {
  const rejected = { index: 1, action: 'dispatch', tasks: [task('uncreated')], summary: 'Invalid plan' }
  const cancel = { index: 3, action: 'cancel', task_ids: ['alpha'], summary: 'Cancel intent' }
  const value = run([event(1, 'created'), event(2, 'controller_decision', rejected),
    event(3, 'decision_rejected', { decision: rejected, reason: 'Invalid dependency' }),
    event(4, 'controller_decision', { index: 2, action: 'dispatch', tasks: [task('alpha')] }),
    event(5, 'plan_revision', { added: [task('alpha')] }), event(6, 'controller_decision', cancel),
    event(7, 'decision_rejected', { decision: cancel, reason: 'Rejected cancellation' }),
    event(8, 'plan_revision', { action: 'cancel', task_ids: ['alpha'], summary: 'Actual cancellation' })])
  assert.equal(at(value, 3).nodes.some(node => node.kind === 'task'), false)
  assert.equal(at(value, 7).nodes.find(node => node.id === 'task:alpha')?.status, 'pending')
  assert.equal(at(value, 8).nodes.find(node => node.id === 'task:alpha')?.status, 'cancelled')
  assert.equal(at(value, 8).nodes.some(node => node.id === 'task:uncreated'), false)
})

test('arbitrary task nodes carry real assignment and dependency edges, not fabricated Agent conversation', () => {
  const value = run([event(1, 'created'), event(2, 'controller_decision', { index: 7, action: 'dispatch', summary: 'Independent geometry work' }),
    event(3, 'plan_revision', { added: [task('geometry'), task('report', ['geometry'])] }),
    event(4, 'dispatch', { tasks: [{ id: 'geometry', agent_id: 'worker-random-id', write_paths: ['geometry.py'] }] })])
  const graph = at(value, 4)
  const node = graph.nodes.find(node => node.id === 'task:geometry')!
  assert.equal(node.agentId, 'worker-random-id')
  assert.equal((node.details.decision as { index: number }).index, 7)
  assert.ok(graph.edges.some(edge => edge.kind === 'dependency' && edge.source === 'task:geometry' && edge.target === 'task:report'))
  assert.ok(graph.edges.some(edge => edge.kind === 'dispatch' && edge.source === 'main' && edge.target === node.id))
  assert.equal(graph.edges.some(edge => edge.label.includes('聊天')), false)
})

test('HTTP response is not task completion; format retry and transport failure are distinct', () => {
  const value = run([event(1, 'created'), event(2, 'plan_revision', { added: [task('alpha')] }),
    event(3, 'dispatch', { tasks: [{ id: 'alpha', agent_id: 'worker-a' }] }),
    event(4, 'model_request', { sequence: 1, role: 'worker', format_retry: false }, 'alpha'),
    event(5, 'model_response', { sequence: 1, status: 'responded', usage_known: true, tokens: 10 }, 'alpha'),
    event(6, 'format_error', { sequence: 1, retry_scheduled: true, error: { kind: 'json_syntax' } }, 'alpha'),
    event(7, 'model_request', { sequence: 2, role: 'worker', format_retry: true }, 'alpha'),
    event(8, 'model_response', { sequence: 2, status: 'transport_failed', usage_known: false }, 'alpha'),
    event(9, 'task_result', { ...task('alpha'), status: 'failed', error: 'HTTP 429' }, 'alpha')])
  assert.equal(at(value, 5).nodes.find(node => node.id === 'task:alpha')?.status, 'running')
  assert.equal(at(value, 8).nodes.find(node => node.id === 'task:alpha')?.status, 'running')
  assert.equal(at(value, 9).nodes.find(node => node.id === 'task:alpha')?.status, 'failed')
  assert.deepEqual(at(value, 9).metrics, { requests: 2, knownTokens: 10, formatRetries: 1, transportFailures: 1 })
  const replay = buildReplay(value)
  assert.equal(replay.frames[5].category, 'protocol_failure')
  assert.equal(replay.frames[6].category, 'format_retry')
  assert.equal(replay.frames[7].category, 'transport_failure')
})

test('Docker is a tool: requested start never asserts container execution or later test counts', () => {
  const scope = { index: 1, candidate_hash: 'hash', candidate_origin: 'baseline', classification: 'input_baseline', stage: 'preparing', container_confirmed: false }
  const value = run([event(1, 'created'), event(2, 'controller_decision', { index: 1, action: 'verify' }),
    event(3, 'verification_started', scope), event(4, 'verification', verification(1, 'failed', { ...scope, container_invoked: true, isolated: true, test_count: 12 }))])
  assert.equal(at(value, 2).nodes.some(node => node.kind === 'tool'), false)
  const start = at(value, 3).nodes.find(node => node.id === 'test:1')!
  assert.equal(start.kind, 'tool'); assert.equal(start.status, 'preparing')
  assert.equal(start.details.isolationConfirmed, false); assert.equal(start.details.test_count, undefined)
  const result = at(value, 4).nodes.find(node => node.id === 'test:1')!
  assert.equal(result.details.isolationConfirmed, true); assert.equal(result.details.test_count, 12)
  assert.equal(result.details.startObserved, true)
})

test('old completed Docker records do not invent a start timestamp; interruption stays unconfirmed', () => {
  const older = at(run([event(1, 'created'), event(2, 'verification', verification(1, 'passed'))]), 2)
  assert.equal(older.nodes.find(node => node.id === 'test:1')?.details.startObserved, false)
  const interrupted = at(run([event(1, 'created'), event(2, 'verification_started', { index: 1, container_confirmed: false }),
    event(3, 'verification_interrupted', { index: 1, reason: 'cancelled', completion_unknown: true })]), 3)
  const node = interrupted.nodes.find(node => node.id === 'test:1')!
  assert.equal(node.status, 'interrupted'); assert.equal(node.details.isolationConfirmed, false)
})

test('baseline, seeded defect and generated-test failures are visibly separate; feedback never claims repair success', () => {
  const value = run([event(1, 'created'), event(2, 'verification', verification(1, 'failed', { classification: 'input_baseline' })),
    event(3, 'verification', verification(2, 'failed', { classification: 'seeded_defect_baseline' })),
    event(4, 'verification', verification(3, 'failed', { acceptance_origin: 'model_generated' })),
    event(5, 'controller_decision', { index: 1, action: 'dispatch', summary: 'Investigate failure' }),
    event(6, 'plan_revision', { added: [task('investigate')] })])
  const replay = buildReplay(value)
  assert.deepEqual(replay.frames.slice(1, 4).map(frame => frame.category), ['baseline', 'seeded_defect', 'generated_acceptance'])
  const graph = at(value, 6)
  assert.equal(graph.nodes.find(node => node.id === 'task:investigate')?.status, 'pending')
  assert.equal(graph.edges.filter(edge => edge.kind === 'feedback').length, 3)
  assert.equal(graph.edges.some(edge => /修复成功|自动修正成功/.test(edge.label)), false)
})

test('post-hoc rejection only appears at its actual event and preserves the original terminal status', () => {
  const value = run([event(1, 'created'), event(2, 'terminal', { status: 'succeeded', summary: 'Original test pass' }),
    event(3, 'posthoc_review', { passed: false, summary: 'Later independent rejection', original_status: 'succeeded' })],
    { status: 'succeeded', posthoc_review: { passed: false, summary: 'Later independent rejection' } })
  assert.equal(at(value, 2).status, 'succeeded')
  assert.equal(JSON.stringify(at(value, 2)).includes('Later independent rejection'), false)
  const final = at(value, 3)
  assert.equal(final.status, 'needs_human')
  assert.equal(final.nodes.find(node => node.id === 'main')?.details.originalStatus, 'succeeded')
  assert.equal(final.nodes.find(node => node.id === 'main')?.summary, 'Later independent rejection')
  assert.equal(final.nodes.find(node => node.id === 'main')?.details.originalSummary, 'Original test pass')
  assert.equal(at(value, 2).nodes.find(node => node.id === 'main')?.summary, 'Original test pass')
  assert.ok(final.nodes.some(node => node.kind === 'review' && node.status === 'failed'))
})

test('cancelled or recovered running tasks are never projected as successfully completed', () => {
  const value = run([event(1, 'created'), event(2, 'plan_revision', { added: [task('alpha'), task('beta')] }),
    event(3, 'dispatch', { tasks: [{ id: 'alpha' }, { id: 'beta' }] }),
    event(4, 'task_interrupted', { id: 'alpha', error: 'cancelled before completion' }, 'alpha'),
    event(5, 'interrupted_recovery', { metrics_before: { in_flight: 1 } })], { status: 'blocked' })
  const graph = at(value, 5)
  assert.equal(graph.status, 'blocked')
  assert.equal(graph.nodes.find(node => node.id === 'task:alpha')?.status, 'interrupted')
  assert.equal(graph.nodes.find(node => node.id === 'task:beta')?.status, 'interrupted')
})

test('terminal stops unfinished review and tool nodes without inventing successful completion', () => {
  const value = run([event(1, 'created'), event(2, 'contract_review_started', { proposal_index: 1 }),
    event(3, 'model_request', { sequence: 1, role: 'contract_review' }),
    event(4, 'model_response', { sequence: 1, status: 'transport_failed', usage_known: false }),
    event(5, 'verification_started', { index: 1, stage: 'preparing', container_confirmed: false }),
    event(6, 'terminal', { status: 'blocked', summary: 'Transport failed' })], { status: 'blocked' })
  assert.equal(at(value, 5).nodes.find(node => node.id === 'review:contract:1')?.status, 'running')
  const final = at(value, 6)
  for (const id of ['review:contract:1', 'test:1']) {
    const node = final.nodes.find(node => node.id === id)!
    assert.equal(node.status, 'interrupted'); assert.equal(node.details.completionUnknown, true)
    assert.match(node.summary!, /未获完成确认/)
  }
  assert.equal(final.nodes.find(node => node.id === 'test:1')?.details.isolationConfirmed, false)
})

test('review start and result use real proposal indexes and candidate hashes for stable identity', () => {
  const value = run([event(1, 'created'), event(2, 'contract_review_started', { proposal_index: 1 }),
    event(3, 'contract_review', { index: 1, passed: false, issues: ['Wrong ordering'] }),
    event(4, 'contract_review_started', { proposal_index: 2 }),
    event(5, 'contract_review', { index: 2, passed: true, issues: [] }),
    event(6, 'quality_review_started', { candidate_hash: 'actual-hash' }),
    event(7, 'quality_review', { candidate_hash: 'actual-hash', index: 1, passed: true }),
    event(8, 'terminal', { status: 'succeeded' })], { status: 'succeeded' })
  const nodes = at(value, 8).nodes.filter(node => node.kind === 'review')
  assert.deepEqual(nodes.map(node => [node.id, node.status]), [
    ['review:contract:1', 'failed'], ['review:contract:2', 'succeeded'], ['review:quality:actual-hash', 'succeeded'],
  ])
  assert.deepEqual(nodes[0].eventIds, ['2', '3'])
})

test('limited snapshots require a confirmed runtime receipt and retain unknown posthoc conclusions', () => {
  const value = run([], { status: 'blocked', verifications: [
    verification(1, 'passed', { container_invoked: false }), verification(2, 'passed', { status: 'blocked' }),
  ], posthoc_review: { summary: 'Review incomplete' } })
  const replay = buildReplay(value), graph = projectFrame(value, replay.frames, 0)
  assert.deepEqual(graph.nodes.filter(node => node.kind === 'tool').map(node => node.status), ['blocked', 'blocked'])
  assert.equal(graph.nodes.find(node => node.id === 'test:1')?.details.isolationConfirmed, false)
  assert.equal(graph.nodes.find(node => node.id === 'review:posthoc:final')?.status, 'unknown')
  assert.equal(graph.status, 'blocked')
})

test('projection is pure and supports empty prefix without touching frozen run data', () => {
  const value = run([event(1, 'created'), event(2, 'plan_revision', { added: [task('alpha')] })])
  const before = JSON.stringify(value), replay = buildReplay(value)
  assert.deepEqual(projectFrame(value, replay.frames, -1).nodes, [])
  const first = projectFrame(value, replay.frames, 1)
  first.nodes[0].details.requirement = 'mutated consumer'
  first.nodes.find(node => node.kind === 'task')!.writePaths.push('not-real.py')
  assert.equal(JSON.stringify(value), before)
  assert.equal(projectFrame(value, replay.frames, 1).nodes.find(node => node.kind === 'task')?.writePaths.includes('not-real.py'), false)
})

test('real successful run replays arbitrary tasks and Docker results solely from event prefixes', () => {
  const evidence = JSON.parse(readFileSync(new URL('./fixtures/05002763f0dc45ee805c68a04af789e9.json', import.meta.url), 'utf8'))
  const value = { ...evidence.run, events: evidence.events } as Run
  const replay = buildReplay(value)
  assert.equal(replay.limited, false)
  const created = projectFrame(value, replay.frames, 0)
  assert.deepEqual(created.nodes.map(node => node.id), ['main'])
  const plannedIndex = replay.frames.findIndex(frame => frame.type === 'plan_revision')
  assert.equal(projectFrame(value, replay.frames, plannedIndex - 1).nodes.some(node => node.kind === 'task'), false)
  const planned = projectFrame(value, replay.frames, plannedIndex)
  assert.equal(planned.nodes.filter(node => node.kind === 'task').length, 2)
  assert.ok(planned.nodes.filter(node => node.kind === 'task').every(node => node.status === 'pending' && node.details.result === undefined))
  const final = projectFrame(value, replay.frames, replay.frames.length - 1)
  assert.equal(final.status, 'succeeded')
  assert.deepEqual(final.nodes.filter(node => node.kind === 'tool').map(node => node.details.test_count), [12, 12])
  assert.equal(final.metrics.requests, 8)
})

test('real historically test-passing but invalid run only becomes rejected at post-hoc event', () => {
  const evidence = JSON.parse(readFileSync(new URL('./fixtures/d2b9e645213f41b8a56e706427f1a5ed.json', import.meta.url), 'utf8'))
  const value = { ...evidence.run, events: evidence.events } as Run
  const replay = buildReplay(value), index = replay.frames.findIndex(frame => frame.type === 'posthoc_review')
  assert.equal(replay.limited, false); assert.ok(index > 0)
  assert.equal(projectFrame(value, replay.frames, index - 1).status, 'succeeded')
  assert.equal(projectFrame(value, replay.frames, index).status, 'needs_human')
})
