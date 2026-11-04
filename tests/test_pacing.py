"""Rate-limit handling: pacing between sends and flood-wait retries.

Regression cover for the bug where a 55-question manual bulk publish delivered
only ~20 messages: the remaining sends hit Telegram's per-chat flood limit and
were logged as failures instead of being waited out and retried.
"""

from __future__ import annotations

import datetime

import pytest
from telegram.error import Forbidden, RetryAfter

from app.services.publishing_service import MAX_FLOOD_RETRIES, STATUS_FAILED, STATUS_SUCCESS
from app.telegram.telegram_client import PublishResult, retry_after_seconds
from app.storage.validators import ValidationError


@pytest.fixture
def questions(services):
    return [
        services.questions.create(
            {
                "question": f"Question {i}?",
                "options": ["A", "B"],
                "correct_option_id": 0,
                "status": "ready",
            }
        )
        for i in range(4)
    ]


@pytest.fixture
def channel(services):
    return services.channels.create({"name": "Main", "chat_id": "@main_channel"})


# --- retry_after parsing ---------------------------------------------------


def test_retry_after_reads_ptb_attribute() -> None:
    assert retry_after_seconds(RetryAfter(30)) == 30


def test_retry_after_reads_timedelta() -> None:
    exc = RetryAfter(datetime.timedelta(seconds=45))
    assert retry_after_seconds(exc) == 45


def test_retry_after_reads_pyrogram_value() -> None:
    class FloodWait(Exception):
        value = 17

    assert retry_after_seconds(FloodWait("flood")) == 17


def test_retry_after_parses_message_text() -> None:
    exc = Exception("Flood control exceeded. Retry in 32 seconds")
    assert retry_after_seconds(exc) == 32


def test_retry_after_is_none_for_other_errors() -> None:
    assert retry_after_seconds(ValueError("boom")) is None


def test_classify_exposes_flood_wait() -> None:
    from app.telegram.telegram_client import TelegramClient

    result = TelegramClient._classify(RetryAfter(30))
    assert result.flood_wait is True
    assert result.retryable is True
    assert result.retry_after == 30

    forbidden = TelegramClient._classify(Forbidden("kicked"))
    assert forbidden.flood_wait is False
    assert forbidden.retry_after is None


# --- pacing between sends --------------------------------------------------


async def test_bulk_publish_waits_between_questions(
    services, questions, channel, fake_telegram, fake_sleeper
) -> None:
    ids = [q["id"] for q in questions]
    results = await services.publishing.publish_many(ids, channel["id"])

    assert [r["status"] for r in results] == [STATUS_SUCCESS] * 4
    assert len(fake_telegram.sent) == 4
    # One gap between each consecutive pair: 3 gaps for 4 questions.
    assert fake_sleeper.waits == [4, 4, 4]


async def test_pacing_interval_is_configurable(
    services, questions, channel, fake_sleeper
) -> None:
    services.app_settings.update({"pacing": {"interval_seconds": 10, "max_wait_seconds": 120}})
    await services.publishing.publish_many([q["id"] for q in questions], channel["id"])
    assert fake_sleeper.waits == [10, 10, 10]


async def test_zero_interval_sends_without_waiting(
    services, questions, channel, fake_sleeper
) -> None:
    services.app_settings.update({"pacing": {"interval_seconds": 0, "max_wait_seconds": 120}})
    await services.publishing.publish_many([q["id"] for q in questions], channel["id"])
    assert fake_sleeper.waits == []


async def test_publish_many_reports_each_result_in_order(
    services, questions, channel
) -> None:
    ids = [q["id"] for q in questions]
    results = await services.publishing.publish_many(ids, channel["id"])
    assert [r["question_id"] for r in results] == ids
    assert all("entry_id" in r for r in results)


# --- flood-wait retry ------------------------------------------------------


async def test_flood_wait_is_honoured_and_retried(
    services, questions, channel, fake_telegram, fake_sleeper
) -> None:
    # Telegram asks for 30s on the first attempt; the send must succeed after.
    fake_telegram.transient_failure = RetryAfter(30)
    fake_telegram.fail_times = 1

    entry = await services.publishing.publish(questions[0]["id"], channel["id"])

    assert entry["status"] == STATUS_SUCCESS
    assert entry["message_id"] is not None
    assert 30 in fake_sleeper.waits


