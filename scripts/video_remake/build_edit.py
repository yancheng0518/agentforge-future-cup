"""Assemble the reviewed edit from narration and genuine captured UI assets."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MEDIA = 'submission/video-work/remake-20261116/'
manifest = json.loads((ROOT / MEDIA / 'audio/manifest.json').read_text(encoding='utf-8'))
natural = '真实模型开发 · 历史回放 / 4af425ca'
seeded = '预设缺陷实验 · 独立副本 / 1ea0bc86'
live = '2026.11.16 实录 · Docker 交付版本 2'
board = [270, 10, 740, 370]
main = [265, 80, 990, 625]

def image(name, crop=None):
    return dict(kind='image', media=MEDIA + name + '.png', **({'crop': crop} if crop else {}))

def video(name, crop=None, **kwargs):
    return dict(kind='video', media=MEDIA + 'captures/' + name + '/' + name + '.mp4', **({'crop': crop} if crop else {}), **kwargs)

def card(headline, detail='', **kwargs):
    return dict(kind='card', headline=headline, detail=detail, **kwargs)

edits = {
 '01-01': ('一句需求，怎样成为软件？', '产品演示', card('一句需求。\n一个可以操作的交付。', '从动态分工到独立验收，让结果看得见。')),
 '01-02': ('AgentForge · 多智能体软件开发工作台', '奶龙队 / 未来杯 · 赛题一', card('AgentForge', '主 Agent 根据需求组织协作，执行结果决定下一步。', flow=['理解需求', '动态分工', '运行验收', '版本交付'])),
 '01-03': ('成品先看：中文工作项看板', live, video('board-add-live', board)),
 '01-04': ('新增、完成、筛选，直接操作', live, video('board-filter-live', board)),
 '02-01': ('回到开发现场', natural, image('natural-overview', [395,100,835,600])),
 '02-02': ('把需求和接口说清楚', natural, image('natural-requirement', [395,110,835,590])),
 '02-03': ('先冻结验收，再开始实现', '流程说明 · 对应本次真实记录', card('每一轮实现，\n面对同一套验收。', '保留原始需求、项目基线与冻结测试。', flow=['确认需求', '冻结标准', '实现代码', '独立验收'])),
 '03-01': ('需求决定分工', natural, image('natural-graph-complete', [272,204,580,414])),
 '03-02': ('两个任务，分别修改不同文件', '真实执行摘要 · 运行时间存在重叠', card('后端接口 + 中文前端', '这一次，主 Agent 拆出两个可独立执行的任务。', stats=[{'value':'app.py','label':'后端 API 与数据校验'}, {'value':'CSS / JS','label':'本地页面资产与交互'}])),
 '03-03': ('点击节点，查看指令与文件范围', natural, image('natural-backend-full', [270,125,985,580])),
 '04-01': ('首个自然候选：14 项验收通过', natural, image('natural-tests', [790,155,460,500])),
 '04-02': ('再用独立用例复核', '真实回执摘要 · 两套结果分别保存', card('14 项 + 独立 12 项', '两套 HTTP 验收分别记账，不合并成新的任务成功次数。', stats=[{'value':'14 / 14','label':'开发前冻结的 HTTP 验收'}, {'value':'12 / 12','label':'维护者独立 HTTP 用例'}])),
 '04-03': ('格式重试，单独记录', '真实运行分类 · 4af425ca', card('2 次格式重试', '协议输出格式错误与候选代码失败，分别记录。', stats=[{'value':'2','label':'输出格式重试'}, {'value':'0','label':'本次自然候选代码失败'}])),
 '05-01': ('独立实验：预先移除筛选分支', seeded, image('seeded-overview', [400,120,830,540])),
 '05-02': ('失败反馈成为修正任务', seeded, image('seeded-tests-plan', [400,180,830,500])),
 '05-03': ('使用同一套冻结测试复验', seeded, card('1 项失败 → 12 项通过', '恢复 done=true / false 筛选逻辑，重新执行 Docker 验收。', flow=['失败回执', '派发修正', '修改候选', '冻结复验'])),
 '05-04': ('失败、修改、复验，全部保留', seeded, image('seeded-tests-plan', [400,80,830,610])),
 '06-01': ('回到自然开发的交付版本', '真实项目页面 · 自然候选版本 2', image('project-version2', [295,110,945,530])),
 '06-02': ('源码与记录随版本一起保存', '真实项目页面 · 不可变版本源码', image('project-source', [295,270,945,430])),
 '06-03': ('基于当前版本，继续下一项需求', '真实项目页面 · 此处未提交新任务', image('project-source', [295,100,945,330])),
 '07-01': ('打开交付页面', live, image('board-final', board)),
 '07-02': ('新增 → 标记完成 → 筛选', live + ' · 等待片段已剪短', video('board-actions-edit', board)),
 '07-03': ('刷新后，检查当前容器内的状态', live, video('board-refresh-live', board)),
 '07-04': ('结束演示，停止预览', '本次操作截图 · 已确认容器停止', image('preview-stopped', [295,145,945,570])),
 '08-01': ('整个过程，可以查看，也可以回放', natural, video('graph-replay', [330,145,1250,730])),
 '08-02': ('已经验证的能力，清楚标注边界', '验证范围 · 保留成功与失败证据', card('修正闭环已验证。\n证据来自预设缺陷实验。', '自然代码失败后的自动修正，目前尚无成功样本。')),
 '08-03': ('AgentForge · 让交付有据可查', '奶龙队 / 未来杯 · 赛题一', card('能操作的软件。\n经得起检查的记录。', '完整证据与复现入口：submission/证据索引.md', flow=['需求', '协作', '验收', '修正', '交付'])),
}
segments = []
for item in manifest['segments']:
    title, source, design = edits[item['id']]
    segments.append(dict(id=item['id'], chapter=item['chapter'], kicker=item['chapter_title'],
        title=title, source=source, audio=item['audio'], text=item['text'], **design))
config = dict(title='AgentForge · 从一句需求到可操作的软件',
    captions_json=MEDIA+'audio/captions.json', narration_manifest=MEDIA+'audio/manifest.json',
    provenance={'new_model_calls':0, 'development':'Historical real model and Docker records',
                'preview':'Newly captured UI actions against existing natural project version 2'}, segments=segments)
target = ROOT / 'scripts/video_remake/edit.json'
target.write_text(json.dumps(config, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
print(f'Wrote {len(segments)} edits')
