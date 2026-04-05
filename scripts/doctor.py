#!/usr/bin/env python3
"""Health checks for agent-daily-paper."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from config_migration import (
    CONFIG_SCHEMA_VERSION,
    STATE_SCHEMA_VERSION,
    migrate_state_config,
    migrate_subscriptions_config,
    validate_state_config,
    validate_subscriptions_config,
)

ARXIV_API = "http://export.arxiv.org/api/query"
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


@dataclass
class CheckResult:
    level: str  # OK | WARN | ERROR
    name: str
    detail: str


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _normalize_profile_lookup_key(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "").strip())
    text = re.sub(r"\s+", " ", text)
    return text.lower()


def _is_english_term(term: str) -> bool:
    t = str(term or "").strip()
    return bool(t and re.fullmatch(r"[A-Za-z0-9\-\s]+", t))


def _has_replacement_char(value: str) -> bool:
    return "\ufffd" in str(value or "")


def _auto_fix_hint_for_subscription(
    sub: dict[str, Any],
    config_path: Path,
) -> str:
    field_settings = sub.get("field_settings", []) if isinstance(sub.get("field_settings"), list) else []
    profile_rows = sub.get("field_profiles", []) if isinstance(sub.get("field_profiles"), list) else []
    src_fields: list[str] = []
    for row in profile_rows:
        if not isinstance(row, dict):
            continue
        v = str(row.get("field", "")).strip()
        if v:
            src_fields.append(v)
    if not src_fields:
        for row in field_settings:
            if not isinstance(row, dict):
                continue
            v = str(row.get("name", "")).strip()
            if v:
                src_fields.append(v)
    src_fields = list(dict.fromkeys(src_fields))
    fields_text = ",".join(src_fields[:8]) if src_fields else "你的领域1,你的领域2"
    cfg_text = str(config_path).replace("\\", "/")
    return (
        f"建议自动修复：python scripts/prepare_fields.py --fields \"{fields_text}\" --output \"{cfg_text}\"; "
        "或手动将 field_settings[*].name 对齐为 field_profiles[*].canonical_en（英文）。"
    )


def check_field_profile_alignment(config_path: Path, migrated: dict[str, Any]) -> list[CheckResult]:
    out: list[CheckResult] = []
    subs = migrated.get("subscriptions", [])
    if not isinstance(subs, list) or not subs:
        return out

    clean_count = 0
    for idx, sub in enumerate(subs):
        if not isinstance(sub, dict):
            continue
        sid = str(sub.get("id") or sub.get("name") or f"sub-{idx + 1}").strip()
        field_settings = sub.get("field_settings", [])
        profile_rows_raw = sub.get("field_profiles", [])
        if not isinstance(field_settings, list) or not field_settings:
            continue
        profile_rows = [x for x in profile_rows_raw if isinstance(x, dict)] if isinstance(profile_rows_raw, list) else []

        profile_map: dict[str, dict[str, Any]] = {}
        for row in profile_rows:
            canonical = str(row.get("canonical_en", "")).strip()
            field_name = str(row.get("field", "")).strip()
            if canonical:
                profile_map.setdefault(_normalize_profile_lookup_key(canonical), row)
            if field_name:
                profile_map.setdefault(_normalize_profile_lookup_key(field_name), row)

        mismatches: list[str] = []
        positional_only: list[str] = []
        suspect_garbled: list[str] = []
        invalid_canonical: list[str] = []

        for f_idx, item in enumerate(field_settings):
            if not isinstance(item, dict):
                continue
            fs_name = str(item.get("name", "")).strip()
            if not fs_name:
                continue
            profile = profile_map.get(_normalize_profile_lookup_key(fs_name))
            used_positional_only = False
            if profile is None and len(profile_rows) == len(field_settings) and f_idx < len(profile_rows):
                profile = profile_rows[f_idx]
                used_positional_only = True
            if profile is None:
                mismatches.append(fs_name)
                if _has_replacement_char(fs_name):
                    suspect_garbled.append(fs_name)
                continue
            if used_positional_only:
                positional_only.append(fs_name)

            canonical = str(profile.get("canonical_en", "")).strip()
            if _has_replacement_char(fs_name) or _has_replacement_char(canonical):
                suspect_garbled.append(fs_name)
            if not canonical or not _is_english_term(canonical):
                invalid_canonical.append(fs_name)

        if not mismatches and not positional_only and not suspect_garbled and not invalid_canonical:
            clean_count += 1
            continue

        parts: list[str] = []
        if mismatches:
            parts.append(f"field_settings 与 field_profiles 未匹配: {', '.join(mismatches[:6])}")
        if positional_only:
            parts.append(f"仅按位置兜底匹配（建议修正字段名）: {', '.join(list(dict.fromkeys(positional_only))[:6])}")
        if suspect_garbled:
            parts.append(f"疑似乱码（含替换字符或异常编码）: {', '.join(list(dict.fromkeys(suspect_garbled))[:6])}")
        if invalid_canonical:
            parts.append(f"canonical_en 缺失或非英文: {', '.join(list(dict.fromkeys(invalid_canonical))[:6])}")
        parts.append(_auto_fix_hint_for_subscription(sub, config_path))
        out.append(CheckResult("WARN", f"field/profile alignment ({sid})", " | ".join(parts)))

    if clean_count == len([s for s in subs if isinstance(s, dict)]):
        out.append(CheckResult("OK", "field/profile alignment", "field_settings 与 field_profiles 对齐正常"))
    return out


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

    migrated, changes = migrate_subscriptions_config(cfg)
    if changes:
        out.append(
            CheckResult(
                "WARN",
                "subscriptions.json migration",
                "detected auto-migration candidates; run run_digest.py once to persist updates",
            )
        )

    schema_v = migrated.get("schema_version")
    if schema_v == CONFIG_SCHEMA_VERSION:
        out.append(CheckResult("OK", "subscriptions schema", f"schema_version={schema_v}"))
    else:
        out.append(CheckResult("WARN", "subscriptions schema", f"expected {CONFIG_SCHEMA_VERSION}, got {schema_v}"))

    if isinstance(migrated, dict) and bool(migrated.get("setup_required", False)):
        out.append(
            CheckResult(
                "WARN",
                "subscriptions.json",
                "setup_required=true: 尚未完成首次配置（这是预期状态）。请先填写领域、数量、推送时间、时区。",
            )
        )
        return out

    errors, warnings = validate_subscriptions_config(migrated)
    subs = migrated.get("subscriptions", []) if isinstance(migrated, dict) else []
    out.append(CheckResult("OK", "subscriptions.json", f"subscriptions count: {len(subs)}"))
    for item in warnings:
        out.append(CheckResult("WARN", "subscriptions validation", item))
    for item in errors:
        out.append(CheckResult("ERROR", "subscriptions validation", item))
    out.extend(check_field_profile_alignment(config_path, migrated))

    return out


def check_state_schema(path: Path) -> list[CheckResult]:
    out: list[CheckResult] = []
    try:
        state = load_json(path)
    except Exception as exc:
        return [CheckResult("ERROR", "state.json", f"invalid JSON: {exc}")]

    migrated, changes = migrate_state_config(state)
    if changes:
        out.append(
            CheckResult(
                "WARN",
                "state.json migration",
                "detected auto-migration candidates; run run_digest.py once to persist updates",
            )
        )
    schema_v = migrated.get("schema_version")
    if schema_v == STATE_SCHEMA_VERSION:
        out.append(CheckResult("OK", "state schema", f"schema_version={schema_v}"))
    else:
        out.append(CheckResult("WARN", "state schema", f"expected {STATE_SCHEMA_VERSION}, got {schema_v}"))

    errors, warnings = validate_state_config(migrated)
    for item in warnings:
        out.append(CheckResult("WARN", "state validation", item))
    for item in errors:
        out.append(CheckResult("ERROR", "state validation", item))
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
        en = next((x for x in langs if str(getattr(x, "code", "")).lower() == "en"), None)
        zh = next((x for x in langs if str(getattr(x, "code", "")).lower().startswith("zh")), None)
        if not en or not zh:
            out.append(CheckResult("WARN", "argos model", "en/zh language packs may be missing"))
            return out
        ok_en_zh = False
        ok_zh_en = False
        try:
            tr = en.get_translation(zh)
            ok_en_zh = tr is not None and hasattr(tr, "translate")
        except Exception:
            ok_en_zh = False
        try:
            tr = zh.get_translation(en)
            ok_zh_en = tr is not None and hasattr(tr, "translate")
        except Exception:
            ok_zh_en = False
        if ok_en_zh and ok_zh_en:
            out.append(CheckResult("OK", "argos model", "en<->zh translators verified"))
        elif ok_en_zh or ok_zh_en:
            out.append(CheckResult("WARN", "argos model", "only one-way translator is available (en<->zh incomplete)"))
        else:
            out.append(CheckResult("WARN", "argos model", "en<->zh translators unavailable; run install_argos_model.py"))
    except Exception as exc:
        out.append(CheckResult("WARN", "argos model", f"cannot inspect model: {exc}"))
    return out


def check_translate_runtime() -> CheckResult:
    provider = os.getenv("TRANSLATE_PROVIDER", "argos").strip().lower()
    has_openai = bool(os.getenv("OPENAI_API_KEY"))
    if provider == "openai" and not has_openai:
        return CheckResult("ERROR", "translate runtime", "TRANSLATE_PROVIDER=openai but OPENAI_API_KEY is missing")
    if provider in ("auto", "openai") and has_openai:
        return CheckResult("OK", "translate runtime", f"provider={provider}, OPENAI_API_KEY detected")
    if provider in ("argos", "auto"):
        return CheckResult("OK", "translate runtime", f"provider={provider}, local Argos path available")
    if provider == "none":
        return CheckResult("WARN", "translate runtime", "provider=none, translation disabled")
    return CheckResult("WARN", "translate runtime", f"provider={provider}, OPENAI_API_KEY missing")


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
    if state.exists():
        results.extend(check_state_schema(state))
    results.append(check_agent_profiles(agent_profiles))
    results.append(check_translate_runtime())
    results.extend(check_argos())
    results.append(check_arxiv_network())
    results.append(check_workflow(workflow))

    return print_results(results)


if __name__ == "__main__":
    raise SystemExit(main())
