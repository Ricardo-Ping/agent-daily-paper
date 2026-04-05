from __future__ import annotations

from datetime import datetime, timezone

import pytest

from run_digest import Paper, _resolve_canonical_en, embedding_filter_papers


def _paper(idx: int) -> Paper:
    now = datetime.now(timezone.utc)
    return Paper(
        arxiv_id=f"2501.00{idx}",
        version="v1",
        title_en=f"Paper {idx}",
        abstract_en="Test abstract.",
        authors=["A"],
        categories=["cs.AI"],
        primary_category="cs.AI",
        published=now,
        updated=now,
        url=f"https://arxiv.org/abs/2501.00{idx}",
        source_field="test",
    )


def test_resolve_canonical_prefers_profile_english() -> None:
    out = _resolve_canonical_en(
        field_name="数据挖掘",
        profile_canonical="Data Mining",
        english_hints=[],
    )
    assert out == "data mining"


def test_resolve_canonical_falls_back_to_english_hints() -> None:
    out = _resolve_canonical_en(
        field_name="数据挖掘",
        profile_canonical="",
        english_hints=["query optimizer", "execution plan"],
    )
    assert out == "query optimizer execution plan"


def test_resolve_canonical_raises_on_invalid_inputs() -> None:
    with pytest.raises(ValueError):
        _resolve_canonical_en(
            field_name="���",
            profile_canonical="���",
            english_hints=[],
        )


def test_embedding_filter_relaxes_when_all_filtered(monkeypatch: pytest.MonkeyPatch) -> None:
    papers = [_paper(i) for i in range(1, 4)]

    class FakeModel:
        def __init__(self) -> None:
            self._call = 0

        def encode(self, docs, normalize_embeddings=False):  # noqa: ANN001
            if isinstance(docs, str):
                docs = [docs]
            self._call += 1
            if self._call == 1:
                return [[1.0, 0.0] for _ in docs]
            # Cosine with [1,0] => 0.10, 0.20, 0.30
            return [[0.10, 1.0], [0.20, 1.0], [0.30, 1.0]]

    monkeypatch.setattr("run_digest._load_embed_model", lambda _: FakeModel())
    out = embedding_filter_papers(
        papers,
        canonical_en="data mining",
        keywords=["data mining"],
        venues=[],
        cfg={
            "enabled": True,
            "model": "fake",
            "threshold": 0.95,
            "top_k": 10,
            "auto_relax_on_empty": True,
            "min_keep": 2,
        },
        seed_texts=[],
    )
    assert len(out) == 2
    assert out[0].embedding_score >= out[1].embedding_score
    assert out[0].embedding_score > 0

