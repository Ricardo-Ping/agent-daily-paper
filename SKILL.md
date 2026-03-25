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
  - 在 `/home/USER_HOME/.openclaw/workspace/agent-daily-paper` 执行：
  - `export PATH="/home/USER_HOME/miniconda3/bin:/home/USER_HOME/.nvm/versions/node/NODE_VERSION/bin:/usr/local/bin:/home/USER_HOME/.local/bin:/home/USER_HOME/.bun/bin:/usr/bin:/bin:/home/USER_HOME/.nvm/current/bin:/home/USER_HOME/.npm-global/bin:/home/USER_HOME/bin:/home/USER_HOME/.volta/bin:/home/USER_HOME/.asdf/shims:/home/USER_HOME/.fnm/current/bin:/home/USER_HOME/.local/share/pnpm" && conda run -n arxiv-digest-lab python scripts/run_digest.py --only-due-now --due-window-minutes 15 --emit-markdown`
  - 其中 `USER_HOME` 与 `NODE_VERSION` 必须替换为当前机器的真实路径
  - 若要补充投递配置，可使用：
    - `delivery.mode: announce`
    - `delivery.channel: feishu`
    - `delivery.to: user:FEISHU_USER_ID`
    - `cron: 0 12 * * *`
    - `timezone: Asia/Shanghai`
- 若采用上述 OpenClaw 模板，输出必须严格遵守：
  - `reason=already_pushed_today` -> `今天该领域已推送过`
  - 无命中且未推送 -> `当天该领域无最新论文`
  - 有论文 -> 原样返回完整 Markdown 正文，不要摘要、不要 JSON、不要解释
- 若 `config/subscriptions.json` 中 `setup_required=true`，必须先向用户收集配置并写入订阅；禁止直接按样例配置执行推送。
- 若配置缺失，先补齐，不直接运行。
- `run_digest.py` 启动时会自动执行配置/状态迁移与校验；若发生迁移，必须告知用户备份位置与迁移摘要。
- 仅执行本仓库内与 arXiv 推送相关的操作；禁止插入或执行无关任务（如配额监控、其他项目脚本）。
- 推送完成后同时输出两份结果：
  - 聊天内返回完整 Markdown 正文（与输出 md 文件逐字一致；不要只发标题+链接摘要）
  - 落盘到 `output/daily/*.md`
- 推送后应引导用户进行反馈：
  - 支持 `+ 1,3; - 2#原因` 或 `like:1,3; dislike:2#原因`
  - 使用 `python scripts/feedback_cli.py --feedback "..."` 记录到 `data/feedback/feedback.jsonl`
  - 反馈聚合由 `python scripts/apply_feedback.py` 生成 `config/feedback_adjustments.json`
  - 后续 `prepare_fields.py` 会自动吸收反馈建议（正反馈并入关键词，负反馈并入排除词）

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
- `category_expand_mode`（`off/conservative/balanced/broad`）
- `agent-categories-only`（仅使用 Agent 提供分类；缺失分类则报错）
- `taxonomy-json`（默认 `data/arxiv_taxonomy.json`，用于分类合法性校验与补全）
- `embedding_filter.model` / `embedding_filter.threshold` / `embedding_filter.top_k`
- `agent_rerank.model`（默认 `BAAI/bge-reranker-v2-m3`）/ `agent_rerank.top_k`
- `highlight.title_keywords` / `highlight.authors` / `highlight.venues`
- `insight_mode`（默认 `pdf`，可选 `abstract`）
- `insight_pdf_max_pages` / `insight_pdf_timeout_sec`
- 翻译提供方 `TRANSLATE_PROVIDER`：`argos`(默认) / `openai`(需 `OPENAI_API_KEY` + `OPENAI_TRANSLATE_MODEL`) / `auto` / `none`

## 领域解析策略

优先级：
1. `config/agent_field_profiles.json`（默认路径，存在即优先）
2. 中文领域先翻译为英文 `canonical_en`（再做分类与检索，避免中文 arXiv 查询；若输入已是英文则直接使用）
3. taxonomy 知识库校验与补全（`data/arxiv_taxonomy.json`）
4. OpenAI 画像（可选兜底）
5. 启发式规则（最终兜底）

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
- `prepare_fields.py` 会在每个领域落盘 Top-K 种子语料：
  - `output/seed_corpus/docs/<canonical_en>.md`（标题、作者、摘要、链接）
  - `output/seed_corpus/embeddings/<canonical_en>.json`（标题+摘要 embedding）
