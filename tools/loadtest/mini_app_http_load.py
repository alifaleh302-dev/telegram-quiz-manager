#!/usr/bin/env python3
"""End-to-end load test against a *real* Uvicorn server.

Unlike ``mini_app_load.py`` (in-process TestClient), this starts the actual
production entry point in a subprocess and drives it over HTTP, so the numbers
include the event loop, the ASGI stack and any serialisation the app performs.

The bot listener is left disabled (empty bot token) so the server does not try
to poll Telegram; student tokens only need ``secret_key``, not the bot token.

Usage:
    python tools/loadtest/mini_app_http_load.py --students 300 --concurrency 50
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.core.config import get_settings  # noqa: E402
from app.core.telegram_auth import create_student_token  # noqa: E402
from app.services.container import build_services  # noqa: E402

ADMIN_PASSWORD = "LoadTestPass123!"


class FakeUser:
    def __init__(self, i: int) -> None:
        self.user_id = i
        self.first_name = f"طالب{i}"
        self.last_name = ""
        self.username = f"student{i}"
        self.language_code = "ar"
        self.photo_url = ""


def seed(base_dir: Path, students: int, exams: int, questions: int) -> list[str]:
    # Sign tokens with the *same* settings the server process will load, or the
    # HMAC would use a different secret and every request would 401.
    os.environ["APP_BASE_DIR"] = str(base_dir)
    os.environ["SECRET_KEY"] = "loadtest-secret-key"
    os.environ["ADMIN_PASSWORD"] = ADMIN_PASSWORD
    os.environ["TELEGRAM_BOT_TOKEN"] = ""
    get_settings.cache_clear()
    settings = get_settings()
    settings.ensure_directories()
    services = build_services(settings)
    for i in range(questions * exams):
        services.questions.create({
            "question": f"سؤال {i}؟", "options": ["أ", "ب", "ج", "د"],
            "correct_option_id": i % 4, "category": "شبكات", "difficulty": "easy",
            "status": "ready",
        })
    bank = [q["id"] for q in services.questions.list_questions()]
    eids = []
    for e in range(exams):
        ex = services.exams.create({
            "title": f"اختبار {e}", "access_mode": "always",
            "max_attempts": 3, "duration_minutes": 15,
        })
        eids.append(ex["id"])
        services.exams.set_questions(ex["id"], bank[e * questions:(e + 1) * questions])
        services.exams.publish(ex["id"])
    tokens = []
    for s in range(students):
        services.students.touch(FakeUser(900000 + s))
        tokens.append(create_student_token(900000 + s))
    for s in range(min(students, 300)):
        st = services.students.by_telegram_id(900000 + s)
        for eid in eids[:2]:
            with contextlib.suppress(Exception):
                services.attempts.start(eid, st["id"])
    return tokens


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[int(round((pct / 100) * (len(ordered) - 1)))]


async def drive(
    base: str, tokens: list[str], concurrency: int, rounds: int, exam_ids: list[str]
) -> dict[str, Any]:
    lat: list[float] = []
    statuses: dict[int, int] = {}
    sem = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)

    async with httpx.AsyncClient(base_url=base, timeout=120, limits=limits) as client:
        async def one(token: str) -> None:
            async with sem:
                h = {"X-Student-Token": token}
                eid = exam_ids[hash(token) % len(exam_ids)]
                paths = [
                    "/api/student/dashboard", "/api/student/exams",
                    f"/api/student/exams/{eid}", f"/api/student/exams/{eid}/eligibility",
                    "/api/student/notifications",
                ] + ["/api/student/dashboard"] * max(0, rounds - 1)
                for p in paths:
                    start = time.perf_counter()
                    try:
                        r = await client.get(p, headers=h)
                        code = r.status_code
                    except Exception:  # noqa: BLE001
                        code = -1
                    lat.append((time.perf_counter() - start) * 1000)
                    statuses[code] = statuses.get(code, 0) + 1

        t0 = time.perf_counter()
        await asyncio.gather(*(one(t) for t in tokens))
        wall = time.perf_counter() - t0
    return {"wall": wall, "lat": lat, "statuses": statuses}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--students", type=int, default=300)
    ap.add_argument("--concurrency", type=int, default=50)
    ap.add_argument("--exams", type=int, default=12)
    ap.add_argument("--questions", type=int, default=8)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--port", type=int, default=8899)
    args = ap.parse_args()

    base_dir = Path(tempfile.mkdtemp(prefix="tqm-http-"))
    print(f"Seeding {args.students} students into {base_dir} ...")
    t0 = time.perf_counter()
    tokens = seed(base_dir, args.students, args.exams, args.questions)
    print(f"Seeded in {time.perf_counter() - t0:.2f}s")

    env = os.environ.copy()
    env.update({
        "APP_BASE_DIR": str(base_dir),
        "SECRET_KEY": "loadtest-secret-key",
        "ADMIN_PASSWORD": ADMIN_PASSWORD,
        "TELEGRAM_BOT_TOKEN": "",
        "APP_PORT": str(args.port),
        "PYTHONPATH": str(ROOT),
    })
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(args.port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{args.port}"
    try:
        for _ in range(60):
            try:
                if httpx.get(base + "/healthz", timeout=1).status_code == 200:
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        else:
            print("server failed to start")
            print(proc.stdout.read().decode()[-2000:] if proc.stdout else "")
            return 1

        exam_ids = _read_exam_ids(base_dir)

        # Warm-up.
        with httpx.Client(base_url=base) as c:
            c.get("/api/student/dashboard", headers={"X-Student-Token": tokens[0]})

        total = args.students * (4 + max(0, args.rounds - 1) + 1)
        print(f"Driving {total} requests, concurrency={args.concurrency}\n")
        res = asyncio.run(
            drive(base, tokens, args.concurrency, args.rounds, exam_ids)
        )
        lat = sorted(res["lat"])
        print("=== Real Uvicorn server ===")
        print(f"Wall time:    {res['wall']:.2f}s")
        print(f"Throughput:   {total / res['wall']:,.0f} req/s")
        print(f"p50: {percentile(lat,50):.1f} ms   p95: {percentile(lat,95):.1f} ms   "
              f"p99: {percentile(lat,99):.1f} ms   max: {max(lat):.1f} ms")
        print(f"mean: {statistics.mean(lat):.1f} ms")
        print(f"Statuses: {dict(sorted(res['statuses'].items()))}")
    finally:
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)
    return 0


def _read_exam_ids(base_dir: Path) -> list[str]:
    import json
    import sqlite3

    con = sqlite3.connect(base_dir / "data" / "tqm.db")
    try:
        return [json.loads(r[0])["id"] for r in con.execute("SELECT value FROM exams")]
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
