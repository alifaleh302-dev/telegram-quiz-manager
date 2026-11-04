"""JSON storage with atomic writes, locking and automatic backups.

Each logical collection lives in its own file so a corrupted or hand-edited
file cannot take down the whole system. Every write goes through
:meth:`JsonStore.update`, which holds both an in-process lock and a
cross-process file lock, snapshots the previous content into ``backups/`` and
then replaces the file atomically.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from app.core.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

# One lock per absolute path, so two stores never deadlock on different files.
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    key = str(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


class StorageError(Exception):
    """Raised when a data file cannot be read or written safely."""


class _FileLock:
    """Advisory cross-process lock backed by ``fcntl`` when available."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle = None

    def __enter__(self) -> "_FileLock":
        try:
            import fcntl
        except ImportError:  # pragma: no cover - Windows fallback
            return self
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self._path, "a+", encoding="utf-8")
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._handle is None:  # pragma: no cover - Windows fallback
            return
        try:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class JsonStore:
    """A single JSON file holding a document with a known default shape."""

    def __init__(
        self,
        path: Path,
        default: dict[str, Any],
        *,
        backup_dir: Path | None = None,
        keep_backups: int = 20,
        backup_min_interval: float = 60.0,
    ) -> None:
        self.path = Path(path)
        self._default = default
        self.backup_dir = Path(backup_dir) if backup_dir else None
        self.keep_backups = keep_backups
        # Snapshots are throttled: a write-heavy session would otherwise
        # produce one backup directory per write. Zero means "always".
        self.backup_min_interval = backup_min_interval
        self._last_backup_at = 0.0

    # --- reading -----------------------------------------------------------
    def read(self) -> dict[str, Any]:
        """Return a deep copy of the stored document (defaults if absent)."""
        with _lock_for(self.path):
            return copy.deepcopy(self._read_unlocked())

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return copy.deepcopy(self._default)
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise StorageError(f"cannot read {self.path}: {exc}") from exc
        if not raw:
            return copy.deepcopy(self._default)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StorageError(
                f"{self.path.name} is not valid JSON ({exc}); "
                "restore a copy from the backups directory"
            ) from exc
        if not isinstance(data, dict):
            raise StorageError(f"{self.path.name} must contain a JSON object")
        merged = copy.deepcopy(self._default)
        merged.update(data)
        return merged

    # --- writing -----------------------------------------------------------
    def write(self, document: dict[str, Any]) -> None:
        with _lock_for(self.path):
            self._write_unlocked(document)

    def update(self, mutator: Callable[[dict[str, Any]], T]) -> T:
        """Read-modify-write under a lock; returns whatever ``mutator`` returns."""
        with _lock_for(self.path):
            document = self._read_unlocked()
            result = mutator(document)
            self._write_unlocked(document)
            return result

    def _write_unlocked(self, document: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _FileLock(self.path.with_suffix(self.path.suffix + ".lock")):
            if self.path.exists() and self.backup_dir is not None:
                self._snapshot()
            self._atomic_replace(document)

    def _atomic_replace(self, document: dict[str, Any]) -> None:
        payload = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        fd, tmp_name = tempfile.mkstemp(
            dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    def _snapshot(self) -> None:
        if self.backup_dir is None:
            return
        now = time.monotonic()
        if self.backup_min_interval and now - self._last_backup_at < self.backup_min_interval:
            return
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        target_dir = self.backup_dir / stamp
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(self.path, target_dir / self.path.name)
        except OSError as exc:  # a failed backup must not block the write
            logger.warning("backup of %s failed: %s", self.path.name, exc)
            return
        self._last_backup_at = now
        self._prune_backups()

    def _prune_backups(self) -> None:
        if self.backup_dir is None or self.keep_backups <= 0:
            return
        dirs = sorted(
            (d for d in self.backup_dir.iterdir() if d.is_dir()), key=lambda d: d.name
        )
        for stale in dirs[: -self.keep_backups]:
            shutil.rmtree(stale, ignore_errors=True)


class Storage:
    """Owns every JSON collection used by the application."""

    def __init__(self, data_dir: Path, backup_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.backup_dir = Path(backup_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)

        self.questions = JsonStore(
            self.data_dir / "questions.json",
            {"version": 1, "counter": 0, "questions": []},
            backup_dir=self.backup_dir,
        )
        self.channels = JsonStore(
            self.data_dir / "channels.json",
            {"version": 1, "counter": 0, "channels": []},
            backup_dir=self.backup_dir,
        )
        self.schedules = JsonStore(
            self.data_dir / "schedules.json",
            {"version": 1, "counter": 0, "schedules": []},
            backup_dir=self.backup_dir,
        )
        self.history = JsonStore(
            self.data_dir / "publishing_history.json",
            {"version": 1, "counter": 0, "history": []},
            backup_dir=self.backup_dir,
        )
        self.settings = JsonStore(
            self.data_dir / "settings.json",
            {
                "version": 1,
                "timezone": "Asia/Riyadh",
                "publishing_enabled": True,
                "poll_options": {
                    "explanation": True,
                    "open_period": 0,
                    "anonymous": True,
                    "allows_multiple_answers": False,
                },
                "retry": {"max_attempts": 3, "delay_seconds": 60},
                # Spacing between questions in a burst (bulk publish / batch
                # schedule). Telegram rate-limits a chat at roughly 20 messages
                # per minute, so a burst with no gap silently fails partway.
                "pacing": {"interval_seconds": 4, "max_wait_seconds": 120},
                "daily_limit": 0,
                "analytics": {"auto_collect": False, "auto_collect_hours": 6},
                # Which transport new channels use by default: "bot" (Bot API)
                # or "userbot" (a logged-in user account). Per-channel setting
                # wins; this is only the fallback for channels without one.
                "transport": {"default_mode": "bot"},
                "paused_schedules": [],
                "state": {},
            },
            backup_dir=self.backup_dir,
        )
        self.users = JsonStore(
            self.data_dir / "users.json",
            {"version": 1, "users": []},
            backup_dir=self.backup_dir,
        )
        self.secrets = JsonStore(
            self.data_dir / "secrets.json",
            {
                "version": 1,
                "telegram_bot_token": None,
                "telegram_bot_token_updated_at": None,
                # Encrypted per-account userbot credentials, keyed by account id.
                "userbot": {},
            },
            backup_dir=self.backup_dir,
        )
        self.audit = JsonStore(
            self.data_dir / "audit_log.json",
            {"version": 1, "entries": []},
            backup_dir=self.backup_dir,
        )
        # One row per poll snapshot. ``poll_id`` is the Telegram poll id, so the
        # latest row for a poll carries the freshest counts.
        self.poll_results = JsonStore(
            self.data_dir / "poll_results.json",
            {"version": 1, "counter": 0, "results": []},
            backup_dir=self.backup_dir,
        )
        # Live per-user answers, only available for non-anonymous polls.
        self.poll_answers = JsonStore(
            self.data_dir / "poll_answers.json",
            {"version": 1, "counter": 0, "answers": []},
            backup_dir=self.backup_dir,
        )
        # Userbot accounts: metadata only (api_id/api_hash/session are encrypted
        # in ``secrets.json``, never in this file).
        self.userbot_accounts = JsonStore(
            self.data_dir / "userbot_accounts.json",
            {"version": 1, "counter": 0, "accounts": []},
            backup_dir=self.backup_dir,
        )

    def all_stores(self) -> dict[str, JsonStore]:
        return {
            "questions": self.questions,
            "channels": self.channels,
            "schedules": self.schedules,
            "history": self.history,
            "settings": self.settings,
            "users": self.users,
            "secrets": self.secrets,
            "audit_log": self.audit,
            "poll_results": self.poll_results,
            "poll_answers": self.poll_answers,
            "userbot_accounts": self.userbot_accounts,
        }
