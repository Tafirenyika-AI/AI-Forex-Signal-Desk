"""Signal review & authorization dashboard.

This is the human interface the blueprint's architecture doesn't explicitly
draw (sec. 3) but the whole system depends on: run_loop.py proposes trades
and the risk governor clears them, but nothing gets sent to a broker until
a person reviews it here and clicks Authorize. Every decision — approve or
reject — is written to the `authorizations` table with who and when.

Run from the project root with the venv active:
    streamlit run src/dashboard/app.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Running `streamlit run src/dashboard/app.py` locally via `python -m
# streamlit ...` from the project root happens to work without this,
# because the `-m` flag itself adds the current working directory to
# sys.path — but Streamlit Community Cloud's own launcher doesn't invoke
# it that way, so `src` isn't importable as a top-level package there
# (confirmed live 2026-08-14: "ModuleNotFoundError: No module named
# 'src'" on Cloud only, never locally). Inserting the real project root
# (three levels up from this file: app.py -> dashboard -> src -> root)
# makes every `from src.xxx import ...` below resolve identically
# regardless of how or where this script is launched from.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import asyncio
import json
from datetime import datetime, timezone

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import Integer, func, select

from src.auth import service as user_auth_service
from src.authorization import service as auth_service
from src.broker.alpaca import AlpacaBroker
from src.broker.oanda import OandaBroker
from src.broker.registry import asset_class_for, broker_kind_for
from src.config import load_settings
from src.dashboard.auth_gate import LOGO_PATH, current_user, logout, require_auth
from src.dashboard.auth_gate import _logo_base64 as _brand_logo_base64
from src.data.db import (
    cftc_positioning as cftc_positioning_table,
    challenger_decisions as challenger_decisions_table,
    economic_events as economic_events_table,
    get_engine,
    invitations as invitations_table,
    knowledge_documents as knowledge_documents_table,
    market_indicators as market_indicators_table,
    model_registry as model_registry_table,
    news_events as news_events_table,
    orders_fills,
    predictions as predictions_table,
    risk_decisions as risk_decisions_table,
    risk_state,
    risk_state_weekly as risk_state_weekly_table,
    signal_evaluations as signal_evaluations_table,
    strategy_graveyard as strategy_graveyard_table,
    trade_analogs as trade_analogs_table,
    trade_attribution as trade_attribution_table,
    trade_intents,
    trade_outcomes as trade_outcomes_table,
    users as users_table,
)
from src.execution.paper_broker import PaperBroker
from src.execution.service import ExecutionService
from src.decision.fusion import COMPONENT_WEIGHTS, REGIME_WEIGHT_MULTIPLIERS
from src.evaluation.promotion_gates import build_report as promotion_build_report
from src.knowledge.retrieval import search as knowledge_search
from src.models.calibration import MIN_SEGMENT_SAMPLES as calibration_MIN_SEGMENT_SAMPLES
from src.models.calibration import all_reports as calibration_all_reports
from src.models.currency_strength import compute_all_currency_strengths
from src.models.train_meta_model import MIN_SAMPLES, MODEL_NAME
from src.models.track_record import all_track_records
from src.models.trading_sessions import current_session_state
from src.risk import governor as risk_governor
from src.run_loop import run_once

st.set_page_config(
    page_title="AI Forex — Signal Desk",
    page_icon=str(LOGO_PATH) if LOGO_PATH.exists() else "📡",
    layout="wide",
)

# ============================================================== design system
# Colors follow the dataviz skill's validated reference palette
# (references/palette.md): categorical slot 1 (blue) for neutral series,
# the fixed status set for WIN/LOSS/BREAKEVEN, "success text" tokens where
# text needs stronger contrast than a filled status mark. Light/dark both
# declared; @media follows the OS/browser preference since Streamlit's own
# theme toggle isn't exposed to injected CSS.
DESIGN_CSS = """
<style>
:root {
  --af-surface: #fcfcfb;
  --af-page: #f9f9f7;
  --af-ink: #0b0b0b;
  --af-ink-secondary: #52514e;
  --af-ink-muted: #898781;
  --af-border: rgba(11,11,11,0.10);
  --af-good-text: #006300;
  --af-good-bg: rgba(12,163,12,0.12);
  --af-bad-text: #c73c3b;
  --af-bad-bg: rgba(227,73,72,0.12);
  --af-neutral-text: #898781;
  --af-neutral-bg: rgba(137,135,129,0.14);
  --af-accent: #2a78d6;
  --af-accent-bg: rgba(42,120,214,0.10);

  /* Token scale (added for the platform redesign) — spacing/radius/shadow/
     type steps, extending the roles above rather than replacing them. Most
     existing rules below snap onto these exactly (they were reverse-picked
     from what was already hardcoded); a few bespoke sizes (hero stat value,
     small pill badge padding) intentionally stay literal rather than being
     forced onto a step they don't fit — same "hero figure is special-cased"
     principle the dataviz skill itself uses. */
  --af-space-1: 4px;
  --af-space-2: 8px;
  --af-space-3: 12px;
  --af-space-4: 16px;
  --af-space-5: 20px;
  --af-space-6: 24px;
  --af-space-8: 32px;

  --af-radius-sm: 8px;
  --af-radius-md: 14px;
  --af-radius-lg: 20px;
  --af-radius-pill: 999px;

  --af-shadow-1: 0 1px 4px rgba(11,11,11,0.05);
  --af-shadow-2: 0 2px 10px rgba(11,11,11,0.08);

  --af-text-xs: 0.74rem;
  --af-text-sm: 0.84rem;
  --af-text-base: 0.92rem;
  --af-text-lg: 1.02rem;
  --af-text-xl: 1.35rem;
  --af-text-2xl: 1.9rem;

  /* Chart tokens: fixed light, deliberately NOT inside the dark media query
     below. Plotly figures are rendered server-side in Python and can't
     detect the browser's prefers-color-scheme the way this CSS can, so
     every chart added for the redesign renders against one constant light
     surface (see CHART_COLORS below) regardless of card/page dark mode —
     an accepted tradeoff, not a bug. Declared here too so any pure-CSS
     chart-adjacent element (e.g. a meter track) stays visually consistent
     with the Plotly figures sitting next to it. */
  --af-chart-surface: #fcfcfb;
  --af-chart-grid: #e1e0d9;
  --af-chart-axis: #c3c2b7;
  --af-chart-ink: #0b0b0b;
  --af-chart-ink-secondary: #52514e;
  --af-chart-ink-muted: #898781;
}
@media (prefers-color-scheme: dark) {
  :root {
    --af-surface: #1a1a19;
    --af-page: #0d0d0d;
    --af-ink: #ffffff;
    --af-ink-secondary: #c3c2b7;
    --af-ink-muted: #898781;
    --af-border: rgba(255,255,255,0.10);
    --af-good-text: #2ecc2e;
    --af-good-bg: rgba(12,163,12,0.18);
    --af-bad-text: #e66767;
    --af-bad-bg: rgba(230,103,103,0.18);
    --af-neutral-text: #a3a19a;
    --af-neutral-bg: rgba(137,135,129,0.18);
    --af-accent: #3987e5;
    --af-accent-bg: rgba(57,135,229,0.14);
  }
}
.af-stat-tile {
  background: var(--af-surface);
  border: 1px solid var(--af-border);
  border-radius: var(--af-radius-md);
  padding: var(--af-space-4) var(--af-space-5);
  box-shadow: var(--af-shadow-1);
  height: 100%;
}
.af-stat-label {
  font-size: var(--af-text-xs);
  color: var(--af-ink-muted);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  font-weight: 600;
  margin-bottom: var(--af-space-1);
}
.af-stat-value {
  font-size: 1.7rem;
  font-weight: 700;
  color: var(--af-ink);
  font-variant-numeric: tabular-nums;
  line-height: 1.2;
}
.af-stat-value.positive { color: var(--af-good-text); }
.af-stat-value.negative { color: var(--af-bad-text); }
.af-stat-sub {
  font-size: var(--af-text-sm);
  color: var(--af-ink-secondary);
  margin-top: var(--af-space-1);
}
.af-badge {
  display: inline-block;
  padding: 2px 11px;
  border-radius: var(--af-radius-pill);
  font-size: var(--af-text-xs);
  font-weight: 700;
  letter-spacing: 0.02em;
  vertical-align: middle;
}
.af-badge-win { color: var(--af-good-text); background: var(--af-good-bg); }
.af-badge-loss { color: var(--af-bad-text); background: var(--af-bad-bg); }
.af-badge-neutral { color: var(--af-neutral-text); background: var(--af-neutral-bg); }
.af-trade-card {
  border: 1px solid var(--af-border);
  border-radius: var(--af-radius-md);
  padding: 14px var(--af-space-5);
  margin-bottom: var(--af-space-2);
  background: var(--af-surface);
}
.af-trade-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--af-space-2);
}
.af-trade-title {
  font-size: var(--af-text-lg);
  font-weight: 700;
  color: var(--af-ink);
}
.af-pl-value {
  font-size: 1.15rem;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.af-header-title {
  font-size: var(--af-text-2xl);
  font-weight: 800;
  color: var(--af-ink);
  margin-bottom: 0;
}
.af-header-tagline {
  color: var(--af-ink-secondary);
  font-size: var(--af-text-base);
  margin-top: 2px;
}
.af-role-badge {
  display: inline-block;
  padding: 2px 11px;
  border-radius: var(--af-radius-pill);
  font-size: var(--af-text-xs);
  font-weight: 700;
  letter-spacing: 0.03em;
  color: var(--af-accent);
  background: var(--af-accent-bg);
}
.af-section-card {
  background: var(--af-surface);
  border: 1px solid var(--af-border);
  border-radius: var(--af-radius-md);
  padding: var(--af-space-1) var(--af-space-1) 14px var(--af-space-1);
  box-shadow: var(--af-shadow-1);
  margin-bottom: var(--af-space-1);
}
.af-section-card [data-testid="stDataFrame"] { border-radius: var(--af-radius-sm); overflow: hidden; }
.af-empty-state {
  border: 1px dashed var(--af-border);
  border-radius: var(--af-radius-md);
  padding: 28px var(--af-space-5);
  text-align: center;
  color: var(--af-ink-muted);
  font-size: var(--af-text-base);
}
</style>
"""
st.markdown(DESIGN_CSS, unsafe_allow_html=True)

# Plotly figures are rendered server-side in Python, so they can't read the
# --af-chart-* CSS custom properties above (or react to prefers-color-scheme)
# — this dict is the Python-side mirror every chart function must build
# against, kept in sync with the :root chart tokens by hand. Also includes
# the dataviz skill's validated status/diverging colors (references/
# palette.md) for win/loss and signed-score charts.
CHART_COLORS = {
    "surface": "#fcfcfb",
    "grid": "#e1e0d9",
    "axis": "#c3c2b7",
    "ink": "#0b0b0b",
    "ink_secondary": "#52514e",
    "ink_muted": "#898781",
    "good": "#0ca30c",
    "warning": "#fab219",
    "serious": "#ec835a",
    "critical": "#d03b3b",
    "diverging_pos": "#2a78d6",
    "diverging_neg": "#e34948",
    "diverging_mid": "#f0efec",
    "accent": "#2a78d6",
}


@st.cache_resource
def get_settings_and_engine():
    settings = load_settings()
    engine = get_engine(settings.db_path)
    return settings, engine


_bootstrap_settings, engine = get_settings_and_engine()

# Auth gate: renders login/registration/onboarding and halts (st.stop())
# until someone's actually signed in with a real, onboarded account —
# nothing below this line ever runs for a logged-out visitor.
CURRENT_USER = require_auth(engine)
CURRENT_USER_ID = CURRENT_USER["id"]
CURRENT_IS_ADMIN = bool(CURRENT_USER["is_admin"])

# The logged-in user's OWN decrypted OANDA credentials — every broker call
# below runs against whoever is actually signed in, not a single shared
# .env account. `_bootstrap_settings` only ever supplied db_path to build
# `engine` above; it must not leak into any broker construction call.
settings = user_auth_service.get_user_settings(engine, CURRENT_USER_ID)


def run_async(coro):
    return asyncio.run(coro)


def format_duration(seconds: float) -> str:
    seconds = abs(int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _sparkline_svg(values: list[float], width: int = 64, height: int = 20,
                    accent: str = "#2a78d6", deemphasis: str = "#c3c2b7") -> str:
    """12-point-style sparkline as raw inline SVG (never Plotly) — a full
    Plotly.js instance per stat tile would mean 15+ competing chart inits
    on tabs like Mission Control/Currency Map on every rerun. Matches the
    dataviz skill's stat-tile figure contract: de-emphasis-hue line, only
    the current/last point in the accent color."""
    values = [v for v in values if v is not None]
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    pts = [(i / (len(values) - 1) * width, height - (v - lo) / span * height) for i, v in enumerate(values)]
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    last_x, last_y = pts[-1]
    return (
        f'<svg width="{width}" height="{height}" style="vertical-align:middle;">'
        f'<polyline points="{path}" fill="none" stroke="{deemphasis}" stroke-width="1.5" '
        f'stroke-linecap="round" stroke-linejoin="round"/>'
        f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="2.5" fill="{accent}"/>'
        f'</svg>'
    )


def stat_tile(label: str, value: str, sub: str = "", polarity: str | None = None,
              trend: list[float] | None = None) -> None:
    """polarity: 'positive' | 'negative' | None — colors the value token.
    trend: optional series of recent values rendered as a small sparkline
    below the sub-caption (see _sparkline_svg)."""
    cls = f" {polarity}" if polarity else ""
    spark = _sparkline_svg(trend) if trend else ""
    spark_html = f'<div style="margin-top:6px;">{spark}</div>' if spark else ""
    st.markdown(
        f'<div class="af-stat-tile"><div class="af-stat-label">{label}</div>'
        f'<div class="af-stat-value{cls}">{value}</div>'
        f'<div class="af-stat-sub">{sub}</div>{spark_html}</div>',
        unsafe_allow_html=True,
    )


def outcome_badge(outcome: str) -> str:
    styles = {"WIN": ("af-badge-win", "▲ WIN"), "LOSS": ("af-badge-loss", "▼ LOSS")}
    cls, label = styles.get(outcome, ("af-badge-neutral", "· BREAKEVEN"))
    return f'<span class="af-badge {cls}">{label}</span>'


def section_card(render_fn) -> None:
    """Wraps render_fn()'s output in the .af-section-card chrome.

    IMPORTANT: st.markdown('<div>...') / st.markdown('</div>') pairs do NOT
    actually nest native st.* widgets in the real DOM — every st.xxx() call
    renders as a sibling block, not a child of the preceding markdown div
    (see auth_gate.py's own module docstring, which already documents this
    exact trap from an earlier lesson in this project). The only reason the
    existing af-section-card usage visually looks nested today is that
    st.dataframe's own container happens to sit adjacent to the div and
    picks up the ambient card styling. Keep this helper's use limited to
    that same st.dataframe-wrapping case — do not assume it works for
    arbitrary render_fn content.
    """
    st.markdown('<div class="af-section-card">', unsafe_allow_html=True)
    render_fn()
    st.markdown('</div>', unsafe_allow_html=True)


def empty_state(message: str) -> None:
    st.markdown(f'<div class="af-empty-state">{message}</div>', unsafe_allow_html=True)


def trade_card_html(instrument: str, action: str, outcome: str, pl: float,
                     entry: float, exit_: float, opened, closed, duration: str,
                     units: int, execution_mode: str) -> str:
    # Real production crash 2026-08-18: a trade_outcomes row with a null
    # opened_at/closed_at (or entry/exit price) raised TypeError trying to
    # apply a date/float format spec to None. The caller already falls
    # back to "—" for `duration` when either timestamp is missing — this
    # extends the same honest-degrade fallback to the fields formatted
    # directly here, so a display function never crashes on missing data.
    pl_class = "positive" if pl > 0 else ("negative" if pl < 0 else "")
    opened_str = f"{opened:%b %d %H:%M}" if opened is not None else "—"
    closed_str = f"{closed:%b %d %H:%M}" if closed is not None else "—"
    entry_str = f"{entry:.5f}" if entry is not None else "—"
    exit_str = f"{exit_:.5f}" if exit_ is not None else "—"
    return (
        '<div class="af-trade-card"><div class="af-trade-row">'
        f'<div><span class="af-trade-title">{instrument} · {action}</span> '
        f'{outcome_badge(outcome)}</div>'
        f'<div class="af-pl-value {pl_class}">${pl:+,.2f}</div>'
        '</div>'
        f'<div class="af-stat-sub">Entry {entry_str} → Exit {exit_str} '
        f'&nbsp;·&nbsp; {opened_str} → {closed_str} UTC ({duration}) '
        f'&nbsp;·&nbsp; {units} units, {execution_mode}</div>'
        '</div>'
    )


def role_badge_html(is_admin: bool) -> str:
    return '<span class="af-role-badge">ADMIN</span>' if is_admin else '<span class="af-role-badge">MEMBER</span>'


async def _fetch_prices(instruments: list[str]):
    forex = [i for i in instruments if asset_class_for(i) == "forex"]
    non_forex = [i for i in instruments if asset_class_for(i) != "forex"]
    prices = []
    if forex:
        async with OandaBroker(settings) as broker:
            prices += await broker.get_current_prices(forex)
    if non_forex and settings.alpaca_api_key:
        async with AlpacaBroker(settings) as broker:
            prices += await broker.get_current_prices(non_forex)
    return prices


async def _fetch_account_state(mode: str):
    if mode == "paper":
        async with PaperBroker(settings, engine, user_id=CURRENT_USER_ID) as broker:
            return await broker.account_state(), await broker.positions()
    async with OandaBroker(settings) as broker:
        return await broker.account_state(), await broker.positions()


@st.cache_data(ttl=15, show_spinner=False)
def cached_prices(instruments: tuple[str, ...]):
    return run_async(_fetch_prices(list(instruments)))


async def _fetch_candles(instrument: str, granularity: str, count: int):
    # Broker chosen by instrument, not by the sidebar's paper/demo scan_mode
    # — PaperBroker.get_candles() just proxies to the same real OANDA-backed
    # candle source anyway (src/execution/paper_broker.py), so forex candle
    # data is genuinely global market state regardless of mode (same
    # reasoning already applied to cached_prices above, no user_id in the
    # cache key) — but Alpaca instruments were never reachable through
    # OandaBroker at all, mode-independent or not.
    if asset_class_for(instrument) != "forex":
        if not settings.alpaca_api_key:
            return []
        async with AlpacaBroker(settings) as broker:
            return await broker.get_candles(instrument, granularity, count)
    async with OandaBroker(settings) as broker:
        return await broker.get_candles(instrument, granularity, count)


@st.cache_data(ttl=60, show_spinner=False)
def cached_candles(instrument: str, granularity: str, count: int) -> list[dict]:
    # 60s TTL (vs. cached_prices' 15s): a chart's candle closes don't need
    # sub-minute freshness, and this caps OANDA API load from a chart that
    # could render on every rerun. Returns plain dicts (not Candle dataclass
    # instances) since that's what pd.DataFrame(...) wants downstream in
    # candlestick_figure.
    candles = run_async(_fetch_candles(instrument, granularity, count))
    return [
        {"time": c.time, "open": c.open, "high": c.high, "low": c.low, "close": c.close,
         "volume": c.volume, "complete": c.complete}
        for c in candles
    ]


@st.cache_data(ttl=60, show_spinner=False)
def cached_calibration_reports(source: str):
    # Was an uncached plain DB read before the redesign — fine when it only
    # drove a few stat/caption lines, less fine now that it also drives a
    # chart re-render on every rerun of this tab.
    return calibration_all_reports(engine, source)


@st.cache_data(ttl=15, show_spinner=False)
def cached_account_state(mode: str, user_id: int):
    # user_id is part of the cache key on purpose: st.cache_data is shared
    # across every session on this Streamlit process, not per-browser-tab —
    # without it, one user could see another user's cached account
    # balance/positions for up to the 15s TTL window.
    return run_async(_fetch_account_state(mode))


def current_pl_pct(kill_state: dict | None, week_row, mode: str) -> tuple[float | None, float | None]:
    """(daily_pl_pct, weekly_pl_pct) — None where that baseline hasn't been
    recorded yet. Read-only: computed from cached_account_state's live NAV
    against the day/week starting balance already stored in risk_state /
    risk_state_weekly, same as Mission Control did inline before this was
    extracted. Deliberately does NOT call risk_governor's private
    _get_or_init_day_state/_get_or_init_week_state — those insert a new
    state row as a side effect if one doesn't exist yet, which is wrong for
    a read-only display path. Caller wraps this in its own try/except,
    since a broker-fetch failure shouldn't take down the whole tile row."""
    account_mode = mode if mode != "shadow" else "demo"
    daily_pl_pct = None
    weekly_pl_pct = None
    if kill_state and kill_state.get("day_start_balance"):
        state, _ = cached_account_state(account_mode, CURRENT_USER_ID)
        daily_pl_pct = (state.nav - kill_state["day_start_balance"]) / kill_state["day_start_balance"]
    if week_row and week_row["week_start_balance"]:
        state, _ = cached_account_state(account_mode, CURRENT_USER_ID)
        weekly_pl_pct = (state.nav - week_row["week_start_balance"]) / week_row["week_start_balance"]
    return daily_pl_pct, weekly_pl_pct


async def _do_authorize(mode: str, trade_intent_id: int, decision: str, notes: str | None):
    # Broker is resolved from the trade_intent's own instrument, not from
    # the sidebar's mode selectbox — a single intent always belongs to
    # exactly one broker regardless of the paper/demo/shadow mode the user
    # currently has selected (a trader could switch modes between when a
    # signal was proposed and when they get around to authorizing it).
    with engine.connect() as conn:
        row = conn.execute(
            select(trade_intents.c.instrument).where(trade_intents.c.id == trade_intent_id)
        ).first()
    instrument = row[0] if row else None

    if instrument is not None and broker_kind_for(instrument) == "alpaca":
        if not settings.alpaca_api_key:
            raise RuntimeError("Alpaca is not configured for this account — cannot authorize this trade.")
        async with AlpacaBroker(settings) as broker:
            # Alpaca's own paper endpoint IS the real (non-simulated)
            # account — always "demo", same as run_loop.py's broker
            # construction (see src/broker/alpaca.py's module docstring).
            service = ExecutionService(broker, engine, execution_mode="demo", user_id=CURRENT_USER_ID)
            return await auth_service.authorize(
                engine, broker, service, trade_intent_id, decision,
                authorized_by="dashboard_user", notes=notes,
            )

    if mode == "paper":
        async with PaperBroker(settings, engine, user_id=CURRENT_USER_ID) as broker:
            service = ExecutionService(broker, engine, execution_mode="paper", user_id=CURRENT_USER_ID)
            return await auth_service.authorize(
                engine, broker, service, trade_intent_id, decision,
                authorized_by="dashboard_user", notes=notes,
            )
    async with OandaBroker(settings) as broker:
        service = ExecutionService(broker, engine, execution_mode="demo", user_id=CURRENT_USER_ID)
        return await auth_service.authorize(
            engine, broker, service, trade_intent_id, decision,
            authorized_by="dashboard_user", notes=notes,
        )


def get_kill_switch_state() -> dict | None:
    """Each broker (OANDA/Alpaca) now has its own risk_state row per day
    (see the (user_id, day, broker) unique constraint) — surfaces ANY
    active kill switch, since a human checking this sidebar needs to know
    if trading is halted on EITHER broker, not just whichever row happened
    to be fetched first."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with engine.connect() as conn:
        rows = conn.execute(
            select(risk_state).where(risk_state.c.user_id == CURRENT_USER_ID, risk_state.c.day == today)
        ).mappings().all()
    if not rows:
        return None
    active = [dict(r) for r in rows if r["kill_switch_active"]]
    return active[0] if active else dict(rows[0])


def fetch_trade_outcomes(engine, user_id: int) -> list[dict]:
    """Every closed trade belonging to this user, left-joined with the
    trade_intent that caused it (None for trades placed outside the
    pipeline, e.g. manual diagnostics), the analogs looked up before the
    trade was taken, and the post-trade attribution computed after it
    closed (Autonomous Upgrade Spec sec. 13 — src/memory/attribution.py,
    src/memory/analog_retrieval.py). Filtering the driving table
    (trade_outcomes) is sufficient — every joined table is per-user too and
    keyed off ids that don't collide across users."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                trade_outcomes_table,
                trade_intents.c.explanation,
                trade_intents.c.key_drivers_json,
                trade_intents.c.contrary_evidence_json,
                trade_intents.c.regime,
                trade_intents.c.confidence.label("signal_confidence"),
                trade_intents.c.horizon,
                trade_intents.c.invalidation,
                trade_analogs_table.c.basis.label("analog_basis"),
                trade_analogs_table.c.matched_count.label("analog_matched_count"),
                trade_analogs_table.c.win_rate.label("analog_win_rate"),
                trade_analogs_table.c.avg_r_multiple.label("analog_avg_r_multiple"),
                trade_attribution_table.c.r_multiple,
                trade_attribution_table.c.primary_reason,
                trade_attribution_table.c.contributing_factors_json,
            )
            .select_from(trade_outcomes_table)
            .outerjoin(trade_intents, trade_intents.c.id == trade_outcomes_table.c.trade_intent_id)
            .outerjoin(trade_analogs_table, trade_analogs_table.c.trade_intent_id == trade_outcomes_table.c.trade_intent_id)
            .outerjoin(trade_attribution_table, trade_attribution_table.c.trade_outcome_id == trade_outcomes_table.c.id)
            .where(trade_outcomes_table.c.user_id == user_id)
            .order_by(trade_outcomes_table.c.closed_at.desc())
        ).mappings().all()
    return [dict(r) for r in rows]


