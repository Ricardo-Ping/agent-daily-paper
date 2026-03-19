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
import io
import json
import math
import os
import re
import threading
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
ARXIV_CODE_PATTERN = re.compile(r"\b[a-z]{2,}(?:\-[a-z]{2,})?\.[A-Za-z]{2,}\b")


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


def parse_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def maybe_reset_state_weekly(state: dict[str, Any], now_utc: datetime, interval_days: int = 7) -> bool:
    last_reset = parse_iso_datetime(state.get("last_state_reset_at"))
    if last_reset is not None and now_utc - last_reset < timedelta(days=interval_days):
        return False
    state["sent_versions_by_sub"] = {}
    # Legacy keys are deprecated and removed during reset.
    state.pop("sent_ids", None)
    state.pop("sent_versions", None)
    state.pop("sent_ids_by_sub", None)
    state["last_state_reset_at"] = now_utc.isoformat()
    return True


def clamp_limit(v: Any, min_v: int = 5, max_v: int = 20, fallback: int = 10) -> int:
    try:
        iv = int(v)
    except Exception:
        iv = fallback
    return max(min_v, min(max_v, iv))


def parse_arxiv_datetime(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def normalize_field_to_categories(field_name: str) -> list[str]:
    categories = [m.group(0) for m in ARXIV_CODE_PATTERN.finditer(field_name.lower())]
    normalized: list[str] = []
    for code in categories:
        if "." not in code:
            continue
        prefix, suffix = code.split(".", 1)
        normalized.append(f"{prefix.lower()}.{suffix.upper() if len(suffix) <= 3 else suffix.lower()}")
    return list(dict.fromkeys(normalized))


def infer_terms_from_field(field_name: str) -> list[str]:
    text = field_name.strip().lower()
    if not text:
        return []
    words = re.findall(r"[a-z][a-z0-9\-]{2,}", text)
    terms: list[str] = []
    terms.extend(words)
    for i in range(len(words) - 1):
        terms.append(f"{words[i]} {words[i + 1]}")
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
        if " " in k:
            parts = [p for p in k.split() if p]
            out.extend(parts)
        for token in re.findall(r"[a-z][a-z0-9\-]{2,}", k):
            if token.endswith("s") and len(token) > 4:
                out.append(token[:-1])
            elif len(token) > 3:
                out.append(f"{token}s")
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
) -> bool:
    text = f"{paper.title_en} {paper.abstract_en}".lower()
    cat_hit = bool(set(categories).intersection(set(paper.categories)))
    phrase_hits, strong_hits = _keyword_signals(text, keywords)
    fuzzy_hits = _fuzzy_term_score(text, [field_name] + keywords)
    if not categories:
        return phrase_hits >= 1 or strong_hits >= 1 or fuzzy_hits >= 1.2
    if not cat_hit:
        return phrase_hits >= 2 or strong_hits >= 2 or fuzzy_hits >= 2.0
    return phrase_hits >= 1 or strong_hits >= 1 or fuzzy_hits >= 1.2


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
    seed_texts: list[str] | None = None,
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
    seed_docs = [str(x).strip() for x in (seed_texts or []) if str(x).strip()]
    seed_max_docs = int(cfg.get("seed_max_docs", 20))
    if seed_max_docs > 0 and len(seed_docs) > seed_max_docs:
        seed_docs = seed_docs[:seed_max_docs]
    query_docs = [profile_text] + seed_docs

    try:
        query_embs = model.encode(query_docs, normalize_embeddings=False)
        paper_embs = model.encode(paper_texts, normalize_embeddings=False)
    except Exception:
        return papers

    if query_embs is None or len(query_embs) == 0:
        return papers
    query_vecs = [list(v) for v in query_embs]
    dim = len(query_vecs[0]) if query_vecs and query_vecs[0] else 0
    if dim <= 0:
        return papers
    profile_weight = float(cfg.get("profile_weight", 1.0))
    seed_weight = float(cfg.get("seed_weight", 0.7))
    field_emb = [0.0] * dim
    total_weight = 0.0
    for i, vec in enumerate(query_vecs):
        if len(vec) != dim:
            continue
        w = profile_weight if i == 0 else seed_weight
        for j in range(dim):
            field_emb[j] += vec[j] * w
        total_weight += w
    if total_weight <= 0:
        return papers
    field_emb = [x / total_weight for x in field_emb]

    kept: list[Paper] = []
    for p, emb in zip(papers, paper_embs):
        sim = _cosine(field_emb, list(emb))
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


