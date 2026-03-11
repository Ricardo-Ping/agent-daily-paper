---
name: agent-daily-paper
description: 支持用户按一个或多个研究领域订阅 arXiv 最新论文，按重要性排序并以中英双语卡片形式推送（英文标题/中文标题/英文摘要/中文摘要/arXiv 链接）。支持每领域独立数量上限（5-20）、关键词高亮、NEW/UPDATED 版本标识、Markdown 存档，以及定时推送与即时推送双路径。首次使用时先完成订阅配置；领域可由 Agent 画像 JSON 自动补全英文名、关键词与会议列表。
---

# Agent Daily Paper

## 执行原则

- 首次使用必须先执行环境引导，再完成配置，再执行抓取与推送。
- 环境引导命令：`python scripts/bootstrap_env.py --run-doctor`
- taxonomy 本地知识库同步：`python scripts/sync_arxiv_taxonomy.py --output data/arxiv_taxonomy.json`
- `push_time` 必须按用户 `timezone` 的本地时间理解，不能按 UTC 理解。
- 如果用户要求定时推送，优先创建“精确到点”的 cron / 自动任务，而不是 15 分钟轮询。
- cron 表达式应直接由用户时间生成，例如：
  - `12:00 + Asia/Shanghai` -> `0 12 * * * (Asia/Shanghai)`
  - `08:30 + Asia/Shanghai` -> `30 8 * * * (Asia/Shanghai)`
- 若用户使用 OpenClaw cron / automation，可优先采用以下执行模板：
  - 在 `/home/<user>/.openclaw/workspace/agent-daily-paper` 执行：
  - `export PATH="/home/<user>/miniconda3/bin:/home/<user>/.nvm/versions/node/<node-version>/bin:/usr/local/bin:/home/<user>/.local/bin:/home/<user>/.bun/bin:/usr/bin:/bin:/home/<user>/.nvm/current/bin:/home/<user>/.npm-global/bin:/home/<user>/bin:/home/<user>/.volta/bin:/home/<user>/.asdf/shims:/home/<user>/.fnm/current/bin:/home/<user>/.local/share/pnpm" && conda run -n arxiv-digest-lab python scripts/run_digest.py --only-due-now --due-window-minutes 15 --emit-markdown`
  - 其中 `/home/<user>` 与 `<node-version>` 必须替换为当前机器的真实路径
- 若采用上述 OpenClaw 模板，输出必须严格遵守：
  - `reason=already_pushed_today` -> `今天该领域已推送过`
  - 无命中且未推送 -> `当天该领域无最新论文`
  - 有论文 -> 原样返回完整 Markdown 正文，不要摘要、不要 JSON、不要解释
- 若 `config/subscriptions.json` 中 `setup_required=true`，必须先向用户收集配置并写入订阅；禁止直接按样例配置执行推送。
- 若配置缺失，先补齐，不直接运行。
- 仅执行本仓库内与 arXiv 推送相关的操作；禁止插入或执行无关任务（如配额监控、其他项目脚本）。
- 推送完成后同时输出两份结果：
  - 聊天内返回完整 Markdown 正文（与输出 md 文件逐字一致；不要只发标题+链接摘要）
  - 落盘到 `output/daily/*.md`

## 必填配置

- `field_settings[].name`：研究领域名（可多个）
- `field_settings[].limit`：每领域推荐数量（5-20）
- `push_time`：每日推送时间（HH:MM，本地时间）
- `timezone`：时区（默认 `Asia/Shanghai`）

## 可选配置

- `keywords` / `exclude_keywords`
- `time_window_hours`
- `query_strategy`（推荐 `category_keyword_union`）
- `require_primary_category`（推荐 `true`）
- `history_scope`（推荐 `subscription`，避免跨订阅误去重）
- `category_expand_mode`（`off/conservative/balanced/broad`）
- `agent-categories-only`（仅使用 Agent 提供分类；缺失分类则报错）
- `taxonomy-json`（默认 `data/arxiv_taxonomy.json`，用于分类合法性校验与补全）
- `embedding_filter.model` / `embedding_filter.threshold` / `embedding_filter.top_k`
- `agent_rerank.model`（默认 `BAAI/bge-reranker-v2-m3`）/ `agent_rerank.top_k`
- `highlight.title_keywords` / `highlight.authors` / `highlight.venues`
- 翻译提供方 `TRANSLATE_PROVIDER`：`openai` / `argos` / `auto` / `none`

## 领域解析策略

优先级：
1. `config/agent_field_profiles.json`（默认路径，存在即优先）
2. taxonomy 知识库校验与补全（`data/arxiv_taxonomy.json`）
3. OpenAI 画像（可选兜底）
4. 启发式规则（最终兜底）

支持字段画像 JSON 结构：
- `canonical_en`
- `categories`
- `keywords`
- `title_keywords`
- `venues`

## 检索与排序

- 检索采用分层漏斗：
  - 主分类 + 英文关键词并集召回（`query_strategy=category_keyword_union`）
  - 主分类过滤（`require_primary_category=true`）
  - embedding 相似度过滤（`embedding_filter`）
  - 本地 reranker 重排（`agent_rerank`）
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
- 分类字段约定：
  - `primary_categories`：检索与过滤实际使用的主分类
  - `categories`：扩展参考分类（展示用）

## 运行命令

- 健康检查：
  - `python scripts/doctor.py`
- 通用运行：
  - `python scripts/run_digest.py --emit-markdown`
- 精确 cron / 定时任务（推荐）：
  - `python scripts/run_digest.py --config config/subscriptions.json --emit-markdown`
- 精确 cron 示例：
  - `0 12 * * * (Asia/Shanghai)` -> `cd <repo> && conda run -n arxiv-digest-lab python scripts/run_digest.py --config config/subscriptions.json --emit-markdown`
- OpenClaw cron 兼容模板：
  - `0 12 * * * (Asia/Shanghai)` + `export PATH="..." && conda run -n arxiv-digest-lab python scripts/run_digest.py --only-due-now --due-window-minutes 15 --emit-markdown`
- 只有在平台不支持精确 cron，或者一个共享任务需要兼容多个时间点订阅时，才使用轮询模式：
  - `python scripts/run_digest.py --config config/subscriptions.json --only-due-now --due-window-minutes 15 --emit-markdown`
- GitHub Actions 仅是可选远端方案，不应作为“安装到本地 skill 后”的默认调度方式。
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
pip install sentence-transformers
python scripts/install_embedding_model.py --model BAAI/bge-m3
```

## 失败兜底

- 翻译失败输出 `[待翻译]`，不中断主流程。
- API 请求自动重试。
- 无命中时输出“当前窗口无新增论文”及统计信息。
