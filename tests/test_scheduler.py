"""Scheduler tests: trigger construction and schedule execution rules."""

from __future__ import annotations

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.scheduler.engine import SchedulerEngine
from app.scheduler.jobs import once_datetime, weekdays_to_cron


def _schedule(services, **overrides):
    channel = services.channels.default_channel()
    if channel is None:
        channel = services.channels.create({"name": "Main", "chat_id": "@main"})
    payload = {
        "channel_id": channel["id"],
        "type": "daily",
        "times": ["20:00"],
        "timezone": "Asia/Riyadh",
        "question_selection": "sequential",
        "questions_per_day": 0,
        "enabled": True,
    }
    payload.update(overrides)
    return services.schedules.create(payload)


def _make_ready(services, count: int = 3):
    created = []
    for index in range(count):
        created.append(
            services.questions.create(
                {
                    "question": f"question {index}",
                    "options": ["a", "b"],
                    "correct_option_id": 0,
                    "status": "ready",
                }
            )
        )
    return created


def test_weekdays_to_cron_maps_zero_to_monday() -> None:
    assert weekdays_to_cron([0, 2, 4]) == "mon,wed,fri"
    assert weekdays_to_cron([6]) == "sun"


def test_weekdays_to_cron_ignores_out_of_range() -> None:
    assert weekdays_to_cron([9, -1, 1]) == "tue"


def test_once_datetime_uses_start_date() -> None:
    schedule = {"times": ["14:30"], "start_date": "2030-05-15"}
    resolved = once_datetime(schedule)
    assert (resolved.year, resolved.month, resolved.day, resolved.hour, resolved.minute) == (
        2030, 5, 15, 14, 30,
    )


def test_once_datetime_without_time_is_none() -> None:
    assert once_datetime({"times": []}) is None


def test_engine_builds_cron_trigger_for_daily(services) -> None:
    schedule = _schedule(services)
    engine = SchedulerEngine(services.runner, services.schedules)
    trigger = engine._build_trigger(schedule)  # noqa: SLF001
    assert isinstance(trigger, CronTrigger)


def test_engine_builds_interval_trigger(services) -> None:
    schedule = _schedule(services, type="interval", times=[], interval_minutes=120)
    engine = SchedulerEngine(services.runner, services.schedules)
    assert isinstance(engine._build_trigger(schedule), IntervalTrigger)  # noqa: SLF001


def test_engine_builds_date_trigger_for_once(services) -> None:
    schedule = _schedule(
        services, type="once", times=["09:00"], start_date="2030-01-01"
    )
    engine = SchedulerEngine(services.runner, services.schedules)
    assert isinstance(engine._build_trigger(schedule), DateTrigger)  # noqa: SLF001


def test_manual_schedule_has_no_trigger(services) -> None:
    schedule = _schedule(services, type="manual", times=[])
    engine = SchedulerEngine(services.runner, services.schedules)
    assert engine._build_trigger(schedule) is None  # noqa: SLF001


def test_engine_registers_only_enabled_schedules(services) -> None:
    _schedule(services, name="on", enabled=True)
    _schedule(services, name="off", enabled=False)
    engine = SchedulerEngine(services.runner, services.schedules)
    assert engine.reload() == 1


async def test_runner_publishes_first_ready_question(services, fake_telegram) -> None:
    _make_ready(services, 3)
    schedule = _schedule(services)
    result = await services.runner.run(schedule["id"])
    assert result["ok"] is True
    assert fake_telegram.sent[0]["question"]["question"] == "question 0"


async def test_runner_publishes_a_batch_per_run(services, fake_telegram) -> None:
    _make_ready(services, 5)
    schedule = _schedule(services, questions_per_run=3)
    result = await services.runner.run(schedule["id"])
    assert result["ok"] is True
    assert result["published"] == 3
    assert len(fake_telegram.sent) == 3
    assert [s["question"]["question"] for s in fake_telegram.sent] == [
        "question 0", "question 1", "question 2",
    ]
    updated = services.schedules.get(schedule["id"])
    assert updated["cursor"] == 3
    assert updated["published_count"] == 3


async def test_runner_waits_between_batch_questions(services, monkeypatch) -> None:
    _make_ready(services, 3)
    schedule = _schedule(services, questions_per_run=2, batch_interval_seconds=4)

    slept: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("app.scheduler.jobs.asyncio.sleep", _fake_sleep)
    await services.runner.run(schedule["id"])

    # Only the gap *between* questions is awaited, not before the first one.
    assert slept == [4]


async def test_runner_batch_does_not_exceed_daily_limit(services, fake_telegram) -> None:
    _make_ready(services, 5)
    schedule = _schedule(services, questions_per_run=3, questions_per_day=2)
    result = await services.runner.run(schedule["id"])
    assert result["published"] == 2
    assert len(fake_telegram.sent) == 2
    second = await services.runner.run(schedule["id"])
    assert second["reason"] == "schedule-daily-limit"


