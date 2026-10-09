"""V4 Priority 8 — src/integrations/tradingview_webhook.py.

Run: .venv/Scripts/python.exe -m pytest tests/test_tradingview_webhook.py -v
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from src.integrations.tradingview_webhook import normalize_symbol, validate_and_normalize_alert

NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
SECRET = "real-shared-secret-value"


def _payload(**overrides):
    base = {
        "secret": SECRET, "symbol": "NASDAQ:AAPL", "action": "buy",
        "time": NOW.isoformat(), "strategy": "my_pine_strategy",
    }
    base.update(overrides)
    return base


def test_valid_alert_is_accepted_and_normalized():
    result = validate_and_normalize_alert(_payload(), NOW, SECRET)
    assert result.accepted is True
    assert result.symbol_normalized == "AAPL"
    assert result.action == "buy"
    assert result.strategy_name == "my_pine_strategy"
    assert result.dedup_key is not None


def test_no_secret_configured_on_server_always_rejects():
    result = validate_and_normalize_alert(_payload(), NOW, expected_secret=None)
    assert result.accepted is False
    assert "no shared secret" in result.rejection_reason


def test_wrong_secret_is_rejected():
    result = validate_and_normalize_alert(_payload(secret="wrong"), NOW, SECRET)
    assert result.accepted is False
    assert "invalid or missing shared secret" in result.rejection_reason


def test_missing_secret_field_is_rejected():
    payload = _payload()
    del payload["secret"]
    result = validate_and_normalize_alert(payload, NOW, SECRET)
    assert result.accepted is False


def test_missing_symbol_is_rejected():
    payload = _payload()
    del payload["symbol"]
    result = validate_and_normalize_alert(payload, NOW, SECRET)
    assert result.accepted is False
    assert "symbol" in result.rejection_reason


def test_invalid_action_is_rejected():
    result = validate_and_normalize_alert(_payload(action="yolo"), NOW, SECRET)
    assert result.accepted is False
    assert "action" in result.rejection_reason


def test_action_is_case_insensitive():
    result = validate_and_normalize_alert(_payload(action="BUY"), NOW, SECRET)
    assert result.accepted is True
    assert result.action == "buy"


def test_stale_timestamp_is_rejected_replay_protection():
    old_time = (NOW - timedelta(hours=2)).isoformat()
    result = validate_and_normalize_alert(_payload(time=old_time), NOW, SECRET)
    assert result.accepted is False
    assert "replay-protection" in result.rejection_reason


def test_future_timestamp_beyond_tolerance_is_also_rejected():
    future_time = (NOW + timedelta(hours=1)).isoformat()
    result = validate_and_normalize_alert(_payload(time=future_time), NOW, SECRET)
    assert result.accepted is False


def test_timestamp_within_tolerance_is_accepted():
    close_time = (NOW - timedelta(minutes=2)).isoformat()
    result = validate_and_normalize_alert(_payload(time=close_time), NOW, SECRET)
    assert result.accepted is True


def test_missing_timestamp_is_rejected():
    payload = _payload()
    del payload["time"]
    result = validate_and_normalize_alert(payload, NOW, SECRET)
    assert result.accepted is False
    assert "time" in result.rejection_reason


def test_unix_epoch_timestamp_is_accepted():
    result = validate_and_normalize_alert(_payload(time=NOW.timestamp()), NOW, SECRET)
    assert result.accepted is True


def test_two_identical_alerts_produce_the_same_dedup_key():
    r1 = validate_and_normalize_alert(_payload(), NOW, SECRET)
    r2 = validate_and_normalize_alert(_payload(), NOW, SECRET)
    assert r1.dedup_key == r2.dedup_key


def test_different_alerts_produce_different_dedup_keys():
    r1 = validate_and_normalize_alert(_payload(action="buy"), NOW, SECRET)
    r2 = validate_and_normalize_alert(_payload(action="sell"), NOW, SECRET)
    assert r1.dedup_key != r2.dedup_key


def test_crypto_symbol_normalization():
    assert normalize_symbol("BINANCE:BTCUSDT") == "BTC/USD"
    assert normalize_symbol("ETHUSDT") == "ETH/USD"


def test_equity_symbol_normalization_strips_exchange_prefix():
    assert normalize_symbol("NASDAQ:AAPL") == "AAPL"
    assert normalize_symbol("NYSE:GE") == "GE"


def test_symbol_already_in_project_format_is_unchanged():
    assert normalize_symbol("MSFT") == "MSFT"
    assert normalize_symbol("BTC/USD") == "BTC/USD"
