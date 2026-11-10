import type { ProjectDetail, ProjectRevision } from './projects.ts'

const object = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const hash = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)

export interface DeliveryState {
  status: 'checking' | 'unverified' | 'unavailable' | 'needs_review' | 'passed';
  label: string; detail: string; source: string; testCount?: number; requirement?: string; runId?: string;
}

export function deliveryReviewFailed(run: Record<string, unknown>): boolean {
  return [run.contract_review, object(run.intake).contract_review, run.quality_review, run.posthoc_review]
    .some(value => object(value).passed === false)
}

/** A saved version and its latest source-run evidence must agree before showing success. */
export function projectDelivery(project: ProjectDetail, revision: ProjectRevision | null, options: { loading?: boolean; error?: string } = {}): DeliveryState {
  const base = { source: revision?.run_id ? '开发运行保存' : revision ? '导入源码' : '正在读取版本', runId: revision?.run_id || undefined }
  if (options.error) return { ...base, status: 'unavailable', label: '验收状态暂不可确认', detail: '版本或项目记录读取失败。重新读取后再确认本版的验收结论。' }
  if (!revision || options.loading) return { ...base, status: 'checking', label: '正在核对版本记录', detail: '正在读取所选版本的源码、验收和开发记录。' }
  if (revision.project_id !== project.id) return { ...base, status: 'unavailable', label: '版本记录不匹配', detail: '所选版本不属于当前项目，请重新读取。' }
  if (!revision.run_id) return { ...base, status: 'unverified', label: '导入版本 · 尚未验收', detail: '已保存原始源码。可以先查看或运行，再提出改进需求。' }
  const run = project.runs?.find(item => item.id === revision.run_id)
  const context = { ...base, requirement: run?.requirement }
  if (!run) return { ...context, status: 'unavailable', label: '缺少开发运行记录', detail: '已保存此版本，但暂时无法核对来源运行及后续复核。' }
  if (deliveryReviewFailed(run)) return { ...context, status: 'needs_review', label: '复核未通过 · 需要处理', detail: '来源运行有未通过的审查。已保存版本仍可查看，当前不能作为已验证交付。' }
  const verification = object(revision.verification), test = object(verification.test), review = object(verification.review)
  const publication = object(run.publication), currentReview = object(run.quality_review)
  const latest = object(Array.isArray(run.verifications) ? run.verifications.at(-1) : undefined)
  const fingerprint = revision.source_sha256
  const web = revision.web
  const boundWeb = !web || (test.image === web.image_id && test.dependency_id === web.dependency_id
    && test.lock_sha256 === web.lock_sha256 && hash(test.web_runtime_sha256)
    && review.web_runtime_sha256 === test.web_runtime_sha256 && currentReview.web_runtime_sha256 === test.web_runtime_sha256
    && latest.web_runtime_sha256 === test.web_runtime_sha256)
  const attestation = object(test.attestation)
  const latestAttestation = object(latest.attestation)
  const receiptRejected = [attestation, latestAttestation].some(item => item.passed === false
    || ['failures', 'errors', 'skipped'].some(key => typeof item[key] === 'number' && (item[key] as number) > 0))
  const passed = hash(fingerprint) && verification.status === 'passed'
    && verification.candidate_sha256 === fingerprint && verification.verified_hash === fingerprint
    && test.status === 'passed' && test.isolated === true && test.exit_code === 0
    && Number.isSafeInteger(test.test_count) && (test.test_count as number) > 0
    && test.candidate_sha256 === fingerprint && hash(test.acceptance_sha256)
    && (verification.source_run === undefined || verification.source_run === revision.run_id)
    && review.passed === true && review.candidate_hash === fingerprint
    && run.status === 'succeeded' && run.verified_hash === fingerprint
    && (run.candidate_current_hash === undefined || run.candidate_current_hash === fingerprint)
    && publication.status === 'saved' && publication.revision_id === revision.id && publication.source_sha256 === fingerprint
    && latest.status === 'passed' && latest.isolated === true && latest.exit_code === 0 && latest.candidate_hash === fingerprint
    && latest.test_count === test.test_count && latest.acceptance_sha256 === test.acceptance_sha256
    && currentReview.passed === true && currentReview.candidate_hash === fingerprint && boundWeb && !receiptRejected
  if (!passed) return { ...context, status: 'needs_review', label: '验收证据待核对', detail: '本版源码、测试、审查或保存记录尚未完整对应。请查看开发过程中的原始回执。' }
  return { ...context, status: 'passed', label: '已通过冻结验收与审查', testCount: test.test_count as number,
    detail: web ? '冻结依赖环境中的 HTTP 验收已通过；页面交互可在下方预览中检查。' : '所选版本已通过 Docker 验收和模型审查，可运行或下载。' }
}
