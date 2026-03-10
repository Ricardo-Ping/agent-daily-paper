---
name: agent-daily-paper
description: 支持用户按一个或多个研究领域订阅 arXiv 最新论文，按重要性排序并在每日固定时间推送中英双语卡片（英文标题/中文标题/英文摘要/中文摘要/arXiv 链接）。支持每领域独立数量上限（5-20）、关键词高亮、NEW/UPDATED 版本标识与 Markdown 存档。首次使用时必须先引导用户完成订阅配置（领域、每领域论文数、每日提醒时间、时区、关键词/排除词、翻译方式）。
---

# Agent Daily Paper

## 执行原则

- 首次使用时，先收集并确认配置，再执行推荐。
- 配置缺失时，不直接抓取，先补齐配置。
- 推送执行后，除了保存 Markdown 文件，还要在聊天中返回完整 Markdown 内容。

## 首次使用必填配置

- 研究领域：`field_settings[].name`（可多个）
- 每领域数量：`field_settings[].limit`（5-20）
- 每日提醒时间：`push_time`（HH:MM）
- 时区：`timezone`（默认 `Asia/Shanghai`）

## 建议配置

- `keywords` / `exclude_keywords`
- `time_window_hours`
- `highlight.title_keywords` / `highlight.authors` / `highlight.venues`
- `TRANSLATE_PROVIDER`：`openai` / `argos` / `auto` / `none`

## 检索与排序规则

- 检索不只依赖 arXiv 大类，使用“类别 + 标题/摘要关键词”召回。
- 对细分方向（如“数据库优化器”）启用标题/摘要模糊匹配打分。
- 默认按重要性分数排序：类别命中 + 关键词命中 + 模糊命中 + 新鲜度。
- 若 24h 无结果，自动启用回退策略（扩大时间窗、可放宽关键词）。

## 输出规范

每篇论文必须包含：
- English Title
- Chinese Title
- English Abstract
- 中文摘要
- arXiv URL
- Flags（`NEW` / `UPDATED(vX->vY)` + 高亮标签）

Markdown 命名：`<领域1>_<领域2>_<YYYY-MM-DD>.md`

- 多领域时按领域分组。
- 单领域时不分组。

## 运行命令

- 调试（不写文件）：`python scripts/run_digest.py --dry-run --emit-markdown`
- 正式运行：`python scripts/run_digest.py --emit-markdown`
- 定时场景（如 GitHub Actions）：`python scripts/run_digest.py --only-due-now --due-window-minutes 15 --emit-markdown`

## 安装（Conda，跨系统）

推荐环境名：`arxiv-digest-lab`

Windows / macOS / Linux：

```bash
conda create -n arxiv-digest-lab python=3.10 -y
conda activate arxiv-digest-lab
pip install argostranslate
python -c "from argostranslate import package; package.update_package_index(); p=[x for x in package.get_available_packages() if x.from_code=='en' and x.to_code=='zh'][0]; package.install_from_path(p.download())"
```

## 失败兜底

- 翻译失败时输出 `[待翻译]`，不阻塞主流程。
- 请求失败自动重试。
- 若无论文命中，输出“当前窗口无新增论文”并给出统计。
