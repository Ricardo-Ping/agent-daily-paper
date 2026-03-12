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
from pathlib import Path
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


def _canonicalize_category(code: str) -> str:
    raw = str(code or "").strip()
    if not raw:
        return raw
    if "." not in raw:
        return raw.lower()
    prefix, suffix = raw.split(".", 1)
    prefix = prefix.lower()
    if re.fullmatch(r"[A-Za-z]{2}", suffix):
        suffix = suffix.upper()
    else:
        suffix = suffix.lower()
    return f"{prefix}.{suffix}"


def _load_taxonomy(path: str) -> tuple[dict[str, dict[str, Any]], set[str]]:
    p = Path(path)
    if not p.exists():
        return {}, set()
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}, set()
    rows = data.get("entries", []) if isinstance(data, dict) else []
    if not isinstance(rows, list):
        return {}, set()
    by_code: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = _canonicalize_category(str(row.get("code", "")))
        if not code:
            continue
        by_code[code] = row
    return by_code, set(by_code.keys())


def _validate_categories(categories: list[str], known_codes: set[str]) -> list[str]:
    normalized = [_canonicalize_category(c) for c in categories if str(c).strip()]
    deduped = list(dict.fromkeys(normalized))
    if not known_codes:
        return deduped
    return [c for c in deduped if c in known_codes]


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _is_english_term(term: str) -> bool:
    t = str(term or "").strip()
    return bool(t and re.fullmatch(r"[A-Za-z0-9\-\s]+", t))


def _taxonomy_suggest_categories(
    field_name: str,
    canonical_en: str,
    keywords: list[str],
    taxonomy_rows: dict[str, dict[str, Any]],
    preferred_groups: list[str] | None = None,
    top_n: int = 12,
) -> list[str]:
    if not taxonomy_rows:
        return []
    query_text = " ".join([field_name, canonical_en] + keywords)
    q_tokens = _tokenize(query_text)
    generic = {"system", "systems", "learning", "model", "models", "method", "methods", "paper", "papers"}
    q_tokens = {t for t in q_tokens if t not in generic and len(t) >= 2}
    if not q_tokens:
        return []
    preferred = set([g.strip().lower() for g in (preferred_groups or []) if g.strip()])

    ranked: list[tuple[float, str]] = []
    for code, row in taxonomy_rows.items():
        name = str(row.get("name", ""))
        group = str(row.get("group", ""))
        if preferred and group.lower() not in preferred:
            continue
        desc = str(row.get("description", ""))
        doc = f"{code} {name} {group} {desc}".lower()
        d_tokens = _tokenize(doc)
        if not d_tokens:
            continue

        overlap = len(q_tokens.intersection(d_tokens)) / max(1, len(q_tokens))
        score = overlap
        if code.lower() in query_text.lower():
            score += 1.0
        if name.lower() and name.lower() in query_text.lower():
            score += 0.8
        if score > 0.12:
            ranked.append((score, code))

    ranked.sort(key=lambda x: x[0], reverse=True)
    out = [c for _, c in ranked[: max(1, top_n)]]
    return list(dict.fromkeys(out))


