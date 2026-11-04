# Project memory — /workspace/project

**Telegram Quiz Manager** — Arabic (RTL) web dashboard that schedules and publishes
Telegram *quiz* polls (single correct answer) to one or more channels. FastAPI +
Jinja2 + HTMX + Bootstrap 5 RTL, APScheduler, JSON file storage. Python 3.13.

> An earlier project occupied this same path (an Android SMS messenger in Kotlin).
> Its memory was archived to `2026-11-04-android-sms-messenger-legacy.md`; it is
> unrelated to this codebase.

## Layout and entry points
- `run.py` → `uvicorn app.main:app`; `app/main.py:create_app()` wires routers and
  owns the lifespan (startup: reconcile pending publishes → scheduler.start() →
  bot.start(); shutdown in reverse).
- `app/services/container.py:build_services(settings)` is the composition root;
  `Services` is a dataclass stored on `app.state.services` and reached via
  `app/api/deps.py:get_services(request)`.
- Layers: `core/` (config, logging, security) → `storage/` (JSON) →
  `services/` (business logic) → `api/` (routers) → `templates/` + `static/`.
  `scheduler/` and `telegram/` sit beside services. The JSON layer is isolated so
  it can be swapped for SQLite without touching services.

## Build, run, test
- `pip install -r requirements.txt` — FastAPI, uvicorn, jinja2, python-multipart,
  python-telegram-bot, APScheduler (<4), itsdangerous, argon2-cffi,
  pydantic-settings, httpx, pytest, pytest-asyncio. Installed to `~/.local`
  (not on PATH; invoke `python3 -m pytest` / `python3 -m uvicorn`).
- `python3 -m pytest` → **105 tests, ~14 s**. `pytest.ini` sets `asyncio_mode = auto`,
  so `async def test_*` needs no decorator.
- `python3 -m pyflakes app/ run.py tests/` is clean — keep it that way.
- `bash tools/smoke_test.sh` drives a live server on port **12000** (the work-host
  port) through the whole workflow with curl + the session cookie.
- **Run with `--workers 1`.** The scheduler and publish state live in-process;
  multiple workers would double-publish.

## Configuration gotchas that cost real time
- **`get_settings()` is the only accessor; there is no module-level `settings`.**
  An eager `settings = get_settings()` singleton existed briefly and broke tests:
  modules that did `from app.core.config import settings` captured a stale instance,
  so `get_settings.cache_clear()` + env changes never reached them. All modules now
  call `get_settings()` at use time. Do not reintroduce the singleton.
- `Settings.templates_dir` / `static_dir` resolve from `PROJECT_ROOT` (the code
  location), **not** `base_dir`. Data/logs/backups follow `base_dir` (env
  `APP_BASE_DIR`). Conflating them makes `StaticFiles(directory=...)` raise when
  tests point `APP_BASE_DIR` at a tmp dir.
- Tests must `monkeypatch.setenv(...)` **and** `get_settings.cache_clear()` before
  `create_app()`; the `app_client` fixture in `tests/test_api.py` shows the pattern.
- `.env` is gitignored. Local dev values used: `ADMIN_USERNAME=admin`,
  `ADMIN_PASSWORD=Admin12345!`, `APP_PORT=12000`, empty `TELEGRAM_BOT_TOKEN`.
- Empty `TELEGRAM_BOT_TOKEN` ⇒ `dry_run` mode: the whole pipeline runs and history
  records successes flagged تجريبي, but nothing is sent. This is how the app is
  developed and tested.

## Design invariants (enforced in code + tests)
- A question becomes `published` **only** after Telegram confirms; `pick_candidates`
  returns `ready` questions only, so a restart cannot republish.
- `PublishingService._claim` inserts a `pending` history row **before** the network
  call; `_finish` updates it. A duplicate `pending` for the same
  (question, channel) is refused, and so is re-publishing a `success`.
- `publish()` is serialised by an `asyncio.Lock`; APScheduler jobs use
  `max_instances=1`.
- Stale `pending` rows (older than `PENDING_TIMEOUT`, 10 min) become `unknown` via
  `reconcile_pending()` at startup and surface in the dashboard for manual review
  rather than being retried blind.
- Retryable vs permanent is decided in `TelegramClient._classify`.

## Storage notes
- `JsonStore` writes atomically (mkstemp + fsync + `os.replace`) under an
  `fcntl`-based `_FileLock` (`<name>.json.lock`, gitignored).
- Snapshots into `data/backups/<timestamp>/` are **throttled** by
  `backup_min_interval` (default 60 s). Without it every write created a backup dir
  — a few UI actions produced 20+ directories. Tests that assert per-write backups
  pass `backup_min_interval=0`.
- `Storage` exposes named stores: `questions`, `channels`, `schedules`, `history`,
  `settings`, `users`, `audit`. All writes go through `store.update(fn)` where `fn`
  mutates the document and may return a value.
- Documents carry a `counter` used to mint IDs (`Q0001`, `CH001`, `SCH001`,
  `PUB00001`).

## Security
- Argon2id hashing (`argon2-cffi`), signed session cookie via itsdangerous
  (`HttpOnly`, `SameSite=Lax`), per-session CSRF token checked on every mutating
  `/api/*` call via the `X-CSRF-Token` header (`app/api/deps.py:api_guard`).
- Bot token, `SECRET_KEY` and the admin password live only in env vars — never in
  the JSON files. `PUT /api/settings` rejects unknown keys instead of silently
  storing them (it takes a raw `dict`, not a pydantic model, precisely so unknown
  keys are visible).

## Conventions
- Arabic user-facing text; code, identifiers and comments in English.
- `ValidationError` from `app/storage/validators.py` is handled globally in
  `main.py` → HTTP 400 `{"ok": false, "error": "<arabic message>"}`. Do not raise
  bare `HTTPException` for domain validation.
- Templates get `active_page` from `app/api/deps.py:active_page_for(path)`, computed
  server-side. Jinja `{% set %}` in a child template does not propagate to the base.
