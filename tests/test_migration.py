from __future__ import annotations

from config_migration import (
    migrate_state_config,
    migrate_subscriptions_config,
    validate_state_config,
    validate_subscriptions_config,
)


def test_migrate_subscriptions_from_legacy_fields() -> None:
    cfg = {
        "subscriptions": [
            {
                "id": "legacy-sub",
                "fields": ["推荐系统", "数据库优化器"],
                "daily_count": 3,
            }
        ]
    }
    migrated, changes = migrate_subscriptions_config(cfg)

    assert migrated["schema_version"] == 2
    assert changes
    sub = migrated["subscriptions"][0]
    assert sub["timezone"] == "Asia/Shanghai"
    assert sub["push_time"] == "09:00"
    assert sub["query_strategy"] == "category_keyword_union"
    assert sub["require_primary_category"] is True
    assert len(sub["field_settings"]) == 2
    # 3 is clamped to lower bound 5
    assert sub["field_settings"][0]["limit"] == 5

    errors, _ = validate_subscriptions_config(migrated)
    assert not errors


def test_migrate_state_from_legacy_keys() -> None:
    state = {
        "sent_versions": {"1234.0001": "v3"},
        "sent_ids": ["1234.0002"],
        "sent_ids_by_sub": {"sub-a": ["1234.0003"]},
        "last_push_date": "2026-03-25",
    }
    migrated, changes = migrate_state_config(state)
    assert migrated["schema_version"] == 2
    assert changes

    by_sub = migrated["sent_versions_by_sub"]
    assert by_sub["__legacy__"]["1234.0001"] == "v3"
    assert by_sub["__legacy__"]["1234.0002"] == "v1"
    assert by_sub["sub-a"]["1234.0003"] == "v1"
    assert migrated["last_push_date_by_sub"]["__legacy__"] == "2026-03-25"

    errors, _ = validate_state_config(migrated)
    assert not errors
