"""Login / invite-only registration / first-run onboarding gate for the
Streamlit dashboard.

Session persistence: `st.session_state` (survives reruns within one
browser tab/WebSocket session) backed by a real DB-tracked session token
(`src/auth/service.py`'s `sessions` table) mirrored into a browser cookie
via `extra_streamlit_components.CookieManager`, so login also survives a
full page reload / new tab, not just in-app reruns.

**Honesty note, unlike everything else in this codebase**: the cookie half
of this has NOT been verified against a real browser. This project's
testing tools — headless `streamlit.testing.v1.AppTest` — don't execute
real browser-side Streamlit components, so `CookieManager` (which relies on
a JS component reading/writing `document.cookie`) can't be exercised the
same way the rest of this system's live-verified behavior has been. The
session-token/DB-validation half IS verified (plain Python, testable
headlessly); only the "does the cookie actually round-trip through a real
browser" half is not. If it doesn't work as expected, the fallback is
exactly today's behavior: re-login after a page reload, no worse than
before this was added.

Centering note: earlier versions of this file wrapped each auth form in
`st.markdown('<div class="af-auth-page">')` ... native widgets ...
`st.markdown('</div>')`. That doesn't actually nest the widgets inside the
div — Streamlit renders every `st.xxx()` call as its own sibling block, so
a div opened in one `st.markdown` call and closed in a later one wraps
nothing in the real DOM, and the CSS centering on it had no effect. Real
centering in Streamlit means using `st.columns(...)` so the form itself is
laid out in a narrow middle column — that's what's used below.
"""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path

import extra_streamlit_components as stx
import streamlit as st
from sqlalchemy.engine import Engine

from src.auth import service as auth_service
from src.broker.oanda import OandaBroker
from src.config import settings_for_user

SESSION_USER_KEY = "auth_user"
SESSION_TOKEN_KEY = "auth_session_token"
ONBOARDING_INSTRUMENTS_KEY = "onboarding_instruments"

COOKIE_NAME = "af_session_token"
COOKIE_MAX_AGE_DAYS = 30

LOGO_PATH = Path(__file__).resolve().parent / "assets" / "logo.png"


@st.cache_data(show_spinner=False)
def _logo_base64() -> str | None:
    if not LOGO_PATH.exists():
        return None
    return base64.b64encode(LOGO_PATH.read_bytes()).decode()


def _render_logo(width_px: int = 150) -> None:
    """One self-contained HTML string (image + centering div, all in a
    single st.markdown call) — safe from the div-spanning-multiple-calls
    bug described in the module docstring, since nothing else needs to
    nest inside it."""
    b64 = _logo_base64()
    if b64 is None:
        return
    st.markdown(
        f'<div style="text-align:center; margin-bottom:8px;">'
        f'<img src="data:image/png;base64,{b64}" width="{width_px}" /></div>',
        unsafe_allow_html=True,
    )


def _auth_columns():
    """The middle column every auth form renders into — the actual fix for
    "form isn't centered": real Streamlit-native layout, not CSS on an
    empty div."""
    left, center, right = st.columns([1, 1.3, 1])
    return center


def _cookie_manager() -> stx.CookieManager:
    # Deliberately NOT wrapped in @st.cache_resource: CookieManager's own
    # __init__ makes a Streamlit widget call internally (it reads all
    # cookies via a component call), and Streamlit explicitly forbids
    # widget calls inside a cached function ("Your script uses a widget
    # command in a cached function") — caught by this project's own
    # headless test suite before this ever reached a live session. A
    # session_state-backed singleton achieves the same "one instance per
    # session" goal without tripping that rule.
    if "_af_cookie_manager" not in st.session_state:
        st.session_state["_af_cookie_manager"] = stx.CookieManager(key="af_cookie_manager")
    return st.session_state["_af_cookie_manager"]


def _read_session_cookie() -> str | None:
    """None can mean either "no cookie" or "component hasn't responded
    yet" — this library resolves that ambiguity itself by triggering an
    automatic rerun once the real value arrives, so treating None as
    "nothing to restore (yet)" is the documented-safe behavior, not a
    guess. Wrapped in try/except since a component that never loads
    (e.g. this exact code path running outside a real browser, like
    headless tests) must fail open to the normal login screen, not hang."""
    try:
        return _cookie_manager().get(COOKIE_NAME)
    except Exception:  # noqa: BLE001
        return None


