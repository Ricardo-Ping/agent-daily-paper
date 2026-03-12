#!/usr/bin/env python3
"""Prepare field settings from user free-form field names.

Usage:
  python scripts/prepare_fields.py --fields "数据库优化器, 推荐系统" --limit 20
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


ARXIV_API = "http://export.arxiv.org/api/query"
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
ARXIV_NS = {"arxiv": "http://arxiv.org/schemas/atom"}

STOPWORDS = {
    "a", "an", "the", "and", "or", "for", "of", "to", "in", "on", "with", "without", "by", "from", "into",
    "is", "are", "was", "were", "be", "been", "being", "that", "this", "these", "those", "it", "its", "as",
    "at", "we", "our", "they", "their", "than", "can", "could", "may", "might", "will", "would", "should",
    "paper", "method", "methods", "approach", "approaches", "model", "models", "system", "systems", "task",
    "tasks", "learning", "based", "using", "use", "via", "towards", "new", "study", "results", "show",
    "framework", "problem", "problems", "large", "language", "llm", "data",
}

_EMBED_MODEL_CACHE: dict[str, Any] = {}
_ARGOS_ZH_EN_TRANSLATOR: Any | None = None
_TAXONOMY_EMB_CACHE: dict[str, dict[str, Any]] = {}


def _slugify(text: str) -> str:
    s = str(text or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "field"


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


def _normalize_venue_token(token: str) -> str:
    t = token.strip()
    if not t:
        return t
    return t.upper() if re.fullmatch(r"[A-Za-z]{2,12}", t) else t


def _extract_global_venue_hints(raw: str) -> list[str]:
    if not raw.strip():
        return []
    lines = [ln.strip() for ln in re.split(r"[\n\r]+", raw) if ln.strip()]
    scoped = [
        ln for ln in lines
        if any(k in ln.lower() for k in ["来源", "source", "conference", "journal", "会议", "期刊"])
    ]
    source_text = "\n".join(scoped) if scoped else raw
    tokens = re.findall(r"\b[A-Za-z][A-Za-z0-9\-\+]{2,12}\b", source_text)
    picks: list[str] = []
    stop_tokens = {"SOURCE", "FIELD", "FIELDS", "FOR", "WITH", "FROM", "GPT", "MODEL", "MODELS", "SYSTEM", "SYSTEMS", "DATA", "DB", "AI"}
    for tk in tokens:
        v = _normalize_venue_token(tk)
        if re.fullmatch(r"[A-Za-z0-9\-]{3,12}", v):
            if v.upper() in stop_tokens:
                continue
            picks.append(v)
    return list(dict.fromkeys(picks))[:20]


def _parse_fields_input(raw: str) -> list[dict[str, str]]:
    text = str(raw or "").strip()
    if not text:
        return []

    chunks = re.split(r"[\n\r;；]+", text)
    out: list[dict[str, str]] = []

    for ch in chunks:
        line = re.sub(r"^[\-\*\d\.\)\(、\s]+", "", ch).strip()
        if not line:
            continue
        low = line.lower()
        if any(x in low for x in ["涵盖", "包含", "包括", "热门研究方向", "latest research", "research direction"]):
            continue
        if low.startswith("来源") or low.startswith("source"):
            continue
        if low.startswith("专注领域") or low.startswith("关注领域") or low.startswith("fields"):
            line = re.sub(r"^(专注领域|关注领域|fields?)\s*[:：]\s*", "", line, flags=re.IGNORECASE).strip()
            if not line:
                continue

        # Try "name - description" / "name: description"
        m = re.match(r"^(.{2,60}?)[\s]*(?:-|—|–|:|：)[\s]*(.{4,})$", line)
        if m:
            name = m.group(1).strip(" -—–:：")
            desc = m.group(2).strip()
            if name:
                out.append({"name": name, "context": desc})
            continue

        # Comma-style compact fields.
        split_items = [x.strip() for x in re.split(r"[，,、/]+", line) if x.strip()]
        if len(split_items) >= 2 and all(len(x) <= 40 for x in split_items):
            for it in split_items:
                out.append({"name": it, "context": ""})
            continue

        out.append({"name": line, "context": ""})

    dedup: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in out:
        name = item.get("name", "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        dedup.append({"name": name, "context": item.get("context", "").strip()})
    return dedup


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _is_english_term(term: str) -> bool:
    t = str(term or "").strip()
    return bool(t and re.fullmatch(r"[A-Za-z0-9\-\s]+", t))


def _get_argos_zh_en_translator() -> Any | None:
    global _ARGOS_ZH_EN_TRANSLATOR
    if _ARGOS_ZH_EN_TRANSLATOR is False:
        return None
    if _ARGOS_ZH_EN_TRANSLATOR is not None:
        return _ARGOS_ZH_EN_TRANSLATOR
    try:
        import argostranslate.translate as argos_translate  # type: ignore
    except Exception:
        _ARGOS_ZH_EN_TRANSLATOR = False
        return None
    try:
        langs = argos_translate.get_installed_languages()
    except Exception:
        _ARGOS_ZH_EN_TRANSLATOR = False
        return None
    zh_like = []
    en_like = []
    for lang in langs:
        code = str(getattr(lang, "code", "")).lower()
        if code.startswith("zh"):
            zh_like.append(lang)
        if code.startswith("en"):
            en_like.append(lang)
    for z in zh_like:
        for e in en_like:
            try:
                tr = z.get_translation(e)
                if tr is not None and hasattr(tr, "translate"):
                    _ARGOS_ZH_EN_TRANSLATOR = tr
                    return tr
            except Exception:
                continue
    _ARGOS_ZH_EN_TRANSLATOR = False
    return None


def _translate_field_to_english(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return raw
    tr = _get_argos_zh_en_translator()
    if tr is None:
        return raw
    try:
        out = str(tr.translate(raw)).strip()
    except Exception:
        return raw
    return re.sub(r"\s+", " ", out)


def _contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", str(text or "")))


def _canonicalize_en_phrase(text: str) -> str:
    t = re.sub(r"\s+", " ", str(text or "").strip())
    return t.lower()


def _ensure_english_canonical(
    field_name: str,
    profile_canonical: str,
    field_context: str,
    keywords: list[str],
) -> str:
    # Rule: arXiv query/classification must run on English canonical terms.
    candidates: list[str] = []
    raw = str(profile_canonical or "").strip()
    if raw:
        candidates.append(raw)
    field_raw = str(field_name or "").strip()
    if field_raw and field_raw != raw:
        candidates.append(field_raw)

    for c in candidates:
        if _is_english_term(c):
            return _canonicalize_en_phrase(c)

    for c in candidates:
        translated = _translate_field_to_english(c)
        if _is_english_term(translated):
            return _canonicalize_en_phrase(translated)

    english_terms = [k for k in keywords if _is_english_term(k)]
    english_terms += [x.strip() for x in re.findall(r"[A-Za-z][A-Za-z0-9\-\s]{2,40}", field_context) if x.strip()]
    english_terms = list(dict.fromkeys([_canonicalize_en_phrase(x) for x in english_terms if x.strip()]))
    if english_terms:
        return " ".join(english_terms[:4])

    if _contains_cjk(raw) or _contains_cjk(field_raw):
        raise ValueError(
            f"Field '{field_name}' is non-English and cannot be translated to English. "
            "Please install Argos zh->en model (`python scripts/install_argos_model.py --from-code zh --to-code en`) "
            "or set English `canonical_en` in config/agent_field_profiles.json."
        )
    raise ValueError(
        f"Field '{field_name}' has no usable English canonical term. "
        "Please provide English `canonical_en` in config/agent_field_profiles.json."
    )


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
    q_tokens = {t for t in q_tokens if t not in STOPWORDS and len(t) >= 2}
    semantic_only = not bool(q_tokens)
    preferred = set([g.strip().lower() for g in (preferred_groups or []) if g.strip()])

    ranked: list[tuple[float, str]] = []
    docs: list[tuple[str, str]] = []
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
        if semantic_only or score > 0.04:
            ranked.append((score, code))
            docs.append((code, f"{code} {name} {group} {desc}"))

    # Semantic refinement (multilingual): helps Chinese field descriptions map to taxonomy.
    if docs:
        model = _load_embed_model(os.getenv("SEED_EMBED_MODEL", "BAAI/bge-m3"))
        if model is not None:
            try:
                cache_key = f"{getattr(model, 'model_name', None) or os.getenv('SEED_EMBED_MODEL', 'BAAI/bge-m3')}::{len(taxonomy_rows)}::{len(docs)}"
                if cache_key not in _TAXONOMY_EMB_CACHE:
                    doc_embs = model.encode([d for _, d in docs], normalize_embeddings=False)
                    _TAXONOMY_EMB_CACHE[cache_key] = {
                        "codes": [c for c, _ in docs],
                        "embs": [list(v) for v in doc_embs],
                    }
                query_emb = list(model.encode(query_text, normalize_embeddings=False))
                emb_codes = _TAXONOMY_EMB_CACHE[cache_key]["codes"]
                emb_vecs = _TAXONOMY_EMB_CACHE[cache_key]["embs"]
                sem_map = {c: _cosine(query_emb, list(v)) for c, v in zip(emb_codes, emb_vecs)}
                ranked = [(s + max(0.0, sem_map.get(c, 0.0)) * 0.9, c) for s, c in ranked]
            except Exception:
                pass

    ranked.sort(key=lambda x: x[0], reverse=True)
    out = [c for _, c in ranked[: max(1, top_n)]]
    return list(dict.fromkeys(out))


def _expand_categories(
    field_name: str,
    categories: list[str],
    mode: str = "balanced",
) -> tuple[list[str], list[str]]:
    cats = list(dict.fromkeys([c for c in categories if c]))
    primary = list(cats)

    mode = (mode or "balanced").strip().lower()
    if mode == "off":
        return cats, primary

    if not cats:
        return [], []

    if mode == "conservative":
        keep_n = max(1, min(4, len(cats)))
        primary = cats[:keep_n]
    elif mode == "balanced":
        keep_n = max(1, min(6, len(cats)))
        primary = cats[:keep_n]
    elif mode == "broad":
        primary = list(cats)

    if not primary:
        primary = list(cats)
    return cats, primary


def _parse_arxiv_datetime(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _http_get(url: str, params: dict[str, Any], retries: int = 2) -> str:
    query = urlencode(params)
    full = f"{url}?{query}"
    req = Request(
        full,
        headers={"User-Agent": "agent-daily-paper/1.0 (seed-corpus)"},
        method="GET",
    )
    last_error: Exception | None = None
    for i in range(max(1, retries + 1)):
        try:
            with urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as exc:  # pragma: no cover
            last_error = exc
            if i < retries:
                time.sleep(1.0 + i * 0.6)
    if last_error is None:
        raise RuntimeError("arXiv request failed")
    raise last_error


def _extract_arxiv_id_and_version(id_url: str) -> tuple[str, str]:
    raw = id_url.strip().split("/")[-1]
    m = re.match(r"(?P<aid>.+?)(?P<ver>v\d+)$", raw)
    if not m:
        return raw, "v1"
    return m.group("aid"), m.group("ver")


def _expand_seed_query_terms(canonical_en: str, keywords: list[str], max_terms: int = 4) -> list[str]:
    terms: list[str] = []
    c = canonical_en.strip()
    if c:
        terms.append(c)
    terms.extend([str(k).strip() for k in keywords if _is_english_term(str(k)) and str(k).strip()])

    expanded: list[str] = []
    for t in terms:
        lowered = t.lower()
        expanded.append(t)
        words = [w for w in re.findall(r"[a-z][a-z0-9\-]{2,}", lowered)]
        for w in words:
            if len(w) > 4 and w.endswith("s"):
                expanded.append(w[:-1])
            elif len(w) > 3:
                expanded.append(f"{w}s")
        if len(words) >= 2:
            expanded.append(" ".join(words[:2]))

    out = list(dict.fromkeys([x.strip() for x in expanded if x.strip()]))
    return out[: max(1, max_terms)]


def _fetch_seed_papers_by_query(search_query: str, query_label: str, max_results: int = 20) -> list[dict[str, Any]]:
    if not search_query.strip():
        return []
    xml_text = _http_get(
        ARXIV_API,
        {
            "search_query": search_query,
            "start": 0,
            "max_results": max(1, max_results),
            "sortBy": "relevance",
            "sortOrder": "descending",
        },
    )
    root = ET.fromstring(xml_text)
    papers: list[dict[str, Any]] = []
    for idx, entry in enumerate(root.findall("atom:entry", ATOM_NS)):
        id_text = entry.findtext("atom:id", default="", namespaces=ATOM_NS).strip()
        aid, ver = _extract_arxiv_id_and_version(id_text)
        title = re.sub(r"\s+", " ", entry.findtext("atom:title", default="", namespaces=ATOM_NS)).strip()
        abstract = re.sub(r"\s+", " ", entry.findtext("atom:summary", default="", namespaces=ATOM_NS)).strip()
        updated = entry.findtext("atom:updated", default="", namespaces=ATOM_NS).strip()
        published = entry.findtext("atom:published", default="", namespaces=ATOM_NS).strip()
        categories = [
            str(c.attrib.get("term", "")).strip()
            for c in entry.findall("atom:category", ATOM_NS)
            if str(c.attrib.get("term", "")).strip()
        ]
        primary = ""
        pnode = entry.find("arxiv:primary_category", ARXIV_NS)
        if pnode is not None:
            primary = str(pnode.attrib.get("term", "")).strip()
        if not primary and categories:
            primary = categories[0]
        authors = [
            re.sub(r"\s+", " ", str(a.findtext("atom:name", default="", namespaces=ATOM_NS))).strip()
            for a in entry.findall("atom:author", ATOM_NS)
            if re.sub(r"\s+", " ", str(a.findtext("atom:name", default="", namespaces=ATOM_NS))).strip()
        ]
        papers.append(
            {
                "arxiv_id": aid,
                "version": ver,
                "title_en": title,
                "authors": authors,
                "abstract_en": abstract,
                "categories": categories,
                "primary_category": primary,
                "url": f"https://arxiv.org/abs/{aid}",
                "published": published,
                "updated": updated,
                "_seed_query": query_label,
                "_seed_rank": idx + 1,
            }
        )
    return papers


def _fetch_seed_papers_for_term(term: str, max_results: int = 20) -> list[dict[str, Any]]:
    q = term.strip()
    if not q:
        return []
    if " " in q:
        search_query = f'all:"{q}"'
    else:
        search_query = f"all:{q}"
    return _fetch_seed_papers_by_query(search_query=search_query, query_label=q, max_results=max_results)


def _fetch_seed_papers_for_category(category: str, max_results: int = 40) -> list[dict[str, Any]]:
    cat = _canonicalize_category(category)
    if not cat:
        return []
    return _fetch_seed_papers_by_query(
        search_query=f"cat:{cat}",
        query_label=f"cat:{cat}",
        max_results=max_results,
    )


def _build_seed_corpus(
    canonical_en: str,
    keywords: list[str],
    context_text: str = "",
    top_k: int = 20,
    embed_model_name: str = "BAAI/bge-m3",
    category_bias: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    query_terms = _expand_seed_query_terms(canonical_en, keywords, max_terms=4)
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    per_term_fetch = max(20, top_k * 2)
    for term in query_terms:
        try:
            rows = _fetch_seed_papers_for_term(term, max_results=per_term_fetch)
        except Exception:
            rows = []
        for row in rows:
            aid = str(row.get("arxiv_id", "")).strip()
            if not aid or aid in seen:
                continue
            seen.add(aid)
            merged.append(row)
            if len(merged) >= max(top_k * 6, 80):
                break
        if len(merged) >= max(top_k * 6, 80):
            break
    filtered = _filter_seed_papers_relevant(
        canonical_en=canonical_en,
        keywords=keywords,
        context_text=context_text,
        seed_papers=merged,
        top_k=top_k,
        embed_model_name=embed_model_name,
        category_bias=category_bias,
    )
    if category_bias and len(filtered) < top_k:
        for cat in category_bias[:3]:
            try:
                extra = _fetch_seed_papers_for_category(cat, max_results=max(30, top_k * 3))
            except Exception:
                extra = []
            for row in extra:
                aid = str(row.get("arxiv_id", "")).strip()
                if not aid or aid in seen:
                    continue
                seen.add(aid)
                merged.append(row)
        filtered = _filter_seed_papers_relevant(
            canonical_en=canonical_en,
            keywords=keywords,
            context_text=context_text,
            seed_papers=merged,
            top_k=top_k,
            embed_model_name=embed_model_name,
            category_bias=category_bias,
        )
    return filtered[:top_k], query_terms


def _collect_prior_categories(seed_papers: list[dict[str, Any]], known_codes: set[str]) -> list[str]:
    if not seed_papers:
        return []
    score: dict[str, int] = {}
    for p in seed_papers:
        primary = _canonicalize_category(str(p.get("primary_category", "")))
        all_cats = [_canonicalize_category(str(x)) for x in p.get("categories", []) if str(x).strip()]
        for c in all_cats:
            if known_codes and c not in known_codes:
                continue
            score[c] = score.get(c, 0) + 1
        if primary:
            if not known_codes or primary in known_codes:
                score[primary] = score.get(primary, 0) + 2
    return [k for k, _ in sorted(score.items(), key=lambda kv: (-kv[1], kv[0]))]


def _infer_keywords_from_seed(
    canonical_en: str,
    seed_papers: list[dict[str, Any]],
    base_keywords: list[str],
    min_k: int = 7,
    max_k: int = 10,
) -> list[str]:
    base = [str(k).strip().lower() for k in base_keywords if _is_english_term(str(k).strip()) and str(k).strip()]
    base = list(dict.fromkeys(base))
    counts: dict[str, float] = {}

    def add_term(term: str, weight: float) -> None:
        t = term.strip().lower()
        if not t or len(t) < 3:
            return
        if not re.fullmatch(r"[a-z0-9][a-z0-9\-\s]{1,80}", t):
            return
        tokens = [x for x in re.findall(r"[a-z0-9]+", t) if x and x not in STOPWORDS]
        if not tokens:
            return
        t_norm = " ".join(tokens)
        counts[t_norm] = counts.get(t_norm, 0.0) + weight

    for p in seed_papers:
        text = f"{p.get('title_en', '')} {p.get('abstract_en', '')}".lower()
        tokens = [x for x in re.findall(r"[a-z][a-z0-9\-]{2,}", text) if x not in STOPWORDS]
        for tk in tokens:
            add_term(tk, 1.0)
        for i in range(len(tokens) - 1):
            bg = f"{tokens[i]} {tokens[i + 1]}"
            add_term(bg, 1.8)
        for i in range(len(tokens) - 2):
            tg = f"{tokens[i]} {tokens[i + 1]} {tokens[i + 2]}"
            add_term(tg, 2.2)

    canonical_tokens = set(re.findall(r"[a-z0-9]+", canonical_en.lower())) - STOPWORDS
    ranked: list[tuple[float, str]] = []
    for term, score in counts.items():
        words = term.split()
        # Skip weak one-off long noise phrases.
        if len(words) >= 4:
            continue
        if len(words) == 1 and score < 2.0:
            continue
        term_tokens = set(words)
        if canonical_tokens.intersection(term_tokens):
            score += 1.0
        if term in base:
            score += 2.0
        ranked.append((score, term))
    ranked.sort(key=lambda x: (-x[0], x[1]))

    out: list[str] = []
    for b in base:
        if b not in out:
            out.append(b)
    for _, term in ranked:
        if term not in out:
            out.append(term)
        if len(out) >= max_k:
            break

    if len(out) < min_k:
        for t in _expand_seed_query_terms(canonical_en, base_keywords, max_terms=6):
            low = t.lower().strip()
            if low and low not in out:
                out.append(low)
            if len(out) >= min_k:
                break
    return out[:max_k]


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


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _seed_lexical_score(query_tokens: set[str], text: str) -> float:
    if not query_tokens:
        return 0.0
    doc_tokens = set(re.findall(r"[a-z0-9]+", text.lower())) - STOPWORDS
    if not doc_tokens:
        return 0.0
    inter = len(query_tokens.intersection(doc_tokens))
    return inter / max(1, len(query_tokens))


def _filter_seed_papers_relevant(
    canonical_en: str,
    keywords: list[str],
    context_text: str,
    seed_papers: list[dict[str, Any]],
    top_k: int,
    embed_model_name: str,
    category_bias: list[str] | None = None,
    min_relevance: float = 0.14,
) -> list[dict[str, Any]]:
    if not seed_papers:
        return []

    query_text = " | ".join(
        [
            f"field: {canonical_en}",
            f"keywords: {', '.join(keywords[:20])}",
            f"context: {context_text}",
        ]
    )
    q_tokens = set(re.findall(r"[a-z0-9]+", query_text.lower())) - STOPWORDS
    model = _load_embed_model(embed_model_name)

    query_emb: list[float] | None = None
    paper_embs: list[list[float]] = []
    if model is not None:
        try:
            query_emb_raw = model.encode(query_text, normalize_embeddings=False)
            paper_embs_raw = model.encode(
                [f"{p.get('title_en', '')}\n\n{p.get('abstract_en', '')}" for p in seed_papers],
                normalize_embeddings=False,
            )
            query_emb = list(query_emb_raw)
            paper_embs = [list(v) for v in paper_embs_raw]
        except Exception:
            query_emb = None
            paper_embs = []

    bias = set([_canonicalize_category(x) for x in (category_bias or []) if str(x).strip()])
    scored: list[tuple[float, dict[str, Any]]] = []
    for i, p in enumerate(seed_papers):
        text = f"{p.get('title_en', '')}\n\n{p.get('abstract_en', '')}"
        lex = _seed_lexical_score(q_tokens, text)
        sem = 0.0
        if query_emb is not None and i < len(paper_embs):
            sem = _cosine(query_emb, paper_embs[i])
        # Semantic dominates when available; lexical is robust fallback.
        score = (0.75 * sem + 0.25 * lex) if query_emb is not None else lex
        primary = _canonicalize_category(str(p.get("primary_category", "")))
        all_cats = [_canonicalize_category(str(x)) for x in p.get("categories", []) if str(x).strip()]
        if bias and (primary in bias or bool(set(all_cats).intersection(bias))):
            score += 0.18
        row = dict(p)
        row["_seed_relevance"] = round(float(score), 6)
        row["_seed_bias_hit"] = bool(bias and (primary in bias or bool(set(all_cats).intersection(bias))))
        scored.append((score, row))
    scored.sort(key=lambda x: x[0], reverse=True)
    bias_rows = [row for _, row in scored if row.get("_seed_bias_hit")]
    if bias and bias_rows:
        if len(bias_rows) >= top_k:
            return bias_rows[: max(1, top_k)]
        tail = [row for _, row in scored if (not row.get("_seed_bias_hit")) and row.get("_seed_relevance", 0.0) >= min_relevance]
        return (bias_rows + tail)[: max(1, top_k)]
    strong = [row for s, row in scored if s >= min_relevance]
    if len(strong) >= max(4, min(top_k, 10)):
        return strong[: max(1, top_k)]
    return [row for _, row in scored[: max(1, top_k)]]


def _persist_seed_artifacts(
    field_name: str,
    canonical_en: str,
    seed_papers: list[dict[str, Any]],
    embed_model_name: str,
    docs_dir: Path,
    emb_dir: Path,
    seed_query_terms: list[str] | None = None,
    seed_category_bias: list[str] | None = None,
    seed_fingerprint: str | None = None,
) -> dict[str, str]:
    slug = _slugify(canonical_en or field_name)
    docs_dir.mkdir(parents=True, exist_ok=True)
    emb_dir.mkdir(parents=True, exist_ok=True)
    md_path = docs_dir / f"{slug}.md"
    emb_path = emb_dir / f"{slug}.json"

    lines: list[str] = []
    lines.append(f"# Seed Papers - {canonical_en}")
    lines.append("")
    lines.append(f"- Source Field: {field_name}")
    lines.append(f"- Canonical EN: {canonical_en}")
    lines.append(f"- Total: {len(seed_papers)}")
    lines.append(f"- Generated At (UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    lines.append("")
    for idx, p in enumerate(seed_papers, start=1):
        title = str(p.get("title_en", "")).strip()
        authors = [str(x).strip() for x in p.get("authors", []) if str(x).strip()]
        abstract = str(p.get("abstract_en", "")).strip()
        url = str(p.get("url", "")).strip()
        arxiv_id = str(p.get("arxiv_id", "")).strip()
        lines.append(f"## {idx}. {title}")
        lines.append("")
        lines.append(f"- arXiv ID: {arxiv_id}")
        lines.append(f"- Authors: {', '.join(authors) if authors else '[Unknown]'}")
        lines.append(f"- Link: {url}")
        lines.append("")
        lines.append("### Abstract")
        lines.append(abstract)
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")

    model = _load_embed_model(embed_model_name)
    if model is None:
        raise RuntimeError(
            f"Seed embedding model unavailable: {embed_model_name}. "
            "Please install it first (e.g. python scripts/install_embedding_model.py --model BAAI/bge-m3)."
        )
    texts = [
        f"{str(p.get('title_en', '')).strip()}\n\n{str(p.get('abstract_en', '')).strip()}"
        for p in seed_papers
    ]
    vecs_raw = model.encode(texts, normalize_embeddings=False)
    rows = []
    for p, v, text in zip(seed_papers, vecs_raw, texts):
        rows.append(
            {
                "arxiv_id": str(p.get("arxiv_id", "")).strip(),
                "title_en": str(p.get("title_en", "")).strip(),
                "authors": [str(x).strip() for x in p.get("authors", []) if str(x).strip()],
                "abstract_en": str(p.get("abstract_en", "")).strip(),
                "primary_category": _canonicalize_category(str(p.get("primary_category", ""))),
                "categories": [_canonicalize_category(str(x)) for x in p.get("categories", []) if str(x).strip()],
                "url": str(p.get("url", "")).strip(),
                "text_for_embedding": text,
                "embedding": [float(x) for x in list(v)],
            }
        )
    emb_obj = {
        "field": field_name,
        "canonical_en": canonical_en,
        "model": embed_model_name,
        "seed_query_terms": list(seed_query_terms or []),
        "seed_category_bias": list(seed_category_bias or []),
        "seed_fingerprint": str(seed_fingerprint or ""),
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(rows),
        "items": rows,
    }
    emb_path.write_text(json.dumps(emb_obj, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"seed_doc_md": str(md_path).replace("\\", "/"), "seed_embedding_json": str(emb_path).replace("\\", "/")}


def _build_seed_fingerprint(
    field_name: str,
    canonical_en: str,
    keywords: list[str],
    context_text: str,
    top_k: int,
    embed_model_name: str,
) -> str:
    payload = {
        "field_name": str(field_name).strip().lower(),
        "canonical_en": str(canonical_en).strip().lower(),
        "keywords": sorted({str(k).strip().lower() for k in keywords if str(k).strip()}),
        "context_text": re.sub(r"\s+", " ", str(context_text or "").strip().lower()),
        "top_k": int(top_k),
        "embed_model_name": str(embed_model_name or "").strip().lower(),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _load_seed_cache(
    field_name: str,
    canonical_en: str,
    docs_dir: Path,
    emb_dir: Path,
    expected_fingerprint: str,
) -> dict[str, Any] | None:
    slug = _slugify(canonical_en or field_name)
    md_path = docs_dir / f"{slug}.md"
    emb_path = emb_dir / f"{slug}.json"
    if not emb_path.exists():
        return None
    try:
        data = json.loads(emb_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    got_fp = str(data.get("seed_fingerprint", "")).strip()
    if not got_fp or got_fp != expected_fingerprint:
        return None
    items = data.get("items", [])
    if not isinstance(items, list) or not items:
        return None

    seed_papers: list[dict[str, Any]] = []
    for row in items:
        if not isinstance(row, dict):
            continue
        cats = [_canonicalize_category(str(x)) for x in row.get("categories", []) if str(x).strip()]
        primary = _canonicalize_category(str(row.get("primary_category", "")))
        if not cats and not primary:
            # Old cache format (missing category fields) should be rebuilt to keep category inference stable.
            return None
        seed_papers.append(
            {
                "arxiv_id": str(row.get("arxiv_id", "")).strip(),
                "title_en": str(row.get("title_en", "")).strip(),
                "authors": [str(x).strip() for x in row.get("authors", []) if str(x).strip()],
                "abstract_en": str(row.get("abstract_en", "")).strip(),
                "primary_category": primary,
                "categories": cats,
                "url": str(row.get("url", "")).strip(),
            }
        )
    if not seed_papers:
        return None

    return {
        "seed_papers": seed_papers,
        "seed_query_terms": [str(x).strip() for x in data.get("seed_query_terms", []) if str(x).strip()],
        "seed_category_bias": [_canonicalize_category(str(x)) for x in data.get("seed_category_bias", []) if str(x).strip()],
        "seed_doc_md": str(md_path).replace("\\", "/"),
        "seed_embedding_json": str(emb_path).replace("\\", "/"),
        "cache_hit": True,
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


def _normalize_profile_key(text: str) -> str:
    return re.sub(r"[\s\-\_\:\：\,，\.\(\)（）]+", "", str(text or "").strip().lower())


def _lookup_agent_profile(field_name: str, profiles: dict[str, Any]) -> dict[str, Any] | None:
    if not profiles:
        return None
    direct = profiles.get(field_name)
    if isinstance(direct, dict):
        return direct
    n = _normalize_profile_key(field_name)
    if not n:
        return None
    # Exact normalized key match first.
    for k, v in profiles.items():
        if not isinstance(v, dict):
            continue
        if _normalize_profile_key(k) == n:
            return v
    # Containment fallback for user inputs with descriptions.
    for k, v in profiles.items():
        if not isinstance(v, dict):
            continue
        nk = _normalize_profile_key(k)
        if nk and (nk in n or n in nk):
            return v
    return None


def _openai_profile(field_name: str) -> dict[str, Any] | None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    model_name = os.getenv("FIELD_PROFILE_MODEL", "").strip() or os.getenv("OPENAI_FIELD_PROFILE_MODEL", "").strip()
    if not model_name:
        return None

    body = {
        "model": model_name,
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
    categories: list[str] = []
    for m in re.finditer(r"\b[a-z]{2,}(?:\-[a-z]{2,})?\.[a-z]{2,}\b", field_name.lower()):
        code = _canonicalize_category(m.group(0))
        if code:
            categories.append(code)

    english_terms = [x.strip() for x in re.findall(r"[A-Za-z][A-Za-z0-9\-\s]{1,40}", field_name) if x.strip()]
    keywords = list(dict.fromkeys([x.lower() for x in english_terms]))[:12]
    canonical_en = " ".join([k for k in keywords if _is_english_term(k)][:4]) or field_name
    venues: list[str] = []

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
    field_context: str = "",
    global_venues: list[str] | None = None,
    category_expand_mode: str = "balanced",
    require_agent_categories: bool = False,
    taxonomy_rows: dict[str, dict[str, Any]] | None = None,
    known_codes: set[str] | None = None,
    seed_top_k: int = 20,
    seed_embed_model: str = "BAAI/bge-m3",
    seed_docs_dir: str = "output/seed_corpus/docs",
    seed_embeddings_dir: str = "output/seed_corpus/embeddings",
    seed_force_refresh: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    profile = agent_profile
    source = "agent" if profile else "heuristic"
    if profile is None and use_openai:
        profile = _openai_profile(field_name)
        source = "openai" if profile else "heuristic"
    if not profile:
        profile = _heuristic_profile(field_name)

    keywords = [str(x).strip() for x in profile.get("keywords", []) if _is_english_term(str(x).strip())]
    context_keywords = [x for x in re.findall(r"[A-Za-z][A-Za-z0-9\-\s]{2,30}", field_context) if _is_english_term(x)]
    keywords = list(dict.fromkeys(keywords + [k.strip() for k in context_keywords if k.strip()]))
    valid_codes = known_codes or set()
    categories_raw = [str(x).strip() for x in profile.get("categories", []) if str(x).strip()]
    categories = _validate_categories(categories_raw, valid_codes)
    agent_categories = list(categories)
    canonical_en = _ensure_english_canonical(
        field_name=field_name,
        profile_canonical=str(profile.get("canonical_en", "")).strip(),
        field_context=field_context,
        keywords=keywords,
    )
    canonical_terms = [w for w in re.findall(r"[a-z][a-z0-9\-]{1,40}", canonical_en.lower())]
    keywords = list(dict.fromkeys(keywords + canonical_terms))
    if not keywords:
        keywords = canonical_terms[:8]

    if require_agent_categories and not categories:
        raise ValueError(
            f"Field '{field_name}' missing agent-provided categories. "
            "Please add categories in config/agent_field_profiles.json."
        )

    seed_fingerprint = _build_seed_fingerprint(
        field_name=field_name,
        canonical_en=canonical_en,
        keywords=keywords,
        context_text=field_context,
        top_k=seed_top_k,
        embed_model_name=seed_embed_model,
    )
    docs_dir = Path(seed_docs_dir)
    emb_dir = Path(seed_embeddings_dir)
    cache = None if seed_force_refresh else _load_seed_cache(
        field_name=field_name,
        canonical_en=canonical_en,
        docs_dir=docs_dir,
        emb_dir=emb_dir,
        expected_fingerprint=seed_fingerprint,
    )

    if cache:
        seed_papers = list(cache.get("seed_papers", []))
        seed_query_terms = list(cache.get("seed_query_terms", []))
        seed_category_bias = [_canonicalize_category(str(x)) for x in cache.get("seed_category_bias", []) if str(x).strip()]
        artifacts = {
            "seed_doc_md": str(cache.get("seed_doc_md", "")).strip(),
            "seed_embedding_json": str(cache.get("seed_embedding_json", "")).strip(),
        }
    else:
        seed_category_bias = _taxonomy_suggest_categories(
            field_name=field_name,
            canonical_en=canonical_en,
            keywords=keywords,
            taxonomy_rows=taxonomy_rows or {},
            preferred_groups=[c.split(".", 1)[0] for c in agent_categories if "." in c],
            top_n=6,
        )
        seed_category_bias = _validate_categories(seed_category_bias, valid_codes)
        seed_papers, seed_query_terms = _build_seed_corpus(
            canonical_en=canonical_en,
            keywords=keywords,
            context_text=field_context,
            top_k=seed_top_k,
            embed_model_name=seed_embed_model,
            category_bias=seed_category_bias,
        )
        artifacts = _persist_seed_artifacts(
            field_name=field_name,
            canonical_en=canonical_en,
            seed_papers=seed_papers,
            embed_model_name=seed_embed_model,
            docs_dir=docs_dir,
            emb_dir=emb_dir,
            seed_query_terms=seed_query_terms,
            seed_category_bias=seed_category_bias,
            seed_fingerprint=seed_fingerprint,
        )
    seed_prior_categories = _collect_prior_categories(seed_papers, valid_codes)
    seed_keywords = _infer_keywords_from_seed(
        canonical_en=canonical_en,
        seed_papers=seed_papers,
        base_keywords=keywords,
        min_k=7,
        max_k=10,
    )

    taxonomy_suggested = _taxonomy_suggest_categories(
        field_name=field_name,
        canonical_en=canonical_en,
        keywords=list(dict.fromkeys(seed_keywords + keywords)),
        taxonomy_rows=taxonomy_rows or {},
        preferred_groups=[c.split(".", 1)[0] for c in agent_categories if "." in c],
        top_n=12,
    )
    taxonomy_suggested = _validate_categories(taxonomy_suggested, valid_codes)

    if not categories:
        categories = list(dict.fromkeys(seed_prior_categories + taxonomy_suggested))

    primary_categories = [str(x).strip() for x in profile.get("primary_categories", []) if str(x).strip()]
    primary_categories = _validate_categories(primary_categories, valid_codes)
    categories, auto_primary = _expand_categories(field_name, categories, mode=category_expand_mode)
    if not primary_categories:
        primary_categories = list(
            dict.fromkeys(seed_prior_categories + taxonomy_suggested + agent_categories + auto_primary)
        )
    primary_categories = _validate_categories(primary_categories, valid_codes)
    if not primary_categories:
        primary_categories = list(categories)
    # Keep primary categories as a subset of categories.
    categories = list(dict.fromkeys(categories + primary_categories))
    keywords_final = list(dict.fromkeys(seed_keywords + keywords))[:10]
    if len(keywords_final) < 7:
        keywords_final = list(dict.fromkeys(keywords + seed_keywords))[:10]

    title_keywords = [str(x).strip() for x in profile.get("title_keywords", []) if _is_english_term(str(x).strip())]
    title_keywords = list(dict.fromkeys(title_keywords + keywords_final))[:10]
    venues = [str(x).strip() for x in profile.get("venues", []) if str(x).strip()]
    venues = list(dict.fromkeys(venues + [str(v).strip() for v in (global_venues or []) if str(v).strip()]))

    # Run digest uses field name + keywords for fuzzy retrieval.
    setting = {
        "name": canonical_en,
        "limit": limit,
        "categories": categories,
        "primary_categories": primary_categories,
        "keywords": keywords_final[:16],
        "exclude_keywords": [],
    }
    highlight = {
        "title_keywords": title_keywords[:10],
        "authors": [],
        "venues": venues[:8],
    }

    return setting, highlight, {
        "field": field_name,
        "field_context": field_context,
        "canonical_en": canonical_en,
        "source": source,
        "categories": categories,
        "primary_categories": primary_categories,
        "keywords": keywords_final[:16],
        "venues": venues[:8],
        "seed_query_terms": seed_query_terms,
        "seed_paper_count": len(seed_papers),
        "seed_prior_categories": seed_prior_categories,
        "seed_category_bias": seed_category_bias,
        "seed_keywords": seed_keywords,
        "seed_papers": [
            {
                "arxiv_id": p.get("arxiv_id", ""),
                "title_en": p.get("title_en", ""),
                "authors": p.get("authors", []),
                "abstract_en": p.get("abstract_en", ""),
                "primary_category": p.get("primary_category", ""),
                "categories": p.get("categories", []),
                "url": p.get("url", ""),
            }
            for p in seed_papers
        ],
        "seed_cache_hit": bool(cache),
        "seed_fingerprint": seed_fingerprint,
        "seed_doc_md": artifacts.get("seed_doc_md", ""),
        "seed_embedding_json": artifacts.get("seed_embedding_json", ""),
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
    parser.add_argument(
        "--seed-top-k",
        type=int,
        default=20,
        help="Top-k seed papers fetched from arXiv all-fields relevance per field",
    )
    parser.add_argument(
        "--seed-embed-model",
        default="BAAI/bge-m3",
        help="Embedding model for seed relevance filtering",
    )
    parser.add_argument(
        "--seed-docs-dir",
        default="output/seed_corpus/docs",
        help="Directory for saving Top-K seed papers markdown per field",
    )
    parser.add_argument(
        "--seed-embeddings-dir",
        default="output/seed_corpus/embeddings",
        help="Directory for saving seed title+abstract embeddings per field",
    )
    parser.add_argument(
        "--seed-force-refresh",
        action="store_true",
        help="Force refresh seed corpus even if fingerprint cache matches",
    )
    parser.add_argument(
        "--use-openai-profile",
        action="store_true",
        help="Opt-in: use OpenAI Responses API for profile generation when agent profile is missing",
    )
    parser.add_argument("--no-openai", action="store_true", help="Deprecated: no-op compatibility flag")
    args = parser.parse_args()

    parsed_fields = _parse_fields_input(args.fields)
    if not parsed_fields:
        raise ValueError("No valid fields parsed from --fields")
    global_venue_hints = _extract_global_venue_hints(args.fields)
    use_openai = bool(args.use_openai_profile) and bool(os.getenv("OPENAI_API_KEY"))
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
    docs_dir = Path(args.seed_docs_dir)
    emb_dir = Path(args.seed_embeddings_dir)

    for item in parsed_fields:
        f = item.get("name", "").strip()
        context = item.get("context", "").strip()
        agent_profile = _lookup_agent_profile(f, agent_profiles)
        setting, highlight, trace = build_field_setting(
            f,
            args.limit,
            use_openai=use_openai,
            agent_profile=agent_profile,
            field_context=context,
            global_venues=global_venue_hints,
            category_expand_mode=args.category_expand_mode,
            require_agent_categories=args.agent_categories_only,
            taxonomy_rows=taxonomy_rows,
            known_codes=known_codes,
            seed_top_k=args.seed_top_k,
            seed_embed_model=args.seed_embed_model,
            seed_docs_dir=args.seed_docs_dir,
            seed_embeddings_dir=args.seed_embeddings_dir,
            seed_force_refresh=args.seed_force_refresh,
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
                "seed_query_terms": trace.get("seed_query_terms", []),
                "seed_category_bias": trace.get("seed_category_bias", []),
                "seed_keywords": trace.get("seed_keywords", trace.get("keywords", setting.get("keywords", []))),
                "seed_papers": trace.get("seed_papers", []),
                "seed_doc_md": trace.get("seed_doc_md", ""),
                "seed_embedding_json": trace.get("seed_embedding_json", ""),
                "seed_cache_hit": bool(trace.get("seed_cache_hit", False)),
                "seed_fingerprint": trace.get("seed_fingerprint", ""),
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
            "global_venue_hints": global_venue_hints,
            "seed_docs_dir": str(docs_dir).replace("\\", "/"),
            "seed_embeddings_dir": str(emb_dir).replace("\\", "/"),
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
