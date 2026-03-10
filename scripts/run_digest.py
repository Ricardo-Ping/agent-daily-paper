#!/usr/bin/env python3
"""Daily arXiv digest runner.

Core features:
- User-selectable one or multiple fields
- Per-field independent limit (5-20)
- Importance ranking
- Optional grouping by field (only when multiple fields)
- Bilingual output (EN + ZH)
- NEW / UPDATED status by arXiv version tracking
- Highlight rules (title keywords / authors / venues)
- Translation providers: OpenAI API or offline Argos Translate
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore


ARXIV_API = "http://export.arxiv.org/api/query"
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


FIELD_TO_CATEGORIES = {
    "machine learning": ["cs.LG", "stat.ML"],
    "llm": ["cs.CL", "cs.LG"],
    "nlp": ["cs.CL"],
    "computer vision": ["cs.CV"],
    "reinforcement learning": ["cs.LG", "cs.AI"],
    "robotics": ["cs.RO"],
    "ai safety": ["cs.AI"],
    "multimodal": ["cs.CV", "cs.CL", "cs.AI"],
    "\u56fe\u50cf": ["cs.CV"],
    "\u8ba1\u7b97\u673a\u89c6\u89c9": ["cs.CV"],
    "\u81ea\u7136\u8bed\u8a00\u5904\u7406": ["cs.CL"],
    "\u5927\u6a21\u578b": ["cs.CL", "cs.LG"],
    "\u673a\u5668\u5b66\u4e60": ["cs.LG", "stat.ML"],
    "\u5f3a\u5316\u5b66\u4e60": ["cs.LG", "cs.AI"],
    "\u673a\u5668\u4eba": ["cs.RO"],
    "\u63a8\u8350\u7cfb\u7edf": ["cs.IR", "cs.LG"],
    "\u6570\u636e\u5e93": ["cs.DB"],
}


DEFAULT_VENUES = [
    "AAAI", "ACL", "COLING", "CVPR", "ECCV", "EMNLP", "ICCV", "ICLR", "ICML",
    "IJCAI", "KDD", "NAACL", "NeurIPS", "SIGIR", "SIGMOD", "VLDB", "WWW",
]


@dataclass
class FieldSetting:
    name: str
    limit: int
    keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)


@dataclass
class Paper:
    arxiv_id: str
    version: str
    title_en: str
    abstract_en: str
    authors: list[str]
    categories: list[str]
    published: datetime
    updated: datetime
    url: str
    source_field: str
    score: float = 0.0
    title_zh: str = ""
    abstract_zh: str = ""
    status: str = "NEW"
    highlight_tags: list[str] = field(default_factory=list)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def clamp_limit(v: Any, min_v: int = 5, max_v: int = 20, fallback: int = 10) -> int:
    try:
        iv = int(v)
    except Exception:
        iv = fallback
    return max(min_v, min(max_v, iv))


def parse_arxiv_datetime(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def normalize_field_to_categories(field_name: str) -> list[str]:
    lowered = field_name.strip().lower()
    if lowered in FIELD_TO_CATEGORIES:
        return FIELD_TO_CATEGORIES[lowered]
    categories = re.findall(r"\b[a-z]{2,}\.[A-Z]{2}\b", field_name)
    return categories or ["cs.AI"]


def parse_field_settings(sub: dict[str, Any]) -> list[FieldSetting]:
    if isinstance(sub.get("field_settings"), list) and sub["field_settings"]:
        out: list[FieldSetting] = []
        for item in sub["field_settings"]:
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            out.append(
                FieldSetting(
                    name=name,
                    limit=clamp_limit(item.get("limit", 10)),
                    keywords=[str(x).strip() for x in item.get("keywords", []) if str(x).strip()],
                    exclude_keywords=[str(x).strip() for x in item.get("exclude_keywords", []) if str(x).strip()],
                )
            )
        if out:
            return out

    # Backward compatible fallback from fields + daily_count
    fields = [str(x).strip() for x in sub.get("fields", []) if str(x).strip()]
    limit = clamp_limit(sub.get("daily_count", 10))
    return [FieldSetting(name=f, limit=limit) for f in fields]


def build_search_query(categories: list[str], keywords: list[str]) -> str:
    cat_query = " OR ".join(f"cat:{c}" for c in sorted(set(categories)))
    if not keywords:
        return f"({cat_query})"

    kw_clauses = []
    for kw in keywords:
        safe = kw.replace('"', "")
        kw_clauses.append(f'ti:"{safe}"')
        kw_clauses.append(f'abs:"{safe}"')
    return f"({cat_query}) AND ({' OR '.join(kw_clauses)})"


def http_get(url: str, params: dict[str, Any], retries: int = 2) -> str:
    full_url = f"{url}?{urlencode(params)}"
    for attempt in range(retries + 1):
        try:
            req = Request(full_url, headers={"User-Agent": "arxiv-daily-field-digest/1.0"})
            with urlopen(req, timeout=25) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception:
            if attempt >= retries:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


def fetch_arxiv_papers(search_query: str, source_field: str, max_results: int) -> list[Paper]:
    xml_text = http_get(
        ARXIV_API,
        {
            "search_query": search_query,
            "start": 0,
            "max_results": max_results,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        },
    )

    root = ET.fromstring(xml_text)
    papers: list[Paper] = []
    for entry in root.findall("atom:entry", ATOM_NS):
        raw_id = (entry.findtext("atom:id", default="", namespaces=ATOM_NS) or "").strip()
        full_id = raw_id.rsplit("/", 1)[-1]
        if not full_id:
            continue

        if "v" in full_id:
            base, v = full_id.rsplit("v", 1)
            if v.isdigit():
                arxiv_id, version = base, f"v{v}"
            else:
                arxiv_id, version = full_id, "v1"
        else:
            arxiv_id, version = full_id, "v1"

        title = " ".join((entry.findtext("atom:title", default="", namespaces=ATOM_NS) or "").split())
        abstract = " ".join((entry.findtext("atom:summary", default="", namespaces=ATOM_NS) or "").split())
        published = parse_arxiv_datetime(entry.findtext("atom:published", default="", namespaces=ATOM_NS))
        updated = parse_arxiv_datetime(entry.findtext("atom:updated", default="", namespaces=ATOM_NS))
        categories = [c.attrib.get("term", "") for c in entry.findall("atom:category", ATOM_NS)]
        authors = [
            (a.findtext("atom:name", default="", namespaces=ATOM_NS) or "").strip()
            for a in entry.findall("atom:author", ATOM_NS)
        ]

        papers.append(
            Paper(
                arxiv_id=arxiv_id,
                version=version,
                title_en=title,
                abstract_en=abstract,
                authors=[x for x in authors if x],
                categories=[x for x in categories if x],
                published=published,
                updated=updated,
                url=f"https://arxiv.org/abs/{arxiv_id}",
                source_field=source_field,
            )
        )

    return papers


def within_hours(paper: Paper, hours: int, now_utc: datetime) -> bool:
    return paper.updated >= now_utc - timedelta(hours=hours)


def contains_any(text: str, terms: list[str]) -> bool:
    lowered = text.lower()
    return any(term.lower() in lowered for term in terms)


def score_paper(p: Paper, categories: list[str], keywords: list[str], now_utc: datetime) -> float:
    age_hours = max(0.0, (now_utc - p.updated).total_seconds() / 3600)
    recency = max(0.0, 30.0 - age_hours)

    cat_hits = len(set(categories).intersection(set(p.categories)))
    field_score = cat_hits * 25.0

    text = f"{p.title_en} {p.abstract_en}".lower()
    kw_hits = sum(1 for kw in keywords if kw.lower() in text)
    keyword_score = kw_hits * 12.0

    return recency + field_score + keyword_score


def _extract_json_from_text(text: str) -> dict[str, Any] | None:
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


def _openai_translate(title_en: str, abstract_en: str) -> tuple[str, str] | None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None

    body = {
        "model": os.getenv("OPENAI_TRANSLATE_MODEL", "gpt-4.1-mini"),
        "input": [
            {
                "role": "system",
                "content": [{
                    "type": "input_text",
                    "text": "Translate to Simplified Chinese and return JSON with title_zh and abstract_zh.",
                }],
            },
            {
                "role": "user",
                "content": [{
                    "type": "input_text",
                    "text": json.dumps({"title_en": title_en, "abstract_en": abstract_en}, ensure_ascii=False),
                }],
            },
        ],
    }

    req = Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urlopen(req, timeout=45) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None

    chunks: list[str] = []
    for item in payload.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                chunks.append(content.get("text", ""))

    obj = _extract_json_from_text("\n".join(chunks).strip())
    if not obj:
        return None

    title_zh = str(obj.get("title_zh", "")).strip()
    abstract_zh = str(obj.get("abstract_zh", "")).strip()
    if not title_zh or not abstract_zh:
        return None
    return title_zh, abstract_zh


def _argos_translate(title_en: str, abstract_en: str) -> tuple[str, str] | None:
    try:
        from argostranslate import translate as argos_translate
    except Exception:
        return None

    try:
        langs = argos_translate.get_installed_languages()
        en = next((x for x in langs if x.code == "en"), None)
        zh = next((x for x in langs if x.code in ("zh", "zh_CN")), None)
        if not en or not zh:
            return None
        tr = en.get_translation(zh)
        return tr.translate(title_en).strip(), tr.translate(abstract_en).strip()
    except Exception:
        return None


def select_translate_provider() -> str:
    provider = os.getenv("TRANSLATE_PROVIDER", "auto").strip().lower()
    if provider in {"openai", "argos", "none"}:
        return provider
    return "auto"


def translate_paper(paper: Paper) -> str:
    provider = select_translate_provider()

    translated: tuple[str, str] | None = None
    used = "none"
    if provider in ("openai", "auto"):
        translated = _openai_translate(paper.title_en, paper.abstract_en)
        if translated:
            used = "openai"

    if not translated and provider in ("argos", "auto"):
        translated = _argos_translate(paper.title_en, paper.abstract_en)
        if translated:
            used = "argos"

    if translated:
        paper.title_zh, paper.abstract_zh = translated
        return used

    paper.title_zh = f"[待翻译] {paper.title_en}"
    paper.abstract_zh = f"[待翻译] {paper.abstract_en}"
    return "none"


def build_highlight_tags(paper: Paper, highlight: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    text = f"{paper.title_en} {paper.abstract_en}".lower()

    title_keywords = [str(x).strip() for x in highlight.get("title_keywords", []) if str(x).strip()]
    for kw in title_keywords:
        if kw.lower() in text:
            tags.append(f"KW:{kw}")

    author_rules = [str(x).strip() for x in highlight.get("authors", []) if str(x).strip()]
    author_text = " ".join(paper.authors).lower()
    for name in author_rules:
        if name.lower() in author_text:
            tags.append(f"AUTHOR:{name}")

    venues = [str(x).strip() for x in highlight.get("venues", []) if str(x).strip()]
    if not venues:
        venues = DEFAULT_VENUES
    for v in venues:
        if re.search(rf"\b{re.escape(v)}\b", f"{paper.title_en} {paper.abstract_en}", flags=re.IGNORECASE):
            tags.append(f"VENUE:{v}")

    return tags


def to_local(dt: datetime, tz_name: str) -> datetime:
    if ZoneInfo is None:
        return dt
    return dt.astimezone(ZoneInfo(tz_name))


def render_markdown(
    sub: dict[str, Any],
    selected: list[Paper],
    candidate_count: int,
    generated_at: datetime,
    by_field: dict[str, list[Paper]],
) -> str:
    tz_name = sub.get("timezone", "Asia/Shanghai")
    local_now = to_local(generated_at, tz_name)
    field_names = list(by_field.keys())

    lines = [
        f"# arXiv Daily Digest ({local_now.strftime('%Y-%m-%d')})",
        "",
        f"- Fields: {', '.join(field_names)}",
        f"- Window: Last {sub.get('time_window_hours', 24)} hours",
        f"- Candidates / Selected: {candidate_count} / {len(selected)}",
        "- Sorted by: importance score (field match + keyword match + recency)",
        "",
    ]

    def block(i: int, p: Paper) -> list[str]:
        authors = p.authors[:3]
        author_text = ", ".join(authors) + (" et al." if len(p.authors) > 3 else "")
        updated_local = to_local(p.updated, tz_name).strftime("%Y-%m-%d %H:%M")
        flags = [p.status] + p.highlight_tags
        return [
            f"## {i}. {p.title_en}",
            "",
            f"- Chinese Title: {p.title_zh}",
            f"- Flags: {', '.join(flags)}",
            f"- Authors: {author_text}",
            f"- Updated: {updated_local} ({tz_name})",
            f"- Categories: {', '.join(p.categories)}",
            f"- Score: {p.score:.2f}",
            f"- arXiv: {p.url}",
            "",
            "### English Abstract",
            p.abstract_en,
            "",
            "### 中文摘要",
            p.abstract_zh,
            "",
        ]

    multi_field = len(field_names) > 1
    if multi_field:
        idx = 1
        for f in field_names:
            items = by_field.get(f, [])
            if not items:
                continue
            lines.append(f"## Field: {f}")
            lines.append("")
            for p in items:
                lines.extend(block(idx, p))
                idx += 1
    else:
        for i, p in enumerate(selected, start=1):
            lines.extend(block(i, p))

    if not selected:
        lines.extend(["## No New Papers", "No papers matched this subscription in the current window.", ""])

    return "\n".join(lines).rstrip() + "\n"


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', '-', name)
    cleaned = re.sub(r"\s+", "_", cleaned).strip(" ._-")
    return cleaned or "digest"


def pick_best_by_id(candidates: list[Paper]) -> list[Paper]:
    best: dict[str, Paper] = {}
    for p in candidates:
        old = best.get(p.arxiv_id)
        if old is None or p.score > old.score:
            best[p.arxiv_id] = p
    return list(best.values())


def run_subscription(sub: dict[str, Any], state: dict[str, Any], output_dir: Path, dry_run: bool = False) -> dict[str, Any]:
    now_utc = datetime.now(timezone.utc)
    time_window_hours = int(sub.get("time_window_hours", 24))
    global_keywords = [str(x).strip() for x in sub.get("keywords", []) if str(x).strip()]
    global_excludes = [str(x).strip() for x in sub.get("exclude_keywords", []) if str(x).strip()]
    highlight = sub.get("highlight", {}) if isinstance(sub.get("highlight"), dict) else {}

    field_settings = parse_field_settings(sub)
    if not field_settings:
        raise ValueError("No fields configured. Add field_settings or fields.")

    sent_versions = state.get("sent_versions", {})
    if not isinstance(sent_versions, dict):
        sent_versions = {}

    legacy_sent_ids = set(state.get("sent_ids", []))

    all_selected: list[Paper] = []
    by_field: dict[str, list[Paper]] = {f.name: [] for f in field_settings}
    total_candidates = 0

    for fs in field_settings:
        cats = normalize_field_to_categories(fs.name)
        keywords = list(dict.fromkeys(global_keywords + fs.keywords))
        excludes = list(dict.fromkeys(global_excludes + fs.exclude_keywords))

        query = build_search_query(cats, keywords)
        # Pull extra candidates so ranking/filters still have room.
        fetch_size = max(50, fs.limit * 8)
        papers = fetch_arxiv_papers(query, source_field=fs.name, max_results=fetch_size)
        candidates = [p for p in papers if within_hours(p, time_window_hours, now_utc)]

        if excludes:
            candidates = [p for p in candidates if not contains_any(f"{p.title_en} {p.abstract_en}", excludes)]

        # Score and status (NEW/UPDATED). Skip unchanged already-sent papers.
        scored: list[Paper] = []
        for p in candidates:
            prev_v = sent_versions.get(p.arxiv_id)
            if prev_v is None and p.arxiv_id in legacy_sent_ids:
                prev_v = "v1"

            if prev_v is None:
                p.status = "NEW"
            elif prev_v != p.version:
                p.status = f"UPDATED({prev_v}->{p.version})"
            else:
                continue

            p.score = score_paper(p, cats, keywords, now_utc)
            p.highlight_tags = build_highlight_tags(p, highlight)
            scored.append(p)

        total_candidates += len(scored)

        scored.sort(key=lambda x: x.score, reverse=True)
        selected_field = pick_best_by_id(scored)[: fs.limit]
        by_field[fs.name] = selected_field
        all_selected.extend(selected_field)

    # Global de-duplication across fields: keep highest score assignment.
    deduped = pick_best_by_id(all_selected)
    deduped.sort(key=lambda x: x.score, reverse=True)

    by_field = {f.name: [] for f in field_settings}
    for p in deduped:
        by_field.setdefault(p.source_field, []).append(p)

    translation_stats = {"openai": 0, "argos": 0, "none": 0}
    for p in deduped:
        used = translate_paper(p)
        translation_stats[used] = translation_stats.get(used, 0) + 1

    markdown = render_markdown(
        sub=sub,
        selected=deduped,
        candidate_count=total_candidates,
        generated_at=now_utc,
        by_field=by_field,
    )

    date_label = now_utc.strftime("%Y-%m-%d")
    field_label = "_".join([f.name for f in field_settings])
    output_file = output_dir / f"{sanitize_filename(field_label)}_{date_label}.md"

    if not dry_run:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(markdown, encoding="utf-8")

        for p in deduped:
            sent_versions[p.arxiv_id] = p.version
        state["sent_versions"] = sent_versions
        state["sent_ids"] = sorted(sent_versions.keys())[-5000:]
        state["last_run_at"] = now_utc.isoformat()

    return {
        "subscription": sub.get("name") or sub.get("id") or "digest",
        "output_file": str(output_file),
        "selected_count": len(deduped),
        "candidate_count": total_candidates,
        "translation_stats": translation_stats,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run arXiv daily digest.")
    parser.add_argument("--config", default="config/subscriptions.json")
    parser.add_argument("--state", default="data/state.json")
    parser.add_argument("--output-dir", default="output/daily")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = load_json(Path(args.config), default={"subscriptions": []})
    state = load_json(Path(args.state), default={"sent_ids": [], "sent_versions": {}, "last_run_at": None})

    subs = config.get("subscriptions", [])
    if not subs:
        print("No subscriptions found. Please edit config/subscriptions.json.")
        return 1

    results = []
    for sub in subs:
        try:
            results.append(run_subscription(sub, state, Path(args.output_dir), dry_run=args.dry_run))
        except Exception as exc:
            print(f"[ERROR] Subscription failed ({sub.get('name', 'unknown')}): {exc}")

    if not args.dry_run:
        save_json(Path(args.state), state)

    print(json.dumps({"dry_run": args.dry_run, "results": results}, ensure_ascii=False, indent=2))
    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())