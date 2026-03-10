#!/usr/bin/env python3
"""Prepare field settings from user free-form field names.

Usage:
  python scripts/prepare_fields.py --fields "数据库优化器, 推荐系统" --limit 20
"""

from __future__ import annotations

import argparse
import json
import os
import re
from typing import Any
from urllib.request import Request, urlopen


FALLBACK_KEYWORDS = {
    "database": ["database", "sql", "query", "relational"],
    "optimizer": ["optimizer", "query optimizer", "execution plan", "cost model", "cardinality estimation"],
    "recsys": ["recommendation", "recommender", "ranking", "retrieval"],
    "cv": ["computer vision", "image", "detection", "segmentation"],
    "nlp": ["natural language", "llm", "language model", "reasoning"],
}

CATEGORY_VENUES = {
    "cs.DB": ["SIGMOD", "VLDB", "ICDE", "PODS", "CIDR"],
    "cs.IR": ["SIGIR", "WSDM", "ECIR", "WWW", "RecSys"],
    "cs.CV": ["CVPR", "ICCV", "ECCV", "WACV"],
    "cs.CL": ["ACL", "EMNLP", "NAACL", "COLING"],
    "cs.LG": ["ICML", "NeurIPS", "ICLR", "AAAI"],
}


def _extract_json(text: str) -> dict[str, Any] | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None


def _openai_profile(field_name: str) -> dict[str, Any] | None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None

    body = {
        "model": os.getenv("OPENAI_FIELD_PROFILE_MODEL", "gpt-4.1-mini"),
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": (
                    "Return strict JSON with keys: canonical_en, categories, keywords, title_keywords, venues. "
                    "Use concise retrieval keywords and top-tier venues."
                )}],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": json.dumps({"field_name": field_name}, ensure_ascii=False)}],
            },
        ],
    }

    req = Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )

    try:
        with urlopen(req, timeout=45) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None

    out = []
    for item in payload.get("output", []):
        for c in item.get("content", []):
            if c.get("type") == "output_text":
                out.append(c.get("text", ""))

    obj = _extract_json("\n".join(out))
    if not obj:
        return None
    return obj


def _heuristic_profile(field_name: str) -> dict[str, Any]:
    lowered = field_name.lower()
    categories: list[str] = []
    keywords: list[str] = [field_name]

    if "数据库" in field_name or "database" in lowered or "db" in lowered:
        categories.append("cs.DB")
        keywords += FALLBACK_KEYWORDS["database"]
    if "优化器" in field_name or "optimizer" in lowered:
        categories.append("cs.DB")
        keywords += FALLBACK_KEYWORDS["optimizer"]
    if "推荐" in field_name or "recsys" in lowered or "recommend" in lowered:
        categories += ["cs.IR", "cs.LG"]
        keywords += FALLBACK_KEYWORDS["recsys"]
    if "视觉" in field_name or "vision" in lowered or "cv" == lowered:
        categories.append("cs.CV")
        keywords += FALLBACK_KEYWORDS["cv"]
    if "语言" in field_name or "nlp" in lowered or "llm" in lowered:
        categories += ["cs.CL", "cs.LG"]
        keywords += FALLBACK_KEYWORDS["nlp"]

    if not categories:
        categories = ["cs.AI"]

    venues: list[str] = []
    for c in categories:
        venues += CATEGORY_VENUES.get(c, [])

    return {
        "canonical_en": " ".join([k for k in keywords if k.isascii()][:3]) or field_name,
        "categories": sorted(set(categories)),
        "keywords": list(dict.fromkeys(keywords))[:12],
        "title_keywords": list(dict.fromkeys(keywords))[:8],
        "venues": list(dict.fromkeys(venues))[:8],
    }


