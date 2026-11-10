import assert from 'node:assert/strict'
import test from 'node:test'
import { deliveryReviewFailed, projectDelivery } from '../src/delivery.ts'
import type { ProjectDetail, ProjectRevision } from '../src/projects.ts'

const fingerprint = 'a'.repeat(64), acceptanceHash = 'b'.repeat(64)
function fixture() {
  const receipt = { status: 'passed', isolated: true, exit_code: 0, test_count: 8, candidate_sha256: fingerprint, candidate_hash: fingerprint, acceptance_sha256: acceptanceHash }
  const review = { passed: true, candidate_hash: fingerprint }
  const revision: ProjectRevision = { id: 'rev2', project_id: 'project', number: 2, parent_id: 'rev1', created_at: '', source_sha256: fingerprint, run_id: 'run',
    files: { 'main.py': 'print(1)' }, verification: { status: 'passed', candidate_sha256: fingerprint, verified_hash: fingerprint, test: receipt, review } }
  const run = { id: 'run', requirement: '增加导出', status: 'succeeded', created_at: '', verified_hash: fingerprint, quality_review: review, verifications: [receipt], publication: { status: 'saved', revision_id: 'rev2', source_sha256: fingerprint } }
  const project: ProjectDetail = { id: 'project', name: 'Example', created_at: '', head_revision_id: 'rev2', revisions: [revision], runs: [run], knowledge: [], executions: [] }
  return { project, revision, run, receipt, review }
}

test('delivery uses matching saved-version, Docker and review evidence', () => {
  const { project, revision } = fixture()
  assert.equal(projectDelivery(project, revision).status, 'passed')
  assert.equal(projectDelivery(project, revision).testCount, 8)
})
test('import and run identifiers cannot grant success by themselves', () => {
  const { project, revision } = fixture()
  assert.equal(projectDelivery(project, { ...revision, run_id: null }).status, 'unverified')
  assert.equal(projectDelivery(project, { ...revision, verification: {} }).status, 'needs_review')
  assert.equal(projectDelivery({ ...project, runs: [] }, revision).status, 'unavailable')
})
test('loading, failure and switching revisions remove success immediately', () => {
  const { project, revision } = fixture()
  assert.equal(projectDelivery(project, revision, { loading: true }).status, 'checking')
  assert.equal(projectDelivery(project, revision, { error: 'offline' }).status, 'unavailable')
  assert.equal(projectDelivery(project, null).status, 'checking')
  assert.equal(projectDelivery(project, { ...revision, project_id: 'other' }).status, 'unavailable')
})
test('historical versions use their own records, never the head version', () => {
  const { project, revision } = fixture()
  project.head_revision_id = 'rev3'
  assert.equal(projectDelivery(project, revision).status, 'passed')
  assert.equal(projectDelivery(project, { ...revision, id: 'rev1', run_id: null }).status, 'unverified')
  assert.equal(projectDelivery(project, { ...revision, id: 'rev3' }).status, 'needs_review')
})
test('posthoc rejection overrides previously passed frozen evidence', () => {
  const { project, revision } = fixture()
  project.runs![0].posthoc_review = { passed: false, summary: '需求未满足' }
  assert.equal(projectDelivery(project, revision).status, 'needs_review')
  assert.equal(deliveryReviewFailed(project.runs![0]), true)
})
test('mismatched source hash and absent current review cannot appear verified', () => {
  const { project, revision } = fixture()
  assert.equal(projectDelivery(project, { ...revision, source_sha256: 'c'.repeat(64) }).status, 'needs_review')
  project.runs![0].quality_review = null
  assert.equal(projectDelivery(project, revision).status, 'needs_review')
})
test('failed latest receipt or unfinished run invalidates delivery status', () => {
  const { project, revision, run } = fixture()
  run.verifications = [{ ...run.verifications[0], status: 'failed' }]
  assert.equal(projectDelivery(project, revision).status, 'needs_review')
  run.verifications = [{ ...run.verifications[0], status: 'passed' }]
  run.status = 'running'
  assert.equal(projectDelivery(project, revision).status, 'needs_review')
})
test('later receipt cannot change test scope or report failures behind the saved pass', () => {
  const { project, revision, run } = fixture()
  run.verifications = [{ ...run.verifications[0], acceptance_sha256: 'c'.repeat(64) }]
  assert.equal(projectDelivery(project, revision).status, 'needs_review')
  run.verifications = [{ ...run.verifications[0], acceptance_sha256: acceptanceHash, test_count: 0 }]
  assert.equal(projectDelivery(project, revision).status, 'needs_review')
  run.verifications = [{ ...run.verifications[0], test_count: 8 }]
  Object.assign(run.verifications[0], { attestation: { errors: 1 } })
  assert.equal(projectDelivery(project, revision).status, 'needs_review')
})
test('preview activity and successful program execution cannot grant acceptance', () => {
  const { project, revision } = fixture()
  project.previews = [{ id: 'preview', revision_id: revision.id, status: 'running', isolated: true }]
  project.executions = [{ id: 'execution', revision_id: revision.id, status: 'succeeded', isolated: true, exit_code: 0 }]
  assert.equal(projectDelivery(project, { ...revision, verification: null }).status, 'needs_review')
})
test('test receipts require positive integer counts and cannot hide failures', () => {
  for (const count of [0, -1, 2.5, '8']) {
    const { project, revision, receipt } = fixture()
    Object.assign(receipt, { test_count: count })
    assert.equal(projectDelivery(project, revision).status, 'needs_review')
  }
  const { project, revision, receipt } = fixture()
  Object.assign(receipt, { attestation: { failures: 1 } })
  assert.equal(projectDelivery(project, revision).status, 'needs_review')
})
test('Web evidence must bind the selected dependency runtime', () => {
  const { project, revision, receipt, review } = fixture()
  revision.web = { schema: 'python-web-v1', entrypoint: 'main.py', dependency_id: 'dependency', image_id: 'image', lock_sha256: 'lock', requirements: [] }
  assert.equal(projectDelivery(project, revision).status, 'needs_review')
  Object.assign(receipt, { image: 'image', dependency_id: 'dependency', lock_sha256: 'lock', web_runtime_sha256: acceptanceHash })
  Object.assign(review, { web_runtime_sha256: acceptanceHash })
  assert.equal(projectDelivery(project, revision).status, 'passed')
  revision.web.image_id = 'changed'
  assert.equal(projectDelivery(project, revision).status, 'needs_review')
})
