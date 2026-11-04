"""Manages the pool of userbot clients and the interactive login flow.

Two responsibilities:

* **Client pool** — one :class:`UserbotClient` per account, created lazily and
  reused, so a publish does not pay the connect cost each time.
* **Login flow** — Telegram's MTProto login is two-step (request a code, then
  sign in), with an optional 2FA password. The intermediate state
  (``phone_code_hash`` plus the not-yet-authorized client) is kept in memory
  only; nothing is persisted until ``export_session_string`` succeeds.

A client is rebuilt automatically when its credentials change, and stale
clients are closed on shutdown.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.services.audit_service import AuditService
from app.services.secret_store import SecretStore
from app.services.userbot_service import UserbotAccountService
from app.storage.validators import ValidationError
from app.telegram.userbot_client import PYROGRAM_AVAILABLE, UserbotClient, UserbotError

logger = get_logger(__name__)


class UserbotManager:
    def __init__(
        self,
        accounts: UserbotAccountService,
        secrets: SecretStore,
        audit: AuditService,
        *,
        timeout: float = 30.0,
    ) -> None:
        self._accounts = accounts
        self._secrets = secrets
        self._audit = audit
        self._timeout = timeout
        self._clients: dict[str, UserbotClient] = {}
        # account_id -> {"client": Client, "phone": str, "phone_code_hash": str}
        self._pending: dict[str, dict[str, Any]] = {}

    # --- pool --------------------------------------------------------------
    def available(self) -> bool:
        """True when the userbot transport can be used at all."""
        return PYROGRAM_AVAILABLE

    def client_for(self, account_id: str) -> UserbotClient | None:
        """A client bound to one account, or None when the account is unusable."""
        account = self._accounts.get(account_id)
        if account is None:
            return None
        creds = self._secrets.userbot_credentials(account_id)
        if not creds.get("api_id") or not creds.get("api_hash") or not creds.get("session"):
            return None

        existing = self._clients.get(account_id)
        if existing is not None and existing.session_fingerprint == creds["session"]:
            return existing
        if existing is not None:
            # Credentials were rotated: drop the cached connection.
            self._clients.pop(account_id, None)

        client = UserbotClient(
            account_id,
            api_id=creds["api_id"],
            api_hash=creds["api_hash"],
            session=creds["session"],
            timeout=self._timeout,
        )
        self._clients[account_id] = client
        return client

    def resolve_for_channel(self, channel: dict[str, Any]) -> UserbotClient | None:
        """Pick the account a channel should publish through.

        Order of preference: the channel's pinned account, then the account in
        the channel's rotation pool (first usable one), then the global default.
        """
        pinned = channel.get("userbot_account_id")
        if pinned:
            client = self.client_for(pinned)
            if client is not None:
                return client
        for account_id in channel.get("userbot_account_ids") or []:
            client = self.client_for(account_id)
            if client is not None:
                return client
        default = self._accounts.default_account()
        if default is not None:
            return self.client_for(default["id"])
        return None

    def invalidate(self, account_id: str) -> None:
        """Forget a cached client so the next call reconnects."""
        client = self._clients.pop(account_id, None)
        if client is not None:
            client.reset()

    def record_used(self, account_id: str) -> None:
        """Stamp an account as the one that just handled a publish."""
        self._accounts.record_used(account_id)

    def record_error(self, account_id: str, message: str) -> None:
        """Attach a failure to an account so it shows up in the dashboard."""
        self._accounts.record_error(account_id, message)

    async def close_all(self) -> None:
        clients = list(self._clients.values())
        self._clients.clear()
        for client in clients:
            await client.close()
        for state in self._pending.values():
            app = state.get("client")
            if app is not None:
                try:
                    await app.disconnect()
                except Exception:  # noqa: BLE001 - best effort
                    pass
        self._pending.clear()

    # --- login flow --------------------------------------------------------
    def pending_accounts(self) -> list[str]:
        return list(self._pending)

    async def start_login(self, account_id: str, phone: str) -> dict[str, Any]:
        """Step 1: connect as the user and ask Telegram to send a login code."""
        if not PYROGRAM_AVAILABLE:
            raise ValidationError(
                "مكتبة Pyrogram غير مثبّتة على السيرفر؛ لا يمكن تسجيل الدخول."
            )
        account = self._accounts.get(account_id)
        if account is None:
            raise ValidationError(f"الحساب {account_id} غير موجود.")
        phone = (phone or account.get("phone") or "").strip()
        if not phone:
            raise ValidationError("رقم الهاتف مطلوب لإرسال رمز الدخول.")
        creds = self._secrets.userbot_credentials(account_id)
        if not creds.get("api_id") or not creds.get("api_hash"):
            raise ValidationError("أدخل api_id و api_hash أولاً.")

        from pyrogram import Client

        await self._discard_pending(account_id)
        app = Client(
            name=f"login-{account_id}",
            api_id=creds["api_id"],
            api_hash=creds["api_hash"],
            in_memory=True,
            no_updates=True,
        )
        try:
            await app.connect()
            sent = await app.send_code(phone)
        except Exception as exc:  # noqa: BLE001 - report a readable reason
            try:
                await app.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._audit.record(
                "userbot.login_failed",
                level="error",
                message=f"فشل إرسال رمز الدخول للحساب {account_id}: {type(exc).__name__}",
            )
            raise ValidationError(self._login_error(exc)) from exc

        self._pending[account_id] = {
            "client": app,
            "phone": phone,
            "phone_code_hash": sent.phone_code_hash,
        }
        self._audit.record(
            "userbot.login_code_sent",
            level="warning",
            message=f"إرسال رمز الدخول لحساب userbot {account_id}",
        )
        return {"account_id": account_id, "phone": phone, "step": "code"}

    async def complete_login(
        self, account_id: str, *, code: str, password: str = "", actor: str = "system"
    ) -> dict[str, Any]:
        """Step 2: submit the login code, then the 2FA password when asked."""
        state = self._pending.get(account_id)
        if state is None:
            raise ValidationError("لم تبدأ عملية الدخول لهذا الحساب. أرسل الرمز أولاً.")

        from pyrogram.errors import SessionPasswordNeeded

        app = state["client"]
        phone = state["phone"]
        phone_code_hash = state["phone_code_hash"]

        try:
            if password:
                # Resuming after a 2FA prompt.
                await app.check_password(password)
            else:
                try:
                    await app.sign_in(phone, phone_code_hash, code)
                except SessionPasswordNeeded:
                    return {
                        "account_id": account_id,
                        "step": "password",
                        "message": "الحساب محمي بكلمة مرور (2FA). أدخل كلمة مرور الخطوة الثانية.",
                    }
        except Exception as exc:  # noqa: BLE001
            self._audit.record(
                "userbot.login_failed",
                level="error",
                message=f"فشل تسجيل دخول الحساب {account_id}: {type(exc).__name__}",
            )
            raise ValidationError(self._login_error(exc)) from exc

        try:
            session = await app.export_session_string()
            me = await app.get_me()
        finally:
            try:
                await app.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._pending.pop(account_id, None)

        account = self._accounts.mark_logged_in(
            account_id,
            session=session,
            username=getattr(me, "username", "") or "",
            telegram_id=getattr(me, "id", None),
            actor=actor,
        )
        self.invalidate(account_id)
        return {"account_id": account_id, "step": "done", "account": account}

    async def cancel_login(self, account_id: str) -> None:
        await self._discard_pending(account_id)

    async def _discard_pending(self, account_id: str) -> None:
        state = self._pending.pop(account_id, None)
        if state is not None:
            try:
                await state["client"].disconnect()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _login_error(exc: Exception) -> str:
        name = type(exc).__name__
        text = str(exc)
        if name == "FloodWait":
            seconds = getattr(exc, "value", None) or getattr(exc, "seconds", None)
            return f"تيليجرام يطلب الانتظار {seconds} ثانية قبل المحاولة مجدداً."
        if name in {"PhoneCodeInvalid", "PhoneCodeExpired"}:
            return "رمز الدخول غير صحيح أو منتهي الصلاحية."
        if name == "PasswordHashInvalid":
            return "كلمة مرور الخطوة الثانية غير صحيحة."
        if name in {"ApiIdInvalid", "ApiIdPublishedFlood"}:
            return "api_id أو api_hash غير صالح."
        if name == "PhoneNumberInvalid":
            return "رقم الهاتف غير صالح (استخدم الصيغة الدولية +…)."
        if name == "SessionPasswordNeeded":
            return "الحساب محمي بكلمة مرور (2FA)."
        return f"تعذّر تسجيل الدخول: {text}"


def build_userbot_manager(
    accounts: UserbotAccountService, secrets: SecretStore, audit: AuditService, *, timeout: float
) -> UserbotManager:
    return UserbotManager(accounts, secrets, audit, timeout=timeout)


__all__ = ["UserbotManager", "build_userbot_manager", "UserbotClient", "UserbotError"]
