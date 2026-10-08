"""Unit tests for AI Trading Desk V4 Section 2: src/v4/feature_flags.py.

Run: .venv/Scripts/python.exe -m pytest tests/test_v4_feature_flags.py -v
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from src.v4 import feature_flags


@pytest.mark.parametrize("flag_fn,env_name,safe_default", [
    (feature_flags.v4_enabled, "V4_ENABLED", False),
    (feature_flags.v4_shadow_only, "V4_SHADOW_ONLY", True),
    (feature_flags.v4_auto_promotion, "V4_AUTO_PROMOTION", False),
    (feature_flags.v4_allow_new_paper_orders, "V4_ALLOW_NEW_PAPER_ORDERS", False),
    (feature_flags.v4_tradingview_enabled, "V4_TRADINGVIEW_ENABLED", False),
])
def test_default_matches_the_briefs_own_safe_value(flag_fn, env_name, safe_default, monkeypatch):
    monkeypatch.delenv(env_name, raising=False)
    assert flag_fn() == safe_default


@pytest.mark.parametrize("flag_fn,env_name", [
    (feature_flags.v4_enabled, "V4_ENABLED"),
    (feature_flags.v4_shadow_only, "V4_SHADOW_ONLY"),
    (feature_flags.v4_auto_promotion, "V4_AUTO_PROMOTION"),
    (feature_flags.v4_allow_new_paper_orders, "V4_ALLOW_NEW_PAPER_ORDERS"),
    (feature_flags.v4_tradingview_enabled, "V4_TRADINGVIEW_ENABLED"),
])
@pytest.mark.parametrize("truthy_value", ["1", "true", "True", "YES", "on"])
def test_truthy_string_values_enable_the_flag(flag_fn, env_name, truthy_value, monkeypatch):
    monkeypatch.setenv(env_name, truthy_value)
    assert flag_fn() is True


@pytest.mark.parametrize("flag_fn,env_name", [
    (feature_flags.v4_enabled, "V4_ENABLED"),
    (feature_flags.v4_shadow_only, "V4_SHADOW_ONLY"),
])
@pytest.mark.parametrize("falsy_value", ["0", "false", "False", "no", "off", ""])
def test_falsy_string_values_disable_the_flag(flag_fn, env_name, falsy_value, monkeypatch):
    monkeypatch.setenv(env_name, falsy_value)
    assert flag_fn() is False


def test_flags_are_read_fresh_not_cached(monkeypatch):
    # A human flipping this in the real environment must take effect
    # without a process restart -- confirms no caching anywhere in the
    # read path.
    monkeypatch.delenv("V4_ENABLED", raising=False)
    assert feature_flags.v4_enabled() is False
    monkeypatch.setenv("V4_ENABLED", "true")
    assert feature_flags.v4_enabled() is True
    monkeypatch.delenv("V4_ENABLED", raising=False)
    assert feature_flags.v4_enabled() is False
