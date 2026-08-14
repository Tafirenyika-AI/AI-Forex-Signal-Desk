"""Multi-user auth/account service. Built incrementally: this first pass
covers exactly what src/run_loop.py needs to run the pipeline per-user
instead of against one global .env account (the "one shared engine,
per-user data" architecture). Login/registration/invitation/session
management land here too as that work continues.
"""
from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Engine

from src.auth.crypto import decrypt_secret, encrypt_secret, hash_password, verify_password
from src.config import Settings, settings_for_user
from src.data.db import invitations as invitations_table
from src.data.db import sessions as sessions_table
from src.data.db import user_oanda_accounts as user_oanda_accounts_table
from src.data.db import user_preferences as user_preferences_table
from src.data.db import users as users_table

logger = logging.getLogger("auth.service")

INVITATION_TTL_DAYS = 7
SESSION_TTL_DAYS = 30

# Fallback only for a pathological row with no instrument list recorded —
# every real path (migration seeding, onboarding) always sets one. Kept
# tiny and safe rather than silently defaulting to a large instrument set.
_FALLBACK_INSTRUMENTS = ["EUR_USD"]


@dataclass(frozen=True)
class UserTradingContext:
    user_id: int
    email: str
    username: str
    settings: Settings
    instrument_list: list[str]
    execution_mode: str  # paper / demo
    auto_execute: bool


