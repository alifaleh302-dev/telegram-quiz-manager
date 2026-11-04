"""Engagement analytics: snapshot collection, live answers and aggregations."""

from __future__ import annotations

import pytest

from app.telegram.telegram_client import PollSnapshot


@pytest.fixture
def channel(services):
    return services.channels.create({"name": "Main", "chat_id": "@main_channel"})


def _snapshot(message_id, counts, *, correct=1, closed=True, explanation=""):
    return PollSnapshot(
        ok=True,
        poll_id=f"poll-{message_id}",
        is_closed=closed,
        total_voters=sum(counts),
        options=[
            {"text": f"Option {i + 1}", "voter_count": count}
            for i, count in enumerate(counts)
        ],
        correct_option_ids=[correct],
        explanation=explanation,
    )


async def test_collect_stores_snapshot_and_aggregates(services, channel, fake_telegram):
    question = services.questions.create(
        {
            "question": "Which protocol uses port 53?",
            "options": ["HTTP", "DNS", "FTP", "SSH"],
            "correct_option_id": 1,
            "category": "Networking",
            "difficulty": "easy",
            "status": "ready",
        }
    )
    entry = await services.publishing.publish(question["id"], channel["id"])
    assert entry["poll_id"] == "poll-1001"

    fake_telegram.poll_snapshots[entry["message_id"]] = _snapshot(
        entry["message_id"], [1, 3, 0, 0]
    )
    fake_telegram.member_counts["@main_channel"] = 100

    summary = await services.analytics.collect(services.telegram)
    assert summary["ok"] is True
    assert summary["collected"] == 1

    rows = services.analytics.per_question()
    assert len(rows) == 1
    row = rows[0]
    assert row["question_id"] == question["id"]
    assert row["votes"] == 4
    assert row["correct_pct"] == 75.0
    assert row["participation_pct"] == 4.0
    assert row["total_members"] == 100
    assert [o["is_correct"] for o in row["options"]] == [False, True, False, False]

    overview = services.analytics.overview()
    assert overview["polls"] == 1
    assert overview["total_votes"] == 4
    assert overview["avg_correct_pct"] == 75.0
    assert overview["collected"] == 1


async def test_collect_refreshes_the_same_poll_in_place(services, channel, fake_telegram):
    question = services.questions.create(
        {
            "question": "Which protocol uses port 53?",
            "options": ["HTTP", "DNS"],
            "correct_option_id": 1,
            "category": "Networking",
            "difficulty": "easy",
            "status": "ready",
        }
    )
    entry = await services.publishing.publish(question["id"], channel["id"])
    fake_telegram.poll_snapshots[entry["message_id"]] = _snapshot(
        entry["message_id"], [1, 1], correct=1
    )
    await services.analytics.collect(services.telegram)

    fake_telegram.poll_snapshots[entry["message_id"]] = _snapshot(
        entry["message_id"], [1, 5], correct=1
    )
    await services.analytics.collect(services.telegram)

    rows = services.analytics.per_question()
    assert len(rows) == 1
    assert rows[0]["votes"] == 6  # refreshed, not appended


async def test_collect_skips_already_closed_polls(services, channel, fake_telegram):
    question = services.questions.create(
        {
            "question": "Which protocol uses port 53?",
            "options": ["HTTP", "DNS"],
            "correct_option_id": 1,
            "category": "Networking",
            "difficulty": "easy",
            "status": "ready",
        }
    )
    # No snapshot registered => the fake reports "Poll can't be stopped".
    await services.publishing.publish(question["id"], channel["id"])
    summary = await services.analytics.collect(services.telegram)
    assert summary["collected"] == 0
    assert summary["skipped"] == 1
    assert summary["errors"] == 0