def build_field_setting(
    field_name: str,
    limit: int,
    use_openai: bool,
    agent_profile: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    profile = agent_profile
    source = "agent" if profile else "heuristic"
    if profile is None and use_openai:
        profile = _openai_profile(field_name)
        source = "openai" if profile else "heuristic"
    if not profile:
        profile = _heuristic_profile(field_name)

    keywords = [str(x).strip() for x in profile.get("keywords", []) if str(x).strip()]
    title_keywords = [str(x).strip() for x in profile.get("title_keywords", []) if str(x).strip()]
    venues = [str(x).strip() for x in profile.get("venues", []) if str(x).strip()]
    canonical_en = str(profile.get("canonical_en", field_name)).strip() or field_name

    # Run digest uses field name + keywords for fuzzy retrieval.
    setting = {
        "name": canonical_en,
        "limit": limit,
        "keywords": list(dict.fromkeys(keywords + [field_name]))[:16],
        "exclude_keywords": [],
    }
    highlight = {
        "title_keywords": title_keywords[:10],
        "authors": [],
        "venues": venues[:8],
    }

    return setting, highlight, {
        "field": field_name,
        "canonical_en": canonical_en,
        "source": source,
        "keywords": list(dict.fromkeys(keywords + [field_name]))[:16],
        "venues": venues[:8],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare field settings for run_digest.py")
    parser.add_argument("--fields", required=True, help="Comma-separated field names")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--name", default="Auto Field Subscription", help="Subscription name")
    parser.add_argument("--id", default="auto-subscription", help="Subscription id")
    parser.add_argument("--push-time", default="09:00", help="Push time HH:MM")
    parser.add_argument("--timezone", default="Asia/Shanghai", help="Timezone")
    parser.add_argument("--time-window-hours", type=int, default=24)
    parser.add_argument("--output", default="", help="Optional output path for subscriptions json")
    parser.add_argument(
        "--profiles-json",
        default="config/agent_field_profiles.json",
        help="Agent profile JSON path. Default: config/agent_field_profiles.json",
    )
    parser.add_argument("--no-openai", action="store_true", help="Disable OpenAI generation")
    args = parser.parse_args()

    fields = [x.strip() for x in args.fields.split(",") if x.strip()]
    use_openai = (not args.no_openai) and bool(os.getenv("OPENAI_API_KEY"))
    agent_profiles: dict[str, Any] = {}
    if args.profiles_json and os.path.exists(args.profiles_json):
        with open(args.profiles_json, "r", encoding="utf-8-sig") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            agent_profiles = loaded
            # When agent profiles are available, they are used as primary source.
            use_openai = False

    field_settings = []
    merged_title_keywords: list[str] = []
    merged_venues: list[str] = []
    traces = []
    field_profiles = []

    for f in fields:
        setting, highlight, trace = build_field_setting(
            f,
            args.limit,
            use_openai=use_openai,
            agent_profile=agent_profiles.get(f),
        )
        field_settings.append(setting)
        traces.append(trace)
        field_profiles.append(
            {
                "field": trace.get("field", f),
                "canonical_en": trace.get("canonical_en", setting.get("name", f)),
                "keywords": trace.get("keywords", setting.get("keywords", [])),
                "venues": trace.get("venues", highlight.get("venues", [])),
                "source": trace.get("source", "heuristic"),
            }
        )
        merged_title_keywords += highlight.get("title_keywords", [])[:5]
        merged_venues += highlight.get("venues", [])[:6]

    result = {
        "subscriptions": [
            {
                "id": args.id,
                "name": args.name,
                "timezone": args.timezone,
                "push_time": args.push_time,
                "time_window_hours": args.time_window_hours,
                "field_settings": field_settings,
                "field_profiles": field_profiles,
                "highlight": {
                    "title_keywords": list(dict.fromkeys(merged_title_keywords))[:20],
                    "authors": [],
                    "venues": list(dict.fromkeys(merged_venues))[:10],
                },
            }
        ],
        "meta": {
            "openai_enabled": use_openai,
            "agent_profiles_enabled": bool(agent_profiles),
            "fields": traces,
        },
    }

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