def _openai_translate_model() -> str | None:
    model = os.getenv("OPENAI_TRANSLATE_MODEL", "").strip()
    return model or None


def _argos_translate_timeout_sec() -> float:
    raw = os.getenv("ARGOS_TRANSLATE_TIMEOUT_SEC", "").strip()
    if not raw:
        return 20.0
    try:
        v = float(raw)
    except Exception:
        return 20.0
    # <=0 means no timeout guard.
    return max(0.0, v)


def _openai_translate(title_en: str, abstract_en: str) -> tuple[str, str] | None:
    api_key = os.getenv("OPENAI_API_KEY")
    model = _openai_translate_model()
    if not api_key or not model:
        return None

    body = {
        "model": model,
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


def _argos_translate_impl(title_en: str, abstract_en: str) -> tuple[str, str] | None:
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


def _argos_translate(title_en: str, abstract_en: str) -> tuple[str, str] | None:
    timeout_sec = _argos_translate_timeout_sec()
    if timeout_sec <= 0:
        return _argos_translate_impl(title_en, abstract_en)

    holder: dict[str, tuple[str, str] | None] = {"value": None}

    def _worker() -> None:
        holder["value"] = _argos_translate_impl(title_en, abstract_en)

    # Daemon thread prevents a hung translation call from blocking process exit.
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)
    if t.is_alive():
        return None
    return holder["value"]


def _openai_translate_text(text_en: str) -> str | None:
    api_key = os.getenv("OPENAI_API_KEY")
    model = _openai_translate_model()
    if not api_key or not model:
        return None
    raw = (text_en or "").strip()
    if not raw:
        return None

    body = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": "Translate to Simplified Chinese. Return JSON: {\"zh\":\"...\"}"}],
            },
            {"role": "user", "content": [{"type": "input_text", "text": raw}]},
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
    zh = str(obj.get("zh", "")).strip()
    return zh or None


def _argos_translate_text(text_en: str) -> str | None:
    raw = (text_en or "").strip()
    if not raw:
        return None
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
        out = tr.translate(raw).strip()
        return out or None
    except Exception:
        return None


def select_translate_provider() -> str:
    provider = os.getenv("TRANSLATE_PROVIDER", "argos").strip().lower()
    if provider in {"openai", "argos", "none", "auto"}:
        return provider
    return "argos"


def translate_paper(paper: Paper) -> str:
    provider = select_translate_provider()

    translated: tuple[str, str] | None = None
    used = "none"
    if provider in ("argos", "auto"):
        translated = _argos_translate(paper.title_en, paper.abstract_en)
        if translated:
            used = "argos"

    if not translated and provider in ("openai", "auto"):
        translated = _openai_translate(paper.title_en, paper.abstract_en)
        if translated:
            used = "openai"

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
        if s:
            out.append(s)
    return out


def _truncate_line(text: str, max_len: int = 1400) -> str:
    s = (text or "").strip()
    if len(s) <= max_len:
        return s
    # Prefer sentence-safe trimming to avoid hard-cut artifacts like trailing "..."
    for token in ["。", "！", "？", ". ", "; ", "；"]:
        idx = s.rfind(token, 0, max_len)
        if idx >= int(max_len * 0.55):
            end = idx + (0 if token in {"。", "！", "？"} else len(token.strip()))
            return s[:end].strip()
    idx = s.rfind(" ", 0, max_len)
    if idx >= int(max_len * 0.7):
        return s[:idx].strip()
    return s[:max_len].strip()