def _naive_to_utc(row: dict) -> dict:
    return {
        k: (v.replace(tzinfo=timezone.utc) if isinstance(v, datetime) and v.tzinfo is None else v)
        for k, v in row.items()
    }


@st.cache_data(ttl=60, show_spinner=False)
def compute_currency_map():
    """Currency-strength state (Autonomous Upgrade Spec sec. 10) — an
    aggregation over data already collected, cached 60s since it's read on
    every dashboard render but the underlying data doesn't change that fast."""
    with engine.connect() as conn:
        subq = (
            select(predictions_table.c.instrument, func.max(predictions_table.c.id).label("max_id"))
            .where(predictions_table.c.component == "price")
            .group_by(predictions_table.c.instrument)
            .subquery()
        )
        pred_rows = conn.execute(
            select(predictions_table).join(subq, predictions_table.c.id == subq.c.max_id)
        ).mappings().all()
        pair_p_ups = {r["instrument"]: json.loads(r["raw_json"])["p_up"] for r in pred_rows}

        econ = [_naive_to_utc(dict(r)) for r in conn.execute(select(economic_events_table)).mappings().all()]
        news = [_naive_to_utc(dict(r)) for r in conn.execute(select(news_events_table)).mappings().all()]
        mkt = [_naive_to_utc(dict(r)) for r in conn.execute(select(market_indicators_table)).mappings().all()]

        positioning_by_currency: dict[str, list[dict]] = {}
        for row in conn.execute(select(cftc_positioning_table)).mappings().all():
            positioning_by_currency.setdefault(row["currency"], []).append(_naive_to_utc(dict(row)))

    now = datetime.now(timezone.utc)
    return compute_all_currency_strengths(
        pair_p_ups, econ, news, mkt, now, positioning_by_currency=positioning_by_currency
    )


