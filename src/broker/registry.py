"""Instrument -> asset-class / broker routing, purely by string shape.

No schema change needed for user_preferences.instrument_list_json: OANDA
uses underscore-separated pairs (EUR_USD), Alpaca equities use a bare
ticker (AAPL), and Alpaca crypto uses a slash-separated pair (BTC/USD) —
these three shapes never collide, so classification is a one-line check
rather than a stored tag per instrument.
"""
from __future__ import annotations

from typing import Literal

AssetClass = Literal["forex", "equity", "crypto"]
BrokerKind = Literal["oanda", "alpaca"]


def asset_class_for(instrument: str) -> AssetClass:
    if "/" in instrument:
        return "crypto"
    if "_" in instrument:
        return "forex"
    return "equity"


def broker_kind_for(instrument: str) -> BrokerKind:
    return "oanda" if asset_class_for(instrument) == "forex" else "alpaca"