def _clean_summary_text(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return s
    # Remove common inline section headers leaked from PDF extraction.
    s = re.split(
        r"\b\d+(?:\.\d+)*\s+(Introduction|Background|Related Work|Method|Methods|Experiment|Experiments|Conclusion|Conclusions)\b",
        s,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    s = re.sub(r"\[[0-9,\s]+\]", " ", s)
    s = re.sub(r"\(\s*[0-9,\s]+\s*\)", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" -;,:")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _visible_len(text: str) -> int:
    return len(re.sub(r"\s+", "", (text or "").strip()))


def _to_float_list(vec: Any) -> list[float]:
    if hasattr(vec, "tolist"):
        return [float(x) for x in vec.tolist()]
    return [float(x) for x in vec]


def _jaccard_tokens(a: str, b: str) -> float:
    ta = set(re.findall(r"[a-z0-9]+", (a or "").lower()))
    tb = set(re.findall(r"[a-z0-9]+", (b or "").lower()))
    if not ta or not tb:
        return 0.0
    inter = len(ta.intersection(tb))
    union = len(ta.union(tb))
    if union <= 0:
        return 0.0
    return inter / union


def _clean_zh_summary_output(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return raw
    s = raw.replace("•", " ").replace("·", " ").replace("▪", " ").replace("◦", " ").replace("*", " ")
    s = re.sub(r"\s+", " ", s).strip()
    parts = re.split(r"(?<=[。！？；;])\s*", s)
    kept: list[str] = []
    seen: list[str] = []
    for p in parts:
        cur = p.strip()
        if not cur:
            continue
        norm = re.sub(r"[，,。！？；;\s]", "", cur)
        if len(norm) < 10:
            continue
        if any(_jaccard_tokens(norm, old) > 0.92 for old in seen):
            continue
        seen.append(norm)
        kept.append(cur)
        if len(kept) >= 10:
            break
    out = " ".join(kept).strip() if kept else s
    out = re.sub(r"\s+", " ", out).strip()
    return out


def _strip_source_prefix(text: str) -> str:
    return re.sub(r"^\[.*?\]\s*", "", (text or "").strip())


def _normalize_reader_perspective_zh(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return s
    replacements = [
        ("我们的", "该研究的"),
        ("我们提出", "本文提出"),
        ("我们发现", "研究发现"),
        ("我们认为", "本文认为"),
        ("我们通过", "该研究通过"),
        ("我们采用", "该研究采用"),
        ("我们设计", "该研究设计"),
        ("我们构建", "该研究构建"),
        ("我们将", "该研究将"),
        ("我们在", "该研究在"),
        ("我们", "该研究"),
    ]
    for old, new in replacements:
        s = s.replace(old, new)
    s = s.replace("该研究改变该研究的观点", "该研究改变其观点")
    s = s.replace("该研究通过Rec Pilot", "本文通过Rec Pilot")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _build_insight_paragraph(problem: str, core: str, innovation: str, min_chars: int = 500) -> str:
    p = _clean_zh_summary_output(_strip_source_prefix(problem))
    c = _clean_zh_summary_output(_strip_source_prefix(core))
    i = _clean_zh_summary_output(_strip_source_prefix(innovation))

    # Free-form paragraph assembly without fixed scaffold phrases.
    blocks = [x for x in [p, c, i] if x]
    raw = " ".join(blocks).strip()
    raw = _clean_zh_summary_output(raw)
    sentences = [s.strip() for s in re.split(r"(?<=[。！？；;])\s*", raw) if s.strip()]

    kept: list[str] = []
    for s in sentences:
        norm = re.sub(r"[，,。！？；;\s]", "", s)
        if len(norm) < 12:
            continue
        if any(_jaccard_tokens(norm, re.sub(r"[，,。！？；;\s]", "", t)) > 0.88 for t in kept):
            continue
        kept.append(s)
        if len(kept) >= 18:
            break

    para = _clean_zh_summary_output(" ".join(kept) if kept else raw)
    if _visible_len(para) < max(280, int(min_chars)):
        para = _clean_zh_summary_output(f"{para} {raw}")
    para = _normalize_reader_perspective_zh(para)
    return _truncate_line(para, max_len=2600)


def _is_summary_candidate(sentence: str) -> bool:
    s = _clean_summary_text(sentence)
    if len(s) < 40:
        return False
    low = s.lower()
    if low.startswith(("figure ", "table ", "references", "appendix ")):
        return False
    if re.search(r"\b(arxiv|doi|http|www\.)\b", low):
        return False
    if "*" in s:
        return False
    return True


def _pick_sentence(sentences: list[str], cue_words: list[str], used: set[int]) -> str:
    for i, s in enumerate(sentences):
        if i in used:
            continue
        if not _is_summary_candidate(s):
            continue
        low = s.lower()
        if any(c in low for c in cue_words):
            used.add(i)
            return s
    for i, s in enumerate(sentences):
        if i not in used and _is_summary_candidate(s):
            used.add(i)
            return s
    return ""


def _pick_sentences(
    sentences: list[str],
    cue_words: list[str],
    used: set[int],
    preferred_count: int = 3,
) -> str:
    picked_idx: list[int] = []

    for i, s in enumerate(sentences):
        if i in used:
            continue
        if not _is_summary_candidate(s):
            continue
        low = s.lower()
        if any(c in low for c in cue_words):
            picked_idx.append(i)
            if len(picked_idx) >= max(1, preferred_count):
                break

    if not picked_idx:
        for i in range(len(sentences)):
            if i not in used and _is_summary_candidate(sentences[i]):
                picked_idx.append(i)
                break

    # Add adjacent sentences for context when possible.
    if picked_idx:
        base = picked_idx[0]
        j = base + 1
        while len(picked_idx) < max(1, preferred_count) and j < min(base + 6, len(sentences)):
            if j not in used and _is_summary_candidate(sentences[j]):
                picked_idx.append(j)
            j += 1

    for i in picked_idx:
        used.add(i)
    merged = " ".join(_clean_summary_text(sentences[i]) for i in picked_idx if 0 <= i < len(sentences))
    return merged.strip()


def _semantic_rank_sentences(
    sentences: list[str],
    query: str,
    model_name: str,
    prefer_terms: list[str] | None = None,
    penalize_terms: list[str] | None = None,
) -> list[tuple[float, str]]:
    clean_sentences = [_clean_summary_text(s) for s in sentences if _is_summary_candidate(s)]
    if not clean_sentences:
        return []
    model = _load_embed_model(model_name)
    if model is None:
        return []
    try:
        embs = model.encode([query] + clean_sentences, normalize_embeddings=False)
    except Exception:
        return []
    qv = _to_float_list(embs[0])
    ranked: list[tuple[float, str]] = []
    pref = [x.lower() for x in (prefer_terms or []) if x.strip()]
    pen = [x.lower() for x in (penalize_terms or []) if x.strip()]
    for sent, vec in zip(clean_sentences, embs[1:]):
        sv = _to_float_list(vec)
        score = _cosine(qv, sv)
        low = sent.lower()
        if pref:
            score += 0.04 * sum(1 for t in pref if t in low)
        if pen:
            score -= 0.05 * sum(1 for t in pen if t in low)
        ranked.append((score, sent))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return ranked


def _compose_semantic_paragraph(
    sentences: list[str],
    query: str,
    model_name: str,
    min_chars: int,
    max_sentences: int,
    prefer_terms: list[str] | None = None,
    penalize_terms: list[str] | None = None,
) -> str:
    ranked = _semantic_rank_sentences(
        sentences,
        query=query,
        model_name=model_name,
        prefer_terms=prefer_terms,
        penalize_terms=penalize_terms,
    )
    if not ranked:
        return ""

    selected: list[str] = []
    total = 0
    for _, sent in ranked:
        if any(_jaccard_tokens(sent, s) > 0.82 for s in selected):
            continue
        selected.append(sent)
        total += _visible_len(sent)
        if len(selected) >= max(2, max_sentences):
            break
        if total >= max(200, min_chars):
            break
    return " ".join(selected).strip()


def _extract_contribution_span(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    lower = raw.lower()
    patterns = [
        "our contributions",
        "main contributions",
        "contributions are summarized",
        "in summary, our contributions",
        "we make the following contributions",
    ]
    for p in patterns:
        idx = lower.find(p)
        if idx >= 0:
            return raw[idx: idx + 7000]
    return ""


def _expand_section_en(
    source_text: str,
    section: str,
    model_name: str,
    min_chars: int,
) -> str:
    sec = (section or "").strip().lower()
    base_source = source_text
    if sec == "innovation":
        contrib = _extract_contribution_span(source_text)
        if contrib:
            base_source = contrib
    sentences = _split_sentences(base_source)
    if not sentences:
        return ""
    if sec == "problem":
        return _compose_semantic_paragraph(
            sentences=sentences,
            query="Elaborate the concrete problem setting, practical pain points, and why prior work is insufficient.",
            model_name=model_name,
            min_chars=min_chars,
            max_sentences=6,
            prefer_terms=["problem", "challenge", "limitation", "insufficient", "costly", "burden"],
            penalize_terms=["we propose", "our method"],
        )
    if sec == "core":
        return _compose_semantic_paragraph(
            sentences=sentences,
            query="Elaborate the technical pipeline, key modules, and how components interact end-to-end.",
            model_name=model_name,
            min_chars=min_chars,
            max_sentences=6,
            prefer_terms=["framework", "module", "pipeline", "method", "architecture", "algorithm"],
            penalize_terms=["dataset", "benchmark", "accuracy", "metric"],
        )
    return _compose_semantic_paragraph(
        sentences=sentences,
        query="Elaborate the novelty and explicit contributions. Avoid experimental metrics and focus on what is newly proposed.",
        model_name=model_name,
        min_chars=min_chars,
        max_sentences=6,
        prefer_terms=["contribution", "novel", "first", "our contributions", "we contribute"],
        penalize_terms=["dataset", "benchmark", "accuracy", "f1", "auc", "evaluation", "results"],
    )


def _ensure_min_zh_chars(
    current_zh: str,
    section: str,
    source_text: str,
    model_name: str,
    min_chars: int,
) -> str:
    cur = _clean_zh_summary_output(current_zh)
    if _visible_len(cur) >= min_chars:
        return cur
    extra_en = _expand_section_en(
        source_text=source_text,
        section=section,
        model_name=model_name,
        min_chars=max(600, min_chars * 3),
    )
    if not extra_en:
        return cur
    extra_zh = _translate_text_to_zh(extra_en) or ""
    if not extra_zh:
        return cur
    merged = _clean_zh_summary_output(f"{cur} {extra_zh}")
    return merged


_PDF_TEXT_CACHE: dict[str, str] = {}


def _download_pdf_bytes(arxiv_id: str, timeout_sec: int = 35) -> bytes | None:
    url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
    try:
        req = Request(url, headers={"User-Agent": "agent-daily-paper/1.0"})
        with urlopen(req, timeout=max(5, timeout_sec)) as resp:
            return resp.read()
    except Exception:
        return None


def _extract_pdf_text(pdf_bytes: bytes, max_pages: int = 20) -> str | None:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        return None
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        out: list[str] = []
        for i, page in enumerate(reader.pages):
            if i >= max(1, int(max_pages)):
                break
            text = page.extract_text() or ""
            if text.strip():
                out.append(text)
        merged = "\n".join(out).strip()
        return merged if merged else None
    except Exception:
        return None


def _focus_pdf_text(raw: str) -> str:
    text = " ".join((raw or "").split())
    # Fix common PDF extraction artifact: split words like "de- pendencies".
    text = re.sub(r"([A-Za-z])-\s+([a-z])", r"\1\2", text)
    # Fix token sticking, e.g. "throughRecPilot" -> "through RecPilot".
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    if not text:
        return ""
    lower = text.lower()
    picks: list[str] = []

    idx_abs = lower.find("abstract")
    if idx_abs >= 0:
        picks.append(text[idx_abs: idx_abs + 2500])

    idx_intro = lower.find("introduction")
    if idx_intro >= 0:
        picks.append(text[idx_intro: idx_intro + 3000])

    idx_method = lower.find("method")
    if idx_method >= 0:
        picks.append(text[idx_method: idx_method + 3500])

    idx_concl = lower.find("conclusion")
    if idx_concl >= 0:
        left = max(0, idx_concl - 2000)
        picks.append(text[left: idx_concl + 3500])

    if not picks:
        picks.append(text[:12000])
    return " ".join(picks)[:20000]


def _load_pdf_focus_text(arxiv_id: str, max_pages: int, timeout_sec: int) -> str | None:
    key = f"{arxiv_id}:{max_pages}:{timeout_sec}"
    if key in _PDF_TEXT_CACHE:
        return _PDF_TEXT_CACHE[key]
    pdf_bytes = _download_pdf_bytes(arxiv_id, timeout_sec=timeout_sec)
    if not pdf_bytes:
        return None
    raw = _extract_pdf_text(pdf_bytes, max_pages=max_pages)
    if not raw:
        return None
    focus = _focus_pdf_text(raw)
    if not focus:
        return None
    _PDF_TEXT_CACHE[key] = focus
    return focus


def _summarize_from_en_text(
    en_text: str,
    source_prefix: str,
    embed_model_name: str = "BAAI/bge-m3",
    min_source_chars: int = 900,
) -> tuple[str, str, str]:
    source = (en_text or "").strip()
    sentences = _split_sentences(source)
    if not sentences:
        return (
            _truncate_line(f"{source_prefix} Problem statement not explicit in source text."),
            _truncate_line(f"{source_prefix} Core approach not explicit in source text."),
            _truncate_line(f"{source_prefix} Innovation not explicit in source text."),
        )

    contribution_span = _extract_contribution_span(source)
    contribution_sentences = _split_sentences(contribution_span) if contribution_span else []

    problem_en = _compose_semantic_paragraph(
        sentences=sentences,
        query=(
            "Summarize the concrete research problem, limitation, and real-world pain point. "
            "Focus on why existing methods are insufficient."
        ),
        model_name=embed_model_name,
        min_chars=min_source_chars,
        max_sentences=8,
        prefer_terms=["problem", "challenge", "limitation", "insufficient", "pain point", "costly", "burden"],
        penalize_terms=["we propose", "our method", "our framework"],
    )
    core_en = _compose_semantic_paragraph(
        sentences=sentences,
        query=(
            "Summarize the core technical approach, architecture, modules, and workflow of the proposed method "
            "without focusing on metrics."
        ),
        model_name=embed_model_name,
        min_chars=min_source_chars,
        max_sentences=8,
        prefer_terms=["propose", "framework", "architecture", "method", "module", "pipeline", "algorithm"],
        penalize_terms=["accuracy", "outperform", "benchmark", "dataset"],
    )
    innovation_pool = contribution_sentences or sentences
    innovation_en = _compose_semantic_paragraph(
        sentences=innovation_pool,
        query=(
            "Summarize the key contributions and novelty claims. "
            "Prioritize explicit contribution statements and novel design choices, not experimental results."
        ),
        model_name=embed_model_name,
        min_chars=max(700, int(min_source_chars * 0.8)),
        max_sentences=6,
        prefer_terms=["contribution", "contributions", "novel", "first", "we contribute", "our contributions"],
        penalize_terms=["dataset", "benchmark", "accuracy", "f1", "auc", "results demonstrate"],
    )

    # Fallback to extractive heuristic when semantic path yields empty segments.
    if not problem_en or not core_en or not innovation_en:
        used_en: set[int] = set()
        if not problem_en:
            problem_en = _pick_sentences(
                sentences,
                ["challenge", "problem", "difficult", "limitation", "bottleneck", "critical", "remain"],
                used_en,
                preferred_count=3,
            )
        if not core_en:
            core_en = _pick_sentences(
                sentences,
                ["we propose", "we present", "introduce", "design", "framework", "method", "approach", "architecture"],
                used_en,
                preferred_count=3,
            )
        if not innovation_en:
            innovation_en = _pick_sentences(
                contribution_sentences or sentences,
                ["our contributions", "novel", "first", "we contribute", "contribution"],
                used_en,
                preferred_count=2,
            )

    return (
        _truncate_line(_clean_summary_text(f"{source_prefix} {problem_en or 'Problem statement not explicit in source text.'}")),
        _truncate_line(_clean_summary_text(f"{source_prefix} {core_en or 'Core approach not explicit in source text.'}")),
        _truncate_line(_clean_summary_text(f"{source_prefix} {innovation_en or 'Innovation not explicit in source text.'}")),
    )


_INSIGHT_ZH_CACHE: dict[str, str] = {}


def _translate_text_to_zh(text_en: str) -> str | None:
    raw = (text_en or "").strip()
    if not raw:
        return None
    if raw in _INSIGHT_ZH_CACHE:
        return _INSIGHT_ZH_CACHE[raw]

    provider = select_translate_provider()
    translated: str | None = None
    if provider in ("argos", "auto"):
        translated = _argos_translate_text(raw)
    if not translated and provider in ("openai", "auto"):
        translated = _openai_translate_text(raw)

    if translated:
        _INSIGHT_ZH_CACHE[raw] = translated
    return translated


def summarize_paper_insight(
    paper: Paper,
    insight_mode: str = "pdf",
    insight_pdf_max_pages: int = 20,
    insight_pdf_timeout_sec: int = 35,
    insight_lang: str = "zh",
    insight_min_chars: int = 300,
    insight_embed_model: str = "BAAI/bge-m3",
) -> tuple[str, str, str]:
    mode = (insight_mode or "pdf").strip().lower()
    lang = (insight_lang or "zh").strip().lower()
    min_chars = max(120, int(insight_min_chars))
    source_min_chars = max(1200, min_chars * 4)
    if mode == "pdf":
        pdf_text = _load_pdf_focus_text(
            paper.arxiv_id,
            max_pages=insight_pdf_max_pages,
            timeout_sec=insight_pdf_timeout_sec,
        )
        if pdf_text:
            problem, core, innovation = _summarize_from_en_text(
                pdf_text,
                "[基于PDF全文-语义凝练]",
                embed_model_name=insight_embed_model,
                min_source_chars=source_min_chars,
            )
            if lang == "zh":
                src = "[基于PDF全文-语义凝练]"
                p = _translate_text_to_zh(re.sub(r"^\[.*?\]\s*", "", problem)) or re.sub(r"^\[.*?\]\s*", "", problem)
                c = _translate_text_to_zh(re.sub(r"^\[.*?\]\s*", "", core)) or re.sub(r"^\[.*?\]\s*", "", core)
                i = _translate_text_to_zh(re.sub(r"^\[.*?\]\s*", "", innovation)) or re.sub(r"^\[.*?\]\s*", "", innovation)
                p = _ensure_min_zh_chars(p, "problem", pdf_text, insight_embed_model, min_chars)
                c = _ensure_min_zh_chars(c, "core", pdf_text, insight_embed_model, min_chars)
                i = _ensure_min_zh_chars(i, "innovation", pdf_text, insight_embed_model, min_chars)
                return (
                    _truncate_line(f"{src} {p}"),
                    _truncate_line(f"{src} {c}"),
                    _truncate_line(f"{src} {i}"),
                )
            return problem, core, innovation

    zh_abstract = (paper.abstract_zh or "").strip()
    has_zh = bool(zh_abstract) and not zh_abstract.startswith("[待翻译]")
    if has_zh:
        sentences = _split_sentences(zh_abstract)
        used: set[int] = set()
        problem = _pick_sentences(sentences, ["问题", "挑战", "瓶颈", "困难", "关键", "仍然"], used, preferred_count=3)
        core = _pick_sentences(sentences, ["提出", "设计", "构建", "方法", "框架", "通过", "采用"], used, preferred_count=3)
        innovation = _pick_sentences(sentences, ["创新", "首次", "显著", "提升", "优于", "改进", "达到"], used, preferred_count=2)
        return (
            _truncate_line(_clean_summary_text(problem or "摘要未明确问题定义。")),
            _truncate_line(_clean_summary_text(core or "摘要未明确核心方法。")),
            _truncate_line(_clean_summary_text(innovation or "摘要未明确创新点。")),
        )

    problem, core, innovation = _summarize_from_en_text(
        paper.abstract_en or "",
        "[基于英文摘要-语义凝练]",
        embed_model_name=insight_embed_model,
        min_source_chars=max(600, source_min_chars - 200),
    )
    if lang == "zh":
        src = "[基于英文摘要-语义凝练]"
        p = _translate_text_to_zh(re.sub(r"^\[.*?\]\s*", "", problem)) or re.sub(r"^\[.*?\]\s*", "", problem)
        c = _translate_text_to_zh(re.sub(r"^\[.*?\]\s*", "", core)) or re.sub(r"^\[.*?\]\s*", "", core)
        i = _translate_text_to_zh(re.sub(r"^\[.*?\]\s*", "", innovation)) or re.sub(r"^\[.*?\]\s*", "", innovation)
        src_text = paper.abstract_en or ""
        p = _ensure_min_zh_chars(p, "problem", src_text, insight_embed_model, min_chars)
        c = _ensure_min_zh_chars(c, "core", src_text, insight_embed_model, min_chars)
        i = _ensure_min_zh_chars(i, "innovation", src_text, insight_embed_model, min_chars)
        return (
            _truncate_line(f"{src} {p}"),
            _truncate_line(f"{src} {c}"),
            _truncate_line(f"{src} {i}"),
        )
    return problem, core, innovation


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

    insight_mode = str(sub.get("insight_mode", "pdf")).strip().lower()
    insight_pdf_max_pages = int(sub.get("insight_pdf_max_pages", 20))
    insight_pdf_timeout_sec = int(sub.get("insight_pdf_timeout_sec", 35))
    insight_lang = str(sub.get("insight_lang", "zh")).strip().lower()
    insight_min_chars = int(sub.get("insight_min_chars", 300))
    insight_embed_model = str(sub.get("insight_embed_model", "BAAI/bge-m3")).strip() or "BAAI/bge-m3"
    insight_paragraph_min_chars = int(sub.get("insight_paragraph_min_chars", 500))

    def block(i: int, p: Paper) -> list[str]:
        authors = p.authors[:3]
        author_text = ", ".join(authors) + (" et al." if len(p.authors) > 3 else "")
        updated_local = to_local(p.updated, tz_name).strftime("%Y-%m-%d %H:%M")
        flags = [p.status] + p.highlight_tags
        problem, core, innovation = summarize_paper_insight(
            p,
            insight_mode=insight_mode,
            insight_pdf_max_pages=insight_pdf_max_pages,
            insight_pdf_timeout_sec=insight_pdf_timeout_sec,
            insight_lang=insight_lang,
            insight_min_chars=insight_min_chars,
            insight_embed_model=insight_embed_model,
        )
        insight_paragraph = _build_insight_paragraph(
            problem=problem,
            core=core,
            innovation=innovation,
            min_chars=insight_paragraph_min_chars,
        )
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
            insight_paragraph,
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
    # Dedup history is now subscription-scoped only.
    sent_versions_by_sub = state.get("sent_versions_by_sub", {})
    if not isinstance(sent_versions_by_sub, dict):
        sent_versions_by_sub = {}
    active = sent_versions_by_sub.get(sub_key, {})
    if not isinstance(active, dict):
        active = {}
    sent_versions_active = active

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
            inferred_terms = infer_terms_from_field(fs.name)
            profile = field_profile_map.get(fs.name, {})
            profile_keywords = [str(x).strip() for x in profile.get("keywords", []) if str(x).strip()]
            profile_seed_keywords = [str(x).strip() for x in profile.get("seed_keywords", []) if str(x).strip()]
            profile_venues = [str(x).strip() for x in profile.get("venues", []) if str(x).strip()]
            seed_papers = profile.get("seed_papers", []) if isinstance(profile.get("seed_papers"), list) else []
            seed_texts = [
                f"{str(p.get('title_en', '')).strip()}\n\n{str(p.get('abstract_en', '')).strip()}"
                for p in seed_papers
                if isinstance(p, dict)
            ]
            canonical_en = str(profile.get("canonical_en", fs.name)).strip() or fs.name
            if relax_keywords:
                keywords: list[str] = [fs.name] + inferred_terms + profile_seed_keywords + profile_keywords
            else:
                keywords = list(
                    dict.fromkeys(
                        global_keywords + fs.keywords + profile_seed_keywords + profile_keywords + [fs.name] + inferred_terms
                    )
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
                seed_texts=seed_texts,
            )

            scored: list[Paper] = []
            for p in candidates:
                if not should_keep_for_specific_field(
                    p, fs.name, keywords, cats
                ):
                    continue
                prev_v = None if ignore_history else sent_versions_active.get(p.arxiv_id)
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
            sent_versions_by_sub[sub_key] = sent_versions_active
            state["sent_versions_by_sub"] = sent_versions_by_sub
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
    state = load_json(
        Path(args.state),
        default={
            "sent_versions_by_sub": {},
            "last_run_at": None,
            "last_push_date_by_sub": {},
            "last_state_reset_at": None,
        },
    )

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

    now_utc = datetime.now(timezone.utc)
    state_reset = maybe_reset_state_weekly(state, now_utc, interval_days=7)

    results = []
    has_real_run = False
    last_push_date_by_sub = state.get("last_push_date_by_sub", {})
    if not isinstance(last_push_date_by_sub, dict):
        last_push_date_by_sub = {}
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

    if not args.dry_run and (has_real_run or state_reset):
        state["last_push_date_by_sub"] = last_push_date_by_sub
        save_json(Path(args.state), state)

    if not args.emit_markdown:
        for r in results:
            r.pop("markdown", None)

    print(json.dumps({"dry_run": args.dry_run, "results": results}, ensure_ascii=False, indent=2))
    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())