def equity_curve_figure(df: pd.DataFrame) -> go.Figure:
    ordered = df.sort_values("closed_at").copy()
    ordered["cumulative_pl"] = ordered["realized_pl_usd"].cumsum()
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=ordered["closed_at"], y=ordered["cumulative_pl"],
            mode="lines+markers",
            line=dict(color="#2a78d6", width=2),
            marker=dict(size=6, color="#2a78d6"),
            fill="tozeroy",
            fillcolor="rgba(42,120,214,0.10)",
            hovertemplate="%{x|%b %d, %H:%M}<br>Cumulative P&L: $%{y:,.2f}<extra></extra>",
            name="Cumulative P&L",
        )
    )
    fig.update_layout(
        margin=dict(l=10, r=10, t=10, b=10),
        height=260,
        xaxis=dict(showgrid=False, title=None),
        yaxis=dict(
            showgrid=True, gridcolor="rgba(137,135,129,0.25)", title="Cumulative P&L ($)",
            zeroline=True, zerolinecolor="rgba(137,135,129,0.5)", zerolinewidth=1,
        ),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        showlegend=False,
        font=dict(color="#898781"),
        hoverlabel=dict(bgcolor="#1a1a19", font_color="#ffffff"),
    )
    return fig


def per_trade_pl_figure(df: pd.DataFrame) -> go.Figure:
    ordered = df.sort_values("closed_at").copy()
    colors = ["#0ca30c" if v > 0 else ("#d03b3b" if v < 0 else "#898781") for v in ordered["realized_pl_usd"]]
    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=ordered["closed_at"], y=ordered["realized_pl_usd"],
            marker_color=colors,
            hovertemplate="%{x|%b %d, %H:%M}<br>P&L: $%{y:,.2f}<extra></extra>",
        )
    )
    fig.update_layout(
        margin=dict(l=10, r=10, t=10, b=10),
        height=220,
        xaxis=dict(showgrid=False, title=None),
        yaxis=dict(showgrid=True, gridcolor="rgba(137,135,129,0.25)", title="Trade P&L ($)",
                   zeroline=True, zerolinecolor="rgba(137,135,129,0.5)"),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        showlegend=False,
        font=dict(color="#898781"),
    )
    return fig


def reliability_figure(report) -> go.Figure:
    """report: a CalibrationReport (src/models/calibration.py) — predicted
    confidence vs. observed hit rate per bin, against the y=x "perfect
    calibration" reference. Marker size scales with sqrt(n) so a bin's
    weight is visible without a number stamped on every point."""
    xs = [b.mean_predicted_confidence for b in report.bins]
    ys = [b.observed_hit_rate for b in report.bins]
    sizes = [max(8, min(28, 6 + b.n ** 0.5 * 2)) for b in report.bins]
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=[0, 1], y=[0, 1], mode="lines",
        line=dict(color="#c3c2b7", width=1.5, dash="dash"),
        hoverinfo="skip", name="Perfect calibration",
    ))
    fig.add_trace(go.Scatter(
        x=xs, y=ys, mode="lines+markers",
        line=dict(color="#2a78d6", width=2),
        marker=dict(size=sizes, color="#2a78d6", line=dict(color="#fcfcfb", width=2)),
        customdata=[b.n for b in report.bins],
        hovertemplate="Predicted: %{x:.0%}<br>Observed hit rate: %{y:.0%}<br>n=%{customdata}<extra></extra>",
        name=report.segment,
    ))
    fig.update_layout(
        margin=dict(l=10, r=10, t=10, b=10),
        height=300,
        xaxis=dict(title="Mean predicted confidence", range=[0, 1], tickformat=".0%",
                   showgrid=True, gridcolor="rgba(137,135,129,0.25)"),
        yaxis=dict(title="Observed hit rate", range=[0, 1], tickformat=".0%",
                   showgrid=True, gridcolor="rgba(137,135,129,0.25)"),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#898781"),
        legend=dict(orientation="h", y=1.15, x=0),
        hoverlabel=dict(bgcolor="#1a1a19", font_color="#ffffff"),
    )
    return fig


def component_votes_figure(vote_rows: list[dict]) -> go.Figure:
    """vote_rows: the same list already built at the Agent Council call site
    (Component/Score/Confidence/Base weight/Regime multiplier per component)
    — diverging horizontal bar, since each score is signed around zero."""
    ordered = sorted(vote_rows, key=lambda r: r["Score"])
    labels = [r["Component"].replace("_", " ").title() for r in ordered]
    colors = [CHART_COLORS["diverging_pos"] if r["Score"] >= 0 else CHART_COLORS["diverging_neg"] for r in ordered]
    effective_weight = [r["Base weight"] * r["Regime multiplier"] for r in ordered]
    fig = go.Figure()
    fig.add_trace(go.Bar(
        y=labels, x=[r["Score"] for r in ordered], orientation="h",
        marker=dict(color=colors),
        width=0.55,
        text=[f"{r['Score']:+.2f}" for r in ordered], textposition="outside",
        customdata=list(zip(effective_weight, [r["Confidence"] for r in ordered])),
        hovertemplate="%{y}<br>Score: %{x:+.2f}<br>Confidence: %{customdata[1]:.0%}<br>"
                       "Effective weight (base × regime): %{customdata[0]:.2f}<extra></extra>",
    ))
    fig.add_vline(x=0, line=dict(color="#c3c2b7", width=1))
    fig.update_layout(
        margin=dict(l=10, r=40, t=10, b=10),
        height=max(120, 40 * len(labels)),
        xaxis=dict(range=[-1, 1], showgrid=True, gridcolor="rgba(137,135,129,0.25)", title=None),
        yaxis=dict(showgrid=False),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        showlegend=False,
        font=dict(color="#898781"),
        hoverlabel=dict(bgcolor="#1a1a19", font_color="#ffffff"),
    )
    return fig


def currency_strength_heatmap(ranked: list) -> go.Figure:
    """ranked: currency-strength objects already sorted by composite_score
    descending (the same list driving the stat-tile row above this chart in
    Currency Map) — diverging heatmap, since every dimension is a signed
    score around zero."""
    dims = ["Technical", "Macro", "Cross-market", "News", "Composite"]
    currencies = [s.currency for s in ranked]
    z = [[s.technical_score, s.macro_score, s.cross_market_score, s.news_score, s.composite_score] for s in ranked]
    fig = go.Figure(go.Heatmap(
        z=z, x=dims, y=currencies,
        colorscale=[[0.0, CHART_COLORS["diverging_neg"]], [0.5, CHART_COLORS["diverging_mid"]], [1.0, CHART_COLORS["diverging_pos"]]],
        zmid=0, zmin=-1, zmax=1,
        text=[[f"{v:+.2f}" for v in row] for row in z], texttemplate="%{text}",
        textfont=dict(size=11, color="#0b0b0b"),
        colorbar=dict(title="Score", tickvals=[-1, 0, 1], ticktext=["-1", "0", "+1"], outlinewidth=0, len=0.8),
        hovertemplate="%{y} · %{x}<br>Score: %{z:+.2f}<extra></extra>",
        xgap=2, ygap=2,
    ))
    fig.update_layout(
        margin=dict(l=10, r=10, t=30, b=10),
        height=60 + 42 * len(currencies),
        xaxis=dict(side="top", showgrid=False),
        yaxis=dict(showgrid=False, autorange="reversed"),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#898781"),
        hoverlabel=dict(bgcolor="#1a1a19", font_color="#ffffff"),
    )
    return fig


def loss_meter_figure(pl_pct: float, limit_pct: float, label: str) -> go.Figure:
    """A same-ramp progress-track meter (not a radial gauge — the dataviz
    skill's own form table maps "a single ratio against a limit" to a
    meter). pl_pct is signed (negative = a loss); usage is how much of the
    limit that loss has consumed, 0..1+ (can exceed 1 right at breach —
    clamped for the bar's own width, not for the reported percentage)."""
    usage = max(0.0, -pl_pct) / limit_pct if limit_pct else 0.0
    usage_clamped = min(usage, 1.15)
    if usage < 0.6:
        fill_color = CHART_COLORS["accent"]
    elif usage < 0.85:
        fill_color = CHART_COLORS["warning"]
    else:
        fill_color = CHART_COLORS["critical"]
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=[1.15], y=[label], orientation="h",
        marker=dict(color="rgba(42,120,214,0.12)"), width=0.5,
        hoverinfo="skip", showlegend=False,
    ))
    fig.add_trace(go.Bar(
        x=[usage_clamped], y=[label], orientation="h",
        marker=dict(color=fill_color), width=0.5,
        hovertemplate=f"{label}<br>P/L: {pl_pct:+.2%} of NAV<br>Limit: {-limit_pct:.1%}<extra></extra>",
        showlegend=False,
    ))
    fig.add_vline(x=1.0, line=dict(color="#0b0b0b", width=1.5),
                  annotation_text="limit", annotation_position="top",
                  annotation_font=dict(size=10, color="#898781"))
    fig.update_layout(
        barmode="overlay",
        margin=dict(l=10, r=10, t=24, b=10),
        height=70,
        xaxis=dict(visible=False, range=[0, 1.2]),
        yaxis=dict(visible=False),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        showlegend=False,
        hoverlabel=dict(bgcolor="#1a1a19", font_color="#ffffff"),
    )
    return fig


def candlestick_figure(candles: list[dict], *, entry: float | None = None,
                        stop: float | None = None, target: float | None = None,
                        height: int = 320) -> go.Figure:
    df = pd.DataFrame(candles)
    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=df["time"], open=df["open"], high=df["high"], low=df["low"], close=df["close"],
        increasing=dict(line=dict(color=CHART_COLORS["good"], width=1), fillcolor=CHART_COLORS["good"]),
        decreasing=dict(line=dict(color=CHART_COLORS["critical"], width=1), fillcolor=CHART_COLORS["critical"]),
        name="Price",
    ))
    for value, color, label, dash in [
        (entry, CHART_COLORS["accent"], "Entry", "solid"),
        (stop, CHART_COLORS["critical"], "Stop", "dot"),
        (target, CHART_COLORS["good"], "Target", "dot"),
    ]:
        if value is not None:
            fig.add_hline(
                y=value, line=dict(color=color, width=1.5, dash=dash),
                annotation_text=label, annotation_position="right",
                annotation_font=dict(color=color, size=11),
            )
    fig.update_layout(
        margin=dict(l=10, r=60, t=10, b=30),
        height=height,
        xaxis=dict(showgrid=False, rangeslider=dict(visible=False), type="date"),
        yaxis=dict(showgrid=True, gridcolor="rgba(137,135,129,0.25)", title="Price"),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#898781"),
        showlegend=False,
        hovermode="x unified",
        hoverlabel=dict(bgcolor="#1a1a19", font_color="#ffffff"),
    )
    return fig