async def test_flood_wait_gives_up_after_max_retries(
    services, questions, channel, fake_telegram, fake_sleeper
) -> None:
    fake_telegram.transient_failure = RetryAfter(5)
    fake_telegram.fail_times = MAX_FLOOD_RETRIES + 5

    entry = await services.publishing.publish(questions[0]["id"], channel["id"])

    assert entry["status"] == STATUS_FAILED
    assert entry["error_code"] == "RetryAfter"
    assert fake_telegram.attempts == MAX_FLOOD_RETRIES + 1
    assert fake_sleeper.waits == [5] * MAX_FLOOD_RETRIES


async def test_flood_wait_longer_than_cap_fails_fast(
    services, questions, channel, fake_telegram, fake_sleeper
) -> None:
    services.app_settings.update({"pacing": {"interval_seconds": 4, "max_wait_seconds": 60}})
    fake_telegram.transient_failure = RetryAfter(600)
    fake_telegram.fail_times = 5

    entry = await services.publishing.publish(questions[0]["id"], channel["id"])

    # A wait beyond the cap must not hang the request or be retried immediately.
    assert entry["status"] == STATUS_FAILED
    assert fake_telegram.attempts == 1
    assert fake_sleeper.waits == []


async def test_flood_wait_records_an_audit_note(
    services, questions, channel, fake_telegram
) -> None:
    fake_telegram.transient_failure = RetryAfter(20)
    fake_telegram.fail_times = 1
    await services.publishing.publish(questions[0]["id"], channel["id"])

    events = [e for e in services.audit.recent(50) if e["action"] == "publishing.flood_wait"]
    assert events and "20" in events[0]["message"]


async def test_non_flood_failure_is_not_retried(
    services, questions, channel, fake_telegram, fake_sleeper
) -> None:
    fake_telegram.fail_with = Forbidden("bot was kicked from the supergroup chat")
    entry = await services.publishing.publish(questions[0]["id"], channel["id"])

    assert entry["status"] == STATUS_FAILED
    assert fake_telegram.attempts == 1
    assert fake_sleeper.waits == []


async def test_bulk_publish_keeps_going_after_one_failure(
    services, questions, channel, fake_telegram
) -> None:
    # Every send fails with a permanent error: all four must still be attempted
    # and reported, none silently dropped.
    fake_telegram.fail_with = Forbidden("kicked")
    results = await services.publishing.publish_many(
        [q["id"] for q in questions], channel["id"]
    )
    assert len(results) == 4
    assert all(r["status"] == STATUS_FAILED for r in results)
    assert fake_telegram.attempts == 4


# --- settings --------------------------------------------------------------


def test_pacing_defaults_are_present(services) -> None:
    interval, max_wait = services.publishing.pacing()
    assert interval == 4
    assert max_wait == 120


def test_pacing_settings_are_editable(services) -> None:
    services.app_settings.update({"pacing": {"interval_seconds": 7, "max_wait_seconds": 90}})
    assert services.publishing.pacing() == (7, 90)


def test_negative_pacing_is_rejected(services) -> None:
    with pytest.raises(ValidationError):
        services.app_settings.update({"pacing": {"interval_seconds": -1, "max_wait_seconds": 10}})


def test_pacing_survives_partial_payload(services) -> None:
    services.app_settings.update({"pacing": {"interval_seconds": 3}})
    interval, max_wait = services.publishing.pacing()
    assert interval == 3
    assert max_wait == 120


# --- response contract -----------------------------------------------------


async def test_publish_many_result_shape(services, questions, channel) -> None:
    results = await services.publishing.publish_many([questions[0]["id"]], channel["id"])
    assert set(results[0]) == {"question_id", "status", "entry_id", "error"}


def test_publish_result_flood_flag_defaults_false() -> None:
    assert PublishResult(ok=True).flood_wait is False
