#!/usr/bin/env python3
"""Load test for the Student Mini App read paths.

Simulates N students opening the Mini App at once and hitting the read
endpoints (dashboard, exams list, exam detail, eligibility, notifications)
against a seeded database, then reports latency percentiles and throughput.

Why a custom runner instead of locust/ab
----------------------------------------
The point is to measure the *real* server-side cost, so the test drives the
actual FastAPI app through Starlette's TestClient. TestClient runs the ASGI app
on one event-loop thread inside a blocking portal; every worker thread's
request is marshalled onto that single loop. The endpoints are ``async def`` but
their database work is synchronous and is *not* offloaded to a threadpool, so it
blocks the loop exactly as it does under Uvicorn — which is the behaviour under
test. Latency therefore already reflects the serialisation, not just the DB.

Usage:
    python tools/loadtest/mini_app_load.py --students 300 --concurrency 50
"""

from __future__ import annotations

import argparse
import contextlib
import random
import statistics
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from starlette.testclient import TestClient  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.core.telegram_auth import create_student_token  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services.container import build_services  # noqa: E402

BOT_TOKEN = "123456:LOADTESTTOKENabcdefghijklmnopqrstuvwx"
ADMIN_PASSWORD = "LoadTestPass123!"


class FakeTelegramUser:
    """Attribute-compatible stand-in for ``TelegramUser`` (seeding only)."""

    def __init__(self, user_id: int, first_name: str, username: str) -> None:
        self.user_id = user_id
        self.first_name = first_name
        self.last_name = ""
        self.username = username
        self.language_code = "ar"
        self.photo_url = ""


def seed(services: Any, *, students: int, exams: int, questions_per_exam: int) -> list[str]:
    """Populate a realistic dataset; returns one signed student token each."""
    # Question bank first, so exam snapshots can reference real questions.
    for i in range(questions_per_exam * exams):
        services.questions.create(
            {
                "question": f"سؤال رقم {i}؟",
                "options": ["أ", "ب", "ج", "د"],
                "correct_option_id": i % 4,
                "explanation": "شرح الإجابة",
                "category": "شبكات",
                "difficulty": "easy",
                "status": "ready",
            }
        )
    bank = [q["id"] for q in services.questions.list_questions()]

    exam_ids: list[str] = []
    for e in range(exams):
        exam = services.exams.create(
            {
                "title": f"اختبار رقم {e + 1}",
                "description": "اختبار تحميل",
                "category": random.choice(["شبكات", "أمن", "برمجة", "قواعد بيانات"]),
                "duration_minutes": 15,
                "max_attempts": 2,
                "access_mode": "always",
                "show_solutions_after_submit": True,
            }
        )
        exam_ids.append(exam["id"])
        chunk = bank[e * questions_per_exam : (e + 1) * questions_per_exam]
        services.exams.set_questions(exam["id"], chunk)
        services.exams.publish(exam["id"])

    tokens: list[str] = []
    for s in range(students):
        tg_id = 900000 + s
        services.students.touch(FakeTelegramUser(tg_id, f"طالب{s}", f"student{s}"))
        tokens.append(create_student_token(tg_id))

    # Give a slice of students real attempt history so dashboards are not empty.
    for s in range(min(students, 40)):
        student = services.students.by_telegram_id(900000 + s)
        for exam_id in exam_ids[:2]:
            with contextlib.suppress(Exception):
                services.attempts.start(exam_id, student["id"])
    return tokens


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = int(round((pct / 100) * (len(ordered) - 1)))
    return ordered[idx]


def _noop_async(*_a: Any, **_k: Any) -> Any:
    async def _inner() -> None:
        return None

    return _inner()


