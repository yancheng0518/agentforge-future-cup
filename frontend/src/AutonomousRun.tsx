import type { ReactNode } from 'react'
import type { Run } from './types'
import { actionNames, currentVerification, displayStatus, dynamicData, dynamicProgress, dynamicResult, dynamicStateNames, executionReviews, generatedAcceptance, hasCandidateProject, projectActivity, reviewProblems, verificationLabel } from './autonomous'

export type DynamicPanel = 'changes' | 'validation' | 'activity' | 'project' | 'graph' | null
const formatTime = (value?: string) => value ? new Date(value).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }) : ''
const pretty = (value: unknown) => typeof value === 'string' ? value : JSON.stringify(value, null, 2)
const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}

export function AutonomousThread({ run, elapsed, onPanel, onNewTask, onRevise, onProject }: {
  run: Run; elapsed: number; onPanel: (panel: DynamicPanel) => void; onNewTask: () => void; onRevise: () => void; onProject?: () => void;
}) {
  const value = dynamicData(run), tasks = value.task_graph ?? [], decisions = value.controller_decisions ?? [], verifications = value.verifications ?? []
  const running = ['queued', 'running'].includes(run.status), verified = currentVerification(run), result = dynamicResult(run)
  const latest = decisions.at(-1), metrics = value.model_metrics
  const questions = Array.isArray(value.intake?.questions) ? value.intake.questions : []
  const assumptions = Array.isArray(value.intake?.assumptions) ? value.intake.assumptions.filter(item => typeof item === 'string') : []
  const aiAcceptance = generatedAcceptance(run)
  const problems = reviewProblems(run)
  const hasProject = hasCandidateProject(run)
  const projectRevision = record(value.project_context), publication = record(value.publication)
  const projectRun = typeof value.project_id === 'string'
  const profiles = Array.isArray(value.profile_snapshot) ? value.profile_snapshot.map(record) : []
  const knowledge = Array.isArray(value.knowledge_snapshot) ? value.knowledge_snapshot.map(record) : []
  const directActivities = (run.events ?? []).flatMap(event => { const activity = projectActivity(event); return activity ? [{ event, ...activity }] : [] })
  const requestPreview = Array.from(run.requirement).length > 280 ? Array.from(run.requirement).slice(0, 160).join('') + '…' : run.requirement
  const rejected = new Set((run.events ?? []).filter(event => event.type === 'decision_rejected').map(event => {
    const data = event.data as { decision?: { index?: number } } | undefined
    return data?.decision?.index
  }))
  return <div className="conversation-thread autonomous-thread">
    <div className="thread-topline"><span>{new Date(run.created_at).toLocaleString('zh-CN', { hour12: false })}</span><span className="run-mode live">真实模型 · {projectRun ? '项目迭代' : '动态编排'}</span></div>
    <article className="user-message"><span className="sr-only">你的需求</span><p>{requestPreview}</p>{requestPreview !== run.requirement && <details><summary>展开完整需求</summary><pre>{run.requirement}</pre></details>}</article>
    <article className="assistant-message">
      <div className="assistant-heading"><span className="assistant-mark" aria-hidden="true">A</span><strong>AgentForge</strong><span>主 Agent 与按需创建的子 Agent</span></div>
      <div className={`execution-summary ${running ? 'active' : ''}`} role="status" aria-live="polite"><span className={`task-dot ${displayStatus(run)}`} /><span>{dynamicProgress(run)}</span><small>{elapsed < 60 ? `${elapsed} 秒` : `${Math.floor(elapsed / 60)} 分 ${elapsed % 60} 秒`}</small></div>
      {projectRun && <details className="model-plan"><summary>项目基线与本次协作配置</summary><p>基于项目版本 {String(projectRevision.number ?? '未记录')}，保留已有实现与回归验收。{publication.status === 'saved' ? `结果已保存为版本 ${String(publication.number ?? '')}。` : publication.status === 'conflict' ? '项目版本已发生变化，本次候选未合入。' : publication.status === 'failed' ? '保存项目版本失败，候选与执行记录仍保留。' : '项目版本保存结果以实际记录为准。'}</p><h3>冻结的协作配置</h3>{profiles.length ? <ul>{profiles.map(profile => <li key={`${profile.id}-${profile.version}`}>{String(profile.name ?? profile.id)} · v{String(profile.version)}{profile.enabled === false ? ' · 已停用' : ''}</li>)}</ul> : <p>本次没有绑定额外协作配置。</p>}<h3>冻结的项目资料</h3>{knowledge.length ? <ul>{knowledge.map(document => <li key={`${document.id}-${document.version}`}>{String(document.title ?? document.id)} · v{String(document.version)}</li>)}</ul> : <p>本次没有绑定项目资料。</p>}<p className="muted">上述版本在创建任务时固定；后续编辑不改变这份运行记录。实际资料检索和配置使用以过程事件为准。</p><details><summary>完整版本、配置与资料依据</summary><pre>{pretty({ base_revision_id: value.base_revision_id, configuration_sha256: value.configuration_sha256, profiles: value.profile_snapshot, knowledge: value.knowledge_snapshot, publication: value.publication })}</pre></details></details>}
      {['failed', 'blocked'].includes(run.status) && run.summary && <p className="latest-progress warning-text"><strong>停止原因：</strong>{run.summary}</p>}
      {problems.length > 0 && <section className="task-result warning" aria-label="验收复核提醒"><h2>{running ? '检查发现问题，执行仍在继续' : hasProject ? '验收检查仍有问题' : '验收提案尚未通过检查'}</h2>{running && <p>{dynamicProgress(run)}</p>}{problems.map(review => <div key={review.key}><strong>{review.label}</strong><p>{review.summary || '这次检查发现尚未解决的问题。'}</p>{review.issues.length > 0 && <ul>{review.issues.map((issue, index) => <li key={index}>{issue}</li>)}</ul>}</div>)}<p className="result-note">原始运行状态和测试回执保留；通过生成的测试不代表需求已经正确实现。</p><button className="text-button" onClick={() => onPanel('validation')}>查看检查依据</button></section>}
      {latest && running && <p className="latest-progress"><strong>主 Agent：</strong>{latest.summary}</p>}
      {!tasks.length && <button className="details-link" onClick={() => onPanel('graph')}>查看协作图</button>}
      {directActivities.length > 0 && <details className="execution-steps"><summary><span>主 Agent 的修改与工具操作</span><span>{directActivities.length} 条实际记录</span></summary><ol>{directActivities.map(({ event, label, summary }, index) => <li key={event.id}><span className="step-indicator">{index + 1}</span><span>{label}<small>{summary}</small></span><time>{formatTime(event.timestamp)}</time></li>)}</ol><button className="text-button" onClick={() => onPanel('activity')}>查看输入、输出和源码指纹</button></details>}
      {(value.intake?.specification || assumptions.length > 0) && <details className="model-plan intake-summary"><summary>需求理解与接口约定</summary><p>{value.intake?.specification}</p>{assumptions.length > 0 && <><h3>本次采用的假设</h3><ul>{assumptions.map((item, index) => <li key={index}>{item}</li>)}</ul></>}</details>}
      {tasks.length > 0 && <section className="autonomous-task-list" aria-label="动态任务分工"><div className="section-line"><h3>任务分工</h3><div className="task-map-entry"><span className="muted">{tasks.filter(task => task.status === 'succeeded').length} / {tasks.length} 已完成</span><button className="text-button" onClick={() => onPanel('graph')}>查看协作图</button></div></div><ol>{tasks.map(task => <li className={`autonomous-task ${task.status}`} key={task.id}>
        <div className="section-line"><strong>{task.title}</strong><span className={task.status === 'failed' ? 'warning-text' : task.status === 'succeeded' ? 'success-text' : 'muted'}>{dynamicStateNames[task.status] ?? task.status}</span></div>
        <p className="task-meta">{task.agent_id}{task.profile_id ? ` · ${String(profiles.find(profile => profile.id === task.profile_id)?.name ?? task.profile_id)} v${task.profile_version ?? '?'}` : ''}{task.depends_on.length > 0 ? ` · 依赖：${task.depends_on.map(id => tasks.find(other => other.id === id)?.title ?? id).join('、')}` : ' · 可独立执行'}</p>
        {task.result?.summary && <p className="task-result-preview">{task.result.summary}</p>}{task.error && <p className="warning-text">{task.error}</p>}
        <details><summary>任务详情</summary><p>{task.instructions}</p><p className="muted">{task.write_paths.length ? task.write_paths.join(' · ') : '分析任务，无文件写入权限'}</p>{task.result?.summary && <><strong>完整执行结果</strong><p>{task.result.summary}</p></>}</details>
      </li>)}</ol></section>}
      {verifications.length > 0 && <section className="verification-rounds" aria-label="实际测试进度"><div className="section-line"><h3>Docker 测试</h3><button className="text-button" onClick={() => onPanel('validation')}>查看详情</button></div>{verifications.map(verification => <div className="verification-round" key={verification.index}><span className={`task-dot ${verification.status === 'passed' ? 'succeeded' : verification.status}`} /><span>第 {verification.index} 次验收<small>{verificationLabel(verification, !!value.intake)}</small></span><strong className={verification.status === 'passed' ? 'success-text' : 'warning-text'}>{verification.status === 'passed' ? `通过 · ${verification.test_count} 项` : verification.status === 'failed' ? `未通过 · ${verification.attestation?.failures ?? '?'} 失败 / ${verification.attestation?.errors ?? '?'} 错误` : '隔离执行未确认'}</strong></div>)}</section>}
      {decisions.length > 0 && <details className="execution-steps controller-decisions"><summary><span>主 Agent 的计划与调整</span><span>{decisions.length} 次决定</span></summary><ol>{decisions.map(decision => <li key={decision.index}><span className="step-indicator">{decision.index}</span><span>{actionNames[decision.action] ?? decision.action}{rejected.has(decision.index) && <small className="warning-text">该决定未执行，原因保留在完整记录中</small>}<small>{decision.summary}</small>{decision.tasks?.length ? <small>提出任务：{decision.tasks.map(task => task.title).join('、')}</small> : null}{decision.task_ids?.length ? <small>提出撤销：{decision.task_ids.join('、')}</small> : null}</span><time>{formatTime(decision.timestamp)}</time></li>)}</ol></details>}
      {!running && <section className={`task-result ${result.tone}`}><h2>{result.title}</h2>{problems.length > 0 && run.status === 'succeeded' ? <details><summary>原始主 Agent 结论（已被后续检查指出问题）</summary><p>{run.summary || '没有原始结论。'}</p></details> : <p>{['failed', 'blocked'].includes(run.status) && '停止原因：'}{run.summary || '请查看本次执行记录。'}</p>}{questions.length > 0 && <ul>{questions.map((question, index) => <li key={index}>{question}</li>)}</ul>}<div className="result-facts"><button onClick={() => onPanel('validation')}><span>{verified ? `Docker 验收通过 · ${verified.test_count} 项` : verifications.length ? '查看实际验收记录' : '尚未执行 Docker 验收'}</span></button>{tasks.some(task => task.result?.changed_paths?.length) && <button onClick={() => onPanel('changes')}>查看生成的代码</button>}</div><div className="result-actions"><button className="primary-button" onClick={() => onPanel(hasProject ? 'changes' : 'activity')}>{hasProject ? '查看代码' : '查看执行记录'}</button><button className="secondary-button" onClick={() => onPanel('validation')}>{verifications.length ? '查看验证结果' : '查看验收准备记录'}</button>{(run.status !== 'succeeded' || problems.length > 0) && <button className="secondary-button" onClick={onRevise}>{projectRun ? '补充需求继续项目' : '补充需求并新建任务'}</button>}</div><p className="result-note">{hasProject ? <>候选保存在独立副本中。{aiAcceptance ? '验收标准由 AI 生成，' : '本次采用运行开始时冻结的验收，'}测试通过仍需要结合需求检查结果。</> : '项目和可验证候选尚未建立；已有模型请求和检查记录已经保留。'}</p></section>}
      <button className="details-link" onClick={() => onPanel('activity')}>查看完整执行记录 <span>{run.events?.length ?? 0}</span></button>
      <div className="run-footnote"><span>{metrics?.model_calls ?? 0} 次真实模型请求</span><span>{(metrics?.tokens ?? 0).toLocaleString()} tokens{metrics?.metrics_complete === false ? '（已知用量）' : ''}</span>{!!metrics?.contract_retries && <span>格式重试 {metrics.contract_retries} 次</span>}{!!metrics?.unknown_usage_calls && <span>{metrics.unknown_usage_calls} 次请求用量未知</span>}</div>
    </article>
    <div className="next-task"><div><strong>{running ? '任务会继续在后台运行' : projectRun ? '继续改进这个项目' : '开始下一项需求'}</strong><p>{projectRun ? '返回项目查看当前版本、运行程序或提出下一项需求。' : '每次新建一个独立 Python 项目，当前记录会保留。'}</p></div><button className="secondary-button" onClick={projectRun && onProject ? onProject : onNewTask}>{projectRun ? '返回项目' : '新任务'}</button></div>
  </div>
}

