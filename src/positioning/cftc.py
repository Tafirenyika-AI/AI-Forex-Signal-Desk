"""CFTC Commitments of Traders (COT) positioning (Autonomous Upgrade Spec
sec. 6, sec. 10: "Positioning: Crowding, changes in exposure and extremes").

Free, no API key, real Socrata Open Data API — verified live 2026-08-13
against https://publicreporting.cftc.gov/resource/6dca-aqww.json (the
"Legacy Futures Only" report). Contract names for our 6 currencies
verified directly from a real response, not assumed: "EURO FX", "BRITISH
POUND", "JAPANESE YEN", "CANADIAN DOLLAR", "AUSTRALIAN DOLLAR", and — not
obviously named — a standalone "USD INDEX" contract that tracks the dollar
directly, same long=bullish sign convention as the others (no sign flip
needed, unlike some of this project's other USD-derived scores).

COT is published weekly (Fridays, reflecting the prior Tuesday) — a
genuinely different cadence from everything else in this system. Crowding
is measured self-relatively (z-score of the current net position against
its own trailing history, squashed through tanh), same philosophy as the
regime detector and cross-market model: no fixed magic-number thresholds.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

import httpx

CFTC_LEGACY_FUTURES_URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"

CONTRACT_NAMES = {
    "EUR": "EURO FX", "GBP": "BRITISH POUND", "JPY": "JAPANESE YEN",
    "CAD": "CANADIAN DOLLAR", "AUD": "AUSTRALIAN DOLLAR", "USD": "USD INDEX",
}

TRAILING_WEEKS_FOR_CROWDING = 52


async def fetch_positioning_history(currency: str, weeks: int = 60) -> list[dict[str, Any]]:
    contract = CONTRACT_NAMES.get(currency)
    if contract is None:
        return []
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            CFTC_LEGACY_FUTURES_URL,
            params={
                "contract_market_name": contract,
                "$order": "report_date_as_yyyy_mm_dd DESC",
                "$limit": weeks,
            },
        )
        if response.status_code != 200:
            return []
        rows = response.json()

    results = []
    for row in rows:
        try:
            report_date = datetime.fromisoformat(
                row["report_date_as_yyyy_mm_dd"].replace(".000", "")
            ).replace(tzinfo=timezone.utc)
            noncomm_long = float(row["noncomm_positions_long_all"])
            noncomm_short = float(row["noncomm_positions_short_all"])
            open_interest = float(row["open_interest_all"])
        except (KeyError, ValueError):
            continue
        results.append(
            {
                "currency": currency,
                "report_date": report_date,
                "noncomm_long": noncomm_long,
                "noncomm_short": noncomm_short,
                "net_position": noncomm_long - noncomm_short,
                "open_interest": open_interest,
            }
        )
    return results


def crowding_score(history_rows: list[dict[str, Any]]) -> tuple[float, float]:
    """(score, confidence). score positive = crowded long (bullish
    positioning extreme), negative = crowded short — a self-relative
    z-score of the latest net position against its own trailing weekly
    history, not a fixed threshold. Extreme crowding is often read as
    reversal risk, not confirmation — this returns the raw signed extremity;
    interpreting direction is left to the caller."""
    if len(history_rows) < 10:
        return 0.0, 0.0
    ordered = sorted(history_rows, key=lambda r: r["report_date"])
    net_positions = [r["net_position"] for r in ordered]
    latest = net_positions[-1]
    trailing = net_positions[-TRAILING_WEEKS_FOR_CROWDING:]

    mean = sum(trailing) / len(trailing)
    variance = sum((v - mean) ** 2 for v in trailing) / len(trailing)
    std_dev = math.sqrt(variance)
    if std_dev == 0:
        return 0.0, min(1.0, len(trailing) / TRAILING_WEEKS_FOR_CROWDING)

    z = (latest - mean) / std_dev
    score = math.tanh(z / 2)  # /2 keeps a "normal" 1-sigma move from already saturating
    confidence = min(1.0, len(trailing) / TRAILING_WEEKS_FOR_CROWDING)
    return score, confidence
