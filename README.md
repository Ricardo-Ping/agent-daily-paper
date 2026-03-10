# agent-daily-paper

一个可被 Agent 调用的 arXiv 每日论文推送 Skill：
- 用户可选择一个或多个研究领域
- 每领域可独立设置每日推荐数量（5-20）
- 每日固定时间推送
- 按重要性排序
- 输出英文标题、中文标题、英文摘要、中文摘要、arXiv 链接
- 支持 `NEW/UPDATED` 标识、关键词高亮、Markdown 存档
- 实际返回篇数为“最多 limit 篇”，若当日命中不足会少于 limit

## 1. 首次使用必须先配置

首次安装或首次运行时，Agent 必须先询问并写入以下配置，再执行推送：
- 领域（一个或多个）
- 每领域论文数（5-20）
- 每日提醒时间（`push_time`，HH:MM）
- 时区（默认 `Asia/Shanghai`）
- 可选：关键词、排除词、翻译方式

配置文件：`config/subscriptions.json`

## 2. 为什么你之前会遇到“无内容”

旧逻辑偏向“按 arXiv 大类字段召回”，对“数据库优化器”这类细分方向不友好。

现在已改为：
- 召回：`类别 + 标题/摘要关键词`
- 排序：增加标题/摘要模糊匹配分数（细分词可命中）
- 兜底：若 24h 无结果，可自动扩大时间窗并放宽关键词

## 3. 输出行为（已修复）

运行脚本时加 `--emit-markdown`，会在 JSON 输出中包含完整 Markdown 文本。

也就是说，Agent 推送时应同时：
- 保存 `output/daily/*.md`
- 在聊天里直接返回完整 Markdown 内容

## 4. Conda 环境安装（推荐）

环境名建议：`arxiv-digest-lab`

Windows / macOS / Linux 通用：

```bash
conda create -n arxiv-digest-lab python=3.10 -y
conda activate arxiv-digest-lab
pip install argostranslate
python -c "from argostranslate import package; package.update_package_index(); p=[x for x in package.get_available_packages() if x.from_code=='en' and x.to_code=='zh'][0]; package.install_from_path(p.download())"
```

翻译提供方：
- `TRANSLATE_PROVIDER=openai`（需 `OPENAI_API_KEY`）
- `TRANSLATE_PROVIDER=argos`（离线免费）
- `TRANSLATE_PROVIDER=auto`（先 OpenAI，失败后 Argos）
- `TRANSLATE_PROVIDER=none`

## 5. 本地运行

调试（不写文件）：

```bash
python scripts/run_digest.py --dry-run --emit-markdown
```

正式运行：

```bash
python scripts/run_digest.py --emit-markdown
```

按配置时间执行（给定时器/CI）：

```bash
python scripts/run_digest.py --only-due-now --due-window-minutes 15 --emit-markdown
```

## 6. GitHub Actions 定时推送

已提供工作流：`.github/workflows/daily-digest.yml`

机制：
- 每 10 分钟触发一次
- 脚本根据每个订阅的 `push_time + timezone` 判断“现在是否到点”
- 仅在到点窗口内执行，且同一订阅每天只推送一次
- 产物变更后自动提交 `output/daily` 和 `data/state.json`

### 启用步骤

1. 将仓库推送到 GitHub（默认分支 `main`）
2. 在仓库 `Settings -> Secrets and variables -> Actions` 配置：
- `OPENAI_API_KEY`（可选，仅 OpenAI 翻译需要）
- `TRANSLATE_PROVIDER`（可选，建议设为 `auto` 或 `argos`）
3. 打开 `Actions`，启用工作流
4. 可用 `Run workflow` 手动触发测试

## 7. 关键文件

- `SKILL.md`：Skill 行为规范（中文）
- `agents/openai.yaml`：Skill 元信息
- `scripts/run_digest.py`：主逻辑（模糊匹配、到点执行、完整 Markdown 输出）
- `config/subscriptions.json`：订阅配置
- `data/state.json`：去重状态与每日推送状态
- `output/daily/`：每日 Markdown 归档