function Code({ value }: { value: string }) {
  return <div className="code-scroll" tabIndex={0} aria-label="Python 项目源码"><pre>{value.split('\n').map((line, index) => <span className="code-line" key={index}><span className="line-number" aria-hidden="true">{index + 1}</span><code>{line || ' '}</code></span>)}</pre></div>
}

function ReviewEvidence({ run }: { run: Run }) {
  const value = dynamicData(run)
  return <>{executionReviews(run).map(review => <section className="review-section" key={review.key}>
    <div className="section-line"><h3>{review.label}</h3><span className={review.passed === true ? 'success-text' : 'warning-text'}>{review.passed === true ? '检查通过' : review.passed === false ? '需要复核' : '尚未完成'}</span></div>
    <p className="muted">{review.key === 'posthoc_review' ? '开发阶段的事后复核记录，与运行时生成的验收分开保存。' : '真实模型进行的 AI 检查，不是人工验收。'}</p>
    {typeof review.raw.source === 'string' && <p className="muted">检查来源：{review.raw.source}</p>}
    <p>{review.summary}</p>{review.issues.length > 0 && <ul>{review.issues.map((issue, index) => <li key={index}>{issue}</li>)}</ul>}
    <details><summary>完整检查依据与记录</summary><pre>{pretty(review.raw)}</pre></details>
  </section>)}{(Array.isArray(value.contract_reviews) || Array.isArray(value.quality_reviews)) && <details className="raw-evidence"><summary>所有 AI 检查轮次（含未通过记录）</summary><pre>{pretty({ contract_reviews: value.contract_reviews, quality_reviews: value.quality_reviews })}</pre></details>}</>
}

