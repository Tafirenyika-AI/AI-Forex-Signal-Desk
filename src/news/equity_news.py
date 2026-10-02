"""Equity V2 Phase 6 — equity news intelligence.

Deliberately separate from src/news/alpha_vantage.py and src/news/gdelt.py
(forex news paths — untouched by this phase) and from the existing
currency-keyed news_events table (Phase 3's own instruction: "do NOT
repurpose existing currency-keyed tables"). Writes into equity_news
instead, ticker-aware from the ground up.

Two distinct jobs live here, matching the brief's own Phase 6 correction
("classify event types... measure actual subsequent market reaction
rather than equating sentiment with BUY"):

1. AlpacaNewsClient + classify_event_type: fetch real articles and tag
   them with a coarse event type via keyword matching — a disclosed
   heuristic, not an NLP/ML classifier, same honesty standard as
   src/news/alpha_vantage.py's own module docstring about its real
   free-tier limitations.
2. compute_reactions: fills in equity_news.price_reaction_1h/1d AFTER the
   fact, from this project's own already-stored candles — never estimated,
   never treated as known at publish time. A row is only processed once
   enough real time has actually passed for both windows to have genuine
   price data (see _REACTION_MIN_AGE) — this is the mechanism that
   prevents sentiment from ever being equated with a trade outcome:
   whether a headline was followed by a real move is measured, not assumed.

Alpaca's News API (https://data.alpaca.markets/v1beta1/news) was chosen
over Alpha Vantage's NEWS_SENTIMENT for equities specifically because it
uses credentials this project already has (no second API key to manage)
and has a far more generous free-tier rate limit — verified live
2026-10-02 against this account's real credentials, including a check
that a non-equity symbol like QQQ returns sensible results too (ETFs do
get real news coverage even though they have no SEC CIK, per Phase 4's
own disclosed QQQ-has-no-CIK finding).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.data.db import candles as candles_table
from src.data.db import equity_news as equity_news_table

ALPACA_NEWS_URL = "https://data.alpaca.markets/v1beta1/news"

# A deliberately simple, disclosed keyword heuristic -- not an NLP/ML
# classifier. Order matters: checked top-to-bottom, first match wins, so
# more specific phrases are listed before more generic ones (e.g.
# "earnings" before "analyst", since an earnings-reaction analyst note
# should classify as EARNINGS, not ANALYST_RATING). Same vocabulary as
# company_events.event_type (Phase 3) for cross-referencing the two.
_EVENT_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("EARNINGS", ("earnings", "quarterly results", "q1 results", "q2 results", "q3 results",
                  "q4 results", "beats estimates", "misses estimates", "eps of", "reports revenue")),
    ("GUIDANCE", ("guidance", "outlook raised", "outlook cut", "raises its forecast", "lowers its forecast")),
    ("M&A", ("to acquire", "acquisition of", "merger", "to buy", "buyout", "takeover bid")),
    ("SEC_FILING", ("10-k", "10-q", "8-k", "sec filing", "files with the sec")),
    ("DIVIDEND", ("dividend",)),
    ("SPLIT", ("stock split", "share split")),
    ("LEGAL", ("lawsuit", "sec investigation", "settles", "class action", "subpoena")),
    ("ANALYST_RATING", ("upgrades", "downgrades", "price target", "initiates coverage")),
    ("PRODUCT", ("unveils", "launches", "announces new")),
]


def classify_event_type(headline: str, summary: str = "") -> str | None:
    """None (not a forced "OTHER" bucket) when nothing matches -- an
    unclassified article is a real, common outcome, not a gap to paper
    over with a catch-all label that would just mean "we don't know"."""
    text = f"{headline} {summary}".lower()
    for event_type, keywords in _EVENT_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return event_type
    return None


def _parse_alpaca_news_time(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)


def _article_to_row(article: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    symbols = article.get("symbols") or []
    if not symbols:
        return None  # an article with no ticker tag at all isn't equity-actionable
    headline = article.get("headline", "")
    summary = article.get("summary", "")
    return {
        "publish_time": _parse_alpaca_news_time(article["created_at"]),
        "ingest_time": now,
        "source": article.get("source") or "alpaca_news",
        "headline": headline,
        "url": article.get("url"),
        "tickers": ",".join(sorted(symbols)),
        "event_type": classify_event_type(headline, summary),
        "sentiment_score": None,  # Alpaca's News API doesn't provide sentiment -- not fabricated here
        "novelty_score": None,  # requires a dedup/similarity pass; not in this phase
        "confidence": None,
        "price_reaction_1h": None,
        "price_reaction_1d": None,
        "reaction_computed_at": None,
    }


class AlpacaNewsClient:
    def __init__(self, api_key: str, api_secret: str):
        self._client = httpx.AsyncClient(
            headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret}, timeout=15.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AlpacaNewsClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def fetch_news(self, symbols: list[str], limit: int = 50) -> list[dict[str, Any]]:
        """Raw articles for these symbols, newest first (Alpaca's own
        default sort) -- normalization into equity_news rows happens
        separately in news_rows_for(), kept apart so the raw-fetch/
        normalize/classify steps are each independently testable."""
        response = await self._client.get(
            ALPACA_NEWS_URL, params={"symbols": ",".join(symbols), "limit": limit},
        )
        response.raise_for_status()
        return response.json().get("news", [])

    async def news_rows_for(self, symbols: list[str], limit: int = 50) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        articles = await self.fetch_news(symbols, limit=limit)
        rows = [_article_to_row(a, now) for a in articles]
        return [r for r in rows if r is not None]


# --- Reaction measurement: fills equity_news.price_reaction_1h/1d AFTER
# the fact, from this project's own stored candles. ---

# Only a row whose BOTH reaction windows are fully in the past gets
# processed -- a margin above the raw 24h target so a slightly-delayed
# candle backfill cycle doesn't race this job and leave a permanently
# unfilled NULL for a row that's actually old enough.
_REACTION_MIN_AGE = timedelta(hours=27)
_BASELINE_MAX_LOOKAHEAD = timedelta(hours=3)  # publish could land outside market hours
_REACTION_1H_MAX_LOOKAHEAD = timedelta(hours=4)
_REACTION_1D_MAX_LOOKAHEAD = timedelta(hours=51)  # covers a weekend/holiday gap after publish


def _price_at_or_after(engine: Engine, instrument: str, granularity: str, target: datetime, max_lookahead: timedelta) -> float | None:
    stmt = (
        select(candles_table.c.close)
        .where(
            candles_table.c.broker == "alpaca",
            candles_table.c.instrument == instrument,
            candles_table.c.granularity == granularity,
            candles_table.c.complete.is_(True),
            candles_table.c.time >= target,
            candles_table.c.time <= target + max_lookahead,
        )
        .order_by(candles_table.c.time.asc())
        .limit(1)
    )
    with engine.connect() as conn:
        row = conn.execute(stmt).first()
    return row.close if row is not None else None


def compute_reaction(engine: Engine, instrument: str, publish_time: datetime) -> tuple[float | None, float | None]:
    """Returns (reaction_1h, reaction_1d) as fractional price changes from
    the nearest real candle at/after publish_time. None (not 0, not
    interpolated) for whichever window has no real candle close enough --
    e.g. a thinly-traded instrument, or a lookup instrument this project
    never backfilled candles for at all."""
    baseline = _price_at_or_after(engine, instrument, "M15", publish_time, _BASELINE_MAX_LOOKAHEAD)
    if baseline is None or baseline == 0:
        return None, None

    price_1h = _price_at_or_after(engine, instrument, "M15", publish_time + timedelta(hours=1), _REACTION_1H_MAX_LOOKAHEAD)
    price_1d = _price_at_or_after(engine, instrument, "H1", publish_time + timedelta(hours=24), _REACTION_1D_MAX_LOOKAHEAD)

    reaction_1h = (price_1h - baseline) / baseline if price_1h is not None else None
    reaction_1d = (price_1d - baseline) / baseline if price_1d is not None else None
    return reaction_1h, reaction_1d


def pending_reaction_rows(engine: Engine, now: datetime | None = None) -> list[dict[str, Any]]:
    """Every equity_news row old enough for both reaction windows to have
    real data, that hasn't had its reaction computed yet."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - _REACTION_MIN_AGE
    stmt = select(equity_news_table).where(
        equity_news_table.c.reaction_computed_at.is_(None),
        equity_news_table.c.publish_time <= cutoff,
    )
    with engine.connect() as conn:
        return [dict(row._mapping) for row in conn.execute(stmt)]
