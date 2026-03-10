---
name: agent-daily-paper
description: 支持用户按一个或多个研究领域订阅 arXiv 最新论文，按重要性排序并以中英双语卡片形式推送（英文标题/中文标题/英文摘要/中文摘要/arXiv 链接）。支持每领域独立数量上限（5-20）、关键词高亮、NEW/UPDATED 版本标识、Markdown 存档，以及定时推送与即时推送双路径。首次使用时先完成订阅配置；领域可由 Agent 画像 JSON 自动补全英文名、关键词与会议列表。
---

# Agent Daily Paper

## 执行原则

- 首次使用先完成配置，再执行抓取与推送。
- 若配置缺失，先补齐，不直接运行。
- 推送完成后同时输出两份结果：
  - 聊天内返回完整 Markdown 正文（不要只发附件或只给文件路径）
  - 落盘到 `output/daily/*.md`

## 必填配置

- `field_settings[].name`：研究领域名（可多个）
- `field_settings[].limit`：每领域推荐数量（5-20）
- `push_time`：每日推送时间（HH:MM）
- `timezone`：时区（默认 `Asia/Shanghai`）

## 可选配置

- `keywords` / `exclude_keywords`
- `time_window_hours`
- `highlight.title_keywords` / `highlight.authors` / `highlight.venues`
- 翻译提供方 `TRANSLATE_PROVIDER`：`openai` / `argos` / `auto` / `none`

## 领域解析策略

优先级：
1. `config/agent_field_profiles.json`（默认路径，存在即优先）
2. OpenAI 画像（可选兜底）
3. 启发式规则（最终兜底）

支持字段画像 JSON 结构：
- `canonical_en`
- `categories`
- `keywords`
- `title_keywords`
- `venues`

## 检索与排序

- 检索采用“类别 + 标题/摘要关键词”召回，不依赖单一大类。
- 细分方向支持模糊匹配评分（如“数据库优化器”）。
- 重要性分数综合：类别命中 + 关键词命中 + 模糊命中 + 新鲜度。
- 命中不足时可自动扩大时间窗口并放宽关键词。

## 输出规范

每篇论文输出：
- English Title
- Chinese Title
- English Abstract
- 中文摘要
- arXiv URL
- Flags（`NEW` / `UPDATED(vX->vY)` + 高亮标签）

命名规则：`<领域1>_<领域2>_<YYYY-MM-DD>.md`

- 多领域时按领域分组。
- 单领域时不分组。
- 日报头部必须包含 `Field Profiles`，每个领域给出：
  - `Canonical EN`（英文领域名）
  - `Keywords`（检索关键词）
  - `Venues/Journals`（相关会议或期刊）

## 运行命令

- 健康检查：
  - `python scripts/doctor.py`
- 通用运行：
  - `python scripts/run_digest.py --emit-markdown`
- 定时轮询（GitHub Actions）：
  - `python scripts/run_digest.py --only-due-now --due-window-minutes 15 --emit-markdown`
- 即时推送（不依赖 Actions）：
  - `python scripts/instant_digest.py --fields "数据库优化器,推荐系统" --limit 20 --time-window-hours 72`
  - 默认仅输出完整 Markdown 正文到聊天（不附加 JSON 摘要）

## 安装

推荐 Conda 环境：`arxiv-digest-lab`

```bash
conda create -n arxiv-digest-lab python=3.10 -y
conda activate arxiv-digest-lab
pip install argostranslate
python scripts/install_argos_model.py
```

## 失败兜底

- 翻译失败输出 `[待翻译]`，不中断主流程。
- API 请求自动重试。
- 无命中时输出“当前窗口无新增论文”及统计信息。