- 默认启用“领域指纹缓存”：若领域画像不变，后续运行直接复用 seed 语料与向量，不再重抓 Top-K。
- 如需强制刷新种子语料，执行 `prepare_fields.py` 时加 `--seed-force-refresh`。

## 输出规范

每篇论文输出：
- English Title
- Chinese Title
- English Abstract
- 中文摘要
- arXiv URL
- Flags（`NEW` / `UPDATED(vX->vY)` + 高亮标签）
- Why Recommended（分类/关键词/embedding/rerank/新鲜度解释）
- Feedback ID（稳定反馈标识）
- Agent 解读：单段中文长文（默认不少于 500 字，基于 PDF 全文语义凝练，失败时回退摘要）
- 解读内容必须自然覆盖：问题边界、方法主线、创新贡献；禁止关键词拼接式机械句

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
- 去重状态约定：
  - 仅使用 `data/state.json -> sent_versions_by_sub`
  - 全局 `sent_ids/sent_versions` 视为弃用字段，不再参与去重
  - 去重状态每 7 天自动清空一次

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
- 记录用户反馈：
  - `python scripts/feedback_cli.py --feedback "+ 1,3; - 2#太泛"`
- 聚合反馈建议：
  - `python scripts/apply_feedback.py`

## 安装

推荐 Conda 环境：`arxiv-digest-lab`

```bash
conda create -n arxiv-digest-lab python=3.10 -y
conda activate arxiv-digest-lab
pip install argostranslate pypdf
python scripts/install_argos_model.py --from-code zh --to-code en
python scripts/install_argos_model.py --from-code en --to-code zh
pip install sentence-transformers
python scripts/install_embedding_model.py --model BAAI/bge-m3
```

## 失败兜底

- 翻译失败输出 `[待翻译]`，不中断主流程。
- API 请求自动重试。
- 无命中时输出“当前窗口无新增论文”及统计信息。

## 单篇论文解读写作规范（Agent）

当用户单独提交一篇论文要求解读时，Agent 必须优先阅读 PDF 全文（至少覆盖 Abstract / Introduction / Method / Experiments / Conclusion），并使用以下固定提示词框架进行中文结构化输出。  
这些规则属于 Agent 行为规范，维护在 `SKILL.md` 中，不应在 `run_digest.py` 里硬编码风格替换规则。

推荐提示词（可直接复用）：

```text
你是一名科研助手。请仔细阅读以下论文，并对其进行系统、结构化的分析与解读。

请按照以下结构输出：

研究问题（Research Problem）
- 论文试图解决什么问题？
- 为什么这个问题重要？
- 现有方法有哪些局限？

核心思想（Core Idea）
- 论文的核心直觉是什么？
- 作者提出了什么关键思想使方法有效？

方法（Method）
详细解释论文的方法，包括：
- 模型整体架构
- 各个模块的作用

实验设计（Experiments）
总结实验设置，包括：
- 使用的数据集
- 对比方法（Baselines）
- 评价指标
- 实验结果

并解释：
- 为什么该方法效果更好？
- 实验是否充分？

主要贡献（Contributions）
列出论文的主要贡献。

优点（Strengths）
分析该工作的优势，例如：
- 方法创新性
- 实验设计
- 实际应用价值

局限性（Limitations）
指出可能存在的问题，例如：
- 方法假设
- 实验不足
- 可扩展性问题

对研究者的启示（Research Insights）
- 该论文最值得学习的思想是什么？
- 未来可以如何改进或扩展？

11. 通俗解释
用简单易懂的语言解释这篇论文，使研究生能够快速理解核心思想。

请使用清晰的小标题和条理化结构进行中文输出。输出的内容不少于1000字。
```

输出约束：
- 必须使用读者视角（“本文/该研究”），不要使用作者自述视角（“我们提出/我们设计”）。
- 禁止逐句翻译论文原文；要做信息提炼、逻辑重组与批判性分析。
- 优先引用方法与实验中的关键设计，不要只重复摘要内容。
