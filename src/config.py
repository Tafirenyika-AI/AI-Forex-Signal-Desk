from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

OANDA_REST_HOSTS = {
    "practice": "https://api-fxpractice.oanda.com",
    "live": "https://api-fxtrade.oanda.com",
}
OANDA_STREAM_HOSTS = {
    "practice": "https://stream-fxpractice.oanda.com",
    "live": "https://stream-fxtrade.oanda.com",
}


@dataclass(frozen=True)
class Settings:
    oanda_api_token: str
    oanda_environment: str
    oanda_account_id: str
    db_path: Path
    fred_api_key: str | None
    alphavantage_api_key: str | None

    @property
    def oanda_rest_host(self) -> str:
        return OANDA_REST_HOSTS[self.oanda_environment]

    @property
    def oanda_stream_host(self) -> str:
        return OANDA_STREAM_HOSTS[self.oanda_environment]


def _validate_environment(environment: str) -> None:
    """Shared by load_settings() and settings_for_user() (src/auth) so a
    per-user credential set can never bypass this — the hard refusal to
    ever run against OANDA_ENVIRONMENT=live is a safety rail, not a default
    that per-user config should be able to override."""
    if environment not in OANDA_REST_HOSTS:
        raise RuntimeError(f"OANDA_ENVIRONMENT must be 'practice' or 'live', got {environment!r}")
    if environment == "live":
        raise RuntimeError(
            "OANDA_ENVIRONMENT is set to 'live'. This system is paper/demo only "
            "until it has passed backtesting, walk-forward testing and an extended "
            "live-paper evaluation. Refusing to start against a live account."
        )


def load_settings() -> Settings:
    token = os.environ.get("OANDA_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("OANDA_API_TOKEN is not set. Add it to your .env file.")

    environment = os.environ.get("OANDA_ENVIRONMENT", "practice").strip().lower()
    _validate_environment(environment)

    account_id = os.environ.get("OANDA_ACCOUNT_ID", "").strip()
    fred_key = os.environ.get("FRED_API_KEY", "").strip() or None
    av_key = os.environ.get("ALPHAVANTAGE_API_KEY", "").strip() or None

    return Settings(
        oanda_api_token=token,
        oanda_environment=environment,
        oanda_account_id=account_id,
        db_path=ROOT_DIR / "data" / "forex.db",
        fred_api_key=fred_key,
        alphavantage_api_key=av_key,
    )


def settings_for_user(oanda_api_token: str, oanda_environment: str, oanda_account_id: str) -> Settings:
    """Builds a Settings instance from a specific user's stored (decrypted)
    OANDA credentials rather than the process-global .env — src/auth/
    decrypts a user_oanda_accounts row and calls this. Reuses
    _validate_environment() so per-user credentials are held to the exact
    same live-trading refusal as the original single-user path."""
    token = oanda_api_token.strip()
    if not token:
        raise RuntimeError("No OANDA API token configured for this user yet.")
    environment = oanda_environment.strip().lower()
    _validate_environment(environment)
    return Settings(
        oanda_api_token=token,
        oanda_environment=environment,
        oanda_account_id=oanda_account_id.strip(),
        db_path=ROOT_DIR / "data" / "forex.db",
        fred_api_key=os.environ.get("FRED_API_KEY", "").strip() or None,
        alphavantage_api_key=os.environ.get("ALPHAVANTAGE_API_KEY", "").strip() or None,
    )


def auth_encryption_key() -> bytes:
    key = os.environ.get("AUTH_ENCRYPTION_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "AUTH_ENCRYPTION_KEY is not set. Add it to your .env file — generate one with: "
            "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return key.encode()
