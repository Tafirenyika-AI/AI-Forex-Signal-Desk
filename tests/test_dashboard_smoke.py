"""Headless dashboard smoke test (AppTest) — run before/after any dashboard
change. Pre-seeds a real admin user AND a real, valid session token into
session_state to bypass the cookie-based login gate, which AppTest can't
drive directly (see src/dashboard/auth_gate.py's SESSION_USER_KEY =
"auth_user").

Also seeds auth_last_validated_at = now (P0-05, 2026-09-22): require_auth()
now periodically re-validates the cached session against the DB (see
auth_gate.SESSION_REVALIDATE_INTERVAL_SECONDS) — without a real token AND
a fresh timestamp, this test would silently fall through to the LOGIN
FORM instead of the actual dashboard (still "passing" — no exception —
but testing something much weaker than intended, since a rendered login
form has no exceptions either). Caught live: this exact regression
happened while building the revalidation feature; the AppTest markdown
output showed "### Sign in" instead of any real dashboard content, with
`at.exception` still empty. A green run here from now on is only a real
signal if it also implies "reached the actual dashboard" — this seeding
makes that true again.

Run: .venv/Scripts/python.exe -m pytest tests/test_dashboard_smoke.py -v
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from streamlit.testing.v1 import AppTest

from src.auth import service as auth_service
from src.config import load_settings
from src.data.db import get_engine

ADMIN_USER = {
    "id": 1,
    "email": "shoniwatafirenyika@gmail.com",
    "username": "shoniwatafirenyika@gmail.com",
    "is_admin": True,
    "status": "active",
}


def test_dashboard_loads_without_exceptions():
    settings = load_settings()
    engine = get_engine(settings.db_path)
    token = auth_service.create_session(engine, ADMIN_USER["id"])
    try:
        at = AppTest.from_file(str(PROJECT_ROOT / "src" / "dashboard" / "app.py"), default_timeout=120)
        at.session_state["auth_user"] = ADMIN_USER
        at.session_state["auth_session_token"] = token
        at.session_state["auth_last_validated_at"] = datetime.now(timezone.utc)
        at.run()
        assert not at.exception, f"Dashboard raised on load: {at.exception}"
        # Confirms the run actually reached the real dashboard, not a
        # login-form fallback that would also show zero exceptions.
        rendered_text = " ".join(md.value for md in at.markdown)
        assert "Sign in" not in rendered_text, (
            "Dashboard fell back to the login form instead of the authenticated "
            "view -- session pre-seeding is no longer sufficient to bypass the "
            "login gate (see auth_gate.py's require_auth/_revalidate_if_due)."
        )
    finally:
        auth_service.revoke_session(engine, token)
