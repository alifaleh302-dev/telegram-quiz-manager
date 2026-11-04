"""Publishing lifecycle tests, including duplicate and crash-safety rules."""

from __future__ import annotations

import pytest

from app.services.publishing_service import STATUS_FAILED, STATUS_SUCCESS, STATUS_UNKNOWN
from app.storage.validators import ValidationError


@pytest.fixture
def ready_question(services):
    return services.questions.create(
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


@pytest.fixture
def channel(services):
    return services.channels.create({"name": "Main", "chat_id": "@main_channel"})


async def test_publish_marks_question_published_only_after_success(
    services, ready_question, channel, fake_telegram
) -> None:
    entry = await services.publishing.publish(ready_question["id"], channel["id"])
    assert entry["status"] == STATUS_SUCCESS
    assert entry["message_id"] == fake_telegram.sent[0]["message_id"]
    assert services.questions.get(ready_question["id"])["status"] == "published"


async def test_publish_timeout_is_recorded_as_unknown(
    services, ready_question, channel, fake_telegram
) -> None:
    from telegram.error import TimedOut

    fake_telegram.fail_with = TimedOut()
    entry = await services.publishing.publish(ready_question["id"], channel["id"])
    # A timeout may happen after delivery, so the outcome is unresolved rather
    # than a plain failure, and the question stays ready for a human decision.
    assert entry["status"] == STATUS_UNKNOWN
    assert entry["retryable"] is True
    assert "راجع القناة" in entry["error"]
    assert services.questions.get(ready_question["id"])["status"] == "ready"


async def test_publish_network_error_is_recorded_as_unknown(
    services, ready_question, channel, fake_telegram
) -> None:
    from telegram.error import NetworkError

    fake_telegram.fail_with = NetworkError("connection reset")
    entry = await services.publishing.publish(ready_question["id"], channel["id"])
    assert entry["status"] == STATUS_UNKNOWN
    assert entry["retryable"] is True


async def test_publish_blocks_permanent_error_from_retry(
    services, ready_question, channel, fake_telegram
) -> None:
    from telegram.error import Forbidden

    fake_telegram.fail_with = Forbidden("bot is not a member")
    entry = await services.publishing.publish(ready_question["id"], channel["id"])
    assert entry["status"] == STATUS_FAILED
    assert entry["retryable"] is False


async def test_publishing_paused_blocks_publish(services, ready_question, channel) -> None:
    services.app_settings.set_publishing_enabled(False)
    with pytest.raises(ValidationError, match="النشر متوقف"):
        await services.publishing.publish(ready_question["id"], channel["id"])


async def test_publish_now_ignores_pause_when_forced(
    services, ready_question, channel
) -> None:
    services.app_settings.set_publishing_enabled(False)
    entry = await services.publishing.publish(
        ready_question["id"], channel["id"], respect_pause=False
    )
    assert entry["status"] == STATUS_SUCCESS


async def test_question_is_not_republished(services, ready_question, channel) -> None:
    await services.publishing.publish(ready_question["id"], channel["id"])
    with pytest.raises(ValidationError, match="نُشر مسبقاً"):
        await services.publishing.publish(ready_question["id"], channel["id"])


async def test_disabled_channel_rejected(services, ready_question, channel) -> None:
    services.channels.set_enabled(channel["id"], False)
    with pytest.raises(ValidationError, match="معطّلة"):
        await services.publishing.publish(ready_question["id"], channel["id"])


async def test_missing_question_or_channel_rejected(services, ready_question, channel) -> None:
    with pytest.raises(ValidationError, match="غير موجود"):
        await services.publishing.publish("Q9999", channel["id"])
    with pytest.raises(ValidationError, match="غير موجودة"):
        await services.publishing.publish(ready_question["id"], "CH999")


async def test_retry_succeeds_after_transient_failure(
    services, ready_question, channel, fake_telegram
) -> None:
    from telegram.error import RetryAfter

    # RetryAfter means Telegram rejected the call, so the post did not happen.
    fake_telegram.fail_with = RetryAfter(5)
    entry = await services.publishing.publish(ready_question["id"], channel["id"])
    assert entry["status"] == STATUS_FAILED
    assert entry["retryable"] is True

    fake_telegram.fail_with = None
    retried = await services.publishing.retry(entry["id"])
    assert retried["status"] == STATUS_SUCCESS
    assert retried["attempts"] == 2
    assert services.questions.get(ready_question["id"])["status"] == "published"


async def test_retry_recovers_an_unknown_entry(services, ready_question, channel, fake_telegram) -> None:
    from telegram.error import TimedOut

    fake_telegram.fail_with = TimedOut()
    entry = await services.publishing.publish(ready_question["id"], channel["id"])
    assert entry["status"] == STATUS_UNKNOWN

    fake_telegram.fail_with = None
    retried = await services.publishing.retry(entry["id"])
    assert retried["status"] == STATUS_SUCCESS
    assert services.questions.get(ready_question["id"])["status"] == "published"


async def test_retry_refuses_successful_entry(services, ready_question, channel) -> None:
    entry = await services.publishing.publish(ready_question["id"], channel["id"])
    with pytest.raises(ValidationError, match="نجحت مسبقاً"):
        await services.publishing.retry(entry["id"])


def test_reconcile_marks_stale_pending_as_unknown(services, ready_question, channel) -> None:
    services.publishing._claim(  # noqa: SLF001
        ready_question["id"], channel["id"], None, "test"
    )

    def _age(document):
        for entry in document["history"]:
            entry["updated_at"] = "2020-01-01T00:00:00Z"

    services.storage.history.update(_age)
    reconciled = services.publishing.reconcile_pending()
    assert reconciled == 1
    assert services.publishing.history()[0]["status"] == STATUS_UNKNOWN


def test_reconcile_leaves_fresh_pending_alone(services, ready_question, channel) -> None:
    services.publishing._claim(  # noqa: SLF001
        ready_question["id"], channel["id"], None, "test"
    )
    assert services.publishing.reconcile_pending() == 0


async def test_history_records_channel_and_question(
    services, ready_question, channel
) -> None:
    await services.publishing.publish(ready_question["id"], channel["id"])
    entry = services.publishing.history()[0]
    assert entry["question_id"] == ready_question["id"]
    assert entry["channel_id"] == channel["id"]
    assert entry["trigger"] == "manual"
    assert entry["attempts"] == 1


async def test_stats_aggregate_history(services, ready_question, channel, fake_telegram) -> None:
    await services.publishing.publish(ready_question["id"], channel["id"])
    second = services.questions.create(
        {
            "question": "second",
            "options": ["a", "b"],
            "correct_option_id": 0,
            "status": "ready",
        }
    )
    from telegram.error import Forbidden

    fake_telegram.fail_with = Forbidden("nope")
    await services.publishing.publish(second["id"], channel["id"])
    stats = services.publishing.stats()
    assert stats["success"] == 1
    assert stats["failed"] == 1
    assert stats["today"] == 2
