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
    parser.add_argument("--profiles-json", default="", help="Optional field profile json from current agent model")
    parser.add_argument("--no-openai", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
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
    if args.profiles_json:
        prep.extend(["--profiles-json", args.profiles_json])

    run_cmd(prep, root)

    run = [py, "scripts/run_digest.py", "--config", args.config_out, "--emit-markdown"]
    if args.dry_run:
        run.append("--dry-run")
    out = run_cmd(run, root)

    payload = json.loads(out)
    results = payload.get("results", [])
    if not results:
        print(out)
        return 1

    # Print full markdown for chat delivery first, then machine-readable summary.
    first = results[0]
    markdown = first.get("markdown", "")
    if markdown:
        print(markdown)
    print("\n---\n")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