# ==================================================================== header
_brand_logo = _brand_logo_base64()
_logo_img_html = f'<img src="data:image/png;base64,{_brand_logo}" height="44" style="flex-shrink:0;" />' if _brand_logo else ""
st.markdown(
    f'<div style="display:flex; align-items:center; gap:14px; margin-bottom:4px;">'
    f'{_logo_img_html}'
    f'<div><div class="af-header-title">AI Forex Signal Desk</div>'
    f'<div class="af-header-tagline">Live decisions, real outcomes, full audit trail — '
    f'nothing trades without a gate it earned.</div></div></div>',
    unsafe_allow_html=True,
)
st.write("")

_all_outcomes_for_header = fetch_trade_outcomes(engine, CURRENT_USER_ID)
_header_pending_count = len(auth_service.list_pending(engine, CURRENT_USER_ID))
hc1, hc2, hc3, hc4 = st.columns(4)
if _all_outcomes_for_header:
    _hdf = pd.DataFrame(_all_outcomes_for_header)
    _total_pl = _hdf["realized_pl_usd"].sum()
    _wins = int((_hdf["outcome"] == "WIN").sum())
    _closed = len(_hdf)
    _win_rate = _wins / _closed if _closed else 0.0
    with hc1:
        stat_tile("Total Realized P&L", f"${_total_pl:+,.2f}", f"{_closed} closed trades",
                   polarity="positive" if _total_pl >= 0 else "negative")
    with hc2:
        stat_tile("Win Rate", f"{_win_rate:.0%}", f"{_wins}W / {_closed - _wins}L")
else:
    with hc1:
        stat_tile("Total Realized P&L", "$0.00", "no closed trades yet")
    with hc2:
        stat_tile("Win Rate", "—", "no closed trades yet")
with hc3:
    stat_tile("Pending Signals", str(_header_pending_count), "awaiting authorization")
with hc4:
    _kill = get_kill_switch_state()
    _trading_on = not (_kill and _kill["kill_switch_active"])
    stat_tile("Trading Status", "🟢 Enabled" if _trading_on else "🛑 Halted",
              "auto-execute on demo" if _trading_on else "kill switch active")

st.write("")

# ---------------------------------------------------------------- sidebar --
def render_sidebar_ticker(instruments: tuple[str, ...]) -> None:
    """Compact watched-instrument price strip — live price + 15m sparkline +
    % change, reusing cached_prices/cached_candles (zero new uncached broker
    calls). Wrapped so a ticker failure never breaks the sidebar's real
    controls (kill switch, scan button) rendered right after it."""
    try:
        prices = {p.instrument: p for p in cached_prices(instruments)}
    except Exception:  # noqa: BLE001
        return
    rows_html = []
    for inst in instruments:
        p = prices.get(inst)
        if p is None:
            continue
        try:
            closes = [c["close"] for c in cached_candles(inst, "M15", 12)]
        except Exception:  # noqa: BLE001
            closes = []
        delta_pct = (closes[-1] - closes[0]) / closes[0] if len(closes) >= 2 and closes[0] else 0.0
        delta_color = "#0ca30c" if delta_pct >= 0 else "#d03b3b"
        spark = _sparkline_svg(closes, width=40, height=14, accent=delta_color) if closes else ""
        rows_html.append(
            '<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0;">'
            f'<span style="font-size:0.82rem;font-weight:600;">{inst.replace("_", "/")}</span>'
            f'{spark}'
            f'<span style="font-size:0.82rem;font-variant-numeric:tabular-nums;color:{delta_color};">'
            f'{p.mid:.5f} ({delta_pct:+.2%})</span></div>'
        )
    if rows_html:
        st.sidebar.markdown("".join(rows_html), unsafe_allow_html=True)


_prefs = user_auth_service.get_preferences(engine, CURRENT_USER_ID)
_watch_instruments = tuple((json.loads(_prefs["instrument_list_json"]) if _prefs and _prefs.get("instrument_list_json") else ["EUR_USD"])[:5])

if _brand_logo:
    st.sidebar.markdown(
        f'<div style="text-align:center; margin-bottom:6px;">'
        f'<img src="data:image/png;base64,{_brand_logo}" width="120" /></div>',
        unsafe_allow_html=True,
    )
else:
    st.sidebar.title("📡 Signal Desk")
st.sidebar.caption("Nothing is sent to a broker without a gate it earned.")

st.sidebar.markdown(f"**{CURRENT_USER['username']}** {role_badge_html(CURRENT_IS_ADMIN)}", unsafe_allow_html=True)
st.sidebar.caption(CURRENT_USER["email"])
if st.sidebar.button("Sign out", width="stretch"):
    logout(engine)
    st.rerun()

st.sidebar.divider()
render_sidebar_ticker(_watch_instruments)

kill_state = get_kill_switch_state()
if kill_state and kill_state["kill_switch_active"]:
    st.sidebar.error(f"🛑 KILL SWITCH ACTIVE\n\n{kill_state['kill_switch_reason']}")
    if st.sidebar.button("▶️ Resume trading", width="stretch"):
        # Every broker, not just the one that happened to trip — a human
        # resuming trading expects it fully resumed.
        for _broker in ("oanda", "alpaca"):
            risk_governor.set_kill_switch(engine, CURRENT_USER_ID, _broker, False, None)
        st.rerun()
else:
    st.sidebar.success("✅ Trading enabled")
    if st.sidebar.button("🛑 EMERGENCY STOP", width="stretch", type="primary"):
        # Every broker — an emergency stop that only halts OANDA while
        # Alpaca keeps trading would not be an emergency stop.
        for _broker in ("oanda", "alpaca"):
            risk_governor.set_kill_switch(engine, CURRENT_USER_ID, _broker, True, "manual emergency stop from dashboard")
        st.rerun()

st.sidebar.divider()
st.sidebar.subheader("Scan markets now")
scan_mode = st.sidebar.selectbox("Mode", ["paper", "demo", "shadow"], key="scan_mode")
if st.sidebar.button("🔄 Run a decision cycle", width="stretch"):
    with st.spinner("Running one cycle for your account..."):
        run_async(run_once(scan_mode, allow_unverified_event_risk=False, only_user_id=CURRENT_USER_ID))
    st.sidebar.success("Cycle complete.")
    st.cache_data.clear()
    st.rerun()

st.sidebar.divider()
if st.sidebar.button("↻ Refresh view", width="stretch"):
    st.cache_data.clear()
    st.rerun()
st.sidebar.caption("Live data (prices, account) is cached 15s so switching tabs feels instant — refresh forces a real re-fetch.")

# ------------------------------------------------------------------ tabs --
_tab_labels = ["🛰️ Mission Control", "📊 Markets", "🗳️ Agent Council", "🚦 Pending Signals", "📈 Trade History", "🧠 Model Learning",
               "🥊 Challengers", "📚 Knowledge Lab", "🌍 Currency Map", "💰 Account", "🛡️ Risk Center",
               "⚙️ Automation Center", "📜 Audit Log", "📋 All Signals"]
if CURRENT_IS_ADMIN:
    _tab_labels.append("🛠️ Admin")
_tabs = st.tabs(_tab_labels)
(tab_mission, tab_markets, tab_council, tab_signals, tab_trades, tab_learning, tab_challengers, tab_knowledge,
 tab_currency, tab_account, tab_risk, tab_automation, tab_history, tab_all) = _tabs[:14]
tab_admin = _tabs[14] if CURRENT_IS_ADMIN else None

HORIZON_ORDER = {"15m": 0, "1h": 1, "4h": 2, "1d": 3}
HORIZON_STYLE_HINT = {
    "15m": "intraday / frequent — relevant for minutes to about an hour",
    "1h": "intraday — relevant for a few hours",
    "4h": "swing — relevant for the better part of a trading day",
    "1d": "position — relevant across multiple days",
}
# For charting only — run_loop.py's own HORIZON_CONFIGS trains the 4h model
# on H1 candles x4 lookahead (a modeling choice), but a native H4 candle is
# the more readable chart for a human looking at a 4h-horizon signal.
HORIZON_TO_CHART_GRANULARITY = {"15m": "M15", "1h": "H1", "4h": "H4", "1d": "D"}

# --------------------------------------------------------- mission control --
with tab_mission:
    st.subheader("Mission Control")
    st.caption(
        "Autonomous Upgrade Spec sec. 19: the single at-a-glance answer to \"what is this "
        "system doing right now, and is it safe.\" Everything here is read from data other "
        "tabs already collect — this is a summary view, not a new data source."
    )

    kill_state = get_kill_switch_state()
    with engine.connect() as conn:
        week_key = f"{datetime.now(timezone.utc).isocalendar()[0]}-W{datetime.now(timezone.utc).isocalendar()[1]:02d}"
        week_row = conn.execute(
            select(risk_state_weekly_table).where(
                risk_state_weekly_table.c.user_id == CURRENT_USER_ID,
                risk_state_weekly_table.c.iso_week == week_key,
            )
        ).mappings().first()
        last_cycle = conn.execute(
            select(trade_intents.c.time)
            .where(trade_intents.c.user_id == CURRENT_USER_ID)
            .order_by(trade_intents.c.id.desc()).limit(1)
        ).scalar()
        open_signal_count = conn.execute(
            select(func.count()).select_from(trade_intents)
            .where(
                # PROPOSED is a transient, sub-cycle status every intent
                # briefly holds before advancing to a real terminal status
                # (NO_TRADE/RISK_REJECTED/AWAITING_AUTHORIZATION/...) within
                # the same evaluation — never something a human is meant to
                # review, so it must match Pending Signals' own definition
                # of "pending" (auth_service.list_pending), not count it.
                trade_intents.c.status == "AWAITING_AUTHORIZATION",
                trade_intents.c.user_id == CURRENT_USER_ID,
            )
        ).scalar()

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        stat_tile("Broker environment", settings.oanda_environment.upper(),
                   "config.py hard-refuses 'live' — this can only ever be practice/demo")
    with c2:
        stat_tile("Active scan mode", scan_mode, "selected in the sidebar")
    with c3:
        if kill_state and kill_state["kill_switch_active"]:
            stat_tile("Kill switch", "🔴 ACTIVE", kill_state["kill_switch_reason"] or "", polarity="negative")
        else:
            stat_tile("Kill switch", "🟢 clear", "no daily/weekly loss breach or reconciliation failure today")
    with c4:
        age = format_duration((datetime.now(timezone.utc) - last_cycle.replace(tzinfo=timezone.utc)).total_seconds()) if last_cycle else "never"
        stat_tile("Last decision cycle", age, "time since the most recent trade_intent was logged")

    st.write("")
    st.markdown("#### Session clock")
    st.caption(
        "User-requested 2026-08-13: which real forex sessions are open right now, "
        "computed from each market's own local business hours (DST-correct — see "
        "src/models/trading_sessions.py), and whether this is a transition/overlap "
        "window the 'session' component and the NY-open-reversal challenger react to."
    )
    session_state = current_session_state(datetime.now(timezone.utc))
    sc1, sc2, sc3, sc4 = st.columns(4)
    with sc1:
        stat_tile(
            "Active sessions",
            ", ".join(session_state.active_sessions).replace("_", " ") if session_state.active_sessions else (
                "Closed (weekend)" if session_state.market_closed else "None"
            ),
            "real local business hours per session, via zoneinfo",
        )
    with sc2:
        stat_tile(
            "Overlap",
            "🟢 yes" if session_state.overlap else "— no",
            "2+ sessions open at once — highest-liquidity window",
        )
    with sc3:
        stat_tile(
            "Just opened",
            ", ".join(session_state.just_opened).replace("_", " ") if session_state.just_opened else "—",
            "within the transition window — session confidence is damped, not boosted",
        )
    with sc4:
        stat_tile(
            "Closing soon",
            ", ".join(session_state.closing_soon).replace("_", " ") if session_state.closing_soon else "—",
            "within the transition window on the way out",
        )

    st.write("")
    c1, c2 = st.columns(2)
    with c1:
        if kill_state:
            try:
                daily_pl_pct, _ = current_pl_pct(kill_state, week_row, scan_mode)
                daily_pl_pct = daily_pl_pct or 0.0
                stat_tile("Today's P/L", f"{daily_pl_pct:+.2%}",
                          f"vs {-risk_governor.DAILY_LOSS_LIMIT_PCT:.1%} daily limit", polarity="positive" if daily_pl_pct >= 0 else "negative")
            except Exception as exc:  # noqa: BLE001
                st.caption(f"Could not compute today's P/L: {exc!r}")
    with c2:
        if week_row and week_row["week_start_balance"]:
            try:
                _, weekly_pl_pct = current_pl_pct(kill_state, week_row, scan_mode)
                stat_tile("This week's P/L", f"{weekly_pl_pct:+.2%}",
                          f"vs {-risk_governor.WEEKLY_LOSS_LIMIT_PCT:.1%} weekly limit", polarity="positive" if weekly_pl_pct >= 0 else "negative")
            except Exception as exc:  # noqa: BLE001
                st.caption(f"Could not compute this week's P/L: {exc!r}")
        else:
            st.caption("No weekly baseline recorded yet.")

    st.write("")
    st.caption(f"{open_signal_count} signal(s) currently awaiting review or authorization — see 🚦 Pending Signals.")

    st.write("")
    st.markdown("#### Promotion readiness")
    st.caption(
        "Autonomous Upgrade Spec sec. 18: this system currently runs auto-execute against "
        "the real OANDA practice account (Phase C: Demo Autonomous). This is an honest "
        "measurement of whether it has earned Phase D (Stability) yet — never an automatic "
        "promotion, and 'not ready' is the expected, correct answer while sample sizes are "
        "still small. Spec is explicit: do not promote on win rate alone."
    )
    promo_report = promotion_build_report(engine, CURRENT_USER_ID)
    readiness_label = "✅ Ready" if promo_report.ready_for_promotion else "⏳ Not yet"
    st.markdown(f"**{promo_report.current_phase} → {promo_report.target_phase}: {readiness_label}**")
    for c in promo_report.criteria:
        icon = "🟢" if c.passed else ("🔴" if c.passed is False else "⚪")
        st.markdown(f"{icon} **{c.name}** — {c.description}  \n&nbsp;&nbsp;&nbsp;&nbsp;{c.current_value}")

