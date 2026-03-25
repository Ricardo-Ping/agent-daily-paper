---
name: agent-daily-paper
description: 从用户视角执行 arXiv 论文订阅：按研究领域检索、筛选、排序并输出中英双语日报；支持 Why Recommended、Feedback ID、点赞/点踩反馈闭环与自动配置迁移。
---

# Agent Daily Paper（用户预期版）

## 目标

我希望 Agent 做到：
- 按我给的研究领域订阅 arXiv 最新论文
- 每天按我本地时间推送完整 Markdown 日报
- 每篇包含“为什么推荐这篇”
- 我能直接点赞/点踩，下一轮结果更贴合

## 必须遵守

- 首次必须先检查配置，`setup_required=true` 时禁止直接推送。
- `push_time` 按 `timezone` 的本地时间解释，不能按 UTC 误解。
- 默认解读模式必须是 `insight_mode=pdf`（优先读 PDF 全文）。
- 推送输出必须是完整 Markdown 正文（不是标题+链接摘要）。
- 每次推送都要写入 `output/daily/*.md`。

## 运行顺序

1. 环境与健康检查
- `python scripts/bootstrap_env.py --run-doctor`
- `python scripts/doctor.py`

2. 生成订阅（即时场景）
- `python scripts/prepare_fields.py --fields "推荐系统" --limit 10 --output config/subscriptions.instant.json`

3. 执行日报
- `python scripts/run_digest.py --config config/subscriptions.json --emit-markdown`
- 即时可用：`python scripts/instant_digest.py --fields "推荐系统" --limit 10 --time-window-hours 72`

## 配置与状态迁移

`run_digest.py` 启动时会自动：
- 迁移旧配置到新结构
- 补齐 `schema_version`
- 迁移前写备份 `*.bak.<timestamp>`

若发生迁移，Agent 需要在回复中明确告诉用户：
- 备份文件路径
- 迁移了哪些关键字段

## 输出要求

每篇论文至少包含：
- English Title / Chinese Title
- English Abstract / 中文摘要
- arXiv URL
- Flags（`NEW` / `UPDATED(vX->vY)`）
- Why Recommended
- Feedback ID
- 论文解读（Agent）

## 反馈闭环（必须引导）

推送后，Agent 要主动引导用户反馈：
- 支持 `+ 1,3; - 2#原因`
- 支持 `like:1,3; dislike:2#原因`

记录反馈：
- `python scripts/feedback_cli.py --feedback "..."`

聚合反馈：
- `python scripts/apply_feedback.py`

下轮生效：
- `prepare_fields.py` 自动读取 `config/feedback_adjustments.json`
- 正反馈词进入关键词，负反馈词进入排除词

## 配置字段（最小必需）

- `field_settings[].name`
- `field_settings[].limit`（推荐 5-20）
- `push_time`
- `timezone`

## 失败处理

- 翻译失败：允许输出 `[待翻译]`，但流程不能中断。
- 无命中：输出“当天该领域无最新论文”（或等价信息），并保留统计。
- 外部依赖不可用：给出明确修复命令，不要静默失败。

## 关键文件

- `scripts/run_digest.py`
- `scripts/prepare_fields.py`
- `scripts/instant_digest.py`
- `scripts/feedback_cli.py`
- `scripts/apply_feedback.py`
- `config/subscriptions.json`
- `config/agent_field_profiles.json`
- `config/feedback_adjustments.json`
- `data/state.json`
- `output/daily/`
