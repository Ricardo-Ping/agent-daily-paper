# 平台适配

本项目的业务入口与 agent 平台无关。平台适配层只负责四件事：定位仓库、加载 `SKILL.md`、在仓库根目录执行命令、把标准输出回传给用户。

## 统一执行契约

- 工作目录必须是包含 `SKILL.md`、`scripts/`、`config/` 的仓库根目录。不要硬编码 OpenClaw workspace 或任意用户名目录。
- 多个平台共用时优先链接到同一个本地仓库，避免复制后形成多份 `config/` 与 `data/state.json`。
- 首次运行：`python scripts/bootstrap_env.py --run-doctor`。
- 即时日报：`conda run -n arxiv-digest-lab python scripts/instant_digest.py --fields "领域1,领域2" --limit 10 --time-window-hours 72`。
- 精确定时：`conda run -n arxiv-digest-lab python scripts/run_digest.py --config config/subscriptions.json --emit-markdown`。
- 只有共享轮询任务才增加 `--only-due-now --due-window-minutes 75`。
- 进程退出码是运行状态的依据。标准输出中的完整 Markdown 与 `output/daily/*.md` 应一致。
- `reason=already_pushed_today` 时返回“今天该领域已推送过”；无命中且未推送时返回“当天该领域无最新论文”；有结果时返回完整 Markdown，不改写成标题列表。
- 平台如果不支持主动消息投递，仍应保存 Markdown，并在任务记录或下一次会话中给出文件路径。

## Codex

Codex 可从用户级 `$CODEX_HOME/skills/agent-daily-paper`（通常为 `~/.codex/skills/agent-daily-paper`）发现此 skill。可以把仓库克隆到该目录，或把该目录链接到现有本地仓库。仓库自带 `agents/openai.yaml`，供 Codex UI 展示和调用。

即时任务由 Codex 在仓库根目录执行统一命令。用户要求每日或周期推送时，优先使用 Codex 的 automation 能力，并在 automation prompt 中写清：绝对仓库路径、执行命令、时区、结果回传契约。调度时间使用平台字段，不把平台内部的原始调度表达式写进 `SKILL.md`。

## Claude Code

Claude Code 可从项目级 `.claude/skills/agent-daily-paper/` 或用户级 `~/.claude/skills/agent-daily-paper/` 加载 skill。将仓库放入该位置，或链接到已有仓库；入口仍是根目录的 `SKILL.md`。

Claude Code 会话内可直接执行即时命令。周期任务应使用 Claude Code 当前版本提供的 scheduler；若当前环境没有 scheduler，则使用系统 cron、launchd 或 Windows 任务计划程序。不要模拟一个常驻聊天循环。

## OpenClaw

OpenClaw 只是可选宿主。将任务工作目录指向仓库实际路径并执行统一命令即可。飞书、Slack 等 delivery 字段属于部署环境配置，不写入通用 skill，也不假设用户已安装相应渠道。

## 其他 agent harness

满足下列接口即可接入：

1. 启动时把 `SKILL.md` 作为 agent 指令加载，或在系统提示中提供其绝对路径并要求按需读取。
2. 为 agent 提供在仓库根目录运行 Python 进程的工具。
3. 保留进程退出码、stdout 和生成文件。
4. scheduler 用订阅中的 `push_time + timezone` 精确触发；若只能轮询，再使用 `--only-due-now`。
5. delivery adapter 原样发送 stdout 中的 Markdown，并把投递成功与否和论文抓取成功与否分开记录。

最小 harness 任务描述：

```text
读取仓库根目录的 SKILL.md，并在该目录执行：
conda run -n arxiv-digest-lab python scripts/run_digest.py --config config/subscriptions.json --emit-markdown
遵守 SKILL.md 的首次配置、输出和去重规则。返回完整 stdout；同时保留 output/daily 下生成的 Markdown。
```
