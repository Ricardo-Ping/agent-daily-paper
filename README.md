# agent-daily-paper（用户视角说明）

这是一个给我自己用的 arXiv 论文订阅工具：
- 我给出关注领域（可多个）
- 系统每天按时间自动抓取、筛选、排序
- 输出中英双语卡片 + 每篇论文解读
- 支持我对结果点赞/点踩，下一轮自动优化

## 我能得到什么

每篇论文都会包含：
- English Title
- Chinese Title
- English Abstract
- 中文摘要
- arXiv URL
- Flags（`NEW` / `UPDATED(vX->vY)` + 高亮标签）
- `Why Recommended`（为什么推荐）
- `Feedback ID`（用于反馈追踪）
- 论文解读（Agent）

默认解读模式：`insight_mode=pdf`（优先读 PDF 全文，不是只看摘要）。

## 快速开始

1. 初始化环境与依赖

```bash
python scripts/bootstrap_env.py --run-doctor
```

2. 健康检查

```bash
python scripts/doctor.py
```

3. 即时跑一次（不依赖定时器）

```bash
python scripts/instant_digest.py --fields "推荐系统,数据库优化器" --limit 20 --time-window-hours 72
```

## 我需要配置什么

核心配置文件：`config/subscriptions.json`

必填项：
- `field_settings[].name`
- `field_settings[].limit`（建议 5-20）
- `push_time`（`HH:MM`）
- `timezone`（如 `Asia/Shanghai`）

说明：
- `push_time` 永远按 `timezone` 的本地时间解释。
- 当 `setup_required=true` 时，系统只会提示先配置，不会直接推送。

## 两种运行方式

### 1. 即时推送

```bash
python scripts/instant_digest.py --fields "推荐系统" --limit 10 --time-window-hours 72
```

### 2. 定时推送（推荐本机调度器）

```bash
python scripts/run_digest.py --config config/subscriptions.json --emit-markdown
```

## 配置校验与自动迁移（已内置）

`run_digest.py` 启动时会自动做：
- `subscriptions.json` 迁移（旧 `fields/daily_count` -> 新 `field_settings`）
- `state.json` 迁移（旧去重字段 -> `sent_versions_by_sub`）
- `schema_version` 补齐
- 迁移前自动备份为 `*.bak.<timestamp>`

我可以用下面命令先看体检结果：

```bash
python scripts/doctor.py
```

## 反馈闭环（我如何让推荐越来越准）

### 第一步：运行日报后，按序号反馈

支持两种输入格式：
- `+ 1,3; - 2#太泛`
- `like:1,3; dislike:2#太偏NLP`

### 第二步：记录反馈

```bash
python scripts/feedback_cli.py --feedback "+ 1,3; - 2#太泛"
```

默认写入：`data/feedback/feedback.jsonl`

### 第三步：聚合反馈建议

```bash
python scripts/apply_feedback.py
```

输出：`config/feedback_adjustments.json`

### 第四步：下次生成订阅时自动吸收

`prepare_fields.py` 默认会读取 `config/feedback_adjustments.json`：
- 正反馈词 -> 进入关键词
- 负反馈词 -> 进入排除词

## 常用命令

- 健康检查：

```bash
python scripts/doctor.py
```

- 生成订阅配置：

```bash
python scripts/prepare_fields.py --fields "推荐系统" --limit 10 --output config/subscriptions.instant.json
```

- 执行日报：

```bash
python scripts/run_digest.py --config config/subscriptions.instant.json --emit-markdown
```

## 关键文件

- 运行主流程：`scripts/run_digest.py`
- 字段与订阅生成：`scripts/prepare_fields.py`
- 即时入口：`scripts/instant_digest.py`
- 反馈记录：`scripts/feedback_cli.py`
- 反馈聚合：`scripts/apply_feedback.py`
- 生产配置：`config/subscriptions.json`
- 字段画像：`config/agent_field_profiles.json`
- 反馈调整：`config/feedback_adjustments.json`
- 去重状态：`data/state.json`
- 日报输出：`output/daily/`

## 备注

- 本项目支持离线翻译（Argos）和在线翻译（OpenAI）。
- 翻译失败时会标记 `[待翻译]`，但不会中断主流程。
- 若希望强制刷新某领域种子语料，可在 `prepare_fields.py` 使用 `--seed-force-refresh`。
