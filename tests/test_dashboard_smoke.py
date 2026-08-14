"""Headless dashboard smoke test (AppTest) — run before/after any dashboard
change. Pre-seeds a real admin user into session_state to bypass the
cookie-based login gate, which AppTest can't drive directly (see
src/dashboard/auth_gate.py's SESSION_USER_KEY = "auth_user").

Run: .venv/Scripts/python.exe -m pytest tests/test_dashboard_smoke.py -v
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from streamlit.testing.v1 import AppTest

ADMIN_USER = {
    "id": 1,
    "email": "shoniwatafirenyika@gmail.com",
    "username": "shoniwatafirenyika@gmail.com",
    "is_admin": True,
    "status": "active",
}


def test_dashboard_loads_without_exceptions():
    at = AppTest.from_file(str(PROJECT_ROOT / "src" / "dashboard" / "app.py"), default_timeout=120)
    at.session_state["auth_user"] = ADMIN_USER
    at.run()
    assert not at.exception, f"Dashboard raised on load: {at.exception}"