async def test_live_answers_track_each_member(services, channel, fake_telegram):
    question = services.questions.create(
        {
            "question": "Which protocol uses port 53?",
            "options": ["HTTP", "DNS"],
            "correct_option_id": 1,
            "category": "Networking",
            "difficulty": "easy",
            "status": "ready",
        }
    )
    entry = await services.publishing.publish(question["id"], channel["id"])
    poll_id = entry["poll_id"]

    services.analytics.record_answer(
        poll_id=poll_id, user_id=1, username="sara", option_ids=[1], is_anonymous=False
    )
    services.analytics.record_answer(
        poll_id=poll_id, user_id=2, username="omar", option_ids=[0], is_anonymous=False
    )
    # A second answer from the same user replaces the first.
    services.analytics.record_answer(
        poll_id=poll_id, user_id=1, username="sara", option_ids=[0], is_anonymous=False
    )

    answers = services.analytics.answers_for_poll(poll_id)
    assert len(answers) == 2
    sara = next(a for a in answers if a["user_id"] == 1)
    assert sara["option_ids"] == [0]
    assert sara["is_correct"] is False

    overview = services.analytics.overview()
    assert overview["unique_voters"] == 2
    assert overview["live_answers"] == 2
    assert overview["live_correct_pct"] == 0.0


async def test_anonymous_answers_are_not_stored(services, channel, fake_telegram):
    assert (
        services.analytics.record_answer(
            poll_id="poll-1", user_id=None, username=None, option_ids=[1], is_anonymous=True
        )
        is None
    )


async def test_by_channel_and_category_group_correctly(services, channel, fake_telegram):
    for category in ("Networking", "Networking", "Security"):
        question = services.questions.create(
            {
                "question": f"Q in {category}?",
                "options": ["A", "B"],
                "correct_option_id": 0,
                "category": category,
                "difficulty": "easy",
                "status": "ready",
            }
        )
        entry = await services.publishing.publish(question["id"], channel["id"])
        fake_telegram.poll_snapshots[entry["message_id"]] = _snapshot(
            entry["message_id"], [1, 1], correct=0
        )
    await services.analytics.collect(services.telegram)

    by_channel = services.analytics.by_channel()
    assert len(by_channel) == 1
    assert by_channel[0]["polls"] == 3
    assert by_channel[0]["votes"] == 6
    assert by_channel[0]["avg_votes"] == 2.0

    by_category = {c["category"]: c for c in services.analytics.by_category()}
    assert by_category["Networking"]["polls"] == 2
    assert by_category["Security"]["polls"] == 1
    assert by_category["Networking"]["votes"] == 4


async def test_poll_detail_includes_options_and_answers(services, channel, fake_telegram):
    question = services.questions.create(
        {
            "question": "Which protocol uses port 53?",
            "options": ["HTTP", "DNS"],
            "correct_option_id": 1,
            "category": "Networking",
            "difficulty": "easy",
            "status": "ready",
        }
    )
    entry = await services.publishing.publish(question["id"], channel["id"])
    fake_telegram.poll_snapshots[entry["message_id"]] = _snapshot(
        entry["message_id"], [0, 2], correct=1
    )
    services.analytics.record_answer(
        poll_id=entry["poll_id"], user_id=7, username="layla", option_ids=[1]
    )
    await services.analytics.collect(services.telegram)

    detail = services.analytics.poll_detail(entry["poll_id"])
    assert detail is not None
    assert detail["votes"] == 2
    assert detail["options"][1]["pct"] == 100.0
    assert detail["options"][1]["is_correct"] is True
    assert len(detail["answers"]) == 1
    assert detail["answers"][0]["username"] == "layla"
    assert detail["answers"][0]["is_correct"] is True


async def test_activity_series_counts_publications(services, channel, fake_telegram):
    question = services.questions.create(
        {
            "question": "Which protocol uses port 53?",
            "options": ["HTTP", "DNS"],
            "correct_option_id": 1,
            "category": "Networking",
            "difficulty": "easy",
            "status": "ready",
        }
    )
    await services.publishing.publish(question["id"], channel["id"])
    series = services.analytics.activity_series(days=14)
    assert len(series["labels"]) == 14
    assert sum(series["published"]) == 1


