#!/usr/bin/env python3
"""Aggregate like/dislike feedback and generate retrieval adjustments."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "using", "method", "methods", "model", "models",
    "paper", "based", "learning", "system", "systems", "approach", "framework", "towards", "new", "via",
    "study", "task", "tasks", "data", "analysis", "results", "toward",
}


def tokenize(text: str) -> list[str]:
    terms = [t.lower() for t in re.findall(r"[a-z][a-z0-9\-]{2,}", str(text or "").lower())]
    out = [t for t in terms if t not in STOPWORDS]
    return out


def load_feedback_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8-sig") as f:
        for line in f:
            raw = line.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def rank_keywords(counter: Counter[str], limit: int = 12) -> list[str]:
    return [k for k, _ in counter.most_common(limit)]


def build_adjustments(rows: list[dict[str, Any]], min_events: int = 2) -> dict[str, Any]:
    by_field = defaultdict(list)
    for row in rows:
        field = str(row.get("source_field", "")).strip().lower()
        if not field:
            field = "__unknown__"
        by_field[field].append(row)

    field_payload: dict[str, Any] = {}
    for field, items in by_field.items():
        if len(items) < max(1, min_events):
            continue
        like = 0
        dislike = 0
        pos_terms: Counter[str] = Counter()
        neg_terms: Counter[str] = Counter()
        reason_terms: Counter[str] = Counter()

        for item in items:
            label = str(item.get("label", "")).strip().lower()
            title_terms = tokenize(str(item.get("title_en", "")))
            reason = str(item.get("reason", "")).strip()
            if label == "like":
                like += 1
                pos_terms.update(title_terms)
            elif label == "dislike":
                dislike += 1
                neg_terms.update(title_terms)
                reason_terms.update(tokenize(reason))

        if like + dislike <= 0:
            continue

        positive_keywords = rank_keywords(pos_terms - neg_terms, limit=10)
        negative_keywords = rank_keywords((neg_terms + reason_terms) - pos_terms, limit=10)

        threshold_delta = 0.0
        if dislike >= like * 2 and dislike >= 4:
            threshold_delta = 0.02
        elif like >= dislike * 2 and like >= 4:
            threshold_delta = -0.02

        field_payload[field] = {
            "stats": {"events": len(items), "like": like, "dislike": dislike},
            "positive_keywords": positive_keywords,
            "negative_keywords": negative_keywords,
            "suggested_embedding_threshold_delta": threshold_delta,
        }

    return {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "summary": {"feedback_events": len(rows), "fields_with_adjustments": len(field_payload)},
        "fields": field_payload,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggregate feedback into retrieval adjustments")
    parser.add_argument("--feedback-jsonl", default="data/feedback/feedback.jsonl")
    parser.add_argument("--output", default="config/feedback_adjustments.json")
    parser.add_argument("--min-events", type=int, default=2, help="Minimum events required per field")
    args = parser.parse_args()

    rows = load_feedback_rows(Path(args.feedback_jsonl))
    payload = build_adjustments(rows, min_events=args.min_events)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] feedback adjustments saved: {out_path}")
    print(
        json.dumps(
            {
                "events": payload.get("summary", {}).get("feedback_events", 0),
                "fields_with_adjustments": payload.get("summary", {}).get("fields_with_adjustments", 0),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