def _write_session_cookie(token: str) -> None:
    try:
        _cookie_manager().set(
            COOKIE_NAME, token, key="af_set_session_cookie",
            expires_at=datetime.now(timezone.utc) + timedelta(days=COOKIE_MAX_AGE_DAYS),
        )
    except Exception:  # noqa: BLE001 — cookie persistence is a bonus, never allowed to block login itself
        pass


def _clear_session_cookie() -> None:
    try:
        _cookie_manager().delete(COOKIE_NAME, key="af_delete_session_cookie")
    except Exception:  # noqa: BLE001
        pass


def current_user() -> dict | None:
    return st.session_state.get(SESSION_USER_KEY)


def _start_session(engine: Engine, user: dict) -> None:
    token = auth_service.create_session(engine, user["id"])
    st.session_state[SESSION_USER_KEY] = user
    st.session_state[SESSION_TOKEN_KEY] = token
    _write_session_cookie(token)


def _restore_session_from_cookie(engine: Engine) -> bool:
    token = _read_session_cookie()
    if not token:
        return False
    user = auth_service.validate_session(engine, token)
    if user is None:
        return False
    st.session_state[SESSION_USER_KEY] = user
    st.session_state[SESSION_TOKEN_KEY] = token
    return True


def _login_form(engine: Engine) -> None:
    with _auth_columns():
        with st.container(border=True):
            _render_logo()
            st.markdown("### Sign in")
            st.caption("Invite-only — ask an existing member for an invite link if you don't have an account yet.")
            with st.form("login_form"):
                identifier = st.text_input("Email or username")
                password = st.text_input("Password", type="password")
                submitted = st.form_submit_button("Sign in", width="stretch", type="primary")
            if submitted:
                user = auth_service.authenticate(engine, identifier, password)
                if user is None:
                    st.error("Incorrect email/username or password.")
                else:
                    _start_session(engine, user)
                    st.rerun()


def _registration_form(engine: Engine, token: str) -> None:
    invitation = auth_service.validate_invitation(engine, token)
    with _auth_columns():
        with st.container(border=True):
            _render_logo()
            if invitation is None:
                st.error("This invitation link is invalid, expired, or has already been used.")
                return

            st.markdown("### Complete your registration")
            st.caption(f"Invited as **{invitation['email']}**")
            with st.form("registration_form"):
                username = st.text_input("Choose a username")
                password = st.text_input("Choose a password", type="password")
                password2 = st.text_input("Confirm password", type="password")
                submitted = st.form_submit_button("Create account", width="stretch", type="primary")
            if submitted:
                if not username or not password:
                    st.error("Username and password are required.")
                elif password != password2:
                    st.error("Passwords don't match.")
                elif len(password) < 8:
                    st.error("Password must be at least 8 characters.")
                else:
                    try:
                        auth_service.register_via_invitation(engine, token, username, password)
                    except ValueError as exc:
                        st.error(str(exc))
                    else:
                        user = auth_service.authenticate(engine, username, password)
                        _start_session(engine, user)
                        st.success("Account created — welcome!")
                        st.rerun()


def _fetch_instruments(api_token: str, account_id: str, environment: str) -> dict[str, dict]:
    settings = settings_for_user(api_token, environment, account_id)

    async def _fetch() -> dict[str, dict]:
        async with OandaBroker(settings) as broker:
            return await broker.list_instruments()

    return asyncio.run(_fetch())


def _onboarding_credentials_step(engine: Engine, user: dict) -> None:
    with _auth_columns():
        with st.container(border=True):
            _render_logo()
            st.markdown("### One-time setup — step 1 of 2")
            st.caption(
                f"Welcome, {user['username']}. Before you can see any signals, connect your own OANDA "
                "practice account — every trade the system makes for you runs against this account, "
                "never anyone else's."
            )
            with st.form("onboarding_credentials_form"):
                api_token = st.text_input("OANDA API token", type="password",
                                           help="From your OANDA account's 'Manage API Access' page.")
                account_id = st.text_input("OANDA account ID", placeholder="e.g. 101-001-12345678-001")
                st.caption("Practice (demo) accounts only — this system refuses to run against a live OANDA account.")
                submitted = st.form_submit_button("Connect account", width="stretch", type="primary")
            if submitted:
                if not api_token.strip() or not account_id.strip():
                    st.error("Both fields are required.")
                else:
                    try:
                        # Fetch this account's own real tradable instrument list
                        # BEFORE saving anything — a bad token/account id shows up
                        # here as a real connection failure, not a silent save.
                        instruments = _fetch_instruments(api_token, account_id, "practice")
                        auth_service.save_oanda_credentials(engine, user["id"], api_token, account_id, "practice")
                    except Exception as exc:  # noqa: BLE001 — surface a real broker-side rejection to the form, not a crash
                        st.error(f"Could not connect: {exc!r}")
                    else:
                        st.session_state[ONBOARDING_INSTRUMENTS_KEY] = sorted(instruments.keys())
                        st.rerun()


