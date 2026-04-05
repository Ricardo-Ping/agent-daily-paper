from __future__ import annotations

from pathlib import Path

from doctor import check_field_profile_alignment


def test_field_profile_alignment_ok() -> None:
    cfg = {
        "subscriptions": [
            {
                "id": "sub-ok",
                "field_settings": [{"name": "data mining", "limit": 10}],
                "field_profiles": [
                    {"field": "数据挖掘", "canonical_en": "data mining"},
                ],
            }
        ]
    }
    rows = check_field_profile_alignment(Path("config/subscriptions.json"), cfg)
    assert rows
    assert any(r.level == "OK" for r in rows)


def test_field_profile_alignment_warns_on_mismatch() -> None:
    cfg = {
        "subscriptions": [
            {
                "id": "sub-mismatch",
                "field_settings": [{"name": "database optimizer", "limit": 10}],
                "field_profiles": [
                    {"field": "推荐系统", "canonical_en": "recommender systems"},
                ],
            }
        ]
    }
    rows = check_field_profile_alignment(Path("config/subscriptions.json"), cfg)
    warns = [r for r in rows if r.level == "WARN"]
    assert warns
    assert "兜底匹配" in warns[0].detail or "未匹配" in warns[0].detail
    assert "建议自动修复" in warns[0].detail


def test_field_profile_alignment_warns_on_garbled_or_invalid_canonical() -> None:
    cfg = {
        "subscriptions": [
            {
                "id": "sub-invalid",
                "field_settings": [{"name": "data � mining", "limit": 10}],
                "field_profiles": [
                    {"field": "数据挖掘", "canonical_en": "数据挖掘"},
                ],
            }
        ]
    }
    rows = check_field_profile_alignment(Path("config/subscriptions.json"), cfg)
    warns = [r for r in rows if r.level == "WARN"]
    assert warns
    detail = warns[0].detail
    assert "疑似乱码" in detail or "canonical_en" in detail