def active_trading_users(engine: Engine) -> list[UserTradingContext]:
    """Every active user who has completed onboarding (real OANDA
    credentials + an instrument list on file) — src/run_loop.py's
    scheduled entrypoint iterates this instead of using one global
    Settings, so each user trades independently against their own account.
    A user whose stored credentials fail to decrypt or validate (revoked
    token, bad environment) is skipped with a logged warning rather than
    aborting every other user's cycle."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                users_table.c.id,
                users_table.c.email,
                users_table.c.username,
                user_oanda_accounts_table.c.encrypted_api_token,
                user_oanda_accounts_table.c.oanda_account_id,
                user_oanda_accounts_table.c.oanda_environment,
                user_preferences_table.c.instrument_list_json,
                user_preferences_table.c.execution_mode_default,
                user_preferences_table.c.auto_execute,
            )
            .select_from(users_table)
            .join(user_oanda_accounts_table, user_oanda_accounts_table.c.user_id == users_table.c.id)
            .join(user_preferences_table, user_preferences_table.c.user_id == users_table.c.id)
            .where(users_table.c.status == "active", user_preferences_table.c.onboarding_complete.is_(True))
        ).all()

    contexts = []
    for row in rows:
        try:
            token = decrypt_secret(row.encrypted_api_token)
            settings = settings_for_user(token, row.oanda_environment, row.oanda_account_id)
        except Exception:  # noqa: BLE001 — one bad account must not take down every other user's cycle
            logger.warning("Skipping user_id=%s (%s): could not build valid OANDA settings", row.id, row.email, exc_info=True)
            continue
        instrument_list = json.loads(row.instrument_list_json) if row.instrument_list_json else _FALLBACK_INSTRUMENTS
        contexts.append(UserTradingContext(
            user_id=row.id, email=row.email, username=row.username, settings=settings,
            instrument_list=instrument_list, execution_mode=row.execution_mode_default,
            auto_execute=row.auto_execute,
        ))
    return contexts


# ------------------------------------------------------------- invitations --
def create_invitation(engine: Engine, invited_by_user_id: int, email: str) -> str:
    """Invite-only registration (no open self-signup, per the product
    requirement): only an existing user — starting with the admin — can
    mint a token. Returns the raw token; there's no email-sending
    infrastructure in this local-only deployment, so the caller (admin
    panel) displays/copies the invite link directly."""
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.execute(insert(invitations_table).values(
            email=email.strip().lower(), token=token, invited_by_user_id=invited_by_user_id,
            created_at=now, expires_at=now + timedelta(days=INVITATION_TTL_DAYS),
            used_at=None, used_by_user_id=None,
        ))
    return token


def validate_invitation(engine: Engine, token: str) -> dict | None:
    """Returns the invitation row if the token is real, unexpired and
    unused; None otherwise. Registration screens call this before showing
    the signup form — an invalid token means no form, not a confusing
    partial one."""
    now = datetime.now(timezone.utc)
    with engine.connect() as conn:
        row = conn.execute(select(invitations_table).where(invitations_table.c.token == token)).mappings().first()
    if row is None:
        return None
    row = dict(row)
    for key in ("created_at", "expires_at", "used_at"):
        if isinstance(row.get(key), datetime) and row[key].tzinfo is None:
            row[key] = row[key].replace(tzinfo=timezone.utc)
    if row["used_at"] is not None or row["expires_at"] < now:
        return None
    return row


def register_via_invitation(engine: Engine, token: str, username: str, password: str) -> int:
    """Completes registration for a valid invitation: creates the user row
    and marks the invitation used, atomically. Raises ValueError on an
    invalid/expired/used token or a username/email already taken — the
    caller (registration screen) turns that into a form error."""
    invitation = validate_invitation(engine, token)
    if invitation is None:
        raise ValueError("This invitation link is invalid, expired, or has already been used.")

    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        existing = conn.execute(
            select(users_table.c.id).where(
                (users_table.c.email == invitation["email"]) | (users_table.c.username == username)
            )
        ).first()
        if existing is not None:
            raise ValueError("An account with this email or username already exists.")

        result = conn.execute(insert(users_table).values(
            email=invitation["email"], username=username, password_hash=hash_password(password),
            is_admin=False, status="active", created_at=now, updated_at=now,
        ))
        user_id = result.inserted_primary_key[0]
        conn.execute(
            update(invitations_table)
            .where(invitations_table.c.id == invitation["id"])
            .values(used_at=now, used_by_user_id=user_id)
        )
    return user_id


# ------------------------------------------------------------------- login --
def authenticate(engine: Engine, email_or_username: str, password: str) -> dict | None:
    identifier = email_or_username.strip().lower()
    with engine.connect() as conn:
        row = conn.execute(
            select(users_table).where(
                (users_table.c.email == identifier) | (users_table.c.username == email_or_username.strip())
            )
        ).mappings().first()
    if row is None or row["status"] != "active":
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    return dict(row)


def create_session(engine: Engine, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.execute(insert(sessions_table).values(
            session_token=token, user_id=user_id, created_at=now,
            expires_at=now + timedelta(days=SESSION_TTL_DAYS),
        ))
    return token


def validate_session(engine: Engine, session_token: str) -> dict | None:
    """Returns the logged-in user's row, or None if the session cookie is
    missing/expired/revoked — the dashboard's login gate calls this on
    every page load before rendering anything else."""
    if not session_token:
        return None
    now = datetime.now(timezone.utc)
    with engine.connect() as conn:
        session_row = conn.execute(
            select(sessions_table).where(sessions_table.c.session_token == session_token)
        ).mappings().first()
        if session_row is None:
            return None
        expires_at = session_row["expires_at"]
        if isinstance(expires_at, datetime) and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < now:
            return None
        user_row = conn.execute(
            select(users_table).where(users_table.c.id == session_row["user_id"])
        ).mappings().first()
    if user_row is None or user_row["status"] != "active":
        return None
    return dict(user_row)


def revoke_session(engine: Engine, session_token: str) -> None:
    with engine.begin() as conn:
        conn.execute(sessions_table.delete().where(sessions_table.c.session_token == session_token))


def change_password(engine: Engine, user_id: int, new_password: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(users_table).where(users_table.c.id == user_id)
            .values(password_hash=hash_password(new_password), updated_at=datetime.now(timezone.utc))
        )


# ------------------------------------------------------------------- admin --
def list_users(engine: Engine) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(select(users_table).order_by(users_table.c.created_at.desc())).mappings().all()
    return [dict(r) for r in rows]


def list_invitations(engine: Engine) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(select(invitations_table).order_by(invitations_table.c.created_at.desc())).mappings().all()
    return [dict(r) for r in rows]


def set_user_status(engine: Engine, user_id: int, status: str) -> None:
    if status not in ("active", "disabled"):
        raise ValueError(f"Invalid status: {status!r}")
    with engine.begin() as conn:
        conn.execute(
            update(users_table).where(users_table.c.id == user_id)
            .values(status=status, updated_at=datetime.now(timezone.utc))
        )


# --------------------------------------------------------------- onboarding --
def save_oanda_credentials(engine: Engine, user_id: int, api_token: str, account_id: str, environment: str) -> None:
    """Validates the credentials build a real Settings object (same
    hard-refuse-if-live check as everywhere else) before ever encrypting
    and storing them — a typo'd or malicious environment value never
    reaches the database."""
    settings_for_user(api_token, environment, account_id)  # raises on invalid input; not otherwise used here
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        stmt = insert(user_oanda_accounts_table).values(
            user_id=user_id, encrypted_api_token=encrypt_secret(api_token),
            oanda_account_id=account_id, oanda_environment=environment.strip().lower(),
            created_at=now, updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["user_id"],
            set_={
                "encrypted_api_token": encrypt_secret(api_token),
                "oanda_account_id": account_id,
                "oanda_environment": environment.strip().lower(),
                "updated_at": now,
            },
        )
        conn.execute(stmt)


def save_preferences(
    engine: Engine, user_id: int, instrument_list: list[str] | None, execution_mode_default: str,
    auto_execute: bool, onboarding_complete: bool,
) -> None:
    now = datetime.now(timezone.utc)
    instrument_list_json = json.dumps(instrument_list) if instrument_list else None
    with engine.begin() as conn:
        stmt = insert(user_preferences_table).values(
            user_id=user_id, instrument_list_json=instrument_list_json,
            execution_mode_default=execution_mode_default, auto_execute=auto_execute,
            onboarding_complete=onboarding_complete, created_at=now, updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["user_id"],
            set_={
                "instrument_list_json": instrument_list_json,
                "execution_mode_default": execution_mode_default,
                "auto_execute": auto_execute,
                "onboarding_complete": onboarding_complete,
                "updated_at": now,
            },
        )
        conn.execute(stmt)


def get_preferences(engine: Engine, user_id: int) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(
            select(user_preferences_table).where(user_preferences_table.c.user_id == user_id)
        ).mappings().first()
    return dict(row) if row else None


def has_oanda_credentials(engine: Engine, user_id: int) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            select(user_oanda_accounts_table.c.id).where(user_oanda_accounts_table.c.user_id == user_id)
        ).first()
    return row is not None


def get_user_settings(engine: Engine, user_id: int) -> Settings | None:
    """One user's own decrypted OANDA credentials as a Settings instance —
    used by the dashboard so every broker call (prices, account state,
    order placement) runs against whoever is actually logged in, not a
    single global .env account. None if this user hasn't completed
    onboarding yet (shouldn't happen past src/dashboard/auth_gate.py's
    gate, but checked rather than assumed)."""
    with engine.connect() as conn:
        row = conn.execute(
            select(user_oanda_accounts_table).where(user_oanda_accounts_table.c.user_id == user_id)
        ).mappings().first()
    if row is None:
        return None
    return settings_for_user(decrypt_secret(row["encrypted_api_token"]), row["oanda_environment"], row["oanda_account_id"])
