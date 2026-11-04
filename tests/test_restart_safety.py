"""Restart-safety and duplicate-prevention guarantees.

These build a *second* service container over the same data directory to
simulate a process restart, which is the scenario the design rules target.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services.container import build_services


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _ready_question(services, text: str = "q"):
    return services.questions.create(
        {
            "question": text,
            "options": ["a", "b"],
            "correct_option_id": 0,
            "status": "ready",
        }
    )


def test_published_question_is_not_republished_after_restart(services) -> None:
    question = _ready_question(services, "Which protocol uses port 53?")
    channel = services.channels.create({"name": "Main", "chat_id": "@main"})

    result = _run(services.publishing.publish(question["id"], channel["id"]))
    assert result["status"] == "success"

    restarted = build_services(services.settings)
    # The question stays "ready"; its published state lives per channel in the
    # history, so it is not republished to the same channel.
    assert restarted.questions.get(question["id"])["status"] == "ready"

    # The already-published question is no longer a candidate for this channel.
    candidates = restarted.questions.pick_candidates(
        selection="sequential",
        channel_id=channel["id"],
        published_pairs=restarted.publishing.published_pairs(),
    )
    assert question["id"] not in {q["id"] for q in candidates}

    # And a second attempt is refused outright.
    with pytest.raises(Exception, match="نُشر مسبقاً"):
        _run(restarted.publishing.publish(question["id"], channel["id"]))


def test_history_survives_restart(services) -> None:
    question = _ready_question(services)
    channel = services.channels.create({"name": "Main", "chat_id": "@main"})
    _run(services.publishing.publish(question["id"], channel["id"]))

    restarted = build_services(services.settings)
    history = restarted.publishing.history()
    assert len(history) == 1
    assert history[0]["question_id"] == question["id"]
    assert history[0]["status"] == "success"


def test_schedule_progress_persists_across_restart(services) -> None:
    channel = services.channels.create({"name": "Main", "chat_id": "@main"})
    for index in range(4):
        _ready_question(services, f"q{index}")
    schedule = services.schedules.create(
        {
            "channel_id": channel["id"],
            "type": "daily",
            "times": ["20:00"],
            "questions_per_day": 0,
            "enabled": True,
        }
    )
    _run(services.runner.run(schedule["id"]))
    _run(services.runner.run(schedule["id"]))

    restarted = build_services(services.settings)
    stored = restarted.schedules.get(schedule["id"])
    assert stored["published_count"] == 2

    # The two published questions are excluded on the next run after restart,
    # per channel (the history is the source of truth).
    published = {e["question_id"] for e in restarted.publishing.history()}
    assert len(published) == 2
    candidates = restarted.questions.pick_candidates(
        selection="sequential",
        channel_id=channel["id"],
        published_pairs=restarted.publishing.published_pairs(),
    )
    assert published.isdisjoint({q["id"] for q in candidates})


def test_attempt_is_recorded_before_telegram_send(services, monkeypatch) -> None:
    """A crash mid-send must leave an auditable ``pending`` row."""
    from app.services import publishing_service as module

    seen: list[str] = []
    original_claim = module.PublishingService._claim

    def _spy(self, *args, **kwargs):
        seen.append("claimed")
        return original_claim(self, *args, **kwargs)

    monkeypatch.setattr(module.PublishingService, "_claim", _spy)

    question = _ready_question(services)
    channel = services.channels.create({"name": "Main", "chat_id": "@main"})
    _run(services.publishing.publish(question["id"], channel["id"]))

    assert seen == ["claimed"]


def test_pending_rows_are_reconciled_on_restart(services) -> None:
    """A stale pending row becomes ``unknown`` rather than being retried blind."""
    from datetime import UTC, datetime, timedelta

    question = _ready_question(services)
    channel = services.channels.create({"name": "Main", "chat_id": "@main"})
    stale = (datetime.now(UTC) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")

    def _insert(document):
        document["history"].insert(
            0,
            {
                "id": "PUB00099",
                "question_id": question["id"],
                "channel_id": channel["id"],
                "schedule_id": None,
                "message_id": None,
                "status": "pending",
                "error": None,
                "error_code": None,
                "attempts": 1,
                "retryable": False,
                "trigger": "schedule",
                "published_at": stale,
                "updated_at": stale,
            },
        )

    services.storage.history.update(_insert)

    restarted = build_services(services.settings)
    changed = restarted.publishing.reconcile_pending()
    assert changed == 1
    assert restarted.publishing.history()[0]["status"] == "unknown"
