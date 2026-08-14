"""Macro/Event Gateway - FRED adapter (blueprint sec. 4.2, official validation source).

Important honesty note: FRED gives actual values and revision vintages via
ALFRED (point-in-time), but it does NOT give analyst consensus. Anything
this adapter produces has `consensus=None` — "surprise vs prior", not
"surprise vs consensus". A paid calendar (Trading Economics, per the
blueprint) is required before a real macro-surprise-vs-consensus feature is
possible. Don't let a later component silently treat previous-value drift
as if it were consensus surprise.

Get a free key (instant, no cost) at:
https://fred.stlouisfed.org/docs/api/api_key.html
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

FRED_BASE_URL = "https://api.stlouisfed.org/fred"

# currency -> [(series_id, human event name), ...]. Kept small and high-signal
# on purpose (sec. 4.2 signal families: inflation, labor, growth, policy).
#
# Every series_id below was verified against the live FRED API (2026-08-12)
# before being added here — some of the non-US series (esp. UK/JPY/CAD
# central bank rates, and several CPI mirrors) update with a real lag,
# sometimes a year or more behind the national statistics office's own
# release. That's a known FRED-mirror limitation, not a bug: it's safe to
# include anyway because src/models/macro_model.py's recency decay already
# fades old observations toward zero weight, so a stale series just quietly
# stops contributing rather than misleading anything. Two candidates (a UK
# and an AUD central-bank-rate series) returned HTTP 400 and were dropped —
# those currencies rely on CPI/unemployment/GDP only until a working rate
# series is found.
KEY_SERIES: dict[str, list[tuple[str, str]]] = {
    "USD": [
        ("CPIAUCSL", "US CPI (headline, SA)"),
        ("PAYEMS", "US Nonfarm Payrolls"),
        ("UNRATE", "US Unemployment Rate"),
        ("FEDFUNDS", "US Fed Funds Rate"),
        ("GDP", "US GDP"),
    ],
    "EUR": [
        ("ECBDFR", "EUR Deposit Facility Rate"),
        ("CP0000EZ19M086NEST", "Euro Area HICP"),
        ("LRHUTTTTEZM156S", "Euro Area Unemployment Rate"),
        ("CLVMEURSCAB1GQEA19", "Euro Area GDP"),
    ],
    "GBP": [
        ("BOERUKQ", "UK Bank Rate"),
        ("GBRCPIALLMINMEI", "UK CPI"),
        ("LRHUTTTTGBM156S", "UK Unemployment Rate"),
        ("UKNGDP", "UK GDP"),
    ],
    "JPY": [
        ("IRSTCB01JPM156N", "Japan Policy Rate"),
        ("JPNCPIALLMINMEI", "Japan CPI"),
        ("LRHUTTTTJPM156S", "Japan Unemployment Rate"),
        ("JPNRGDPEXP", "Japan GDP"),
    ],
    "CAD": [
        ("IRSTCB01CAM156N", "Canada Policy Rate"),
        ("CANCPIALLMINMEI", "Canada CPI"),
        ("LRHUTTTTCAM156S", "Canada Unemployment Rate"),
        ("NGDPRSAXDCCAQ", "Canada GDP"),
    ],
    "AUD": [
        ("AUSCPIALLQINMEI", "Australia CPI"),
        ("LRHUTTTTAUM156S", "Australia Unemployment Rate"),
        ("NGDPRSAXDCAUQ", "Australia GDP"),
    ],
}

COUNTRY_NAMES = {
    "USD": "US", "EUR": "Euro Area", "GBP": "UK", "JPY": "Japan", "CAD": "Canada", "AUD": "Australia",
}

# Cross-market confirmation inputs (blueprint sec. 6, row 5: "Rates/yields,
# dollar index proxies, commodities where relevant"). All daily/weekly
# series, verified live against FRED 2026-08-12.
MARKET_INDICATOR_SERIES = {
    "US10Y": "DGS10",       # 10-Year Treasury yield
    "US2Y": "DGS2",         # 2-Year Treasury yield
    "USD_INDEX": "DTWEXBGS",  # Trade-weighted USD index (broad) — DXY proxy
    "WTI_OIL": "DCOILWTICO",  # WTI crude — relevant mainly to USD/CAD (Appendix A)
}


class FredClient:
    def __init__(self, api_key: str | None):
        self.api_key = api_key
        self._client = httpx.AsyncClient(base_url=FRED_BASE_URL, timeout=15.0)

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "FredClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def get_observations(
        self,
        series_id: str,
        realtime_start: str | None = None,
        realtime_end: str | None = None,
    ) -> list[dict[str, Any]]:
        """Point-in-time observations. Passing realtime_start/end pins the
        vintage of the data as it was known at that time — this is what
        prevents revised values from leaking into historical training
        examples (sec. 6.2)."""
        if not self.configured:
            raise RuntimeError("FRED_API_KEY not configured; skipping FRED ingestion.")

        params = {
            "series_id": series_id,
            "api_key": self.api_key,
            "file_type": "json",
        }
        if realtime_start:
            params["realtime_start"] = realtime_start
        if realtime_end:
            params["realtime_end"] = realtime_end

        response = await self._client.get("/series/observations", params=params)
        response.raise_for_status()
        return response.json().get("observations", [])

    async def market_indicator_rows(self, limit: int = 60) -> list[dict[str, Any]]:
        """Recent history (not just latest) for each cross-market series —
        the cross-market model needs a short trend, not a single point, to
        say anything about direction/momentum."""
        if not self.configured:
            return []

        now = datetime.now(timezone.utc)
        rows = []
        for indicator, series_id in MARKET_INDICATOR_SERIES.items():
            observations = await self.get_observations(series_id)
            clean = [o for o in observations if o.get("value") not in (None, ".")][-limit:]
            for obs in clean:
                rows.append(
                    {
                        "indicator": indicator,
                        "observation_date": datetime.fromisoformat(obs["date"]).replace(tzinfo=timezone.utc),
                        "value": float(obs["value"]),
                        "source": "FRED",
                        "ingested_at": now,
                    }
                )
        return rows

    async def latest_event_rows(self, currency: str) -> list[dict[str, Any]]:
        """Returns normalized rows ready for the economic_events table:
        actual = latest observation, previous = prior observation,
        consensus = None (see module docstring), revised = None."""
        if not self.configured:
            return []

        series_list = KEY_SERIES.get(currency, [])
        rows = []
        now = datetime.now(timezone.utc)
        for series_id, event_name in series_list:
            observations = await self.get_observations(series_id)
            clean = [o for o in observations if o.get("value") not in (None, ".")]
            if len(clean) < 2:
                continue
            latest, prior = clean[-1], clean[-2]
            rows.append(
                {
                    "event_time": datetime.fromisoformat(latest["date"]).replace(
                        tzinfo=timezone.utc
                    ),
                    "ingested_at": now,
                    "country": COUNTRY_NAMES.get(currency, currency),
                    "currency": currency,
                    "event_name": event_name,
                    "source": "FRED",
                    "consensus": None,
                    "previous": float(prior["value"]),
                    "actual": float(latest["value"]),
                    "revised": None,
                    "importance": None,
                    "unit": None,
                }
            )
        return rows
