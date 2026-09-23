"""Unit tests for P0-05 (external review, 2026-09-22): auth/session
hardening -- exact-match broker URL validation, session revocation on
password change / account disable, and periodic session revalidation.

Uses an isolated in-memory SQLite engine (never the real production DB).

Run: .venv/Scripts/python.exe -m pytest tests/test_auth_hardening.py -v
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from sqlalchemy import create_engine, insert, select

from src.auth import service as auth_service
from src.auth.crypto import hash_password
from src.config import _validate_alpaca_environment
from src.data.db import metadata
from src.data.db import sessions as sessions_table
from src.data.db import users as users_table


def _fresh_engine():
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    return engine


def _seed_user(engine, *, username="alice", status="active"):
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        user_id = conn.execute(
            insert(users_table).values(
                username=username, email=f"{username}@example.com",
                password_hash=hash_password("original-password-123"),
                status=status, is_admin=False, created_at=now, updated_at=now,
            )
        ).inserted_primary_key[0]
    return user_id


# --- exact-match broker URL validation ---

def test_legit_alpaca_paper_url_passes():
    _validate_alpaca_environment("https://paper-api.alpaca.markets/v2")  # must not raise


def test_subdomain_confusion_attack_rejected():
    # The exact vulnerability: a prefix check let "https://paper-api.
    # alpaca.markets.evil.com" through, since it literally starts with
    # the real host string -- exact hostname match closes it.
    with pytest.raises(RuntimeError):
        _validate_alpaca_environment("https://paper-api.alpaca.markets.evil.com")


def test_live_alpaca_host_rejected():
    with pytest.raises(RuntimeError):
        _validate_alpaca_environment("https://api.alpaca.markets")


def test_none_url_is_allowed():
    _validate_alpaca_environment(None)  # must not raise -- Alpaca is optional per-user


# --- session revocation ---

def test_change_password_revokes_existing_sessions():
    engine = _fresh_engine()
    user_id = _seed_user(engine)
    token = auth_service.create_session(engine, user_id)
    assert auth_service.validate_session(engine, token) is not None

    auth_service.change_password(engine, user_id, "a-brand-new-password-456")

    assert auth_service.validate_session(engine, token) is None
    with engine.connect() as conn:
        remaining = conn.execute(select(sessions_table).where(sessions_table.c.user_id == user_id)).fetchall()
    assert remaining == []


def test_disabling_a_user_revokes_existing_sessions():
    engine = _fresh_engine()
    user_id = _seed_user(engine)
    token = auth_service.create_session(engine, user_id)
    assert auth_service.validate_session(engine, token) is not None

    auth_service.set_user_status(engine, user_id, "disabled")

    assert auth_service.validate_session(engine, token) is None


def test_reactivating_a_user_does_not_touch_sessions():
    # set_user_status("active") should not itself revoke -- only disabling does.
    engine = _fresh_engine()
    user_id = _seed_user(engine)
    token = auth_service.create_session(engine, user_id)
    auth_service.set_user_status(engine, user_id, "active")
    assert auth_service.validate_session(engine, token) is not None


def test_validate_session_already_rejects_disabled_user_without_explicit_revoke():
    # Belt-and-suspenders check: even if a session row somehow survived,
    # validate_session's own status check independently blocks it.
    engine = _fresh_engine()
    user_id = _seed_user(engine, status="disabled")
    token = None
    with engine.begin() as conn:
        token = "manually-inserted-token"
        conn.execute(insert(sessions_table).values(
            session_token=token, user_id=user_id, created_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        ))
    assert auth_service.validate_session(engine, token) is None


# --- periodic session revalidation (src/dashboard/auth_gate.py) ---

def test_revalidate_if_due_skips_recheck_within_interval():
    import streamlit as st
    from src.dashboard import auth_gate

    engine = _fresh_engine()
    st.session_state[auth_gate.SESSION_TOKEN_KEY] = "some-token-never-checked"
    st.session_state[auth_gate.SESSION_LAST_VALIDATED_KEY] = datetime.now(timezone.utc)  # just now
    # Not due yet -- must return True WITHOUT calling validate_session at
    # all (if it did, this invalid token would fail and prove the guard
    # didn't work).
    assert auth_gate._revalidate_if_due(engine) is True
    assert st.session_state.get(auth_gate.SESSION_TOKEN_KEY) == "some-token-never-checked"


def test_revalidate_if_due_clears_session_when_revoked_since_last_check():
    import streamlit as st
    from src.dashboard import auth_gate

    engine = _fresh_engine()
    user_id = _seed_user(engine)
    token = auth_service.create_session(engine, user_id)
    st.session_state[auth_gate.SESSION_USER_KEY] = {"id": user_id, "username": "alice"}
    st.session_state[auth_gate.SESSION_TOKEN_KEY] = token
    # Simulate the revalidation interval having elapsed.
    st.session_state[auth_gate.SESSION_LAST_VALIDATED_KEY] = (
        datetime.now(timezone.utc) - timedelta(seconds=auth_gate.SESSION_REVALIDATE_INTERVAL_SECONDS + 1)
    )
    # Revoke the session (e.g. an admin disabled the account) AFTER it was cached.
    auth_service.revoke_session(engine, token)

    original_clear_cookie = auth_gate._clear_session_cookie
    auth_gate._clear_session_cookie = lambda: None  # avoid real CookieManager component construction
    try:
        result = auth_gate._revalidate_if_due(engine)
    finally:
        auth_gate._clear_session_cookie = original_clear_cookie

    assert result is False
    assert auth_gate.SESSION_USER_KEY not in st.session_state
    assert auth_gate.SESSION_TOKEN_KEY not in st.session_state


def test_revalidate_if_due_refreshes_cached_user_when_still_valid():
    import streamlit as st
    from src.dashboard import auth_gate

    engine = _fresh_engine()
    user_id = _seed_user(engine)
    token = auth_service.create_session(engine, user_id)
    st.session_state[auth_gate.SESSION_USER_KEY] = {"id": user_id, "username": "stale-cached-copy"}
    st.session_state[auth_gate.SESSION_TOKEN_KEY] = token
    st.session_state[auth_gate.SESSION_LAST_VALIDATED_KEY] = (
        datetime.now(timezone.utc) - timedelta(seconds=auth_gate.SESSION_REVALIDATE_INTERVAL_SECONDS + 1)
    )

    result = auth_gate._revalidate_if_due(engine)

    assert result is True
    assert st.session_state[auth_gate.SESSION_USER_KEY]["username"] == "alice"  # refreshed from DB
    # Timestamp advanced -- next call within the interval won't re-check again.
    assert (datetime.now(timezone.utc) - st.session_state[auth_gate.SESSION_LAST_VALIDATED_KEY]).total_seconds() < 5
