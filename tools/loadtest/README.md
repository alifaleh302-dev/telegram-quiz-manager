# Load tests

Measure the Student Mini App read paths under concurrency.

## `mini_app_load.py` — in-process

Drives the real ASGI app through Starlette's `TestClient`. TestClient runs the
app on a single event-loop thread, so this models the single-worker Uvicorn
event loop and its serialisation.

```bash
python tools/loadtest/mini_app_load.py --students 300 --concurrency 50
```

## `mini_app_http_load.py` — real Uvicorn server

Seeds a temp database, starts `uvicorn app.main:app` as a subprocess, and drives
it over HTTP with httpx. Closer to production than the in-process runner.

```bash
python tools/loadtest/mini_app_http_load.py --students 300 --concurrency 50
```

Note: it signs student tokens with the same settings the server process loads
(the env vars are set, then `get_settings` is cleared). If you change that,
tokens will not verify and every request will 401 — which silently measures the
auth-reject path instead of the DB path.

## Reading the output

- **Throughput flat while concurrency rises** ⇒ the server serialises requests
  (a global lock or a single event loop doing blocking work), not a capacity limit.
- **Latency growing linearly with concurrency** ⇒ queueing, same cause.
- Compare p95/p99, not just the mean: the mean hides the tail.
