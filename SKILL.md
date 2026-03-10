---
name: agent-daily-paper
description: 支持用户按一个或多个研究领域订阅 arXiv 最新论文，按重要性排序并在每日固定时间推送中英双语卡片（英文标题、中文标题、英文摘要、中文摘要、arXiv 链接）。支持每领域独立数量上限（5-20）、关键词高亮、NEW/UPDATED 版本标识与 Markdown 存档。首次使用时必须先引导用户完成订阅配置（领域、每领域论文数、每日提醒时间、时区、关键词/排除词、翻译方式）。
---

# Agent Daily Paper

## 1) 首次使用（必须先配置）

当用户第一次使用，或配置文件不存在/不完整时，必须先引导并确认以下配置，再执行抓取：

- 关注领域：`1..N` 个
- 每领域论文数量：`5-20`
- 每日提醒时间：如 `09:00`
- 时区：默认 `Asia/Shanghai`
- 关键词与排除词：可选
- 翻译方式：`openai` / `argos` / `auto` / `none`

最小必填键：

- `field_settings[].name`
- `field_settings[].limit`
- `push_time`
- `timezone`

若任一必填项缺失，不要直接运行推荐，先补齐配置。

## 2) 配置与输出路径

- 订阅配置：`config/subscriptions.json`
- 状态文件：`data/state.json`
- 输出目录：`output/daily`

## 3) 核心能力

- 支持单领域/多领域订阅
- 每领域独立 `limit`（强制 `5-20`）
- 按重要性排序（领域匹配 + 关键词命中 + 新鲜度）
- 多领域时按领域分组；单领域时不分组
- 版本标识：`NEW` / `UPDATED(vX->vY)`
- 高亮规则：标题关键词 / 作者 / 会议
- Markdown 存档命名：`<领域1>_<领域2>_<YYYY-MM-DD>.md`

## 4) 推荐配置结构

```json
{
  "subscriptions": [
    {
      "id": "rs-db-daily",
      "name": "RecSys + DB Daily",
      "timezone": "Asia/Shanghai",
      "push_time": "09:00",
      "time_window_hours": 24,
      "field_settings": [
        {"name": "推荐系统", "limit": 20, "keywords": ["recsys"]},
        {"name": "数据库", "limit": 20, "keywords": ["query optimizer"]}
      ],
      "highlight": {
        "title_keywords": ["benchmark", "RAG"],
        "authors": ["Yann LeCun"],
        "venues": ["ICLR", "ICML", "NeurIPS", "KDD", "SIGMOD", "VLDB"]
      }
    }
  ]
}
```

## 5) 翻译方式

通过环境变量 `TRANSLATE_PROVIDER` 选择：

- `openai`：需 `OPENAI_API_KEY`（质量更高）
- `argos`：免费离线（需安装 `argostranslate` 与 `en->zh` 模型）
- `auto`：先 OpenAI，失败后 Argos
- `none`：不翻译，输出 `[待翻译]`

## 6) 安装指引（按操作系统）

优先建议使用 conda 环境 `arxiv-digest-lab`。

### Windows

```powershell
conda create -n arxiv-digest-lab python=3.10 -y
conda activate arxiv-digest-lab
pip install argostranslate
python -c "from argostranslate import package; package.update_package_index(); p=[x for x in package.get_available_packages() if x.from_code=='en' and x.to_code=='zh'][0]; package.install_from_path(p.download())"
```

### macOS / Linux

```bash
conda create -n arxiv-digest-lab python=3.10 -y
conda activate arxiv-digest-lab
pip install argostranslate
python -c "from argostranslate import package; package.update_package_index(); p=[x for x in package.get_available_packages() if x.from_code=='en' and x.to_code=='zh'][0]; package.install_from_path(p.download())"
```

## 7) 运行命令

调试（不写文件）：

```bash
python scripts/run_digest.py --dry-run
```

正式运行：

```bash
python scripts/run_digest.py
```

## 8) 输出字段（每篇论文）

- English Title
- Chinese Title
- English Abstract
- 中文摘要
- arXiv URL
- Flags（`NEW/UPDATED` + 高亮标签）

## 9) 失败降级策略

- API 失败：自动重试 2 次
- 翻译失败：不阻塞主流程，保留 `[待翻译]`
- 无新增：输出“当前窗口无新增论文”与统计信息

## 10) 开发终端建议

建议使用 UTF-8 + conda 启动脚本：

- PowerShell：`scripts/dev-shell.ps1`
- CMD：`scripts/dev-shell.cmd`

默认环境：`arxiv-digest-lab`
