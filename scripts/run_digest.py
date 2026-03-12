#!/usr/bin/env python3
"""Daily arXiv digest runner.

Core features:
- Multi-field subscription
- Per-field independent limit (5-20)
- Importance ranking
- Optional grouping by field (only when multiple fields)
- Bilingual output (EN + ZH)
- NEW / UPDATED status via arXiv version tracking
- Highlight rules (title keywords / authors / venues)
- Translation providers: OpenAI API or offline Argos Translate
- Optional empty-result fallback window
- Optional full markdown emission in stdout JSON
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
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
ARXIV_NS = {"arxiv": "http://arxiv.org/schemas/atom"}


FIELD_TO_CATEGORIES = {
    "machine learning": ["cs.LG", "stat.ML"],
    "llm": ["cs.CL", "cs.LG"],
    "nlp": ["cs.CL"],
    "computer vision": ["cs.CV"],
    "reinforcement learning": ["cs.LG", "cs.AI"],
    "robotics": ["cs.RO"],
    "ai safety": ["cs.AI"],
    "multimodal": ["cs.CV", "cs.CL", "cs.AI"],
    "图像": ["cs.CV"],
    "计算机视觉": ["cs.CV"],
    "自然语言处理": ["cs.CL"],
    "大模型": ["cs.CL", "cs.LG"],
    "机器学习": ["cs.LG", "stat.ML"],
    "强化学习": ["cs.LG", "cs.AI"],
    "机器人": ["cs.RO"],
    "推荐系统": ["cs.IR", "cs.LG"],
    "数据库": ["cs.DB"],
}


DEFAULT_VENUES = [
    "AAAI", "ACL", "COLING", "CVPR", "ECCV", "EMNLP", "ICCV", "ICLR", "ICML",
    "IJCAI", "KDD", "NAACL", "NeurIPS", "SIGIR", "SIGMOD", "VLDB", "WWW",
]


@dataclass
class FieldSetting:
    name: str
    limit: int
    categories: list[str] = field(default_factory=list)
    primary_categories: list[str] = field(default_factory=list)
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
    primary_category: str
    published: datetime
    updated: datetime
    url: str
    source_field: str
    score: float = 0.0
    embedding_score: float = 0.0
    rerank_score: float = 0.0
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
    # Heuristics for sub-fields that are not exact dictionary keys.
    if "数据库" in field_name or "database" in lowered:
        return ["cs.DB"]
    if "优化器" in field_name or "optimizer" in lowered:
        return ["cs.DB"]
    if "推荐" in field_name or "recsys" in lowered or "recommend" in lowered:
        return ["cs.IR", "cs.LG"]
    if "查询" in field_name or "query" in lowered:
        return ["cs.DB"]
    categories = re.findall(r"\b[a-z]{2,}\.[A-Z]{2}\b", field_name)
    return categories or ["cs.AI"]


def infer_terms_from_field(field_name: str) -> list[str]:
    lowered = field_name.strip().lower()
    terms: list[str] = []
    if "数据库" in field_name or "database" in lowered or "db" in lowered:
        terms.extend(["database", "query"])
    if "优化器" in field_name or "optimizer" in lowered:
        terms.extend(["optimizer", "query optimizer", "cost model", "execution plan", "join order"])
    if "查询" in field_name or "query" in lowered:
        terms.extend(["query", "query optimization", "cardinality estimation"])
    if "推荐" in field_name or "recsys" in lowered or "recommend" in lowered:
        terms.extend(["recommendation", "recommender", "ranking"])
    return list(dict.fromkeys(terms))


def expand_keywords_for_query(keywords: list[str]) -> list[str]:
    out: list[str] = []
    for kw in keywords:
        k = kw.strip()
        if not k:
            continue
        out.append(k)
        # Split phrase keywords to improve recall.
        if " " in k:
            parts = [p.strip() for p in k.split() if len(p.strip()) >= 4]
            out.extend(parts)
    return list(dict.fromkeys(out))


def _is_english_term(term: str) -> bool:
    t = (term or "").strip()
    if not t:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9\-\s]+", t))


def _expand_english_variants(keywords: list[str]) -> list[str]:
    out: list[str] = []
    for kw in keywords:
        k = kw.strip().lower()
        if not k:
            continue
        out.append(k)
        if "recommendation" in k:
            out.extend(["recommender", "recommender system", "recommend"])
        if "recommender" in k:
            out.extend(["recommendation", "recommend", "recsys"])
        if "optimizer" in k:
            out.extend(["optimization", "query optimizer", "cost model", "execution plan"])
        if "retrieval" in k:
            out.extend(["rank", "ranking", "information retrieval"])
    return list(dict.fromkeys(out))


def english_query_terms(field_name: str, keywords: list[str], max_terms: int = 8) -> list[str]:
    seeds = [field_name] + keywords
    seeds = [s for s in seeds if _is_english_term(s)]
    expanded = _expand_english_variants(seeds)
    expanded = expand_keywords_for_query(expanded)
    # Keep informative terms first (longer phrases first).
    expanded.sort(key=lambda x: (-len(x), x))
    return expanded[:max(1, max_terms)]


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
                    categories=[str(x).strip() for x in item.get("categories", []) if str(x).strip()],
                    primary_categories=[str(x).strip() for x in item.get("primary_categories", []) if str(x).strip()],
                    keywords=[str(x).strip() for x in item.get("keywords", []) if str(x).strip()],
                    exclude_keywords=[str(x).strip() for x in item.get("exclude_keywords", []) if str(x).strip()],
                )
            )
        if out:
            return out

    fields = [str(x).strip() for x in sub.get("fields", []) if str(x).strip()]
    limit = clamp_limit(sub.get("daily_count", 10))
    return [FieldSetting(name=f, limit=limit) for f in fields]


def build_search_query(categories: list[str], keywords: list[str], strict: bool = False) -> str:
    cat_query = " OR ".join(f"cat:{c}" for c in sorted(set(categories)))
    if not keywords and cat_query:
        return f"({cat_query})"
    if not keywords and not cat_query:
        return "all:*"

    kw_clauses = []
    for kw in keywords:
        safe = kw.replace('"', "")
        kw_clauses.append(f'ti:"{safe}"')
        kw_clauses.append(f'abs:"{safe}"')
    kw_query = f"({' OR '.join(kw_clauses)})"
    if not cat_query:
        return kw_query
    connector = "AND" if strict else "OR"
    return f"({cat_query}) {connector} {kw_query}"


def http_get(url: str, params: dict[str, Any], retries: int = 2) -> str:
    full_url = f"{url}?{urlencode(params)}"
    for attempt in range(retries + 1):
        try:
            req = Request(full_url, headers={"User-Agent": "agent-daily-paper/1.0"})
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
        primary_node = entry.find("arxiv:primary_category", ARXIV_NS)
        primary_category = primary_node.attrib.get("term", "") if primary_node is not None else (categories[0] if categories else "")
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
                primary_category=primary_category,
                published=published,
                updated=updated,
                url=f"https://arxiv.org/abs/{arxiv_id}",
                source_field=source_field,
            )
        )

    return papers


def fetch_arxiv_papers_union(
    categories: list[str],
    keyword_terms: list[str],
    source_field: str,
    max_results: int,
) -> list[Paper]:
    # Union recall: run one query per keyword and merge, instead of forcing term intersection.
    queries: list[str] = []
    if categories:
        queries.append(build_search_query(categories, [], strict=False))
    for kw in keyword_terms:
        q = build_search_query(categories, [kw], strict=True)
        queries.append(q)
    queries = list(dict.fromkeys([q for q in queries if q]))
    if not queries:
        return []

    per_query = max(20, int(max_results / max(1, len(queries))))
    merged: dict[str, Paper] = {}
    workers_env = os.getenv("ARXIV_UNION_WORKERS", "").strip()
    try:
        workers = int(workers_env) if workers_env else 0
    except Exception:
        workers = 0
    if workers <= 0:
        workers = min(8, len(queries))
    workers = max(1, workers)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_query = {
            executor.submit(fetch_arxiv_papers, q, source_field=source_field, max_results=per_query): q
            for q in queries
        }
        for future in as_completed(future_to_query):
            try:
                chunk = future.result()
            except Exception:
                # Best-effort union recall: skip failed query and keep others.
                continue
            for p in chunk:
                k = f"{p.arxiv_id}:{p.version}"
                if k not in merged:
                    merged[k] = p
    return list(merged.values())


def within_hours(paper: Paper, hours: int, now_utc: datetime) -> bool:
    return paper.updated >= now_utc - timedelta(hours=hours)


def contains_any(text: str, terms: list[str]) -> bool:
    lowered = text.lower()
    return any(term.lower() in lowered for term in terms)


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9\u4e00-\u9fff]+", text.lower()))


def _fuzzy_term_score(text: str, terms: list[str]) -> float:
    text_l = text.lower()
    text_tokens = _tokenize(text_l)
    score = 0.0
    for t in terms:
        tt = t.strip().lower()
        if not tt:
            continue
        if tt in text_l:
            score += 1.0
            continue
        t_tokens = _tokenize(tt)
        if not t_tokens:
            continue
        overlap = len(t_tokens.intersection(text_tokens)) / max(1, len(t_tokens))
        if overlap >= 0.5:
            score += 0.5
    return score


GENERIC_TERMS = {
    "paper", "method", "model", "models", "learning", "system", "systems",
    "approach", "framework", "analysis", "task", "tasks", "results", "data",
    "query", "queries",
}


def _keyword_signals(text: str, keywords: list[str]) -> tuple[int, int]:
    text_l = text.lower()
    phrase_hits = 0
    strong_hits = 0
    seen: set[str] = set()
    for kw in keywords:
        k = kw.strip().lower()
        if not k or k in seen:
            continue
        seen.add(k)
        if k not in text_l:
            continue
        if " " in k and len(k) >= 8:
            phrase_hits += 1
            strong_hits += 1
            continue
        if len(k) >= 6 and k not in GENERIC_TERMS:
            strong_hits += 1
    return phrase_hits, strong_hits


def score_paper(
    p: Paper,
    categories: list[str],
    keywords: list[str],
    field_name: str,
    now_utc: datetime,
) -> float:
    age_hours = max(0.0, (now_utc - p.updated).total_seconds() / 3600)
    recency = max(0.0, 30.0 - age_hours)
    cat_hits = len(set(categories).intersection(set(p.categories)))
    field_score = cat_hits * 25.0
    text = f"{p.title_en} {p.abstract_en}".lower()
    kw_hits = sum(1 for kw in keywords if kw.lower() in text)
    keyword_score = kw_hits * 12.0
    fuzzy_score = _fuzzy_term_score(text, [field_name] + keywords) * 8.0
    embedding_bonus = max(0.0, p.embedding_score) * 35.0
    return recency + field_score + keyword_score + fuzzy_score + embedding_bonus


def should_keep_for_specific_field(
    paper: Paper,
    field_name: str,
    keywords: list[str],
    categories: list[str],
    has_exact_mapping: bool,
) -> bool:
    text = f"{paper.title_en} {paper.abstract_en}".lower()
    cat_hit = bool(set(categories).intersection(set(paper.categories)))
    phrase_hits, strong_hits = _keyword_signals(text, keywords)
    field_l = field_name.lower()

    # For fine-grained DB optimizer fields, require both DB and optimizer semantics.
    if ("数据库" in field_name or "database" in field_l or "db" in field_l) and (
        "优化器" in field_name or "optimizer" in field_l
    ):
        db_terms = ["database", "sql", "relational", "query", "join", "index", "cardinality"]
        opt_terms = ["optimizer", "optimization", "cost model", "execution plan", "query plan"]
        has_db = any(t in text for t in db_terms)
        has_opt = any(t in text for t in opt_terms)
        return has_db and has_opt and (cat_hit or phrase_hits >= 1 or strong_hits >= 2)

    # Exact mapped broad fields still require basic lexical support.
    if has_exact_mapping:
        return cat_hit and (phrase_hits >= 1 or strong_hits >= 1)

    # If category does not match, demand strong textual evidence.
    if not cat_hit:
        return phrase_hits >= 2 or strong_hits >= 3

    fuzzy_hits = _fuzzy_term_score(text, [field_name] + keywords)
    return phrase_hits >= 1 or strong_hits >= 1 or fuzzy_hits >= 1.5


_EMBED_MODEL_CACHE: dict[str, Any] = {}
_RERANK_MODEL_CACHE: dict[str, Any] = {}


def _dot(a: list[float], b: list[float]) -> float:
    return float(sum(x * y for x, y in zip(a, b)))


def _norm(a: list[float]) -> float:
    return math.sqrt(sum(x * x for x in a))


def _cosine(a: list[float], b: list[float]) -> float:
    na, nb = _norm(a), _norm(b)
    if na <= 0 or nb <= 0:
        return 0.0
    return _dot(a, b) / (na * nb)


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _load_embed_model(model_name: str) -> Any | None:
    key = model_name.strip()
    if not key:
        return None
    if key in _EMBED_MODEL_CACHE:
        return _EMBED_MODEL_CACHE[key]
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except Exception:
        return None
    try:
        model = SentenceTransformer(key)
    except Exception:
        return None
    _EMBED_MODEL_CACHE[key] = model
    return model


def embedding_filter_papers(
    papers: list[Paper],
    canonical_en: str,
    keywords: list[str],
    venues: list[str],
    cfg: dict[str, Any],
) -> list[Paper]:
    if not papers:
        return papers
    enabled = bool(cfg.get("enabled", False))
    if not enabled:
        return papers

    model_name = str(cfg.get("model", "BAAI/bge-m3")).strip() or "BAAI/bge-m3"
    threshold = float(cfg.get("threshold", 0.58))
    top_k = int(cfg.get("top_k", max(80, len(papers))))
    model = _load_embed_model(model_name)
    if model is None:
        return papers

    profile_text = " | ".join(
        [
            f"field: {canonical_en}",
            f"keywords: {', '.join(keywords[:20])}",
            f"venues: {', '.join(venues[:12])}",
        ]
    )
    paper_texts = [f"{p.title_en}\n\n{p.abstract_en}" for p in papers]

    try:
        field_emb = model.encode(profile_text, normalize_embeddings=False)
        paper_embs = model.encode(paper_texts, normalize_embeddings=False)
    except Exception:
        return papers

    kept: list[Paper] = []
    for p, emb in zip(papers, paper_embs):
        sim = _cosine(list(field_emb), list(emb))
        p.embedding_score = sim
        if sim >= threshold:
            kept.append(p)

    kept.sort(key=lambda x: x.embedding_score, reverse=True)
    if top_k > 0:
        kept = kept[:top_k]
    return kept


def _load_rerank_model(model_name: str) -> Any | None:
    key = model_name.strip()
    if not key:
        return None
    if key in _RERANK_MODEL_CACHE:
        return _RERANK_MODEL_CACHE[key]
    try:
        from sentence_transformers import CrossEncoder  # type: ignore
    except Exception:
        return None
    try:
        model = CrossEncoder(key)
    except Exception:
        return None
    _RERANK_MODEL_CACHE[key] = model
    return model


def _local_rerank(
    papers: list[Paper],
    field_name: str,
    canonical_en: str,
    keywords: list[str],
    model_name: str,
) -> dict[str, float]:
    if not papers:
        return {}

    model = _load_rerank_model(model_name)
    if model is None:
        return {}

    query = " | ".join(
        [
            f"field: {field_name}",
            f"canonical: {canonical_en}",
            f"keywords: {', '.join(keywords[:20])}",
        ]
    )
    pairs = [(query, f"{p.title_en}\n\n{p.abstract_en[:2000]}") for p in papers]
    try:
        raw_scores = model.predict(pairs)
    except Exception:
        return {}

    out: dict[str, float] = {}
    for p, s in zip(papers, raw_scores):
        try:
            score = float(s)
        except Exception:
            score = 0.0
        out[p.arxiv_id] = _sigmoid(score)
    return out

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
                "content": [{"type": "input_text", "text": "Translate to Simplified Chinese and return JSON with title_zh and abstract_zh."}],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": json.dumps({"title_en": title_en, "abstract_en": abstract_en}, ensure_ascii=False)}],
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


def _split_sentences(text: str) -> list[str]:
    normalized = " ".join((text or "").strip().split())
    if not normalized:
        return []
    parts = re.split(r"(?<=[。！？!?\.])\s+", normalized)
    out: list[str] = []
    for p in parts:
        s = p.strip()
        if not s:
            continue
        out.append(s)
    return out


def _truncate_line(text: str, max_len: int = 220) -> str:
    s = (text or "").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 3].rstrip() + "..."


def _pick_sentence(
    sentences: list[str],
    cue_words: list[str],
    used: set[int],
) -> str:
    for i, s in enumerate(sentences):
        if i in used:
            continue
        low = s.lower()
        if any(c in low for c in cue_words):
            used.add(i)
            return s
    for i, s in enumerate(sentences):
        if i not in used:
            used.add(i)
            return s
    return ""


def summarize_paper_insight(paper: Paper) -> tuple[str, str, str]:
    zh_abstract = (paper.abstract_zh or "").strip()
    has_zh = bool(zh_abstract) and not zh_abstract.startswith("[待翻译]")

    if has_zh:
        sentences = _split_sentences(zh_abstract)
        used: set[int] = set()
        problem = _pick_sentence(sentences, ["问题", "挑战", "瓶颈", "困难", "关键", "仍然"], used)
        core = _pick_sentence(sentences, ["提出", "设计", "构建", "方法", "框架", "通过", "采用"], used)
        innovation = _pick_sentence(sentences, ["创新", "首次", "显著", "提升", "优于", "改进", "达到"], used)
        return (
            _truncate_line(problem or "摘要未明确问题定义。"),
            _truncate_line(core or "摘要未明确核心方法。"),
            _truncate_line(innovation or "摘要未明确创新点。"),
        )

    en_sentences = _split_sentences(paper.abstract_en or "")
    used_en: set[int] = set()
    en_problem = _pick_sentence(
        en_sentences,
        ["challenge", "problem", "difficult", "limitation", "bottleneck", "critical", "remain"],
        used_en,
    )
    en_core = _pick_sentence(
        en_sentences,
        ["we propose", "we present", "introduce", "design", "framework", "method", "approach"],
        used_en,
    )
    en_innovation = _pick_sentence(
        en_sentences,
        ["novel", "first", "significant", "improve", "state-of-the-art", "outperform", "achieve"],
        used_en,
    )
    return (
        _truncate_line(f"[基于英文摘要] {en_problem or 'Problem statement not explicit in abstract.'}"),
        _truncate_line(f"[基于英文摘要] {en_core or 'Core approach not explicit in abstract.'}"),
        _truncate_line(f"[基于英文摘要] {en_innovation or 'Innovation not explicit in abstract.'}"),
    )


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
    used_window_hours: int,
    used_fallback: bool,
) -> str:
    tz_name = sub.get("timezone", "Asia/Shanghai")
    local_now = to_local(generated_at, tz_name)
    field_names = list(by_field.keys())

    lines = [
        f"# arXiv Daily Digest ({local_now.strftime('%Y-%m-%d')})",
        "",
        f"- Fields: {', '.join(field_names)}",
        f"- Window: Last {used_window_hours} hours",
        f"- Candidates / Selected: {candidate_count} / {len(selected)}",
        "- Sorted by: importance score (field match + keyword match + recency)",
    ]
    if used_fallback:
        lines.append("- Fallback: Enabled (expanded window and optional keyword relaxation)")
    lines.append("")

    profile_rows: list[str] = []
    raw_profiles = sub.get("field_profiles", [])
    if isinstance(raw_profiles, list) and raw_profiles:
        for item in raw_profiles:
            if not isinstance(item, dict):
                continue
            field_cn = str(item.get("field", "")).strip()
            canonical_en = str(item.get("canonical_en", "")).strip() or field_cn
            keywords = [str(x).strip() for x in item.get("keywords", []) if str(x).strip()]
            venues = [str(x).strip() for x in item.get("venues", []) if str(x).strip()]
            categories = [str(x).strip() for x in item.get("categories", []) if str(x).strip()]
            primary_categories = [str(x).strip() for x in item.get("primary_categories", []) if str(x).strip()]
            if not field_cn and not canonical_en:
                continue
            profile_rows.append(f"- Field Profile: {field_cn or canonical_en}")
            profile_rows.append(f"  - Canonical EN: {canonical_en}")
            profile_rows.append(f"  - Categories: {', '.join(categories[:12]) if categories else '(none)'}")
            profile_rows.append(
                f"  - Primary Categories: {', '.join(primary_categories[:12]) if primary_categories else '(none)'}"
            )
            profile_rows.append(f"  - Keywords: {', '.join(keywords[:16]) if keywords else '(none)'}")
            profile_rows.append(f"  - Venues/Journals: {', '.join(venues[:12]) if venues else '(none)'}")
    else:
        fs_map: dict[str, dict[str, Any]] = {}
        for fs in sub.get("field_settings", []):
            if isinstance(fs, dict):
                fs_name = str(fs.get("name", "")).strip()
                if fs_name:
                    fs_map[fs_name] = fs
        highlight = sub.get("highlight", {}) if isinstance(sub.get("highlight"), dict) else {}
        venues = [str(x).strip() for x in highlight.get("venues", []) if str(x).strip()]
        for f in field_names:
            fs = fs_map.get(f, {})
            keywords = [str(x).strip() for x in fs.get("keywords", []) if str(x).strip()]
            categories = [str(x).strip() for x in fs.get("categories", []) if str(x).strip()]
            primary_categories = [str(x).strip() for x in fs.get("primary_categories", []) if str(x).strip()]
            canonical_en = f
            profile_rows.append(f"- Field Profile: {f}")
            profile_rows.append(f"  - Canonical EN: {canonical_en}")
            profile_rows.append(f"  - Categories: {', '.join(categories[:12]) if categories else '(none)'}")
            profile_rows.append(
                f"  - Primary Categories: {', '.join(primary_categories[:12]) if primary_categories else '(none)'}"
            )
            profile_rows.append(f"  - Keywords: {', '.join(keywords[:16]) if keywords else '(none)'}")
            profile_rows.append(f"  - Venues/Journals: {', '.join(venues[:12]) if venues else '(none)'}")

    if profile_rows:
        lines.append("## Field Profiles")
        lines.append("")
        lines.extend(profile_rows)
        lines.append("")

    def block(i: int, p: Paper) -> list[str]:
        authors = p.authors[:3]
        author_text = ", ".join(authors) + (" et al." if len(p.authors) > 3 else "")
        updated_local = to_local(p.updated, tz_name).strftime("%Y-%m-%d %H:%M")
        flags = [p.status] + p.highlight_tags
        problem, core, innovation = summarize_paper_insight(p)
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
            "### 论文解读（Agent）",
            f"- 解决问题: {problem}",
            f"- 核心思路: {core}",
            f"- 创新点: {innovation}",
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


def subscription_key(sub: dict[str, Any]) -> str:
    return str(sub.get("id") or sub.get("name") or "digest").strip()


def _parse_push_time(value: str) -> tuple[int, int]:
    raw = (value or "09:00").strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if not m:
        return 9, 0
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return 9, 0
    return hh, mm


def is_due_now(sub: dict[str, Any], now_utc: datetime, window_minutes: int) -> tuple[bool, str]:
    tz_name = sub.get("timezone", "Asia/Shanghai")
    now_local = to_local(now_utc, tz_name)
    hh, mm = _parse_push_time(str(sub.get("push_time", "09:00")))
    target_local = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    diff_min = (now_local - target_local).total_seconds() / 60.0
    due = 0 <= diff_min < max(1, int(window_minutes))
    return due, now_local.strftime("%Y-%m-%d")


def pick_best_by_id(candidates: list[Paper]) -> list[Paper]:
    best: dict[str, Paper] = {}
    for p in candidates:
        old = best.get(p.arxiv_id)
        if old is None or p.score > old.score:
            best[p.arxiv_id] = p
    return list(best.values())


def run_subscription(
    sub: dict[str, Any],
    state: dict[str, Any],
    output_dir: Path,
    dry_run: bool = False,
    ignore_history: bool = False,
) -> dict[str, Any]:
    now_utc = datetime.now(timezone.utc)
    time_window_hours = int(sub.get("time_window_hours", 24))
    fallback_when_empty = bool(sub.get("fallback_when_empty", True))
    fallback_time_window_hours = int(sub.get("fallback_time_window_hours", max(72, time_window_hours)))
    fallback_relax_keywords = bool(sub.get("fallback_relax_keywords", True))

    global_keywords = [str(x).strip() for x in sub.get("keywords", []) if str(x).strip()]
    global_excludes = [str(x).strip() for x in sub.get("exclude_keywords", []) if str(x).strip()]
    highlight = sub.get("highlight", {}) if isinstance(sub.get("highlight"), dict) else {}

    field_settings = parse_field_settings(sub)
    if not field_settings:
        raise ValueError("No fields configured. Add field_settings or fields.")
    raw_profiles = sub.get("field_profiles", [])
    field_profile_map: dict[str, dict[str, Any]] = {}
    if isinstance(raw_profiles, list):
        for item in raw_profiles:
            if not isinstance(item, dict):
                continue
            canonical = str(item.get("canonical_en", "")).strip()
            field_cn = str(item.get("field", "")).strip()
            if canonical:
                field_profile_map[canonical] = item
            if field_cn:
                field_profile_map.setdefault(field_cn, item)

    sub_key = subscription_key(sub)
    history_scope = str(sub.get("history_scope", "subscription")).strip().lower()

    sent_versions_global = state.get("sent_versions", {})
    if not isinstance(sent_versions_global, dict):
        sent_versions_global = {}
    sent_versions_by_sub = state.get("sent_versions_by_sub", {})
    if not isinstance(sent_versions_by_sub, dict):
        sent_versions_by_sub = {}
    sent_ids_by_sub = state.get("sent_ids_by_sub", {})
    if not isinstance(sent_ids_by_sub, dict):
        sent_ids_by_sub = {}

    if history_scope == "global":
        sent_versions_active = sent_versions_global
        legacy_sent_ids = set(state.get("sent_ids", []))
    else:
        active = sent_versions_by_sub.get(sub_key, {})
        if not isinstance(active, dict):
            active = {}
        sent_versions_active = active
        legacy_sent_ids = set(sent_ids_by_sub.get(sub_key, []))

    def collect(window_hours: int, relax_keywords: bool) -> tuple[list[Paper], dict[str, list[Paper]], int]:
        all_selected: list[Paper] = []
        by_field: dict[str, list[Paper]] = {f.name: [] for f in field_settings}
        total_candidates = 0

        query_strategy = str(sub.get("query_strategy", "category_keyword_union")).strip().lower()
        require_primary_category = bool(sub.get("require_primary_category", True))

        for fs in field_settings:
            cats_all = fs.categories or normalize_field_to_categories(fs.name)
            primary_cats = fs.primary_categories or cats_all
            # Use primary categories as the only retrieval/constraint categories.
            cats = list(primary_cats) if primary_cats else list(cats_all)
            lowered_name = fs.name.strip().lower()
            has_exact_mapping = lowered_name in FIELD_TO_CATEGORIES
            inferred_terms = infer_terms_from_field(fs.name)
            profile = field_profile_map.get(fs.name, {})
            profile_keywords = [str(x).strip() for x in profile.get("keywords", []) if str(x).strip()]
            profile_venues = [str(x).strip() for x in profile.get("venues", []) if str(x).strip()]
            canonical_en = str(profile.get("canonical_en", fs.name)).strip() or fs.name
            if relax_keywords:
                keywords: list[str] = [fs.name] + inferred_terms + profile_keywords
            else:
                keywords = list(
                    dict.fromkeys(global_keywords + fs.keywords + profile_keywords + [fs.name] + inferred_terms)
                )
            excludes = list(dict.fromkeys(global_excludes + fs.exclude_keywords))

            strict_query = bool(sub.get("strict_query", False))
            fetch_size = max(50, fs.limit * 8)
            query_terms_en = english_query_terms(canonical_en, keywords, max_terms=8)
            if query_strategy in {"keyword_union", "category_keyword_union"}:
                papers = fetch_arxiv_papers_union(
                    categories=cats,
                    keyword_terms=query_terms_en,
                    source_field=fs.name,
                    max_results=fetch_size,
                )
            else:
                query_keywords = [] if query_strategy == "category_first" else query_terms_en
                query = build_search_query(cats, query_keywords, strict=strict_query)
                papers = fetch_arxiv_papers(query, source_field=fs.name, max_results=fetch_size)
            candidates = [p for p in papers if within_hours(p, window_hours, now_utc)]

            if require_primary_category:
                candidates = [p for p in candidates if p.primary_category in primary_cats]

            if excludes:
                candidates = [p for p in candidates if not contains_any(f"{p.title_en} {p.abstract_en}", excludes)]

            candidates = embedding_filter_papers(
                candidates,
                canonical_en=canonical_en,
                keywords=keywords,
                venues=profile_venues,
                cfg=sub.get("embedding_filter", {}),
            )

            scored: list[Paper] = []
            for p in candidates:
                if not should_keep_for_specific_field(
                    p, fs.name, keywords, cats, has_exact_mapping=has_exact_mapping
                ):
                    continue
                prev_v = None if ignore_history else sent_versions_active.get(p.arxiv_id)
                if (not ignore_history) and prev_v is None and p.arxiv_id in legacy_sent_ids:
                    prev_v = "v1"

                if prev_v is None:
                    p.status = "NEW"
                elif prev_v != p.version:
                    p.status = f"UPDATED({prev_v}->{p.version})"
                else:
                    continue

                p.score = score_paper(p, cats, keywords, fs.name, now_utc)
                p.highlight_tags = build_highlight_tags(p, highlight)
                scored.append(p)

            rerank_cfg = sub.get("agent_rerank", {}) if isinstance(sub.get("agent_rerank"), dict) else {}
            if bool(rerank_cfg.get("enabled", False)) and scored:
                rerank_top_k = int(rerank_cfg.get("top_k", 40))
                rerank_model = str(rerank_cfg.get("model", "BAAI/bge-reranker-v2-m3"))
                scored.sort(key=lambda x: x.score, reverse=True)
                rerank_input = scored[: max(1, rerank_top_k)]
                rerank_map = _local_rerank(
                    rerank_input,
                    field_name=fs.name,
                    canonical_en=canonical_en,
                    keywords=keywords,
                    model_name=rerank_model,
                )
                for p in scored:
                    rr = rerank_map.get(p.arxiv_id, 0.0)
                    p.rerank_score = rr
                    p.score += rr * 45.0

            total_candidates += len(scored)
            scored.sort(key=lambda x: x.score, reverse=True)
            selected_field = pick_best_by_id(scored)[: fs.limit]
            by_field[fs.name] = selected_field
            all_selected.extend(selected_field)

        deduped = pick_best_by_id(all_selected)
        deduped.sort(key=lambda x: x.score, reverse=True)

        by_field_clean: dict[str, list[Paper]] = {f.name: [] for f in field_settings}
        for p in deduped:
            by_field_clean.setdefault(p.source_field, []).append(p)

        return deduped, by_field_clean, total_candidates

    deduped, by_field, total_candidates = collect(window_hours=time_window_hours, relax_keywords=False)
    used_window_hours = time_window_hours
    used_fallback = False
    target_total = sum(fs.limit for fs in field_settings)

    should_fallback = (
        fallback_when_empty
        and fallback_time_window_hours > time_window_hours
        and len(deduped) < target_total
    )
    if should_fallback:
        deduped, by_field, total_candidates = collect(
            window_hours=fallback_time_window_hours,
            relax_keywords=fallback_relax_keywords,
        )
        used_window_hours = fallback_time_window_hours
        used_fallback = True

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
        used_window_hours=used_window_hours,
        used_fallback=used_fallback,
    )

    date_label = now_utc.strftime("%Y-%m-%d")
    field_label = "_".join([f.name for f in field_settings])
    output_file = output_dir / f"{sanitize_filename(field_label)}_{date_label}.md"

    if not dry_run:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(markdown, encoding="utf-8")

        if not ignore_history:
            for p in deduped:
                sent_versions_active[p.arxiv_id] = p.version
            if history_scope == "global":
                state["sent_versions"] = sent_versions_active
                state["sent_ids"] = sorted(sent_versions_active.keys())[-5000:]
            else:
                sent_versions_by_sub[sub_key] = sent_versions_active
                state["sent_versions_by_sub"] = sent_versions_by_sub
                sent_ids_by_sub[sub_key] = sorted(sent_versions_active.keys())[-5000:]
                state["sent_ids_by_sub"] = sent_ids_by_sub
        state["last_run_at"] = now_utc.isoformat()

    return {
        "subscription": sub.get("name") or sub.get("id") or "digest",
        "output_file": str(output_file),
        "selected_count": len(deduped),
        "candidate_count": total_candidates,
        "translation_stats": translation_stats,
        "used_window_hours": used_window_hours,
        "used_fallback": used_fallback,
        "markdown": markdown,
    }


def main() -> int:
    # Avoid Windows GBK console crashes when emitting multilingual markdown.
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="Run arXiv daily digest.")
    parser.add_argument("--config", default="config/subscriptions.json")
    parser.add_argument("--state", default="data/state.json")
    parser.add_argument("--output-dir", default="output/daily")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--ignore-history", action="store_true", help="Ignore sent history for this run")
    parser.add_argument("--emit-markdown", action="store_true", help="Include full markdown in stdout JSON")
    parser.add_argument(
        "--only-due-now",
        action="store_true",
        help="Run only subscriptions whose local push_time is within due window",
    )
    parser.add_argument(
        "--due-window-minutes",
        type=int,
        default=15,
        help="Allowed minutes after push_time when --only-due-now is enabled",
    )
    args = parser.parse_args()

    config = load_json(Path(args.config), default={"subscriptions": []})
    state = load_json(Path(args.state), default={"sent_ids": [], "sent_versions": {}, "last_run_at": None})

    if bool(config.get("setup_required", False)):
        msg = config.get("setup_message") or (
            "Configuration is not initialized. "
            "Please collect user settings (fields, per-field limit, push_time, timezone) first."
        )
        print(msg)
        # For scheduled checks, treat as "skipped" instead of failure.
        if args.only_due_now:
            print(json.dumps({"dry_run": args.dry_run, "results": [], "skipped": "setup_required"}, ensure_ascii=False))
            return 0
        return 1

    subs = config.get("subscriptions", [])
    if not subs:
        print("No subscriptions found. Please edit config/subscriptions.json.")
        return 1

    results = []
    has_real_run = False
    last_push_date_by_sub = state.get("last_push_date_by_sub", {})
    if not isinstance(last_push_date_by_sub, dict):
        last_push_date_by_sub = {}

    now_utc = datetime.now(timezone.utc)
    for sub in subs:
        sub_key = subscription_key(sub)
        if args.only_due_now:
            due, local_date = is_due_now(sub, now_utc, args.due_window_minutes)
            if not due:
                results.append(
                    {
                        "subscription": sub_key,
                        "skipped": True,
                        "reason": "not_due_time",
                    }
                )
                continue
            if last_push_date_by_sub.get(sub_key) == local_date:
                results.append(
                    {
                        "subscription": sub_key,
                        "skipped": True,
                        "reason": "already_pushed_today",
                    }
                )
                continue
        try:
            res = run_subscription(
                sub,
                state,
                Path(args.output_dir),
                dry_run=args.dry_run,
                ignore_history=args.ignore_history,
            )
            results.append(res)
            has_real_run = True
            if not args.dry_run:
                _, local_date = is_due_now(sub, now_utc, args.due_window_minutes)
                last_push_date_by_sub[sub_key] = local_date
        except Exception as exc:
            print(f"[ERROR] Subscription failed ({sub.get('name', 'unknown')}): {exc}")

    if not args.dry_run and has_real_run:
        state["last_push_date_by_sub"] = last_push_date_by_sub
        save_json(Path(args.state), state)

    if not args.emit_markdown:
        for r in results:
            r.pop("markdown", None)

    print(json.dumps({"dry_run": args.dry_run, "results": results}, ensure_ascii=False, indent=2))
    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())

