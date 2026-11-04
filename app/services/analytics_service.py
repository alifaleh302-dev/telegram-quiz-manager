"""Engagement analytics for published quizzes.

Telegram only reveals poll counts through ``stopPoll`` (which also closes the
poll) and only reports per-user answers for non-anonymous polls. This service
therefore works from two sources:

* **snapshots** — one row per poll, refreshed whenever results are collected;
  the source of the vote counts shown per question and per channel.
* **live answers** — ``poll_answer`` updates, stored per user when the poll is
  not anonymous; the source of unique-voter and per-day figures.

Nothing here talks to the network: the caller passes in whatever
:meth:`TelegramClient.fetch_poll_results` returned.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.services.audit_service import AuditService
from app.services.channel_service import ChannelService
from app.services.question_service import QuestionService
from app.storage.json_manager import Storage, utc_now_iso


def _day(iso: str) -> str:
    return (iso or "")[:10]


class AnalyticsService:
    def __init__(
        self,
        storage: Storage,
        audit: AuditService,
        questions: QuestionService,
        channels: ChannelService,
    ) -> None:
        self._storage = storage
        self._audit = audit
        self._questions = questions
        self._channels = channels

    # --- writes ------------------------------------------------------------
    def record_snapshot(
        self,
        *,
        question_id: str,
        channel_id: str,
        message_id: int | None,
        snapshot: Any,
        trigger: str = "manual",
    ) -> dict[str, Any]:
        """Upsert the latest vote counts for one poll."""
        now = utc_now_iso()
        options = [
            {
                "text": option.get("text", ""),
                "voter_count": int(option.get("voter_count", 0)),
            }
            for option in snapshot.options
        ]
        correct = [int(i) for i in (snapshot.correct_option_ids or [])]
        # The explanation as Telegram actually sent it — kept so the dashboard can
        # show what subscribers saw, even if the question is edited later.
        explanation = (getattr(snapshot, "explanation", "") or "").strip()
        if not correct:
            question = self._questions.get(question_id) or {}
            if question.get("correct_option_id") is not None:
                correct = [int(question["correct_option_id"])]

        def _mutate(document: dict[str, Any]) -> dict[str, Any]:
            for row in document["results"]:
                if row["poll_id"] == snapshot.poll_id:
                    row.update(
                        {
                            "question_id": question_id,
                            "channel_id": channel_id,
                            "message_id": message_id,
                            "is_closed": bool(snapshot.is_closed),
                            "total_voters": int(snapshot.total_voters),
                            "options": options,
                            "correct_option_ids": correct,
                            "explanation": explanation,
                            "collected_at": now,
                            "trigger": trigger,
                        }
                    )
                    return row
            document["counter"] = int(document.get("counter", 0)) + 1
            row = {
                "id": f"PR{document['counter']:05d}",
                "poll_id": snapshot.poll_id,
                "question_id": question_id,
                "channel_id": channel_id,
                "message_id": message_id,
                "is_closed": bool(snapshot.is_closed),
                "total_voters": int(snapshot.total_voters),
                "options": options,
                "correct_option_ids": correct,
                "explanation": explanation,
                "first_collected_at": now,
                "collected_at": now,
                "trigger": trigger,
            }
            document["results"].insert(0, row)
            return row

        row = self._storage.poll_results.update(_mutate)
        self._backfill_history_poll_id(channel_id, message_id, snapshot.poll_id)
        return row

    def _backfill_history_poll_id(
        self, channel_id: str, message_id: int | None, poll_id: str | None
    ) -> None:
        """Stamp the poll id onto the history row published before it was known."""
        if not poll_id or not message_id:
            return

        def _mutate(document: dict[str, Any]) -> None:
            for entry in document["history"]:
                if (
                    entry.get("poll_id") is None
                    and entry.get("channel_id") == channel_id
                    and entry.get("message_id") == message_id
                ):
                    entry["poll_id"] = poll_id

        self._storage.history.update(_mutate)

    def record_answer(
        self,
        *,
        poll_id: str,
        user_id: int | None,
        username: str | None,
        option_ids: list[int],
        is_anonymous: bool = False,
    ) -> dict[str, Any] | None:
        """Store the latest answer of one user (non-anonymous polls only)."""
        if is_anonymous or user_id is None:
            return None

        entry = self._history_entry_for_poll(poll_id)
        question_id = entry.get("question_id", "") if entry else ""
        channel_id = entry.get("channel_id", "") if entry else ""
        question = self._questions.get(question_id) if question_id else None
        correct_id = question.get("correct_option_id") if question else None
        is_correct = None
        if correct_id is not None and option_ids:
            is_correct = int(correct_id) in option_ids

        now = utc_now_iso()

        def _mutate(document: dict[str, Any]) -> dict[str, Any]:
            for row in document["answers"]:
                if row["poll_id"] == poll_id and row["user_id"] == user_id:
                    row.update(
                        {
                            "option_ids": option_ids,
                            "username": username,
                            "is_correct": is_correct,
                            "answered_at": now,
                        }
                    )
                    return row
            document["counter"] = int(document.get("counter", 0)) + 1
            row = {
                "id": f"PA{document['counter']:05d}",
                "poll_id": poll_id,
                "question_id": question_id,
                "channel_id": channel_id,
                "user_id": user_id,
                "username": username,
                "option_ids": option_ids,
                "is_correct": is_correct,
                "answered_at": now,
            }
            document["answers"].insert(0, row)
            return row

        return self._storage.poll_answers.update(_mutate)

    def record_member_count(self, channel_id: str, count: int | None) -> None:
        if count is None:
            return

        def _mutate(document: dict[str, Any]) -> None:
            for channel in document["channels"]:
                if channel["id"] == channel_id:
                    channel["member_count"] = int(count)
                    channel["member_count_at"] = utc_now_iso()

        self._storage.channels.update(_mutate)

    # --- lookups -----------------------------------------------------------
    def _history(self) -> list[dict[str, Any]]:
        return self._storage.history.read()["history"]

    def _results(self) -> list[dict[str, Any]]:
        return self._storage.poll_results.read()["results"]

    def _answers(self) -> list[dict[str, Any]]:
        return self._storage.poll_answers.read()["answers"]

    def _history_entry_for_poll(self, poll_id: str) -> dict[str, Any] | None:
        return next(
            (
                entry
                for entry in self._history()
                if entry.get("poll_id") == poll_id
                and entry.get("status") == "success"
            ),
            None,
        )

    def published_polls(self) -> list[dict[str, Any]]:
        """Every successfully published poll, newest first, with its snapshot."""
        by_poll = {row["poll_id"]: row for row in self._results() if row.get("poll_id")}
        by_message = {
            (row["channel_id"], row["message_id"]): row
            for row in self._results()
            if row.get("message_id")
        }
        polls: list[dict[str, Any]] = []
        for entry in self._history():
            if entry.get("status") != "success" or not entry.get("message_id"):
                continue
            snapshot = by_poll.get(entry.get("poll_id") or "")
            if snapshot is None:
                # Rows published before the poll id was captured.
                snapshot = by_message.get((entry["channel_id"], entry["message_id"]))
            polls.append(
                {
                    "poll_id": entry.get("poll_id") or (snapshot["poll_id"] if snapshot else None),
                    "question_id": entry["question_id"],
                    "channel_id": entry["channel_id"],
                    "message_id": entry["message_id"],
                    "published_at": entry.get("published_at"),
                    "snapshot": snapshot,
                }
            )
        return polls

    # --- aggregations ------------------------------------------------------
    def _members(self) -> dict[str, int]:
        return {
            c["id"]: int(c.get("member_count") or 0)
            for c in self._channels.list_channels()
        }

    def per_question(
        self, *, channel_id: str = "", limit: int | None = None
    ) -> list[dict[str, Any]]:
        """One row per published question, with vote breakdown and participation."""
        channel_names = {c["id"]: c.get("name", c["id"]) for c in self._channels.list_channels()}
        members = self._members()
        rows: list[dict[str, Any]] = []

        for poll in self.published_polls():
            if channel_id and poll["channel_id"] != channel_id:
                continue
            question = self._questions.get(poll["question_id"]) or {}
            snapshot = poll["snapshot"]
            total_members = members.get(poll["channel_id"], 0)
            votes = int(snapshot["total_voters"]) if snapshot else 0
            options: list[dict[str, Any]] = []
            correct_pct: float | None = None
            if snapshot:
                correct_ids = set(snapshot.get("correct_option_ids") or [])
                if not correct_ids and question.get("correct_option_id") is not None:
                    correct_ids = {int(question["correct_option_id"])}
                for index, option in enumerate(snapshot.get("options", [])):
                    count = int(option.get("voter_count", 0))
                    options.append(
                        {
                            "text": option.get("text", ""),
                            "voter_count": count,
                            "pct": round(count / votes * 100, 1) if votes else 0.0,
                            "is_correct": index in correct_ids,
                        }
                    )
                if votes:
                    correct_count = sum(
                        o["voter_count"] for o in options if o["is_correct"]
                    )
                    correct_pct = round(correct_count / votes * 100, 1)

            rows.append(
                {
                    "poll_id": poll["poll_id"],
                    "question_id": poll["question_id"],
                    "question_text": question.get("question", poll["question_id"]),
                    # Prefer the text Telegram actually delivered; fall back to the
                    # current question text for rows collected before it was stored.
                    "explanation": (
                        (snapshot or {}).get("explanation")
                        or question.get("explanation")
                        or ""
                    ),
                    "category": question.get("category", ""),
                    "difficulty": question.get("difficulty", ""),
                    "channel_id": poll["channel_id"],
                    "channel_name": channel_names.get(poll["channel_id"], poll["channel_id"]),
                    "message_id": poll["message_id"],
                    "published_at": poll["published_at"],
                    "votes": votes,
                    "total_members": total_members,
                    "participation_pct": round(votes / total_members * 100, 1)
                    if total_members
                    else None,
                    "correct_pct": correct_pct,
                    "is_closed": bool(snapshot["is_closed"]) if snapshot else False,
                    "collected": snapshot is not None,
                    "collected_at": snapshot["collected_at"] if snapshot else None,
                    "options": options,
                }
            )

        if limit is not None:
            rows = rows[:limit]
        return rows

    def by_channel(self) -> list[dict[str, Any]]:
        members = self._members()
        grouped: dict[str, dict[str, Any]] = {}
        for row in self.per_question():
            bucket = grouped.setdefault(
                row["channel_id"],
                {
                    "channel_id": row["channel_id"],
                    "name": row["channel_name"],
                    "polls": 0,
                    "collected": 0,
                    "votes": 0,
                    "correct_sum": 0.0,
                    "correct_n": 0,
                    "total_members": members.get(row["channel_id"], 0),
                },
            )
            bucket["polls"] += 1
            bucket["votes"] += row["votes"]
            if row["collected"]:
                bucket["collected"] += 1
            if row["correct_pct"] is not None:
                bucket["correct_sum"] += row["correct_pct"]
                bucket["correct_n"] += 1

        out = []
        for bucket in grouped.values():
            polls = bucket["polls"]
            out.append(
                {
                    "channel_id": bucket["channel_id"],
                    "name": bucket["name"],
                    "polls": polls,
                    "collected": bucket["collected"],
                    "votes": bucket["votes"],
                    "avg_votes": round(bucket["votes"] / polls, 1) if polls else 0.0,
                    "correct_pct": round(bucket["correct_sum"] / bucket["correct_n"], 1)
                    if bucket["correct_n"]
                    else None,
                    "total_members": bucket["total_members"],
                    "participation_pct": round(bucket["votes"] / polls / bucket["total_members"] * 100, 1)
                    if polls and bucket["total_members"]
                    else None,
                }
            )
        out.sort(key=lambda r: r["votes"], reverse=True)
        return out

    def by_category(self) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for row in self.per_question():
            category = row["category"] or "غير مصنّف"
            bucket = grouped.setdefault(
                category, {"category": category, "polls": 0, "votes": 0, "correct_sum": 0.0, "correct_n": 0}
            )
            bucket["polls"] += 1
            bucket["votes"] += row["votes"]
            if row["correct_pct"] is not None:
                bucket["correct_sum"] += row["correct_pct"]
                bucket["correct_n"] += 1
        out = [
            {
                "category": b["category"],
                "polls": b["polls"],
                "votes": b["votes"],
                "avg_votes": round(b["votes"] / b["polls"], 1) if b["polls"] else 0.0,
                "correct_pct": round(b["correct_sum"] / b["correct_n"], 1)
                if b["correct_n"]
                else None,
            }
            for b in grouped.values()
        ]
        out.sort(key=lambda r: r["votes"], reverse=True)
        return out

    def activity_series(self, days: int = 14) -> dict[str, Any]:
        """Publications and recorded votes per day for the last ``days`` days."""
        today = datetime.now(UTC).date()
        start = today - timedelta(days=days - 1)
        labels = [(start + timedelta(days=i)).isoformat() for i in range(days)]
        index = {label: i for i, label in enumerate(labels)}

        published = [0] * days
        for entry in self._history():
            if entry.get("status") != "success":
                continue
            key = _day(entry.get("published_at", ""))
            if key in index:
                published[index[key]] += 1

        votes = [0] * days
        for answer in self._answers():
            key = _day(answer.get("answered_at", ""))
            if key in index:
                votes[index[key]] += 1

        return {"labels": labels, "published": published, "votes": votes}

    def overview(self) -> dict[str, Any]:
        rows = self.per_question()
        answers = self._answers()
        polls = len(rows)
        collected = sum(1 for r in rows if r["collected"])
        total_votes = sum(r["votes"] for r in rows)
        correct_rows = [r["correct_pct"] for r in rows if r["correct_pct"] is not None]
        answered = [a for a in answers if a.get("option_ids")]
        correct_answers = [a for a in answered if a.get("is_correct")]
        return {
            "polls": polls,
            "collected": collected,
            "total_votes": total_votes,
            "avg_votes": round(total_votes / polls, 1) if polls else 0.0,
            "avg_correct_pct": round(sum(correct_rows) / len(correct_rows), 1)
            if correct_rows
            else None,
            "unique_voters": len({a["user_id"] for a in answered}),
            "live_answers": len(answered),
            "live_correct_pct": round(len(correct_answers) / len(answered) * 100, 1)
            if answered
            else None,
            "channels": len({r["channel_id"] for r in rows}),
            "has_live_tracking": any(
                bool(c.get("poll_options", {}).get("anonymous") is False)
                for c in self._channels.list_channels()
            ),
        }

    def answers_for_poll(self, poll_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = [a for a in self._answers() if a["poll_id"] == poll_id]
        rows.sort(key=lambda r: r.get("answered_at", ""), reverse=True)
        return rows[:limit]

    def poll_detail(self, poll_id: str) -> dict[str, Any] | None:
        for row in self.per_question():
            if row["poll_id"] == poll_id:
                row["answers"] = self.answers_for_poll(poll_id)
                return row
        return None

    # --- collection --------------------------------------------------------
    async def collect(
        self,
        client: Any,
        *,
        poll_ids: list[str] | None = None,
        channel_id: str = "",
        trigger: str = "manual",
        refresh_members: bool = True,
        actor: str = "system",
    ) -> dict[str, Any]:
        """Read vote counts for published polls and store snapshots.

        Note that Telegram only exposes counts via ``stopPoll``, which closes
        the poll; already-closed polls therefore cannot be read here and are
        reported as ``skipped`` (their counts arrive through a ``poll`` update
        instead, handled by the bot listener).
        """
        wanted = set(poll_ids or [])
        targets = [
            poll
            for poll in self.published_polls()
            if (not wanted or poll["poll_id"] in wanted)
            and (not channel_id or poll["channel_id"] == channel_id)
            and poll.get("message_id")
        ]

        collected: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        for poll in targets:
            snapshot = await client.fetch_poll_results(
                self._channel_chat_id(poll["channel_id"]), int(poll["message_id"])
            )
            if snapshot.ok:
                row = self.record_snapshot(
                    question_id=poll["question_id"],
                    channel_id=poll["channel_id"],
                    message_id=poll["message_id"],
                    snapshot=snapshot,
                    trigger=trigger,
                )
                collected.append(row)
            elif snapshot.error_code == "BadRequest" and "stopped" in (snapshot.error or ""):
                # Already closed: counts were captured when it closed.
                skipped.append({"poll_id": poll["poll_id"], "reason": "closed"})
            else:
                errors.append(
                    {
                        "poll_id": poll["poll_id"],
                        "error": snapshot.error,
                        "error_code": snapshot.error_code,
                    }
                )

        if refresh_members:
            for channel in self._channels.list_channels():
                count = await client.get_member_count(channel["chat_id"])
                self.record_member_count(channel["id"], count)

        summary = {
            "ok": not errors,
            "collected": len(collected),
            "skipped": len(skipped),
            "errors": len(errors),
            "details": errors,
        }
        self._audit.record(
            "analytics.collected",
            actor=actor,
            level="warning" if errors else "info",
            message=(
                f"جمع النتائج: {len(collected)} محدّث، {len(skipped)} متخطّى، "
                f"{len(errors)} خطأ"
            ),
        )
        return summary

    def _channel_chat_id(self, channel_id: str) -> str:
        channel = self._channels.get(channel_id)
        return channel["chat_id"] if channel else channel_id