# ----------------------------------------------------------------- markets --
with tab_markets:
    st.subheader("Markets")
    st.caption(
        "Free-form price chart, independent of any specific signal — the same "
        "candlestick_figure() used inline on 🚦 Pending Signals, here for browsing "
        "any of your watched instruments at any granularity."
    )
    market_instruments = _watch_instruments if _watch_instruments else ("EUR_USD",)
    mc1, mc2 = st.columns([2, 1])
    with mc1:
        market_instrument = st.selectbox("Instrument", market_instruments, key="market_instrument")
    with mc2:
        market_granularity_label = st.selectbox(
            "Granularity", ["15m", "1h", "4h", "1d"], index=1, key="market_granularity",
            format_func=lambda h: f"{h} ({HORIZON_STYLE_HINT.get(h, '')})",
        )
    try:
        market_candles = cached_candles(
            market_instrument, HORIZON_TO_CHART_GRANULARITY[market_granularity_label], 150,
        )
        st.plotly_chart(
            candlestick_figure(market_candles, height=420), width="stretch",
            config={"displayModeBar": False},
        )
        with st.expander("Table view (raw candles)"):
            st.dataframe(pd.DataFrame(market_candles), width="stretch", hide_index=True)
    except Exception as exc:  # noqa: BLE001
        st.warning(f"Could not load price chart: {exc!r}")

# ----------------------------------------------------------------- council --
with tab_council:
    st.subheader("Agent Council")
    st.caption(
        "Autonomous Upgrade Spec sec. 19: every component's raw vote for the most recent "
        "decision cycles, the regime-adjusted weight it was actually given (src/decision/"
        "fusion.py's REGIME_WEIGHT_MULTIPLIERS, P7), and where the combined vote landed. "
        "This is the same math run_loop.py already ran — just made visible."
    )

    with engine.connect() as conn:
        recent_intents = conn.execute(
            select(trade_intents)
            .where(trade_intents.c.user_id == CURRENT_USER_ID)
            .order_by(trade_intents.c.id.desc()).limit(15)
        ).mappings().all()

    for intent in recent_intents:
        with engine.connect() as conn:
            votes = conn.execute(
                select(predictions_table).where(
                    predictions_table.c.instrument == intent["instrument"],
                    predictions_table.c.horizon == intent["horizon"],
                    predictions_table.c.time == intent["time"],
                )
            ).mappings().all()
        regime_mult = REGIME_WEIGHT_MULTIPLIERS.get(intent["regime"] or "", {})
        action_color = {"BUY": "positive", "SELL": "negative"}.get(intent["action"], "")
        with st.container(border=True):
            st.markdown(
                f"**{intent['instrument']} / {intent['horizon']}** · regime={intent['regime']} · "
                f"{intent['time']:%b %d %H:%M UTC} → "
                f"<span class='af-pl-value {action_color}'>{intent['action']}</span> "
                f"(confidence {intent['confidence']:.0%})",
                unsafe_allow_html=True,
            )
            if votes:
                vote_rows = [
                    {
                        "Component": v["component"],
                        "Score": round(json.loads(v["raw_json"]).get("score", 0.0), 3),
                        "Confidence": round(v["confidence"], 3),
                        "Base weight": COMPONENT_WEIGHTS.get(v["component"], 0.0),
                        "Regime multiplier": regime_mult.get(v["component"], 1.0),
                    }
                    for v in votes
                ]
                st.plotly_chart(
                    component_votes_figure(vote_rows), width="stretch", config={"displayModeBar": False},
                    key=f"votes_{intent['id']}",
                )
                with st.expander("Raw vote data"):
                    st.dataframe(pd.DataFrame(vote_rows), width="stretch", hide_index=True)
            else:
                st.caption("No component votes logged for this exact cycle (older row, predates full logging).")

# --------------------------------------------------------- pending signals --
with tab_signals:
    expired_count = auth_service.expire_stale_intents(engine, CURRENT_USER_ID)
    if expired_count:
        st.caption(f"{expired_count} stale signal(s) auto-expired (older than their own horizon window).")

    all_pending = auth_service.list_pending(engine, CURRENT_USER_ID)
    available_horizons = sorted(
        {p["horizon"] for p in all_pending}, key=lambda h: HORIZON_ORDER.get(h, 99)
    )

    if not all_pending:
        st.info(
            "No signals are awaiting authorization right now. Use **Run a decision "
            "cycle** in the sidebar to scan the markets, or check back later. Every "
            "cycle checks 15m, 1h and 4h independently for every pair — there's no "
            "single signal per pair, so whatever your trading frequency, something "
            "here is sized for it."
        )
    else:
        st.caption("Filter by how often you trade — each horizon is an independent signal, not a subset of another.")
        horizon_filter = st.multiselect(
            "Horizon", options=available_horizons, default=available_horizons,
            format_func=lambda h: f"{h} ({HORIZON_STYLE_HINT.get(h, '')})",
        )
        pending = [p for p in all_pending if p["horizon"] in horizon_filter]
        pending.sort(key=lambda p: (HORIZON_ORDER.get(p["horizon"], 99), -p["confidence"]))

        instruments = tuple(sorted({p["instrument"] for p in pending}))
        try:
            live_prices = {p.instrument: p for p in cached_prices(instruments)}
        except Exception as exc:  # noqa: BLE001
            st.warning(f"Could not fetch live prices for freshness display: {exc!r}")
            live_prices = {}

        current_horizon_group = None
        for intent in pending:
            if intent["horizon"] != current_horizon_group:
                current_horizon_group = intent["horizon"]
                st.markdown(f"#### {current_horizon_group} signals")
            live = live_prices.get(intent["instrument"])
            current_price = live.mid if live else None
            ticket = auth_service.build_manual_order_ticket(intent, current_price=current_price)

            action_icon = "🟢" if intent["action"] == "BUY" else "🔴"
            with st.container(border=True):
                header_col, meta_col = st.columns([3, 2])
                with header_col:
                    st.subheader(f"{action_icon} {intent['instrument']} — {intent['action']}")
                    st.caption(
                        f"Signal generated {intent['time']:%Y-%m-%d %H:%M UTC} · "
                        f"{intent['age_seconds']/60:.0f} min ago · execution_mode={intent['execution_mode']}"
                    )
                with meta_col:
                    m1, m2, m3 = st.columns(3)
                    m1.metric("Confidence", f"{intent['confidence']:.0%}")
                    m2.metric("Regime", intent["regime"])
                    m3.metric("Horizon", intent["horizon"])

                expires_at = intent["expires_at"]
                remaining = intent["time_remaining_seconds"]
                urgency = intent["urgency"]
                expiry_line = (
                    f"⏰ Expires **{expires_at:%H:%M UTC}** on {expires_at:%Y-%m-%d} "
                    f"({'in ' + format_duration(remaining) if remaining > 0 else format_duration(remaining) + ' ago'})"
                )
                if urgency == "expired":
                    st.error(f"🔴 EXPIRED — {expiry_line}. This signal should not be authorized; re-scan instead.")
                elif urgency == "critical":
                    st.warning(f"🟠 Expiring soon — {expiry_line}")
                elif urgency == "warning":
                    st.info(f"🟡 {expiry_line}")
                else:
                    st.success(f"🟢 {expiry_line}")

                with st.expander("📊 Price chart", expanded=False):
                    try:
                        granularity = HORIZON_TO_CHART_GRANULARITY.get(intent["horizon"], "M15")
                        candles = cached_candles(intent["instrument"], granularity, 96)
                        st.plotly_chart(
                            candlestick_figure(
                                candles,
                                entry=ticket.get("reference_price"),
                                stop=ticket.get("stop_loss"),
                                target=ticket.get("take_profit"),
                            ),
                            width="stretch", config={"displayModeBar": False},
                            key=f"candles_{intent['id']}",
                        )
                        with st.expander("Table view (raw candles)"):
                            st.dataframe(pd.DataFrame(candles), width="stretch", hide_index=True)
                    except Exception as exc:  # noqa: BLE001
                        # A chart failure must never block signal review/authorization
                        # below it — this tab's real job is the approve/reject buttons.
                        st.caption(f"Could not load price chart: {exc!r}")

                col_left, col_right = st.columns(2)
                with col_left:
                    st.markdown("**Key drivers**")
                    for d in intent["key_drivers"]:
                        st.markdown(f"- {d}")
                    if intent["contrary_evidence"]:
                        st.markdown("**⚠️ Contrary evidence**")
                        for c in intent["contrary_evidence"]:
                            st.markdown(f"- {c}")
                    st.markdown(f"**Entry condition:** {intent['entry_condition']}")
                    st.markdown(f"**Invalidation:** {intent['invalidation']}")
                    st.markdown(f"**Target logic:** {intent['target_logic']}")
                    with st.expander("Full model explanation"):
                        st.write(intent["explanation"])
                        st.json(intent["data_freshness"])

                with col_right:
                    st.markdown("**📋 Manual order ticket — for placing by hand on OANDA if you prefer**")
                    ticket_text = (
                        f"Instrument:   {ticket['instrument']}\n"
                        f"Direction:    {ticket['direction']}\n"
                        f"Order type:   {ticket['order_type']}\n"
                        f"Units:        {ticket['units']}\n"
                        f"Stop loss:    {ticket['stop_loss']}\n"
                        f"Take profit:  {ticket['take_profit']}\n"
                        f"Ref. price:   {ticket['reference_price']}"
                    )
                    st.code(ticket_text, language=None)
                    if current_price is not None and intent.get("reference_price"):
                        drift = current_price - intent["reference_price"]
                        st.caption(
                            f"Live price now: {current_price:.5f} "
                            f"(drift since signal: {drift:+.5f})"
                        )
                    if intent["risk"]:
                        st.caption(f"Risk governor sized this at **{intent['risk']['size_units']} units** "
                                   f"({intent['risk']['reason']}).")

                notes = st.text_input("Notes (optional)", key=f"notes_{intent['id']}")

                if urgency == "expired":
                    st.button(
                        "⛔ Expired — re-scan for a fresh signal", key=f"expired_{intent['id']}",
                        width="stretch", disabled=True,
                    )
                else:
                    btn_approve, btn_reject = st.columns(2)
                    with btn_approve:
                        if st.button(
                            f"✅ Authorize & Execute ({intent['execution_mode']})",
                            key=f"approve_{intent['id']}", width="stretch", type="primary",
                        ):
                            with st.spinner("Sending order..."):
                                result = run_async(
                                    _do_authorize(intent["execution_mode"], intent["id"], "APPROVED", notes or None)
                                )
                            if result.order_result and result.order_result.status == "FILLED":
                                st.success(f"Executed: {result.detail}")
                            else:
                                st.error(f"Not filled: {result.detail}")
                            st.cache_data.clear()
                            st.rerun()
                    with btn_reject:
                        if st.button(
                            "❌ Reject", key=f"reject_{intent['id']}", width="stretch",
                        ):
                            run_async(_do_authorize(intent["execution_mode"], intent["id"], "REJECTED", notes or None))
                            st.info("Rejected — no order sent.")
                            st.cache_data.clear()
                            st.rerun()

