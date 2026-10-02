"""Equity V2 Phase 20 — deployment mode confirmation (the final phase of
the brief). Confirms, with direct automated tests rather than a one-time
manual read, that this entire 18-phase effort never touched the hard
"RESEARCH + PAPER ONLY" refusal for either broker, and never loosened any
existing risk constant — serving as a permanent regression guard, not
just a point-in-time confirmation.

Audited directly (not assumed) before writing these tests:
- src/config.py's _validate_environment (OANDA) and
  _validate_alpaca_environment (Alpaca) are the ONLY two places Settings()
  is ever constructed anywhere in src/ (confirmed by grep) — there is no
  other path that could bypass either refusal.
- _validate_alpaca_environment already had direct tests
  (tests/test_auth_hardening.py, from an earlier external-review phase);
  _validate_environment (OANDA's own refusal) had NONE at all — a real,
  confirmed gap this phase closes, matching the brief's own "confirm...
  for either broker" wording literally.
- Every genuinely new risk-adjacent gate this 18-phase effort added
  (Phase 12's max_position_pct_of_equity cap, Phase 13's five equity
  governor extensions, Phase 14's no_pyramid_same_symbol gate) made the
  system MORE conservative, never less — none of them loosened an
  existing constant. This phase locks the pre-existing constants'
  CURRENT values in as an explicit regression guard: if a future change
  ever alters one of these without deliberately updating this test too,
  that's exactly the kind of silent risk-increase the brief's own "do
  not increase leverage/risk to chase returns" instruction forbids.

Run: .venv/Scripts/python.exe -m pytest tests/test_deployment_mode_confirmation.py -v
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from src.config import _validate_alpaca_environment, _validate_environment, load_settings
from src.risk import governor


# --- OANDA live-trading refusal (previously had zero direct test coverage) ---

def test_oanda_practice_environment_is_allowed():
    _validate_environment("practice")  # must not raise


def test_oanda_live_environment_is_refused():
    with pytest.raises(RuntimeError, match="live"):
        _validate_environment("live")


def test_oanda_garbage_environment_value_is_also_refused_not_silently_allowed():
    with pytest.raises(RuntimeError):
        _validate_environment("production")


def test_load_settings_itself_refuses_a_live_oanda_environment(monkeypatch):
    # Integration-level, not just the pure validator in isolation: confirms
    # load_settings() actually CALLS _validate_environment, not just that
    # the function exists and works if invoked directly.
    monkeypatch.setenv("OANDA_API_TOKEN", "fake-token")
    monkeypatch.setenv("OANDA_ENVIRONMENT", "live")
    monkeypatch.setenv("OANDA_ACCOUNT_ID", "fake-account")
    with pytest.raises(RuntimeError, match="live"):
        load_settings()


# --- Alpaca live-trading refusal (already had direct unit tests on the
# pure validator in tests/test_auth_hardening.py; this adds the
# integration-level confirmation that load_settings() actually calls it) ---

def test_alpaca_paper_url_is_allowed():
    _validate_alpaca_environment("https://paper-api.alpaca.markets/v2")  # must not raise


def test_alpaca_live_url_is_refused():
    with pytest.raises(RuntimeError, match="paper"):
        _validate_alpaca_environment("https://api.alpaca.markets")


def test_load_settings_itself_refuses_a_live_alpaca_base_url(monkeypatch):
    monkeypatch.setenv("OANDA_API_TOKEN", "fake-token")
    monkeypatch.setenv("OANDA_ENVIRONMENT", "practice")
    monkeypatch.setenv("OANDA_ACCOUNT_ID", "fake-account")
    monkeypatch.setenv("ALPACA_BASE_URL", "https://api.alpaca.markets")
    with pytest.raises(RuntimeError, match="paper"):
        load_settings()


# --- risk constants: locked at their current, pre-existing values as an
# explicit regression guard -- none of this 18-phase effort's own new
# gates (Phase 12/13/14) ever loosened any of these. ---

def test_risk_per_trade_constants_unchanged():
    assert governor.RISK_PER_TRADE_PCT == 0.01
    assert governor.RISK_PER_TRADE_CEILING_PCT == 0.03


def test_daily_and_weekly_loss_limits_unchanged():
    assert governor.DAILY_LOSS_LIMIT_PCT == 0.015
    assert governor.WEEKLY_LOSS_LIMIT_PCT == 0.04


def test_max_concurrent_positions_and_correlation_cap_unchanged():
    assert governor.MAX_CONCURRENT_POSITIONS == 3
    assert governor.MAX_CORRELATED_EXPOSURE_PCT == 0.05


def test_equity_crypto_no_leverage_notional_ceiling_unchanged():
    assert governor.MAX_EQUITY_CRYPTO_NOTIONAL_PCT == 1.0


def test_min_confidence_unchanged():
    assert governor.MIN_CONFIDENCE == 0.38