async def test_runner_batch_stops_at_available_questions(services, fake_telegram) -> None:
    _make_ready(services, 2)
    schedule = _schedule(services, questions_per_run=5)
    result = await services.runner.run(schedule["id"])
    assert result["published"] == 2
    assert len(fake_telegram.sent) == 2


async def test_runner_advances_cursor_on_success(services) -> None:
    _make_ready(services, 3)
    schedule = _schedule(services)
    await services.runner.run(schedule["id"])
    await services.runner.run(schedule["id"])
    updated = services.schedules.get(schedule["id"])
    assert updated["cursor"] == 2
    assert updated["published_count"] == 2


async def test_runner_respects_daily_question_limit(services) -> None:
    _make_ready(services, 5)
    schedule = _schedule(services, questions_per_day=1)
    first = await services.runner.run(schedule["id"])
    second = await services.runner.run(schedule["id"])
    assert first["ok"] is True
    assert second["reason"] == "schedule-daily-limit"


async def test_runner_respects_global_daily_limit(services) -> None:
    _make_ready(services, 5)
    services.app_settings.update({"daily_limit": 1})
    schedule = _schedule(services)
    await services.runner.run(schedule["id"])
    second = await services.runner.run(schedule["id"])
    assert second["reason"] == "global-daily-limit"


async def test_runner_reports_no_questions(services) -> None:
    schedule = _schedule(services)
    result = await services.runner.run(schedule["id"])
    assert result["reason"] == "no-questions"


async def test_runner_skips_when_publishing_paused(services) -> None:
    _make_ready(services)
    schedule = _schedule(services)
    services.app_settings.set_publishing_enabled(False)
    assert (await services.runner.run(schedule["id"]))["reason"] == "paused"


async def test_runner_skips_disabled_schedule(services) -> None:
    _make_ready(services)
    schedule = _schedule(services, enabled=False)
    assert (await services.runner.run(schedule["id"]))["reason"] == "disabled"


async def test_runner_consumes_skip_next_flag(services) -> None:
    _make_ready(services)
    schedule = _schedule(services)

    def _set_skip(document):
        for item in document["schedules"]:
            item["skip_next"] = True

    services.storage.schedules.update(_set_skip)
    result = await services.runner.run(schedule["id"])
    assert result["reason"] == "skipped"
    assert not services.schedules.get(schedule["id"]).get("skip_next")


async def test_runner_respects_date_window(services) -> None:
    _make_ready(services)
    schedule = _schedule(services, start_date="2099-01-01")
    assert (await services.runner.run(schedule["id"]))["reason"] == "out-of-window"


async def test_runner_selects_by_category(services) -> None:
    services.questions.create(
        {
            "question": "network q",
            "options": ["a", "b"],
            "correct_option_id": 0,
            "category": "Networking",
            "status": "ready",
        }
    )
    services.questions.create(
        {
            "question": "history q",
            "options": ["a", "b"],
            "correct_option_id": 0,
            "category": "History",
            "status": "ready",
        }
    )
    schedule = _schedule(
        services, question_selection="category", category_filter="History"
    )
    await services.runner.run(schedule["id"])
    assert services.publishing.history()[0]["question_id"] == "Q0002"


def test_schedule_service_validates_channel(services) -> None:
    import pytest

    from app.storage.validators import ValidationError

    with pytest.raises(ValidationError, match="غير موجودة"):
        services.schedules.create({"channel_id": "CH404", "type": "daily", "times": ["08:00"]})


async def test_engine_actually_fires_a_job(services) -> None:
    """Drive the real AsyncIOScheduler and confirm a firing publishes."""
    import asyncio

    from apscheduler.triggers.interval import IntervalTrigger

    _make_ready(services, 2)
    schedule = _schedule(services)

    engine = SchedulerEngine(services.runner, services.schedules)
    engine.start()
    try:
        engine._scheduler.add_job(  # noqa: SLF001
            engine._run_job,  # noqa: SLF001
            trigger=IntervalTrigger(seconds=1),
            args=[schedule["id"]],
            id="firing-probe",
        )
        await asyncio.sleep(2.5)
    finally:
        engine.shutdown()

    assert len(services.publishing.history()) >= 1
    assert services.publishing.history()[0]["status"] == "success"


async def test_engine_survives_a_failing_job(services, monkeypatch) -> None:
    """A raising job must be swallowed so the scheduler keeps running."""
    import asyncio

    from apscheduler.triggers.interval import IntervalTrigger

    async def _boom(schedule_id: str):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(services.runner, "run", _boom)
    schedule = _schedule(services)

    engine = SchedulerEngine(services.runner, services.schedules)
    engine.start()
    try:
        engine._scheduler.add_job(  # noqa: SLF001
            engine._run_job,  # noqa: SLF001
            trigger=IntervalTrigger(seconds=1),
            args=[schedule["id"]],
            id="boom-probe",
        )
        await asyncio.sleep(2.5)
        assert engine._scheduler.running  # noqa: SLF001
    finally:
        engine.shutdown()
