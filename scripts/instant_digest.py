#!/usr/bin/env python3
"""Run instant (on-demand) digest without waiting for schedule."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def run_cmd(cmd: list[str], cwd: Path) -> str:
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=False)
    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(stderr.strip() or stdout.strip() or "command failed")
    return stdout


def main() -> int:
    parser = argparse.ArgumentParser(description="Instant arXiv digest for given fields")
    parser.add_argument("--fields", required=True, help="Comma-separated field names, e.g. 数据库优化器,推荐系统")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--push-time", default="09:00")
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--time-window-hours", type=int, default=24)
    parser.add_argument("--config-out", default="config/subscriptions.instant.json")
    parser.add_argument("--profiles-json", default="config/agent_field_profiles.json", help="Agent profile json path")
    parser.add_argument("--taxonomy-json", default="data/arxiv_taxonomy.json", help="Local taxonomy json path")
    parser.add_argument(
        "--category-expand-mode",
        default="balanced",
        choices=["off", "conservative", "balanced", "broad"],
    )
    parser.add_argument("--agent-categories-only", action="store_true")
    parser.add_argument("--no-openai", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--respect-history",
        action="store_true",
        help="Respect sent history (default is ignore history for instant run)",
    )
    parser.add_argument(
        "--with-json-summary",
        action="store_true",
        help="Also print machine-readable JSON summary after markdown",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    py = sys.executable

    prep = [
        py,
        "scripts/prepare_fields.py",
        "--fields",
        args.fields,
        "--limit",
        str(args.limit),
        "--name",
        "Instant Digest",
        "--id",
        "instant-digest",
        "--push-time",
        args.push_time,
        "--timezone",
        args.timezone,
        "--time-window-hours",
        str(args.time_window_hours),
        "--output",
        args.config_out,
    ]
    if args.no_openai:
        prep.append("--no-openai")
    prep.extend(["--category-expand-mode", args.category_expand_mode])
    if args.agent_categories_only:
        prep.append("--agent-categories-only")
    if args.profiles_json:
        prep.extend(["--profiles-json", args.profiles_json])
    if args.taxonomy_json:
        prep.extend(["--taxonomy-json", args.taxonomy_json])

    run_cmd(prep, root)

    run = [py, "scripts/run_digest.py", "--config", args.config_out, "--emit-markdown"]
    if not args.respect_history:
        run.append("--ignore-history")
    if args.dry_run:
        run.append("--dry-run")
    out = run_cmd(run, root)

    payload = json.loads(out)
    results = payload.get("results", [])
    if not results:
        print(out)
        return 1

    # Print markdown from generated file first to guarantee chat output
    # is exactly the same as saved .md content.
    first = results[0]
    markdown = ""
    output_file = str(first.get("output_file", "")).strip()
    if output_file:
        md_path = (root / output_file).resolve() if not Path(output_file).is_absolute() else Path(output_file)
        if md_path.exists():
            markdown = md_path.read_text(encoding="utf-8", errors="replace")
    if not markdown:
        markdown = first.get("markdown", "")
    if markdown:
        print(markdown)
    if args.with_json_summary:
        print("\n---\n")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