def _expand_categories(
    field_name: str,
    categories: list[str],
    mode: str = "balanced",
) -> tuple[list[str], list[str]]:
    lowered = field_name.lower()
    cats = list(dict.fromkeys([c for c in categories if c]))
    primary = list(cats)

    mode = (mode or "balanced").strip().lower()
    if mode == "off":
        if not cats:
            cats = ["cs.AI"]
        if not primary:
            primary = list(cats)
        return cats, primary

    if ("推荐" in field_name or "recsys" in lowered or "recommend" in lowered):
        if mode == "conservative":
            cats = list(dict.fromkeys(cats + ["cs.IR", "cs.LG", "cs.SI"]))
            primary = list(dict.fromkeys(["cs.IR", "cs.LG", "cs.SI"]))
        elif mode == "broad":
            cats = list(dict.fromkeys(cats + ["cs.IR", "cs.LG", "cs.AI", "cs.CL", "cs.SI", "stat.ML"]))
            primary = list(dict.fromkeys(["cs.IR", "cs.LG", "cs.AI"]))
        else:
            cats = list(dict.fromkeys(cats + ["cs.IR", "cs.LG", "cs.SI", "cs.CL"]))
            primary = list(dict.fromkeys(["cs.IR", "cs.LG", "cs.SI"]))
    elif ("数据库" in field_name or "database" in lowered or "db" in lowered) and (
        "优化器" in field_name or "optimizer" in lowered
    ):
        if mode == "conservative":
            cats = list(dict.fromkeys(cats + ["cs.DB"]))
            primary = list(dict.fromkeys(["cs.DB"]))
        elif mode == "broad":
            cats = list(dict.fromkeys(cats + ["cs.DB", "cs.LG", "cs.AI", "cs.IR"]))
            primary = list(dict.fromkeys(["cs.DB", "cs.LG"]))
        else:
            cats = list(dict.fromkeys(cats + ["cs.DB", "cs.LG", "cs.AI"]))
            primary = list(dict.fromkeys(["cs.DB", "cs.LG"]))
    elif "时间序列" in field_name or "time series" in lowered:
        cats = list(dict.fromkeys(cats + ["cs.LG", "stat.ML", "stat.AP"]))
        primary = list(dict.fromkeys(["cs.LG", "stat.ML"]))

    if not cats:
        cats = ["cs.AI"]
        primary = ["cs.AI"]
    if not primary:
        primary = list(cats)
    return cats, primary


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
    keywords: list[str] = []

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

    if ("数据库" in field_name or "database" in lowered or "db" in lowered) and (
        "优化器" in field_name or "optimizer" in lowered
    ):
        canonical_en = "database query optimizer"
    elif "推荐" in field_name or "recsys" in lowered or "recommend" in lowered:
        canonical_en = "recommender systems"
    elif "视觉" in field_name or "vision" in lowered or lowered == "cv":
        canonical_en = "computer vision"
    elif "语言" in field_name or "nlp" in lowered or "llm" in lowered:
        canonical_en = "natural language processing"
    else:
        canonical_en = " ".join([k for k in keywords if k.isascii()][:3]) or field_name

    return {
        "canonical_en": canonical_en,
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
    category_expand_mode: str = "balanced",
    require_agent_categories: bool = False,
    taxonomy_rows: dict[str, dict[str, Any]] | None = None,
    known_codes: set[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    profile = agent_profile
    source = "agent" if profile else "heuristic"
    if profile is None and use_openai:
        profile = _openai_profile(field_name)
        source = "openai" if profile else "heuristic"
    if not profile:
        profile = _heuristic_profile(field_name)

    keywords = [str(x).strip() for x in profile.get("keywords", []) if _is_english_term(str(x).strip())]
    valid_codes = known_codes or set()
    categories_raw = [str(x).strip() for x in profile.get("categories", []) if str(x).strip()]
    categories = _validate_categories(categories_raw, valid_codes)
    agent_categories = list(categories)
    canonical_en = str(profile.get("canonical_en", field_name)).strip() or field_name

    if require_agent_categories and not categories:
        raise ValueError(
            f"Field '{field_name}' missing agent-provided categories. "
            "Please add categories in config/agent_field_profiles.json."
        )

    taxonomy_suggested = _taxonomy_suggest_categories(
        field_name=field_name,
        canonical_en=canonical_en,
        keywords=keywords,
        taxonomy_rows=taxonomy_rows or {},
        preferred_groups=[c.split(".", 1)[0] for c in agent_categories if "." in c],
        top_n=12,
    )
    taxonomy_suggested = _validate_categories(taxonomy_suggested, valid_codes)

    if not categories:
        categories = list(taxonomy_suggested)

    primary_categories = [str(x).strip() for x in profile.get("primary_categories", []) if str(x).strip()]
    primary_categories = _validate_categories(primary_categories, valid_codes)
    categories, auto_primary = _expand_categories(field_name, categories, mode=category_expand_mode)
    if not primary_categories:
        primary_categories = list(dict.fromkeys(taxonomy_suggested + agent_categories + auto_primary))
    primary_categories = _validate_categories(primary_categories, valid_codes)
    if not primary_categories:
        primary_categories = list(categories)
    # Keep primary categories as a subset of categories.
    categories = list(dict.fromkeys(categories + primary_categories))
    title_keywords = [str(x).strip() for x in profile.get("title_keywords", []) if _is_english_term(str(x).strip())]
    venues = [str(x).strip() for x in profile.get("venues", []) if str(x).strip()]

    # Run digest uses field name + keywords for fuzzy retrieval.
    setting = {
        "name": canonical_en,
        "limit": limit,
        "categories": categories,
        "primary_categories": primary_categories,
        "keywords": list(dict.fromkeys(keywords))[:16],
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
        "categories": categories,
        "primary_categories": primary_categories,
        "keywords": list(dict.fromkeys(keywords))[:16],
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
    parser.add_argument("--embedding-model", default="BAAI/bge-m3")
    parser.add_argument("--embedding-threshold", type=float, default=0.58)
    parser.add_argument("--embedding-top-k", type=int, default=120)
    parser.add_argument("--rerank-model", default="BAAI/bge-reranker-v2-m3")
    parser.add_argument("--rerank-top-k", type=int, default=40)
    parser.add_argument(
        "--insight-mode",
        default="pdf",
        choices=["pdf", "abstract"],
        help="Paper insight source. Default: pdf",
    )
    parser.add_argument(
        "--insight-lang",
        default="zh",
        choices=["zh", "en"],
        help="Paper insight output language. Default: zh",
    )
    parser.add_argument("--insight-min-chars", type=int, default=300)
    parser.add_argument("--insight-embed-model", default="BAAI/bge-m3")
    parser.add_argument("--insight-paragraph-min-chars", type=int, default=500)
    parser.add_argument("--insight-pdf-max-pages", type=int, default=20)
    parser.add_argument("--insight-pdf-timeout-sec", type=int, default=35)
    parser.add_argument(
        "--category-expand-mode",
        default="balanced",
        choices=["off", "conservative", "balanced", "broad"],
        help="How to expand categories beyond provided profile categories",
    )
    parser.add_argument(
        "--agent-categories-only",
        action="store_true",
        help="Require agent profile to provide categories; do not fallback to heuristic categories",
    )
    parser.add_argument("--output", default="", help="Optional output path for subscriptions json")
    parser.add_argument(
        "--profiles-json",
        default="config/agent_field_profiles.json",
        help="Agent profile JSON path. Default: config/agent_field_profiles.json",
    )
    parser.add_argument(
        "--taxonomy-json",
        default="data/arxiv_taxonomy.json",
        help="Local arXiv taxonomy JSON path. Default: data/arxiv_taxonomy.json",
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
    taxonomy_rows, known_codes = _load_taxonomy(args.taxonomy_json)

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
            category_expand_mode=args.category_expand_mode,
            require_agent_categories=args.agent_categories_only,
            taxonomy_rows=taxonomy_rows,
            known_codes=known_codes,
        )
        field_settings.append(setting)
        traces.append(trace)
        field_profiles.append(
            {
                "field": trace.get("field", f),
                "canonical_en": trace.get("canonical_en", setting.get("name", f)),
                "keywords": trace.get("keywords", setting.get("keywords", [])),
                "venues": trace.get("venues", highlight.get("venues", [])),
                "categories": trace.get("categories", setting.get("categories", [])),
                "primary_categories": trace.get("primary_categories", setting.get("primary_categories", [])),
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
                "query_strategy": "category_keyword_union",
                "require_primary_category": True,
                "history_scope": "subscription",
                "category_expand_mode": args.category_expand_mode,
                "embedding_filter": {
                    "enabled": True,
                    "model": args.embedding_model,
                    "threshold": args.embedding_threshold,
                    "top_k": args.embedding_top_k,
                },
                "agent_rerank": {
                    "enabled": True,
                    "model": args.rerank_model,
                    "top_k": args.rerank_top_k,
                },
                "insight_mode": args.insight_mode,
                "insight_lang": args.insight_lang,
                "insight_min_chars": args.insight_min_chars,
                "insight_embed_model": args.insight_embed_model,
                "insight_paragraph_min_chars": args.insight_paragraph_min_chars,
                "insight_pdf_max_pages": args.insight_pdf_max_pages,
                "insight_pdf_timeout_sec": args.insight_pdf_timeout_sec,
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
            "taxonomy_loaded": bool(taxonomy_rows),
            "taxonomy_entry_count": len(taxonomy_rows),
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