async def test_legacy_rows_without_poll_id_join_by_message(
    services, channel, fake_telegram
):
    """Rows published before poll ids were stored must still show their votes."""
    question = services.questions.create(
        {
            "question": "Which protocol uses port 53?",
            "options": ["HTTP", "DNS"],
            "correct_option_id": 1,
            "category": "Networking",
            "difficulty": "easy",
            "status": "ready",
        }
    )
    entry = await services.publishing.publish(question["id"], channel["id"])
    message_id = entry["message_id"]

    # A poll_results row collected earlier, plus a history row that predates the
    # poll_id field: the two must still join on (channel_id, message_id).
    def _insert_legacy(document):
        document["counter"] = 1
        document["results"].append(
            {
                "id": "PR00001",
                "poll_id": "legacy-poll",
                "question_id": question["id"],
                "channel_id": channel["id"],
                "message_id": message_id,
                "is_closed": True,
                "total_voters": 2,
                "options": [
                    {"text": "HTTP", "voter_count": 0},
                    {"text": "DNS", "voter_count": 2},
                ],
                "correct_option_ids": [1],
                "first_collected_at": "2026-01-01T00:00:00Z",
                "collected_at": "2026-01-01T00:00:00Z",
                "trigger": "manual",
            }
        )

    services.storage.poll_results.update(_insert_legacy)
    services.storage.history.update(
        lambda doc: [e.update({"poll_id": None}) for e in doc["history"]]
    )

    rows = services.analytics.per_question()
    assert len(rows) == 1
    assert rows[0]["votes"] == 2
    assert rows[0]["collected"] is True
    assert rows[0]["poll_id"] == "legacy-poll"

    # Re-collecting backfills the recovered poll id onto the history row.
    fake_telegram.poll_snapshots[message_id] = _snapshot(message_id, [0, 2], correct=1)
    await services.analytics.collect(services.telegram)
    stored = services.storage.history.read()["history"]
    assert stored[0]["poll_id"] == f"poll-{message_id}"



async def test_collect_keeps_explanation_telegram_delivered(
    services, channel, fake_telegram
):
    """The dashboard must show the explanation Telegram actually sent."""
    question = services.questions.create(
        {
            "question": "Which protocol uses port 53?",
            "options": ["HTTP", "DNS", "FTP", "SSH"],
            "correct_option_id": 1,
            "explanation": "DNS uses port 53.",
            "category": "Networking",
            "difficulty": "easy",
            "status": "ready",
        }
    )
    entry = await services.publishing.publish(question["id"], channel["id"])
    fake_telegram.poll_snapshots[entry["message_id"]] = _snapshot(
        entry["message_id"], [0, 1, 0, 0], explanation="DNS uses port 53."
    )
    await services.analytics.collect(services.telegram)

    row = services.analytics.per_question()[0]
    assert row["explanation"] == "DNS uses port 53."
    detail = services.analytics.poll_detail(row["poll_id"])
    assert detail["explanation"] == "DNS uses port 53."

    # The stored snapshot keeps the delivered text even if the question changes.
    services.storage.questions.update(
        lambda doc: [
            q.update({"explanation": "changed later"})
            for q in doc["questions"]
            if q["id"] == question["id"]
        ]
    )
    assert services.analytics.per_question()[0]["explanation"] == "DNS uses port 53."


async def test_explanation_falls_back_to_question(services, channel, fake_telegram):
    """Rows collected before explanations were stored still show the question text."""
    question = services.questions.create(
        {
            "question": "Which protocol uses port 53?",
            "options": ["HTTP", "DNS"],
            "correct_option_id": 1,
            "explanation": "DNS uses port 53.",
            "status": "ready",
        }
    )
    entry = await services.publishing.publish(question["id"], channel["id"])
    fake_telegram.poll_snapshots[entry["message_id"]] = _snapshot(
        entry["message_id"], [0, 1]
    )
    await services.analytics.collect(services.telegram)
    assert services.analytics.per_question()[0]["explanation"] == "DNS uses port 53."
