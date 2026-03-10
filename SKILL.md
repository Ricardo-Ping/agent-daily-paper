---
name: arxiv-daily-field-digest
description: 支持用户按一个或多个研究领域订阅 arXiv 最新论文，按重要性排序并在每日固定时间推送中英双语卡片（英文标题/中文标题/英文摘要/中文摘要/arXiv 链接）。支持每领域独立数量上限、关键词高亮、NEW/UPDATED 版本标识与 Markdown 存档。
---

# arXiv Daily Field Digest

## 1) 配置文件

主配置：`config/subscriptions.json`
状态文件：`data/state.json`
输出目录：`output/daily`

## 2) 关键能力

- 用户可选一个或多个领域
- 每领域独立 `limit`（强制 5-20）
- 重要性排序（领域匹配 + 关键词命中 + 新鲜度）
- 多领域时按领域分组；单领域时不分组
- `NEW` / `UPDATED(vX->vY)` 标识
- 高亮规则：标题关键词 / 作者 / 会议
- 输出为 Markdown，文件名：`<领域1>_<领域2>_<YYYY-MM-DD>.md`

## 3) 推荐配置结构

使用 `field_settings` 定义每个领域：

```json
{
  "field_settings": [
    {"name": "推荐系统", "limit": 20, "keywords": ["recsys"]},
    {"name": "数据库", "limit": 20, "keywords": ["database"]}
  ],
  "highlight": {
    "title_keywords": ["benchmark", "RAG"],
    "authors": ["Yann LeCun"],
    "venues": ["ICLR", "ICML", "NeurIPS"]
  }
}
```

## 4) 翻译方案（API 与免费离线）

脚本按 `TRANSLATE_PROVIDER` 自动选择：

- `openai`：需要 `OPENAI_API_KEY`（质量更高）
- `argos`：免费离线，无需云 API（需本地安装模型）
- `auto`：先 OpenAI，失败后 Argos
- `none`：不翻译，输出 `[待翻译]`

### 4.1 OpenAI 方式

```bash
set OPENAI_API_KEY=你的Key
set TRANSLATE_PROVIDER=openai
python scripts/run_digest.py
```

### 4.2 免费离线 Argos 方式

安装：

```bash
pip install argostranslate
```

安装英->中模型（首次一次性）：

```bash
python -c "from argostranslate import package; package.update_package_index(); p=[x for x in package.get_available_packages() if x.from_code=='en' and x.to_code=='zh'][0]; package.install_from_path(p.download())"
```

运行：

```bash
set TRANSLATE_PROVIDER=argos
python scripts/run_digest.py
```

## 5) 运行命令

调试不写文件：

```bash
python scripts/run_digest.py --dry-run
```

正式生成：

```bash
python scripts/run_digest.py
```

## 6) 输出字段

每篇固定输出：

- English Title
- Chinese Title
- English Abstract
- 中文摘要
- arXiv URL
- Flags（NEW/UPDATED + 高亮命中）

## 7) 失败降级

- API 请求失败自动重试 2 次
- 翻译失败不阻塞主流程，保留 `[待翻译]`
- 无新增结果时输出空结果说明
## 8) 开发终端建议

推荐使用 UTF-8 + conda 环境启动脚本：

- PowerShell: scripts/dev-shell.ps1 
- CMD: scripts/dev-shell.cmd 

目标环境默认：rxiv-digest-lab。

