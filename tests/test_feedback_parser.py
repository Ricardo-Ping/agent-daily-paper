from __future__ import annotations

from feedback_cli import parse_feedback_text


def test_parse_feedback_short_form() -> None:
    actions = parse_feedback_text("+ 1,3; - 2#too broad")
    assert len(actions) == 2
    assert actions[0]["label"] == "like"
    assert actions[0]["indices"] == [1, 3]
    assert actions[1]["label"] == "dislike"
    assert actions[1]["indices"] == [2]
    assert actions[1]["reason"] == "too broad"


def test_parse_feedback_structured_form() -> None:
    actions = parse_feedback_text("like:1,2; dislike:3#off-topic")
    assert actions[0]["label"] == "like"
    assert actions[1]["label"] == "dislike"
    assert actions[1]["reason"] == "off-topic"