# --------------------------------------------------------------- trade history --
with tab_trades:
    st.subheader("Every trade the system has actually placed, and why")
    outcomes = fetch_trade_outcomes(engine, CURRENT_USER_ID)

    if not outcomes:
        st.info(
            "No trades have closed yet. Once an executed position closes — "
            "stop-loss, take-profit, or manual — it shows up here with full "
            "profit/loss detail and the exact reasoning behind the original decision."
        )
    else:
        df = pd.DataFrame(outcomes)
        total_pl = df["realized_pl_usd"].sum()
        wins = int((df["outcome"] == "WIN").sum())
        losses = int((df["outcome"] == "LOSS").sum())
        total = len(df)
        win_rate = wins / total if total else 0.0
        avg_win = df.loc[df["outcome"] == "WIN", "realized_pl_usd"].mean() if wins else 0.0
        avg_loss = df.loc[df["outcome"] == "LOSS", "realized_pl_usd"].mean() if losses else 0.0
        gains_sum = df.loc[df["realized_pl_usd"] > 0, "realized_pl_usd"].sum()
        losses_sum = abs(df.loc[df["realized_pl_usd"] < 0, "realized_pl_usd"].sum())
        profit_factor = (gains_sum / losses_sum) if losses_sum else float("inf")
        # Same cumulative-P&L series equity_curve_figure builds below, just
        # sliced to the most recent 12 points for the stat tile's sparkline.
        cumulative_pl_trend = df.sort_values("closed_at")["realized_pl_usd"].cumsum().tolist()[-12:]

        c1, c2, c3, c4 = st.columns(4)
        with c1:
            stat_tile("Total Realized P&L", f"${total_pl:+,.2f}", f"{total} closed trades",
                      polarity="positive" if total_pl >= 0 else "negative", trend=cumulative_pl_trend)
        with c2:
            stat_tile("Win Rate", f"{win_rate:.0%}", f"{wins}W / {losses}L")
        with c3:
            stat_tile("Avg Win / Avg Loss", f"${avg_win:,.2f} / ${avg_loss:,.2f}", "per closed trade")
        with c4:
            pf_display = f"{profit_factor:.2f}" if profit_factor != float("inf") else "∞"
            stat_tile("Profit Factor", pf_display, "gross gains ÷ gross losses")

        st.write("")
        st.markdown("**Equity curve — cumulative realized P&L**")
        st.plotly_chart(equity_curve_figure(df), width="stretch", config={"displayModeBar": False})

        with st.expander("Per-trade P&L (bar view)"):
            st.plotly_chart(per_trade_pl_figure(df), width="stretch", config={"displayModeBar": False})

        st.write("")
        st.markdown("#### All trades")
        # Iterate the original dict list, not df.iterrows(): pandas silently
        # turns SQL NULL into float NaN on DataFrame conversion, and NaN is
        # truthy in Python — `if row.get("key_drivers_json")` would pass and
        # json.loads(nan) blows up for any trade with no linked trade_intent.
        for row in sorted(outcomes, key=lambda r: r["closed_at"], reverse=True):
            pl = row["realized_pl_usd"]
            opened = row.get("opened_at")
            closed = row.get("closed_at")
            duration = format_duration((closed - opened).total_seconds()) if opened is not None and closed is not None else "—"

            st.markdown(
                trade_card_html(
                    row["instrument"], row["action"], row["outcome"], pl,
                    row["entry_price"], row["exit_price"], opened, closed, duration,
                    row["units"], row["execution_mode"],
                ),
                unsafe_allow_html=True,
            )

            with st.expander("Why this trade was taken"):
                if row.get("explanation"):
                    st.write(row["explanation"])
                    st.caption(
                        f"Horizon: {row.get('horizon', '—')} · "
                        f"Regime at decision time: {row.get('regime', '—')} · "
                        f"Signal confidence: {(row.get('signal_confidence') or 0):.0%}"
                    )
                    key_drivers = json.loads(row["key_drivers_json"]) if row.get("key_drivers_json") else []
                    contrary = json.loads(row["contrary_evidence_json"]) if row.get("contrary_evidence_json") else []
                    if key_drivers:
                        st.markdown("**Key drivers:**")
                        for d in key_drivers:
                            st.markdown(f"- {d}")
                    if contrary:
                        st.markdown("**⚠️ Contrary evidence at the time:**")
                        for c in contrary:
                            st.markdown(f"- {c}")
                    if row.get("invalidation"):
                        st.markdown(f"**Invalidation was:** {row['invalidation']}")

                    if row.get("analog_basis") and row["analog_basis"] != "insufficient_history":
                        st.markdown(
                            f"**Before this trade, {row['analog_matched_count']} similar past trades** "
                            f"({row['analog_basis'].replace('_', ' ')}) had won "
                            f"{(row['analog_win_rate'] or 0):.0%} of the time"
                            + (f", avg {row['analog_avg_r_multiple']:+.2f}R" if row.get("analog_avg_r_multiple") is not None else "")
                            + "."
                        )
                    elif row.get("analog_basis") == "insufficient_history":
                        st.caption(
                            f"Historical analog check: only {row['analog_matched_count']} past trade(s) "
                            "matched this instrument/regime/action — too few to draw a stat from."
                        )

                    if row.get("primary_reason"):
                        r_str = f"{row['r_multiple']:+.2f}R" if row.get("r_multiple") is not None else "R n/a"
                        st.markdown(f"**Post-trade attribution:** `{row['primary_reason']}` ({r_str})")
                        factors = json.loads(row["contributing_factors_json"]) if row.get("contributing_factors_json") else []
                        for f in factors:
                            st.markdown(f"- {f}")
                else:
                    st.caption(
                        "No linked signal — this trade wasn't placed through the "
                        "automated decision pipeline (e.g. a manual diagnostic order)."
                    )

# ------------------------------------------------------------- model learning --
with tab_learning:
    st.subheader("Is the model actually learning?")
    st.caption(
        "The meta-model recalibrates (or vetoes) the heuristic's decisions once "
        "it's been trained on enough real, realized outcomes — not before. "
        "This tab is the honest answer to \"is it learning yet.\""
    )

    with engine.connect() as conn:
        # Deliberately NOT filtered by user: the meta-model is one shared
        # model (model_registry is a global table) trained on every user's
        # pooled outcomes — src/models/train_meta_model.py itself has no
        # user_id filter either, for the same "pool for statistical power"
        # reason as calibration/challenger evaluation. Showing only this
        # user's own count here would be misleading about what actually
        # gates training.
        n_linked_outcomes = conn.execute(
            select(func.count()).select_from(trade_outcomes_table)
            .where(trade_outcomes_table.c.trade_intent_id.is_not(None))
            .where(trade_outcomes_table.c.outcome.in_(["WIN", "LOSS"]))
        ).scalar()
        versions = conn.execute(
            select(model_registry_table)
            .where(model_registry_table.c.name == MODEL_NAME)
            .order_by(model_registry_table.c.trained_at.desc())
        ).mappings().all()

    progress = min(1.0, n_linked_outcomes / MIN_SAMPLES) if MIN_SAMPLES else 0.0
    st.progress(
        progress,
        text=f"{n_linked_outcomes} / {MIN_SAMPLES} real linked WIN/LOSS outcomes accumulated toward training",
    )

    deployed = [v for v in versions if v["deployed"]]
    if deployed:
        v = deployed[0]
        validation = json.loads(v["validation_json"] or "{}")
        st.success(f"✅ Meta-model **v{v['version']}** is deployed and actively recalibrating live decisions.")
        c1, c2, c3 = st.columns(3)
        with c1:
            stat_tile("Cross-validated accuracy", f"{validation.get('cv_accuracy_mean', 0):.0%}",
                      f"± {validation.get('cv_accuracy_std', 0):.0%}, {validation.get('cv_folds', '?')}-fold")
        with c2:
            stat_tile("Trained on", f"{validation.get('n_samples', '?')} samples",
                      v["trained_at"].strftime("%b %d, %H:%M UTC"))
        with c3:
            balance = validation.get("class_balance", {})
            stat_tile("Class balance", f"{balance.get('win', '?')}W / {balance.get('loss', '?')}L", "")
    else:
        st.info(
            "No meta-model has been deployed yet — decisions are still made by the "
            "fixed-weight heuristic blend in `decision/fusion.py`. A candidate trains "
            "automatically every night at 3am once there's enough data, but is "
            "**never auto-deployed** — review it and promote manually with "
            "`python -m src.models.promote_meta_model <version>` if the accuracy looks trustworthy."
        )

    if versions:
        st.markdown("#### Training history")
        for v in versions:
            validation = json.loads(v["validation_json"] or "{}")
            badge = "🟢 DEPLOYED" if v["deployed"] else "⚪ candidate"
            with st.container(border=True):
                st.markdown(
                    f"**v{v['version']}** &nbsp; {badge} &nbsp;·&nbsp; "
                    f"trained {v['trained_at']:%Y-%m-%d %H:%M UTC}"
                )
                st.caption(
                    f"n={validation.get('n_samples', '?')} · "
                    f"cv_accuracy={validation.get('cv_accuracy_mean', 0):.0%} "
                    f"(± {validation.get('cv_accuracy_std', 0):.0%}) · "
                    f"class_balance={validation.get('class_balance', {})}"
                )

# --------------------------------------------------------------- challengers --
with tab_challengers:
    st.subheader("Champion vs Challengers")
    st.caption(
        "Autonomous Upgrade Spec sec. 14-15: challengers run in full shadow — same "
        "market snapshot as the champion every cycle, but they never reach the risk "
        "governor or a broker. Both are scored the same way: did the actual "
        "subsequent price move match the signal's direction by the time its horizon "
        "elapsed. A challenger is never auto-promoted; it either accumulates enough "
        "shadow samples to prove itself for human review, or gets buried below."
    )

    with engine.connect() as conn:
        # Deliberately pooled across all users, not filtered to this one:
        # champion/challenger hit-rate is a property of the shared models
        # (src/scripts/evaluate_challengers.py itself pools every user's
        # signal_evaluations for the same statistical-power reason
        # calibration does — see src/models/calibration.py), not of any
        # one account's own trading.
        source_stats = conn.execute(
            select(
                signal_evaluations_table.c.source,
                func.count().label("n"),
                func.sum(func.cast(signal_evaluations_table.c.hit, Integer)).label("hits"),
                func.avg(signal_evaluations_table.c.move_in_favor).label("avg_move"),
            ).group_by(signal_evaluations_table.c.source)
        ).mappings().all()
        graveyard_rows = conn.execute(
            select(strategy_graveyard_table).order_by(strategy_graveyard_table.c.buried_at.desc())
        ).mappings().all()

    stats_by_source = {r["source"]: r for r in source_stats}
    champion_stat = stats_by_source.get("champion")

    if champion_stat is None or champion_stat["n"] == 0:
        st.info("No signals have had their horizon elapse yet — nothing to score.")
    else:
        c1, c2 = st.columns(2)
        with c1:
            hit_rate = champion_stat["hits"] / champion_stat["n"]
            stat_tile("Champion hit rate", f"{hit_rate:.0%}", f"{champion_stat['hits']}/{champion_stat['n']} elapsed signals")
        with c2:
            stat_tile("Champion avg move in favor", f"{(champion_stat['avg_move'] or 0):+.5f}", "price units, signed by direction")

        st.write("")
        st.markdown("#### Challengers")
        buried_names = {r["strategy_name"] for r in graveyard_rows}
        challenger_names = sorted(set(stats_by_source) - {"champion"})
        if not challenger_names:
            st.caption("No challenger has produced any shadow decisions yet (e.g. the economic-surprise "
                       "challenger only fires when a fresh, recent surprise exists for the pair in question).")
        for name in challenger_names:
            stat = stats_by_source[name]
            with st.container(border=True):
                buried = name in buried_names
                label = f"⚰️ {name} — buried" if buried else f"🥊 {name} — active shadow"
                st.markdown(f"**{label}**")
                if stat["n"] == 0:
                    st.caption("No elapsed shadow signals yet.")
                else:
                    hit_rate = stat["hits"] / stat["n"]
                    delta = hit_rate - (champion_stat["hits"] / champion_stat["n"])
                    st.caption(
                        f"{stat['hits']}/{stat['n']} elapsed shadow signals · hit_rate={hit_rate:.0%} "
                        f"({delta:+.0%} vs champion) · avg move in favor {(stat['avg_move'] or 0):+.5f}"
                    )

    if graveyard_rows:
        st.write("")
        st.markdown("#### 🪦 Strategy graveyard")
        st.caption("Buried once, never re-run — src/challengers/definitions.py filters these out of every future cycle.")
        for r in graveyard_rows:
            with st.container(border=True):
                st.markdown(f"**{r['strategy_name']}** — buried {r['buried_at']:%Y-%m-%d %H:%M UTC}")
                st.caption(r["reason"])

    st.write("")
    st.markdown("#### Is confidence actually calibrated?")
    st.caption(
        "Autonomous Upgrade Spec sec. 16: a confidence number is only meaningful if it's "
        "empirically checked against what actually happened. Each bar compares the "
        "champion's own predicted confidence against the real observed hit rate in that "
        "bucket, using every signal whose horizon has elapsed (src/models/calibration.py) — "
        "not held-out theory, the system's own real track record so far."
    )
    calibration_reports = cached_calibration_reports("champion")
    aggregate_report = next((r for r in calibration_reports if r.segment == "champion"), None)
    if aggregate_report is None:
        st.info(f"Not enough elapsed, scored signals yet (need {calibration_MIN_SEGMENT_SAMPLES}+) to report calibration.")
    else:
        st.caption(f"n={aggregate_report.n} elapsed signals · Brier score {aggregate_report.brier_score:.3f} "
                   f"(0=perfect, 0.25=a constant 50% guess, 1=worst)")
        st.plotly_chart(reliability_figure(aggregate_report), width="stretch", config={"displayModeBar": False})
        with st.expander("Table view (per-bin detail)"):
            for b in aggregate_report.bins:
                gap = b.observed_hit_rate - b.mean_predicted_confidence
                direction = "underconfident" if gap > 0.05 else ("overconfident" if gap < -0.05 else "well-calibrated")
                st.markdown(
                    f"conf [{b.bin_low:.1f}-{b.bin_high:.1f}) · n={b.n} · "
                    f"predicted **{b.mean_predicted_confidence:.0%}** vs observed **{b.observed_hit_rate:.0%}** "
                    f"({direction})"
                )
        other_segments = [r for r in calibration_reports if r.segment != "champion"]
        if other_segments:
            with st.expander(f"Per pair / horizon / regime breakdown ({len(other_segments)} segments with enough samples)"):
                segment_names = [r.segment for r in other_segments]
                picked = st.selectbox("Segment", segment_names, key="calibration_segment_pick")
                picked_report = next(r for r in other_segments if r.segment == picked)
                st.caption(f"n={picked_report.n} · Brier={picked_report.brier_score:.3f}")
                st.plotly_chart(
                    reliability_figure(picked_report), width="stretch", config={"displayModeBar": False},
                    key=f"reliability_{picked_report.segment}",
                )

