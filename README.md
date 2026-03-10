# arXiv Daily Field Digest Skill

一个可被 Agent 调用的 skill：
- 用户可订阅一个或多个研究领域
- 每天固定时间抓取 arXiv 最新论文
- 按重要性排序推送
- 输出中英双语（英文标题/中文标题/英文摘要/中文摘要/arXiv 链接）
- 支持每领域独立推荐数量（5-20）
- 支持 NEW/UPDATED 版本标识与高亮规则
- 支持 Markdown 落盘，文件名为 `领域_日期.md`

## 目录结构

- `SKILL.md`：skill 定义与执行规范
- `config/subscriptions.json`：订阅配置
- `data/state.json`：去重与运行状态
- `scripts/run_digest.py`：主执行脚本
- `scripts/dev-shell.ps1`：UTF-8 + conda 启动脚本（PowerShell）
- `scripts/dev-shell.cmd`：UTF-8 + conda 启动脚本（CMD）
- `output/daily/`：每日输出的 Markdown

## 环境准备（推荐 Conda）

### 1) 创建并激活环境

```powershell
conda create -n arxiv-digest-lab python=3.10 -y
conda activate arxiv-digest-lab
```

### 2) 安装依赖

```powershell
pip install argostranslate
```

### 3) 安装离线翻译模型（en -> zh）

```powershell
python -c "from argostranslate import package; package.update_package_index(); p=[x for x in package.get_available_packages() if x.from_code=='en' and x.to_code=='zh'][0]; package.install_from_path(p.download())"
```

## 一键开发终端（避免中文乱码）

```powershell
.\scripts\dev-shell.ps1
```

或

```cmd
scripts\dev-shell.cmd
```

这会自动：
- 切换到 UTF-8 终端（`chcp 65001`）
- 激活 `arxiv-digest-lab` 环境

## 配置说明

编辑 `config/subscriptions.json`。

### 核心字段

- `timezone`：时区，例如 `Asia/Shanghai`
- `push_time`：推送时间，例如 `09:00`
- `time_window_hours`：抓取窗口，默认建议 `24`
- `field_settings`：每领域独立配置（重点）
  - `name`：领域名（支持中文或英文）
  - `limit`：该领域推荐数量，范围 `5-20`
  - `keywords`：该领域关键词
  - `exclude_keywords`：排除词
- `highlight`：高亮规则
  - `title_keywords`
  - `authors`
  - `venues`

### 示例

```json
{
  "subscriptions": [
    {
      "id": "rs-db-daily",
      "name": "RecSys + DB Daily",
      "timezone": "Asia/Shanghai",
      "push_time": "09:00",
      "time_window_hours": 24,
      "language": "zh-CN",
      "keywords": ["retrieval", "ranking"],
      "exclude_keywords": ["survey"],
      "field_settings": [
        {"name": "推荐系统", "limit": 20, "keywords": ["recommendation", "recsys"], "exclude_keywords": []},
        {"name": "数据库", "limit": 20, "keywords": ["database", "query", "index"], "exclude_keywords": []}
      ],
      "highlight": {
        "title_keywords": ["benchmark", "alignment", "RAG"],
        "authors": ["Yann LeCun"],
        "venues": ["ICLR", "ICML", "NeurIPS", "KDD", "SIGMOD", "VLDB"]
      }
    }
  ]
}
```

## 运行方式

### Dry-run（不写文件）

```powershell
$env:TRANSLATE_PROVIDER='argos'
python scripts/run_digest.py --dry-run
```

### 正式运行

```powershell
$env:TRANSLATE_PROVIDER='argos'
python scripts/run_digest.py
```

## 翻译提供方

通过环境变量 `TRANSLATE_PROVIDER` 控制：

- `openai`：使用 OpenAI API（需 `OPENAI_API_KEY`）
- `argos`：使用离线 Argos（免费）
- `auto`：先 OpenAI，失败后 Argos
- `none`：不翻译，输出 `[待翻译]`

## 输出格式

每篇论文包含：
- English Title
- Chinese Title
- English Abstract
- 中文摘要
- arXiv URL
- Flags（`NEW` / `UPDATED(vX->vY)` + 高亮标签）

输出文件默认位于：
- `output/daily/<领域>_<YYYY-MM-DD>.md`

## 去重与版本追踪

状态文件 `data/state.json`：
- `sent_ids`：已推送论文 ID
- `sent_versions`：论文 ID -> 版本（v1/v2/...）
- `last_run_at`：上次运行时间

行为：
- 新论文标记 `NEW`
- 已推送但版本升级标记 `UPDATED(v旧->v新)`
- 已推送且版本未变默认不重复推送

## 常见问题

### 1) 输出中文乱码
- 用 `scripts/dev-shell.ps1` 启动
- 确认终端是 `chcp 65001`

### 2) 出现 `[待翻译]`
- 检查 `TRANSLATE_PROVIDER`
- 若用 `openai`，检查 `OPENAI_API_KEY`
- 若用 `argos`，检查 en->zh 模型是否已安装

### 3) 没有结果
- 扩大 `time_window_hours`
- 放宽 `keywords`
- 减少 `exclude_keywords`

## License

MIT