export function AutonomousDetails({ run, panel, files, candidateLoading, candidateError, events }: {
  run: Run; panel: DynamicPanel; files: Record<string, string>; candidateLoading: boolean; candidateError: string; events: ReactNode;
}) {
  const value = dynamicData(run), verifications = value.verifications ?? []
  const original = value.bundle?.files ?? {}
  const projectRun = typeof value.project_id === 'string'
  const aiAcceptance = generatedAcceptance(run)
  if (panel === 'changes') return <><div className="detail-intro"><h2>本次生成的代码</h2><p>文件保存在独立候选中，可查看并导出；未写入你的原项目。</p></div>{candidateLoading && <p className="detail-intro muted">正在读取候选…</p>}{candidateError && <p className="detail-intro warning-text">{candidateError}</p>}{Object.entries(files).map(([path, content]) => <section className="file-section" key={path}><h3>{path}</h3><Code value={content} /></section>)}{!candidateLoading && !candidateError && !Object.keys(files).length && <div className="panel-empty"><h2>暂时还没有候选代码</h2><p>需求整理和实现开始后，实际文件会显示在这里。</p></div>}</>
  if (panel === 'validation') return <><div className="detail-intro"><h2>实际验收结果</h2><p>{aiAcceptance ? '验收标准由 AI 生成，执行前冻结。' : '使用任务提供者提供、运行开始时冻结的验收标准。'}测试实际执行状态以每轮 Docker 回执为准。</p><p className="muted">测试覆盖范围受本次验收标准限制，不等于已经证明所有需求正确。</p></div><ReviewEvidence run={run} />{verifications.length ? verifications.map(verification => <section className="test-section" key={verification.index}><div className="section-line"><h3>第 {verification.index} 次 Docker 验收</h3><span className={verification.status === 'passed' ? 'success-text' : 'warning-text'}>{verification.status === 'passed' ? '通过' : verification.status === 'failed' ? '未通过' : '受阻'}</span></div><p>{verification.test_count > 0 ? `实际执行 ${verification.test_count} 项` : '尚无完整测试计数'}{verification.attestation ? ` · ${verification.attestation.failures ?? 0} 失败 · ${verification.attestation.errors ?? 0} 错误` : ''}</p><p className="muted">{verificationLabel(verification, !!value.intake)} · {verification.isolated ? '隔离执行已确认' : '隔离执行未确认'}</p>{verification.reason && <p>{verification.reason}</p>}<details className="raw-evidence"><summary>测试输出与退出状态</summary><pre>{pretty({ stdout: verification.stdout, stderr: verification.stderr, exit_code: verification.exit_code, candidate_hash: verification.candidate_hash })}</pre></details></section>) : <p className="detail-intro muted">还没有实际测试结果。模型自报完成不算验收通过。</p>}{value.bundle && <details className="raw-evidence"><summary>冻结的验收标准与代码</summary><p>{value.bundle.requirement}</p><Code value={value.bundle.acceptance} /></details>}</>
  if (panel === 'activity') return <><div className="detail-intro"><h2>完整执行记录</h2><p>所有主 Agent 决策、子 Agent 结果、格式重试和失败都会保留。</p></div>{events}<details className="raw-evidence"><summary>预算、模型配置与任务图</summary><pre>{pretty({ budget: value.budget, model: value.model_config, metrics: value.model_metrics, intake: value.intake, task_graph: value.task_graph })}</pre></details></>
  if (panel === 'project') return <><div className="detail-intro"><h2>{projectRun ? '本次修改的项目基线' : '本次 Python 项目'}</h2><p>{projectRun ? `基于项目版本 ${String(record(value.project_context).number ?? '')} 创建独立候选，原有源码与历史版本保留。` : '按你的需求创建独立项目，不再从四类 Todo 示例匹配任务。'}</p>{value.intake?.specification && <p>{value.intake.specification}</p>}</div>{Object.entries(original).map(([path, content]) => <section className="file-section" key={path}><h3>{path} · 初始版本</h3><Code value={content} /></section>)}{!Object.keys(original).length && <p className="detail-intro muted">主 Agent 正在准备项目文件和接口约定。</p>}</>
  return null
}