def _onboarding_instruments_step(engine: Engine, user: dict, available_instruments: list[str]) -> None:
    with _auth_columns():
        with st.container(border=True):
            _render_logo()
            st.markdown("### One-time setup — step 2 of 2")
            st.caption(
                f"Connected — your account can trade {len(available_instruments)} instrument(s). "
                "Everything is selected by default; narrow it down if you'd rather start smaller "
                "(fewer instruments means faster cycles and less to review)."
            )
            with st.form("onboarding_instruments_form"):
                selected = st.multiselect(
                    "Instruments to trade", options=available_instruments, default=available_instruments,
                )
                auto_execute = st.checkbox(
                    "Auto-execute risk-approved trades (skip manual review)", value=False,
                    help="Every risk gate still applies either way — this only skips the human Authorize click.",
                )
                submitted = st.form_submit_button("Finish setup", width="stretch", type="primary")
            if submitted:
                if not selected:
                    st.error("Select at least one instrument.")
                else:
                    auth_service.save_preferences(
                        engine, user["id"], instrument_list=selected, execution_mode_default="demo",
                        auto_execute=auto_execute, onboarding_complete=True,
                    )
                    st.session_state.pop(ONBOARDING_INSTRUMENTS_KEY, None)
                    st.success("All set. Loading your dashboard...")
                    st.rerun()


def _onboarding_form(engine: Engine, user: dict) -> None:
    """First-run gate: nothing else in the app is reachable until a new
    user's own OANDA credentials (and an instrument list) are on file — the
    model has to know what account it's trading, and what for, before it
    can do anything at all, per the product requirement this was built for.
    Two steps because step 2 needs step 1's credentials to already be valid
    — you can't pick from a real instrument list before the account that
    defines it is connected."""
    available_instruments = st.session_state.get(ONBOARDING_INSTRUMENTS_KEY)
    if available_instruments is None:
        _onboarding_credentials_step(engine, user)
    else:
        _onboarding_instruments_step(engine, user, available_instruments)


def require_auth(engine: Engine) -> dict:
    """Call at the very top of app.py, before anything else renders.
    Returns the authenticated, onboarded user's row. Renders login/
    registration/onboarding and halts the script (st.stop()) otherwise —
    nothing below this call in app.py ever executes for a logged-out or
    not-yet-onboarded visitor."""
    query_params = st.query_params
    invite_token = query_params.get("invite")

    user = current_user()
    if user is None and not invite_token:
        # Only try to restore from a cookie on the plain login path — an
        # invite link always means "someone is deliberately registering a
        # new account right now," so it skips straight to that form even
        # if a stale cookie from a previous session happens to be present.
        if _restore_session_from_cookie(engine):
            user = current_user()

    if user is None:
        st.write("")
        st.write("")
        if invite_token:
            _registration_form(engine, invite_token)
        else:
            _login_form(engine)
        st.stop()

    prefs = auth_service.get_preferences(engine, user["id"])
    if prefs is None or not prefs["onboarding_complete"]:
        st.write("")
        st.write("")
        _onboarding_form(engine, user)
        st.stop()

    return user


def logout(engine: Engine) -> None:
    token = st.session_state.get(SESSION_TOKEN_KEY)
    if token:
        try:
            auth_service.revoke_session(engine, token)
        except Exception:  # noqa: BLE001 — logging out client-side must always succeed even if the DB call fails
            pass
    _clear_session_cookie()
    st.session_state.pop(SESSION_USER_KEY, None)
    st.session_state.pop(SESSION_TOKEN_KEY, None)