# ----------------------------------------------------------------- knowledge --
with tab_knowledge:
    st.subheader("Knowledge Lab")
    st.caption(
        "Autonomous Upgrade Spec sec. 4-5: documents dropped into `forex_knowledge/` "
        "are scored, chunked and indexed automatically (hourly). Scoring is rule-based "
        "(no LLM key is configured for this project) — treat it as coarse triage, not a "
        "certified quality rating."
    )

    with engine.connect() as conn:
        docs = conn.execute(
            select(knowledge_documents_table).order_by(knowledge_documents_table.c.ingested_at.desc())
        ).mappings().all()

    d1, d2, d3 = st.columns(3)
    with d1:
        stat_tile("Documents Indexed", str(len(docs)), "across forex_knowledge/")
    with d2:
        total_chunks = sum(d["chunk_count"] for d in docs)
        stat_tile("Chunks Retrievable", str(total_chunks), "searchable passages")
    with d3:
        avg_score = (sum(d["overall_score"] or 0 for d in docs) / len(docs)) if docs else 0.0
        stat_tile("Avg Source Score", f"{avg_score:.2f}", "0-1, rule-based triage")

    st.write("")
    st.markdown("#### Search the knowledge library")
    query = st.text_input("Query", placeholder="e.g. how does the Fed set interest rates")
    if query:
        results = knowledge_search(engine, query, top_k=5)
        if not results:
            st.info("No relevant passages found — the library may not cover this topic yet.")
        for r in results:
            with st.container(border=True):
                st.markdown(f"**{r.document_title or 'Untitled'}** &nbsp; ·&nbsp; similarity={r.similarity:.2f}")
                st.caption(r.text[:400] + ("…" if len(r.text) > 400 else ""))

    st.write("")
    st.markdown("#### Ingested documents")
    if not docs:
        st.info(
            "No documents ingested yet. Drop PDFs, DOCX, TXT or MD files into the "
            "`forex_knowledge/` subfolders — they're picked up automatically within an hour, "
            "or run `python -m src.scripts.ingest_knowledge` to ingest immediately."
        )
    else:
        for d in docs:
            with st.container(border=True):
                st.markdown(f"**{d['title'] or d['filepath']}**")
                st.caption(
                    f"category={d['category']} · type={d['document_type']} · "
                    f"score={d['overall_score']:.2f} · novelty={d['novelty_score']:.2f} · "
                    f"chunks={d['chunk_count']} · ingested {d['ingested_at']:%Y-%m-%d %H:%M UTC}"
                )
                hypotheses = json.loads(d["candidate_hypotheses_json"]) if d.get("candidate_hypotheses_json") else []
                if hypotheses:
                    with st.expander(f"{len(hypotheses)} candidate hypothesis sentence(s) flagged"):
                        for h in hypotheses:
                            st.markdown(f"- {h}")

# ------------------------------------------------------------- currency map --
with tab_currency:
    st.subheader("Currency Strength Map")
    st.caption(
        "Autonomous Upgrade Spec sec. 10: an independent latent strength state per "
        "currency (technical + macro + cross-market + news), rather than only ever "
        "comparing two currencies implicitly inside one pair's decision. Aggregated "
        "from data already collected — no new data source, just a new view over it."
    )

    strengths = compute_currency_map()
    ranked = sorted(strengths.values(), key=lambda s: s.composite_score, reverse=True)

    st.plotly_chart(currency_strength_heatmap(ranked), width="stretch", config={"displayModeBar": False})

    cols = st.columns(len(ranked))
    for col, s in zip(cols, ranked):
        with col:
            polarity = "positive" if s.composite_score > 0.05 else ("negative" if s.composite_score < -0.05 else None)
            stat_tile(s.currency, f"{s.composite_score:+.2f}", f"conf {s.composite_confidence:.0%}", polarity=polarity)

    st.write("")
    st.markdown("#### Component breakdown")
    for s in ranked:
        with st.container(border=True):
            st.markdown(f"**{s.currency}** — composite {s.composite_score:+.2f} (confidence {s.composite_confidence:.0%})")
            b1, b2, b3, b4 = st.columns(4)
            b1.metric("Technical", f"{s.technical_score:+.2f}", f"conf {s.technical_confidence:.0%}")
            b2.metric("Macro", f"{s.macro_score:+.2f}", f"conf {s.macro_confidence:.0%}")
            b3.metric("Cross-market", f"{s.cross_market_score:+.2f}", f"conf {s.cross_market_confidence:.0%}")
            b4.metric("News", f"{s.news_score:+.2f}", f"conf {s.news_confidence:.0%}")
            if s.positioning_confidence > 0:
                crowd_label = "crowded long" if s.positioning_crowding_score > 0.1 else (
                    "crowded short" if s.positioning_crowding_score < -0.1 else "neutral"
                )
                st.caption(
                    f"CFTC positioning (weekly COT, reported separately — not in composite): "
                    f"{s.positioning_crowding_score:+.2f} ({crowd_label}, conf {s.positioning_confidence:.0%})"
                )

    st.caption(
        "Positioning is intentionally excluded from the composite score above: a crowded "
        "position can mean momentum continuing or reversal risk, and folding it in would "
        "silently pick one interpretation. It's shown per-currency instead, for judgment."
    )
    st.caption(
        "Reading this: two currencies far apart on the composite scale (e.g. one strongly "
        "positive, one strongly negative) is exactly the divergence the opportunity scanner "
        "should care about — not yet wired into pair selection (that's the regime router, "
        "a later priority), but visible here first."
    )

# ------------------------------------------------------------------ account --
with tab_account:
    for mode in ["paper", "demo"]:
        st.subheader(f"{'📝 Paper' if mode == 'paper' else '🏦 OANDA Demo'} account")
        try:
            state, positions = cached_account_state(mode, CURRENT_USER_ID)
        except Exception as exc:  # noqa: BLE001
            st.warning(f"Could not load {mode} account: {exc!r}")
            continue

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Balance", f"${state.balance:,.2f}")
        c2.metric("NAV", f"${state.nav:,.2f}")
        c3.metric("Unrealized P/L", f"${state.unrealized_pl:,.2f}")
        c4.metric("Open positions", state.open_position_count)

        if positions:
            st.dataframe(pd.DataFrame(positions), width="stretch", hide_index=True)
        else:
            st.caption("No open positions.")
        st.divider()

# -------------------------------------------------------------- risk center --
with tab_risk:
    st.subheader("Risk Center")
    st.caption(
        "Autonomous Upgrade Spec sec. 17: every hard limit the risk governor enforces, "
        "current state against each one, and what's actually been rejecting trades lately. "
        "src/risk/governor.py's own docstring: this component has veto power over every "
        "other agent, and nothing anywhere is allowed to loosen these to look better."
    )

    st.markdown("#### Hard limits")
    limits_rows = [
        {"Control": "Per-trade risk", "Value": f"{risk_governor.RISK_PER_TRADE_PCT:.2%} of balance (ceiling {risk_governor.RISK_PER_TRADE_CEILING_PCT:.2%})"},
        {"Control": "Daily loss limit", "Value": f"{risk_governor.DAILY_LOSS_LIMIT_PCT:.2%} — engages kill switch"},
        {"Control": "Weekly loss limit", "Value": f"{risk_governor.WEEKLY_LOSS_LIMIT_PCT:.2%} — engages kill switch"},
        {"Control": "Max concurrent positions", "Value": str(risk_governor.MAX_CONCURRENT_POSITIONS)},
        {"Control": "Max correlated USD exposure", "Value": f"{risk_governor.MAX_CORRELATED_EXPOSURE_PCT:.2%} of NAV"},
        {"Control": "Spread guard", "Value": f"reject above {risk_governor.MAX_SPREAD_MULTIPLE}x recent median spread"},
        {"Control": "Min confidence", "Value": f"{risk_governor.MIN_CONFIDENCE:.0%} — below this, no sizing"},
        {"Control": "Event lockout", "Value": f"{risk_governor.EVENT_LOCKOUT_MINUTES} min around tier-1 releases"},
        {"Control": "Price-drift / slippage guard", "Value": f"reject if price moved > {risk_governor.PRICE_DRIFT_STOP_RATIO:.0%} of stop distance since signal"},
        {"Control": "Stale-data guard", "Value": ", ".join(f"{k}: {v:.0f}s" for k, v in risk_governor.FRESHNESS_THRESHOLDS_SECONDS.items())},
        {"Control": "Regime size multipliers", "Value": ", ".join(f"{k}: x{v}" for k, v in risk_governor.REGIME_SIZE_MULTIPLIERS.items()) or "none active outside HIGH_VOLATILITY/SHOCK"},
        {"Control": "Confidence size floor", "Value": f"{risk_governor.CONFIDENCE_SIZE_FLOOR:.0%} of baseline at min confidence, up to 100% at full confidence — never above baseline"},
    ]
    section_card(lambda: st.dataframe(pd.DataFrame(limits_rows), width="stretch", hide_index=True))

    st.write("")
    st.markdown("#### Current state")
    kill_state = get_kill_switch_state()
    c1, c2, c3 = st.columns(3)
    with c1:
        if kill_state and kill_state["kill_switch_active"]:
            stat_tile("Kill switch", "🔴 ACTIVE", kill_state["kill_switch_reason"] or "", polarity="negative")
        else:
            stat_tile("Kill switch", "🟢 clear", "")
    with c2:
        with engine.connect() as conn:
            open_count_paper = conn.execute(
                select(func.count()).select_from(trade_intents)
                .where(trade_intents.c.status == "AUTO_EXECUTED", trade_intents.c.user_id == CURRENT_USER_ID)
            ).scalar()
        stat_tile("Auto-executed today", str(open_count_paper or 0), "trades sent without waiting for human review")
    with c3:
        with engine.connect() as conn:
            rejected_count = conn.execute(
                select(func.count()).select_from(trade_intents)
                .where(trade_intents.c.status == "RISK_REJECTED", trade_intents.c.user_id == CURRENT_USER_ID)
            ).scalar()
        stat_tile("Risk-rejected (all time)", str(rejected_count or 0), "signals the governor vetoed")

    st.write("")
    st.markdown("#### Daily / weekly loss usage")
    with engine.connect() as conn:
        week_key = f"{datetime.now(timezone.utc).isocalendar()[0]}-W{datetime.now(timezone.utc).isocalendar()[1]:02d}"
        week_row = conn.execute(
            select(risk_state_weekly_table).where(
                risk_state_weekly_table.c.user_id == CURRENT_USER_ID,
                risk_state_weekly_table.c.iso_week == week_key,
            )
        ).mappings().first()
    try:
        daily_pl_pct, weekly_pl_pct = current_pl_pct(kill_state, week_row, scan_mode)
        m1, m2 = st.columns(2)
        with m1:
            if daily_pl_pct is not None:
                st.plotly_chart(
                    loss_meter_figure(daily_pl_pct, risk_governor.DAILY_LOSS_LIMIT_PCT, "Daily"),
                    width="stretch", config={"displayModeBar": False}, key="loss_meter_daily",
                )
            else:
                st.caption("No daily baseline recorded yet.")
        with m2:
            if weekly_pl_pct is not None:
                st.plotly_chart(
                    loss_meter_figure(weekly_pl_pct, risk_governor.WEEKLY_LOSS_LIMIT_PCT, "Weekly"),
                    width="stretch", config={"displayModeBar": False}, key="loss_meter_weekly",
                )
            else:
                st.caption("No weekly baseline recorded yet.")
    except Exception as exc:  # noqa: BLE001
        st.caption(f"Could not compute loss usage: {exc!r}")

    st.write("")
    st.markdown("#### Per-instrument track record")
    st.caption(
        "User-requested 2026-08-14: position sizing is dampened (never boosted — same "
        "never-increase-beyond-baseline rule every other multiplier here follows) for "
        "instruments with a poor or unproven real hit rate, so genuinely reliable pairs "
        "end up carrying more of the book's risk without ever inflating confidence in "
        "something that hasn't earned it. 'One-sided' means every signal so far has been "
        "the same direction (e.g. a real trend that never happened to reverse) — that's "
        "deliberately NOT trusted as proven skill until the other direction has real "
        "evidence too (found live: USD_TRY showed a 100% hit rate that was 306/306 BUY-only)."
    )
    try:
        records = all_track_records(engine, CURRENT_USER_ID)
        if records:
            track_df = pd.DataFrame([
                {
                    "Instrument": r.instrument,
                    "Signals": r.n,
                    "Hit rate": f"{r.hit_rate:.0%}" if r.hit_rate is not None else "—",
                    "BUY / SELL": f"{r.buy_n} / {r.sell_n}",
                    "Two-sided evidence": "✅" if r.two_sided else "—",
                    "Size multiplier": f"x{r.multiplier:.2f}",
                }
                for r in records
            ])
            section_card(lambda: st.dataframe(track_df, width="stretch", hide_index=True))
        else:
            empty_state("No scored signals yet — accumulating evidence.")
    except Exception as exc:  # noqa: BLE001
        st.caption(f"Could not compute track records: {exc!r}")

    st.write("")
    st.markdown("#### What's actually been rejecting trades")
    with engine.connect() as conn:
        recent_decisions = conn.execute(
            select(risk_decisions_table.c.approved, risk_decisions_table.c.gates_json)
            .where(risk_decisions_table.c.user_id == CURRENT_USER_ID)
            .order_by(risk_decisions_table.c.id.desc()).limit(500)
        ).mappings().all()
    gate_failures: dict[str, int] = {}
    for d in recent_decisions:
        if d["approved"] or not d["gates_json"]:
            continue
        for g in json.loads(d["gates_json"]):
            if not g["passed"]:
                gate_failures[g["name"]] = gate_failures.get(g["name"], 0) + 1
    if gate_failures:
        fail_df = pd.DataFrame(sorted(gate_failures.items(), key=lambda kv: -kv[1]), columns=["Gate", "Rejections (last 500 decisions)"])
        section_card(lambda: st.dataframe(fail_df, width="stretch", hide_index=True))
    else:
        empty_state("✅ No gate rejections in the most recent decisions.")

