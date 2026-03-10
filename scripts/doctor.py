#!/usr/bin/env python3
"""Health checks for agent-daily-paper."""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


ARXIV_API = "http://export.arxiv.org/api/query"
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


@dataclass
class CheckResult:
    level: str  # OK | WARN | ERROR
    name: str
    detail: str


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def check_file_exists(path: Path, name: str) -> CheckResult:
    if path.exists():
        return CheckResult("OK", name, f"found: {path}")
    return CheckResult("ERROR", name, f"missing: {path}")


def check_subscriptions(config_path: Path) -> list[CheckResult]:
    out: list[CheckResult] = []
    try:
        cfg = load_json(config_path)
    except Exception as exc:
        return [CheckResult("ERROR", "subscriptions.json", f"invalid JSON: {exc}")]

    subs = cfg.get("subscriptions", []) if isinstance(cfg, dict) else []
    if not isinstance(subs, list) or not subs:
        out.append(CheckResult("ERROR", "subscriptions.json", "subscriptions is empty or invalid"))
        return out

    out.append(CheckResult("OK", "subscriptions.json", f"subscriptions count: {len(subs)}"))

    for i, sub in enumerate(subs, start=1):
        sid = str(sub.get("id") or f"sub-{i}")
        tz = str(sub.get("timezone", "Asia/Shanghai"))
        pt = str(sub.get("push_time", ""))
        fs = sub.get("field_settings", [])

        if not re.fullmatch(r"\d{1,2}:\d{2}", pt):
            out.append(CheckResult("ERROR", f"{sid}.push_time", f"invalid HH:MM: {pt}"))
        else:
            hh, mm = [int(x) for x in pt.split(":")]
            if hh > 23 or mm > 59:
                out.append(CheckResult("ERROR", f"{sid}.push_time", f"out of range: {pt}"))
            else:
                out.append(CheckResult("OK", f"{sid}.push_time", pt))

        if ZoneInfo is None:
            out.append(CheckResult("WARN", f"{sid}.timezone", "zoneinfo unavailable in this Python"))
        else:
            try:
                ZoneInfo(tz)
                out.append(CheckResult("OK", f"{sid}.timezone", tz))
            except Exception:
                out.append(CheckResult("ERROR", f"{sid}.timezone", f"invalid timezone: {tz}"))

        if not isinstance(fs, list) or not fs:
            out.append(CheckResult("ERROR", f"{sid}.field_settings", "field_settings missing"))
            continue

        for f in fs:
            name = str(f.get("name", "")).strip()
            limit = f.get("limit", 0)
            if not name:
                out.append(CheckResult("ERROR", f"{sid}.field.name", "empty field name"))
            try:
                il = int(limit)
                if il < 5 or il > 20:
                    out.append(CheckResult("WARN", f"{sid}.field.limit", f"recommended range 5-20, got {il}"))
                else:
                    out.append(CheckResult("OK", f"{sid}.field.limit", f"{name}: {il}"))
            except Exception:
                out.append(CheckResult("ERROR", f"{sid}.field.limit", f"invalid limit: {limit}"))

    return out


def check_agent_profiles(path: Path) -> CheckResult:
    if not path.exists():
        return CheckResult("WARN", "agent_field_profiles.json", f"not found: {path} (fallback still works)")
    try:
        data = load_json(path)
    except Exception as exc:
        return CheckResult("ERROR", "agent_field_profiles.json", f"invalid JSON: {exc}")
    if not isinstance(data, dict):
        return CheckResult("ERROR", "agent_field_profiles.json", "must be a JSON object")
    return CheckResult("OK", "agent_field_profiles.json", f"profiles: {len(data)}")


def check_argos() -> list[CheckResult]:
    out: list[CheckResult] = []
    try:
        from argostranslate import translate as argos_translate  # type: ignore
    except Exception as exc:
        return [CheckResult("WARN", "argostranslate", f"not installed: {exc}")]

    out.append(CheckResult("OK", "argostranslate", "installed"))
    try:
        langs = argos_translate.get_installed_languages()
        has_en = any(x.code == "en" for x in langs)
        has_zh = any(x.code in ("zh", "zh_CN") for x in langs)
        if has_en and has_zh:
            out.append(CheckResult("OK", "argos model", "en/zh language packs detected"))
        else:
            out.append(CheckResult("WARN", "argos model", "en/zh model may be missing"))
    except Exception as exc:
        out.append(CheckResult("WARN", "argos model", f"cannot inspect model: {exc}"))
    return out


def check_arxiv_network() -> CheckResult:
    params = {
        "search_query": "cat:cs.AI",
        "start": 0,
        "max_results": 1,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    try:
        full_url = f"{ARXIV_API}?{urlencode(params)}"
        req = Request(full_url, headers={"User-Agent": "agent-daily-paper-doctor/1.0"})
        with urlopen(req, timeout=20) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        root = ET.fromstring(text)
        entries = root.findall("atom:entry", ATOM_NS)
        return CheckResult("OK", "arxiv network", f"reachable, entries fetched: {len(entries)}")
    except Exception as exc:
        return CheckResult("ERROR", "arxiv network", f"request failed: {exc}")


def check_workflow(path: Path) -> CheckResult:
    if not path.exists():
        return CheckResult("WARN", "github actions", f"workflow missing: {path}")
    text = path.read_text(encoding="utf-8", errors="replace")
    required = ["schedule:", "run_digest.py --only-due-now"]
    missed = [x for x in required if x not in text]
    if missed:
        return CheckResult("WARN", "github actions", f"workflow exists but missing: {', '.join(missed)}")
    return CheckResult("OK", "github actions", "workflow present and key steps found")


def print_results(results: list[CheckResult]) -> int:
    level_rank = {"OK": 0, "WARN": 1, "ERROR": 2}
    max_level = 0
    for r in results:
        max_level = max(max_level, level_rank.get(r.level, 2))
        print(f"[{r.level}] {r.name}: {r.detail}")

    errors = sum(1 for r in results if r.level == "ERROR")
    warns = sum(1 for r in results if r.level == "WARN")
    oks = sum(1 for r in results if r.level == "OK")
    print("\nSummary:")
    print(f"- OK: {oks}")
    print(f"- WARN: {warns}")
    print(f"- ERROR: {errors}")
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Health checks for agent-daily-paper")
    parser.add_argument("--config", default="config/subscriptions.json")
    parser.add_argument("--agent-profiles", default="config/agent_field_profiles.json")
    parser.add_argument("--state", default="data/state.json")
    parser.add_argument("--workflow", default=".github/workflows/daily-digest.yml")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    config = root / args.config
    agent_profiles = root / args.agent_profiles
    state = root / args.state
    workflow = root / args.workflow

    results: list[CheckResult] = []
    results.append(check_file_exists(config, "config"))
    results.append(check_file_exists(state, "state"))
    if config.exists():
        results.extend(check_subscriptions(config))
    results.append(check_agent_profiles(agent_profiles))
    results.extend(check_argos())
    results.append(check_arxiv_network())
    results.append(check_workflow(workflow))

    return print_results(results)


if __name__ == "__main__":
    raise SystemExit(main())
