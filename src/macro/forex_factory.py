"""Macro/Event Gateway - Forex Factory calendar feed (blueprint sec. 4.2).

This is the piece FRED cannot provide: a genuine forward-looking release
schedule with an impact tier (Low/Medium/High/Holiday) and the market's
consensus forecast. Verified live 2026-08-12 against
https://nfs.faireconomy.media/ff_calendar_thisweek.json (and _nextweek.json)
— real events, real forecast/previous values, real impact tags.

Important honesty notes:
- No "actual" (realized) field exists on this feed at all, even for events
  that have already occurred. It answers "when, how important, what's
  expected" — not "what actually happened." That's enough to drive the risk
  governor's event-timing gate, but NOT enough on its own to compute a true
  consensus-surprise score; `actual` is left None here on purpose. Cross-
  referencing against FRED's realized values by matching event names is a
  real future upgrade, not attempted in v1 — the two sources don't share IDs
  and naming doesn't line up cleanly enough to fake it.
- There is no API key and no authenticated per-caller quota — this URL is
  rate-limited to roughly 2 requests / 5 minutes *shared across everyone
  hitting this exact endpoint*, not a private allocation. Fetch it
  infrequently (this repo's ingest script pulls it standalone, not once per
  decision cycle) and cache the results in the database.
- This is a widely-used, semi-official feed (it's what powers the "download
  calendar" button and countless retail EAs), but it is not a formally
  licensed, contractually-supported product — no SLA, no support channel,
  and the URL/format could change without notice. Treat it as free best-
  effort data, not a guaranteed feed, and keep the FRED-based components
  working independently of it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

BASE_URL = "https://nfs.faireconomy.media"
FEEDS = ["ff_calendar_thisweek.json", "ff_calendar_nextweek.json"]

IMPORTANCE_MAP = {
    "Low": "low",
    "Medium": "medium",
    "High": "high",
    "Holiday": "holiday",
}

# FF's country codes are already ISO currency codes for our v1 pairs — no
# mapping table needed, unlike FRED's country-name normalization.
SUPPORTED_CURRENCIES = {"USD", "EUR", "GBP", "JPY", "CAD", "AUD"}


def _parse_forecast(value: str) -> float | None:
    """Best-effort numeric parse of FF's formatted strings ("5.7%", "2.50T",
    "44.6"). Returns None rather than guessing when the format is unclear —
    a wrong parsed number is worse than a missing one."""
    if not value:
        return None
    cleaned = value.strip().rstrip("%")
    multiplier = 1.0
    for suffix, mult in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            multiplier = mult
            break
    try:
        return float(cleaned) * multiplier
    except ValueError:
        return None


class ForexFactoryCalendar:
    def __init__(self):
        self._client = httpx.AsyncClient(
            base_url=BASE_URL, timeout=15.0, headers={"User-Agent": "Mozilla/5.0"}
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "ForexFactoryCalendar":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def fetch_upcoming_events(self) -> list[dict[str, Any]]:
        """Pulls this week + next week (2 requests total — stays within the
        feed's shared rate budget in one call)."""
        now = datetime.now(timezone.utc)
        rows: list[dict[str, Any]] = []
        for feed in FEEDS:
            response = await self._client.get(f"/{feed}")
            if response.status_code != 200:
                continue
            for item in response.json():
                currency = item.get("country", "")
                if currency not in SUPPORTED_CURRENCIES:
                    continue
                impact = IMPORTANCE_MAP.get(item.get("impact", ""), "unknown")
                if impact == "holiday":
                    continue
                try:
                    event_time = datetime.fromisoformat(item["date"])
                except (ValueError, KeyError):
                    continue
                event_time = event_time.astimezone(timezone.utc)

                rows.append(
                    {
                        "event_time": event_time,
                        "ingested_at": now,
                        "country": currency,
                        "currency": currency,
                        "event_name": item.get("title", "unknown"),
                        "source": "ForexFactory",
                        "consensus": _parse_forecast(item.get("forecast", "")),
                        "previous": _parse_forecast(item.get("previous", "")),
                        "actual": None,  # not provided by this feed — see module docstring
                        "revised": None,
                        "importance": impact,
                        "unit": None,
                    }
                )
        return rows
