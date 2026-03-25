from __future__ import annotations

from apply_feedback import build_adjustments


def test_build_adjustments_from_feedback_rows() -> None:
    rows = [
        {
            "label": "like",
            "source_field": "recommender systems",
            "title_en": "Causal Ranking for Recommender Systems",
            "reason": "",
        },
        {
            "label": "dislike",
            "source_field": "recommender systems",
            "title_en": "Generic NLP Prompting Survey",
            "reason": "too broad nlp",
        },
    ]
    payload = build_adjustments(rows, min_events=1)
    assert payload["schema_version"] == 1
    fields = payload["fields"]
    assert "recommender systems" in fields
    data = fields["recommender systems"]
    assert data["stats"]["events"] == 2
    assert "causal" in data["positive_keywords"]
    assert "nlp" in data["negative_keywords"]
