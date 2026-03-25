from __future__ import annotations

from datetime import datetime, timedelta, timezone

from run_digest import Paper, build_why_recommended


def test_build_why_recommended_contains_core_signals() -> None:
    now = datetime.now(timezone.utc)
    p = Paper(
        arxiv_id="2501.00001",
        version="v1",
        title_en="Causal Retrieval for Recommender Systems",
        abstract_en="We propose a retrieval and rerank pipeline for recommender systems.",
        authors=["A", "B"],
        categories=["cs.IR", "cs.LG"],
        primary_category="cs.IR",
        published=now - timedelta(days=1),
        updated=now - timedelta(hours=6),
        url="https://arxiv.org/abs/2501.00001",
        source_field="recommender systems",
        score=88.2,
        embedding_score=0.71,
        rerank_score=0.84,
        status="NEW",
    )
    reason = build_why_recommended(
        paper=p,
        categories=["cs.IR", "cs.LG"],
        keywords=["causal retrieval", "recommender systems"],
        field_name="recommender systems",
        now_utc=now,
    )
    assert "category=" in reason
    assert "keywords=" in reason
    assert "embedding=0.71" in reason
    assert "rerank=0.84" in reason
    assert "status=NEW" in reason
