#!/usr/bin/env python3
"""Config/state validation and auto-migration helpers."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


CONFIG_SCHEMA_VERSION = 2
STATE_SCHEMA_VERSION = 2
_VALID_CATEGORY_EXPAND_MODE = {"off", "conservative", "balanced", "broad"}
_VALID_QUERY_STRATEGY = {"category_keyword_union", "keyword_union", "category_first", "hybrid"}


def clamp_limit(v: Any, min_v: int = 5, max_v: int = 20, fallback: int = 10) -> int:
    try:
        iv = int(v)
    except Exception:
        iv = fallback
    return max(min_v, min(max_v, iv))


def _normalize_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            out.append(text)
    return list(dict.fromkeys(out))


def _normalize_push_time(raw: Any, fallback: str = "09:00") -> str:
    text = str(raw or "").strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not m:
        return fallback
    hh = int(m.group(1))
    mm = int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return fallback
    return f"{hh:02d}:{mm:02d}"


def _normalize_timezone(raw: Any, fallback: str = "Asia/Shanghai") -> str:
    tz = str(raw or "").strip() or fallback
    if ZoneInfo is None:
        return tz
    try:
        ZoneInfo(tz)
        return tz
    except Exception:
        return fallback


def _normalize_field_settings(sub: dict[str, Any], changes: list[str], index: int) -> None:
    raw_field_settings = sub.get("field_settings")
    if not isinstance(raw_field_settings, list) or not raw_field_settings:
        fields = _normalize_str_list(sub.get("fields"))
        daily_count = clamp_limit(sub.get("daily_count", 10))
        if fields:
            sub["field_settings"] = [
                {
                    "name": f,
                    "limit": daily_count,
                    "categories": [],
                    "primary_categories": [],
                    "keywords": [],
                    "exclude_keywords": [],
                }
                for f in fields
            ]
            changes.append(f"subscriptions[{index}]: migrated legacy fields/daily_count -> field_settings")
        else:
            sub["field_settings"] = []
    normalized: list[dict[str, Any]] = []
    for item in sub.get("field_settings", []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        normalized.append(
            {
                "name": name,
                "limit": clamp_limit(item.get("limit", 10)),
                "categories": _normalize_str_list(item.get("categories")),
                "primary_categories": _normalize_str_list(item.get("primary_categories")),
                "keywords": _normalize_str_list(item.get("keywords")),
                "exclude_keywords": _normalize_str_list(item.get("exclude_keywords")),
            }
        )
    if normalized != sub.get("field_settings"):
        changes.append(f"subscriptions[{index}]: normalized field_settings")
    sub["field_settings"] = normalized

    # Keep compatibility, but these are no longer source-of-truth.
    if "fields" in sub:
        sub.pop("fields", None)
        changes.append(f"subscriptions[{index}]: removed deprecated fields")
    if "daily_count" in sub:
        sub.pop("daily_count", None)
        changes.append(f"subscriptions[{index}]: removed deprecated daily_count")


def migrate_subscriptions_config(config: Any) -> tuple[dict[str, Any], list[str]]:
    changes: list[str] = []
    if not isinstance(config, dict):
        config = {"subscriptions": []}
        changes.append("config: reset invalid root to object")

    out: dict[str, Any] = dict(config)
    subs = out.get("subscriptions")
    if not isinstance(subs, list):
        subs = []
        out["subscriptions"] = subs
        changes.append("config: reset invalid subscriptions to empty list")

    normalized_subs: list[dict[str, Any]] = []
    for i, raw_sub in enumerate(subs):
        if not isinstance(raw_sub, dict):
            changes.append(f"subscriptions[{i}]: dropped invalid entry")
            continue
        sub = dict(raw_sub)

        if not str(sub.get("id", "")).strip():
            sub["id"] = f"sub-{i + 1}"
            changes.append(f"subscriptions[{i}]: auto-filled id")
        if not str(sub.get("name", "")).strip():
            sub["name"] = str(sub["id"])
            changes.append(f"subscriptions[{i}]: auto-filled name")

        old_timezone = sub.get("timezone")
        new_timezone = _normalize_timezone(old_timezone)
        if old_timezone != new_timezone:
            changes.append(f"subscriptions[{i}]: normalized timezone -> {new_timezone}")
        sub["timezone"] = new_timezone

        old_push_time = sub.get("push_time")
        new_push_time = _normalize_push_time(old_push_time)
        if old_push_time != new_push_time:
            changes.append(f"subscriptions[{i}]: normalized push_time -> {new_push_time}")
        sub["push_time"] = new_push_time

        if "time_window_hours" not in sub:
            sub["time_window_hours"] = 24
            changes.append(f"subscriptions[{i}]: defaulted time_window_hours=24")
        else:
            try:
                sub["time_window_hours"] = max(1, int(sub.get("time_window_hours", 24)))
            except Exception:
                sub["time_window_hours"] = 24
                changes.append(f"subscriptions[{i}]: fixed invalid time_window_hours")

        strategy = str(sub.get("query_strategy", "")).strip().lower()
        if strategy not in _VALID_QUERY_STRATEGY:
            sub["query_strategy"] = "category_keyword_union"
            changes.append(f"subscriptions[{i}]: defaulted query_strategy=category_keyword_union")
        else:
            sub["query_strategy"] = strategy

        if "require_primary_category" not in sub:
            sub["require_primary_category"] = True
            changes.append(f"subscriptions[{i}]: defaulted require_primary_category=true")
        else:
            sub["require_primary_category"] = bool(sub.get("require_primary_category"))

        mode = str(sub.get("category_expand_mode", "balanced")).strip().lower()
        if mode not in _VALID_CATEGORY_EXPAND_MODE:
            sub["category_expand_mode"] = "balanced"
            changes.append(f"subscriptions[{i}]: defaulted category_expand_mode=balanced")
        else:
            sub["category_expand_mode"] = mode

        _normalize_field_settings(sub, changes, i)
        normalized_subs.append(sub)

    out["subscriptions"] = normalized_subs

    schema_version = out.get("schema_version")
    if schema_version != CONFIG_SCHEMA_VERSION:
        out["schema_version"] = CONFIG_SCHEMA_VERSION
        changes.append(f"config: schema_version -> {CONFIG_SCHEMA_VERSION}")

    # Ensure setup-required shape is consistent.
    if bool(out.get("setup_required", False)) and not out.get("setup_message"):
        out["setup_message"] = (
            "首次使用请先配置：研究领域（可多项）、每领域数量(5-20)、每日推送时间(HH:MM)、时区。"
        )
        changes.append("config: setup_message auto-filled")

    return out, changes


def migrate_state_config(state: Any) -> tuple[dict[str, Any], list[str]]:
    changes: list[str] = []
    if not isinstance(state, dict):
        state = {}
        changes.append("state: reset invalid root to object")

    out: dict[str, Any] = dict(state)
    by_sub = out.get("sent_versions_by_sub")
    if not isinstance(by_sub, dict):
        by_sub = {}
        out["sent_versions_by_sub"] = by_sub
        changes.append("state: initialized sent_versions_by_sub")

    legacy_sent_versions = out.pop("sent_versions", None)
    if isinstance(legacy_sent_versions, dict) and legacy_sent_versions:
        target = by_sub.get("__legacy__")
        if not isinstance(target, dict):
            target = {}
        for k, v in legacy_sent_versions.items():
            pid = str(k).strip()
            ver = str(v).strip()
            if pid and ver:
                target[pid] = ver
        if target:
            by_sub["__legacy__"] = target
            changes.append("state: migrated sent_versions -> sent_versions_by_sub.__legacy__")

    legacy_sent_ids = out.pop("sent_ids", None)
    if isinstance(legacy_sent_ids, list) and legacy_sent_ids:
        target = by_sub.get("__legacy__")
        if not isinstance(target, dict):
            target = {}
        for pid_raw in legacy_sent_ids:
            pid = str(pid_raw).strip()
            if pid and pid not in target:
                target[pid] = "v1"
        if target:
            by_sub["__legacy__"] = target
            changes.append("state: migrated sent_ids -> sent_versions_by_sub.__legacy__")

    legacy_ids_by_sub = out.pop("sent_ids_by_sub", None)
    if isinstance(legacy_ids_by_sub, dict) and legacy_ids_by_sub:
        for sub_key_raw, ids in legacy_ids_by_sub.items():
            sub_key = str(sub_key_raw).strip()
            if not sub_key:
                continue
            target = by_sub.get(sub_key)
            if not isinstance(target, dict):
                target = {}
            if isinstance(ids, list):
                for pid_raw in ids:
                    pid = str(pid_raw).strip()
                    if pid and pid not in target:
                        target[pid] = "v1"
            by_sub[sub_key] = target
        changes.append("state: migrated sent_ids_by_sub -> sent_versions_by_sub")

    # Normalize sent_versions_by_sub shape.
    normalized_by_sub: dict[str, dict[str, str]] = {}
    for sub_key_raw, mapping in by_sub.items():
        sub_key = str(sub_key_raw).strip()
        if not sub_key or not isinstance(mapping, dict):
            continue
        target: dict[str, str] = {}
        for pid_raw, version_raw in mapping.items():
            pid = str(pid_raw).strip()
            version = str(version_raw).strip()
            if pid and version:
                target[pid] = version
        if target:
            normalized_by_sub[sub_key] = target
    if normalized_by_sub != by_sub:
        changes.append("state: normalized sent_versions_by_sub")
    out["sent_versions_by_sub"] = normalized_by_sub

    if "last_run_at" not in out:
        out["last_run_at"] = None
        changes.append("state: defaulted last_run_at=null")
    elif out.get("last_run_at") is not None and not isinstance(out.get("last_run_at"), str):
        out["last_run_at"] = str(out.get("last_run_at"))
        changes.append("state: normalized last_run_at to string")
    if not isinstance(out.get("last_push_date_by_sub"), dict):
        out["last_push_date_by_sub"] = {}
        changes.append("state: defaulted last_push_date_by_sub={}")
    legacy_last_push_date = out.pop("last_push_date", None)
    if isinstance(legacy_last_push_date, str) and legacy_last_push_date.strip():
        date_text = legacy_last_push_date.strip()
        # Legacy singleton date is applied to every known subscription key in state.
        if not out["last_push_date_by_sub"]:
            for sub_key in out.get("sent_versions_by_sub", {}).keys():
                out["last_push_date_by_sub"][sub_key] = date_text
            if out["last_push_date_by_sub"]:
                changes.append("state: migrated legacy last_push_date -> last_push_date_by_sub")
    if "last_state_reset_at" not in out:
        out["last_state_reset_at"] = None
        changes.append("state: defaulted last_state_reset_at=null")
    elif out.get("last_state_reset_at") is not None and not isinstance(out.get("last_state_reset_at"), str):
        out["last_state_reset_at"] = str(out.get("last_state_reset_at"))
        changes.append("state: normalized last_state_reset_at to string")

    schema_version = out.get("schema_version")
    if schema_version != STATE_SCHEMA_VERSION:
        out["schema_version"] = STATE_SCHEMA_VERSION
        changes.append(f"state: schema_version -> {STATE_SCHEMA_VERSION}")

    return out, changes


def validate_subscriptions_config(config: dict[str, Any]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(config, dict):
        return ["config root must be an object"], warnings

    if bool(config.get("setup_required", False)):
        warnings.append("setup_required=true: waiting for user initialization")
        return errors, warnings

    subs = config.get("subscriptions")
    if not isinstance(subs, list) or not subs:
        errors.append("subscriptions must be a non-empty list")
        return errors, warnings

    for i, sub in enumerate(subs):
        if not isinstance(sub, dict):
            errors.append(f"subscriptions[{i}] must be an object")
            continue
        sid = str(sub.get("id", f"sub-{i + 1}")).strip()
        push_time = _normalize_push_time(sub.get("push_time"), fallback="")
        if not push_time:
            errors.append(f"{sid}: push_time must be HH:MM")
        tz = str(sub.get("timezone", "")).strip()
        if not tz:
            errors.append(f"{sid}: timezone is required")
        elif ZoneInfo is not None:
            try:
                ZoneInfo(tz)
            except Exception:
                errors.append(f"{sid}: invalid timezone '{tz}'")
        field_settings = sub.get("field_settings")
        if not isinstance(field_settings, list) or not field_settings:
            errors.append(f"{sid}: field_settings must be a non-empty list")
            continue
        for j, item in enumerate(field_settings):
            if not isinstance(item, dict):
                errors.append(f"{sid}.field_settings[{j}] must be an object")
                continue
            name = str(item.get("name", "")).strip()
            if not name:
                errors.append(f"{sid}.field_settings[{j}]: name is required")
            limit = item.get("limit")
            try:
                iv = int(limit)
                if iv < 5 or iv > 20:
                    warnings.append(f"{sid}.field_settings[{j}]: limit {iv} is outside recommended [5,20]")
            except Exception:
                errors.append(f"{sid}.field_settings[{j}]: limit must be int")

    return errors, warnings


def validate_state_config(state: dict[str, Any]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(state, dict):
        errors.append("state root must be an object")
        return errors, warnings
    by_sub = state.get("sent_versions_by_sub")
    if not isinstance(by_sub, dict):
        errors.append("state.sent_versions_by_sub must be an object")
    else:
        for sub_key, mapping in by_sub.items():
            if not isinstance(mapping, dict):
                errors.append(f"state.sent_versions_by_sub[{sub_key}] must be an object")
                continue
            for pid, version in mapping.items():
                if not str(pid).strip() or not str(version).strip():
                    errors.append(f"state.sent_versions_by_sub[{sub_key}] contains empty id/version")
    if not isinstance(state.get("last_push_date_by_sub", {}), dict):
        errors.append("state.last_push_date_by_sub must be an object")
    return errors, warnings


def backup_json_file(path: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_suffix(path.suffix + f".bak.{ts}")
    backup.write_text(path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
    return backup


def describe_changes(prefix: str, changes: list[str]) -> str:
    if not changes:
        return ""
    lines = [f"[MIGRATE] {prefix}:"]
    lines.extend([f"- {item}" for item in changes])
    return "\n".join(lines)


def dump_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)