def _seeded_lifespan(services: Any, settings: Any):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: Any):
        app.state.services = services
        app.state.app_name = settings.app_name
        app.state.templates_dir = settings.templates_dir
        app.state.static_dir = settings.static_dir
        services.engine.start()
        try:
            yield
        finally:
            services.engine.shutdown()

    return lifespan


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--students", type=int, default=300)
    ap.add_argument("--concurrency", type=int, default=50)
    ap.add_argument("--exams", type=int, default=12)
    ap.add_argument("--questions", type=int, default=8)
    ap.add_argument("--rounds", type=int, default=3, help="dashboard loads per student")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="tqm-load-"))
    settings = Settings(
        _env_file=None,
        secret_key="loadtest-secret-key",
        admin_username="admin",
        admin_password=ADMIN_PASSWORD,
        app_base_dir=str(tmp),
        telegram_bot_token=BOT_TOKEN,
    )
    settings.ensure_directories()

    services = build_services(settings)
    t0 = time.perf_counter()
    tokens = seed(
        services,
        students=args.students,
        exams=args.exams,
        questions_per_exam=args.questions,
    )
    seed_s = time.perf_counter() - t0
    db_path = settings.data_dir / "tqm.db"

    app = create_app()
    # Reuse the seeded container instead of building a second one.
    app.router.lifespan_context = _seeded_lifespan(services, settings)
    # No real bot polling during a load test.
    services.bot.start = _noop_async  # type: ignore[method-assign]

    exam_ids = [e["id"] for e in services.exams.list_exams()]

    def workload(client: TestClient, token: str) -> dict[str, float]:
        h = {"X-Student-Token": token}
        exam_id = random.choice(exam_ids)
        calls = [
            ("dashboard", "/api/student/dashboard"),
            ("exams", "/api/student/exams"),
            ("detail", f"/api/student/exams/{exam_id}"),
            ("eligibility", f"/api/student/exams/{exam_id}/eligibility"),
            ("notifications", "/api/student/notifications"),
        ]
        calls += [("dashboard", "/api/student/dashboard")] * max(0, args.rounds - 1)
        out: dict[str, float] = {}
        for name, path in calls:
            start = time.perf_counter()
            resp = client.get(path, headers=h)
            dt = (time.perf_counter() - start) * 1000
            out[name] = dt
            key = f"__status_{resp.status_code}"
            out[key] = out.get(key, 0) + 1
        return out

    with TestClient(app) as client:
        warm = tokens[0]
        for path in ("/api/student/dashboard", "/api/student/exams", "/api/student/notifications"):
            client.get(path, headers={"X-Student-Token": warm})

        per_endpoint: dict[str, list[float]] = {}
        statuses: dict[str, int] = {}
        all_lat: list[float] = []
        lock = threading.Lock()

        def run(token: str) -> None:
            res = workload(client, token)
            with lock:
                for key, value in res.items():
                    if key.startswith("__status_"):
                        code = key[len("__status_") :]
                        statuses[code] = statuses.get(code, 0) + int(value)
                    else:
                        per_endpoint.setdefault(key, []).append(value)
                        all_lat.append(value)

        total_requests = args.students * (4 + max(0, args.rounds - 1) + 1)
        print(f"DB:   {db_path}  ({db_path.stat().st_size / 1e6:.2f} MB)")
        print(f"Seed: {args.students} students, {args.exams} exams in {seed_s:.2f}s")
        print(f"Load: {total_requests} requests, {args.concurrency} concurrent workers\n")

        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            list(pool.map(run, tokens))
        wall = time.perf_counter() - t0

    print("=== Overall ===")
    print(f"Wall time:      {wall:.2f}s")
    print(f"Throughput:     {total_requests / wall:,.0f} req/s")
    print(f"Latency p50:    {percentile(all_lat, 50):.1f} ms")
    print(f"Latency p95:    {percentile(all_lat, 95):.1f} ms")
    print(f"Latency p99:    {percentile(all_lat, 99):.1f} ms")
    print(f"Latency max:    {max(all_lat):.1f} ms")
    print(f"Latency mean:   {statistics.mean(all_lat):.1f} ms")
    print(f"Status codes:   {dict(sorted(statuses.items()))}")
    print("\n=== Per endpoint (mean / p95 ms) ===")
    for name, values in sorted(per_endpoint.items()):
        print(f"  {name:<14} {statistics.mean(values):7.1f} / {percentile(values, 95):7.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