# ----------------------------------------------------------- automation center --
with tab_automation:
    st.subheader("Automation Center")
    st.caption(
        "Autonomous Upgrade Spec sec. 19-20: every scheduled job this system depends on, "
        "read from its own log file rather than a live Task Scheduler query (simpler and "
        "more robust for a web page to read than shelling out to PowerShell on every render)."
    )
    logs_dir = Path(__file__).resolve().parent.parent.parent / "logs"
    jobs = [
        ("AIForex_DemoTradingCycle", "Core decision loop — price/macro/news/cross-market fusion, risk gating, execution", "continuous", "demo_trading.log"),
        ("AIForex_IngestKnowledge", "Knowledge Lab: scan forex_knowledge/ for new documents, score, chunk, index", "hourly", "ingest_knowledge.log"),
        ("AIForex_IngestGdeltNews", "GDELT news collection, clustering, novelty/velocity scoring", "every 20 min", "gdelt_news.log"),
        ("AIForex_CalendarIngest", "Forex Factory economic calendar refresh", "every 30 min", "calendar_ingest.log"),
        ("AIForex_ComputeEconomicSurprises", "FRED x Forex Factory surprise matching + reaction mismatch", "hourly", "economic_surprises.log"),
        ("AIForex_IngestCentralBank", "Fed statement archive, diff, tone, dissent extraction", "every 2 hours", "central_bank.log"),
        ("AIForex_IngestCftcPositioning", "CFTC weekly Commitments of Traders positioning", "daily", "cftc_positioning.log"),
        ("AIForex_SyncOutcomes", "Walk broker transaction ledger, match closed trades back to signals", "every 30 min", "sync_outcomes.log"),
        ("AIForex_TradeAttribution", "Post-trade attribution (model/execution/event/regime/noise)", "hourly", "trade_attribution.log"),
        ("AIForex_EvaluateChallengers", "Score champion + challenger shadow decisions vs real price outcomes", "every 30 min", "evaluate_challengers.log"),
        ("AIForex_TrainMetaModel", "Retrain meta-model candidate once enough labeled outcomes exist", "daily", "train_meta_model.log"),
        ("AIForex_PromotionGateSnapshot", "Records the current phase-readiness report per user (sample size, calibration, uptime, ...)", "weekly", "promotion_gates.log"),
    ]
    now_utc = datetime.now(timezone.utc)
    rows = []
    for name, description, cadence, log_file in jobs:
        path = logs_dir / log_file
        if path.exists():
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            age = format_duration((now_utc - mtime).total_seconds())
            status = "🟢 recent" if (now_utc - mtime).total_seconds() < 3600 * 6 else "🟡 stale"
        else:
            age, status = "never run", "⚪ no log yet"
        rows.append({"Job": name, "Cadence": cadence, "What it does": description, "Last log write": age, "Status": status})
    section_card(lambda: st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True))
    st.caption(
        "\"Stale\" flags a job whose log hasn't been touched in 6+ hours — for a daily/weekly "
        "cadence job that's normal, not a problem; it's most meaningful for the frequent ones."
    )

# -------------------------------------------------------------- audit log --
with tab_history:
    st.subheader("Authorization audit trail")
    history = auth_service.list_history(engine, CURRENT_USER_ID, limit=100)
    if not history:
        st.info("No authorization decisions have been made yet.")
    else:
        rows = []
        with engine.connect() as conn:
            for h in history:
                fill = None
                if h["resulting_client_order_id"]:
                    fill = conn.execute(
                        select(orders_fills).where(
                            orders_fills.c.client_order_id == h["resulting_client_order_id"],
                            orders_fills.c.user_id == CURRENT_USER_ID,
                        )
                    ).mappings().first()
                rows.append(
                    {
                        "authorized_at": h["authorized_at"],
                        "instrument": h["intent"]["instrument"] if h["intent"] else None,
                        "action": h["intent"]["action"] if h["intent"] else None,
                        "decision": h["decision"],
                        "authorized_by": h["authorized_by"],
                        "notes": h["notes"],
                        "order_status": fill["status"] if fill else None,
                        "fill_price": fill["fill_price"] if fill else None,
                    }
                )
        section_card(lambda: st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True))

# ------------------------------------------------------------- all signals --
with tab_all:
    st.subheader("All recent signals (every status, for full transparency)")
    with engine.connect() as conn:
        rows = conn.execute(
            select(trade_intents)
            .where(trade_intents.c.user_id == CURRENT_USER_ID)
            .order_by(trade_intents.c.id.desc()).limit(100)
        ).mappings().all()
    if rows:
        df = pd.DataFrame([dict(r) for r in rows])
        section_card(lambda: st.dataframe(
            df[["time", "instrument", "horizon", "action", "confidence", "regime", "status", "execution_mode"]],
            width="stretch", hide_index=True,
        ))
    else:
        empty_state("No signals generated yet.")

# ---------------------------------------------------------------- admin --
if CURRENT_IS_ADMIN and tab_admin is not None:
    with tab_admin:
        st.subheader("Admin")
        st.caption("Invite-only registration. Only visible to admin accounts.")

        with engine.connect() as conn:
            _member_count = conn.execute(select(func.count()).select_from(users_table)).scalar()
            _active_count = conn.execute(
                select(func.count()).select_from(users_table).where(users_table.c.status == "active")
            ).scalar()
        _pending_invite_count = len([
            i for i in user_auth_service.list_invitations(engine) if i["used_at"] is None
        ])
        ac1, ac2, ac3 = st.columns(3)
        with ac1:
            stat_tile("Members", str(_member_count or 0), f"{_active_count or 0} active")
        with ac2:
            stat_tile("Pending invitations", str(_pending_invite_count), "not yet registered")
        with ac3:
            stat_tile("Your role", "Admin", CURRENT_USER["email"])

        st.write("")
        st.markdown("#### Invite a new member")
        with st.form("invite_form"):
            invite_email = st.text_input("Email address to invite")
            invite_submitted = st.form_submit_button("Generate invite link", type="primary")
        if invite_submitted:
            if not invite_email.strip():
                st.error("Enter an email address.")
            else:
                token = user_auth_service.create_invitation(engine, CURRENT_USER_ID, invite_email.strip())
                invite_url = f"{'?'}invite={token}"
                st.success("Invite created. Share this link (append it to this dashboard's URL):")
                st.code(invite_url, language=None)
                st.caption(
                    f"Full link looks like: http://localhost:8501/{invite_url} — "
                    "valid for 7 days, single use."
                )

        st.write("")
        st.markdown("#### Pending invitations")
        invitations = user_auth_service.list_invitations(engine)
        pending = [i for i in invitations if i["used_at"] is None]
        if pending:
            inv_rows = [
                {"Email": i["email"], "Created": i["created_at"], "Expires": i["expires_at"],
                 "Link": f"?invite={i['token']}"}
                for i in pending
            ]
            section_card(lambda: st.dataframe(pd.DataFrame(inv_rows), width="stretch", hide_index=True))
        else:
            empty_state("No pending invitations.")

        st.write("")
        st.markdown("#### Members")
        with engine.connect() as conn:
            member_rows = conn.execute(select(users_table).order_by(users_table.c.created_at.desc())).mappings().all()
        for m in member_rows:
            with st.container(border=True):
                c1, c2, c3 = st.columns([3, 1, 1])
                with c1:
                    badge = "🛠️ admin" if m["is_admin"] else "member"
                    status_icon = "🟢" if m["status"] == "active" else "🔴"
                    st.markdown(f"**{m['username']}** ({m['email']}) — {badge} {status_icon} {m['status']}")
                    onboarded = user_auth_service.get_preferences(engine, m["id"])
                    has_creds = user_auth_service.has_oanda_credentials(engine, m["id"])
                    st.caption(
                        f"OANDA credentials: {'✅ connected' if has_creds else '⏳ not yet'} · "
                        f"Onboarding: {'✅ complete' if onboarded and onboarded['onboarding_complete'] else '⏳ pending'}"
                    )
                with c2:
                    if m["id"] != CURRENT_USER_ID and not m["is_admin"]:
                        if m["status"] == "active":
                            if st.button("Disable", key=f"disable_{m['id']}", width="stretch"):
                                user_auth_service.set_user_status(engine, m["id"], "disabled")
                                st.rerun()
                        else:
                            if st.button("Re-enable", key=f"enable_{m['id']}", width="stretch"):
                                user_auth_service.set_user_status(engine, m["id"], "active")
                                st.rerun()

        st.write("")
        st.markdown("#### Change my password")
        with st.form("change_password_form"):
            new_pw = st.text_input("New password", type="password")
            new_pw2 = st.text_input("Confirm new password", type="password")
            pw_submitted = st.form_submit_button("Update password")
        if pw_submitted:
            if not new_pw or new_pw != new_pw2:
                st.error("Passwords don't match or are empty.")
            elif len(new_pw) < 8:
                st.error("Password must be at least 8 characters.")
            else:
                user_auth_service.change_password(engine, CURRENT_USER_ID, new_pw)
                st.success("Password updated.")
