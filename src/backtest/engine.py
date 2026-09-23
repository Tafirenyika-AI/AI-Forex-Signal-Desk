"""Walk-forward backtest engine (blueprint sec. 6.2, sec. 14 Phase 2).

Rules enforced here, matching the blueprint directly:
- No random train/test shuffling — rolling-origin walk-forward only.
- Every model that scores a test window was trained only on data strictly
  before that window.
- Costs are applied: every simulated trade pays the spread on entry and exit.
- Reports the metrics sec. 14.1 asks for: net return, max drawdown, hit
  rate, payoff ratio, profit factor, and calibration.

Replays the REAL live decision path (src/run_loop.py's _evaluate_one_horizon)
per bar — classify_regime() + fuse(), not just a raw confidence-threshold cut
on the price model's p_up — and exits via a genuine bar-by-bar walk against
the same ATR-based stop/target fuse() computes for live trading, not a fixed
holding period. Two real, disclosed gaps versus live trading:

1. macro/news/cross_market/session components are stubbed at
   score=0/confidence=0 for every bar (see STUBBED_COMPONENTS) — their real
   ingestion depth (economic_events, news_events, market_indicators) is far
   shorter than the candle history now available (src/scripts/
   backfill_candles.py), so there's no honest historical value to feed them
   at most backtested bars. Only the `price` component (and regime, via
   fuse()'s REGIME_WEIGHT_MULTIPLIERS/REGIME_THRESHOLD_MULTIPLIERS) drives
   decisions here. This is disclosed in BacktestResult.stubbed_components,
   never silently blended in as if real.
2. The full risk governor (spread/freshness/event/agreement/correlation/
   sizing/kill-switch gates) is not replayed — only its MIN_CONFIDENCE gate
   is, since it's a single cheap, already-real constant and the closest
   analog to "would this actually have been taken."
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.decision.fusion import ATR_STOP_MULTIPLIER, ComponentView, fuse, price_component_view
from src.features.engine import REGIME_FEATURE_COLUMNS, add_forward_target, feature_ready_frame
from src.models.price_model import fit, predict_proba_up, walk_forward_splits
from src.models.regime import classify_regime
from src.risk.governor import MIN_CONFIDENCE

# OANDA majors typically trade single-digit-pip spreads on demo; without a
# stored historical spread series (candles don't carry one) this is a
# conservative flat assumption, not a measured value. Sec. 6.2 requires
# costs be included — this is the documented estimate used to do that.
ASSUMED_SPREAD_PIPS = {
    "EUR_USD": 1.2,
    "GBP_USD": 1.6,
    "USD_JPY": 1.5,
    "USD_CAD": 1.8,
    "AUD_USD": 1.4,
}
PIP_SIZE = {"USD_JPY": 0.01}
DEFAULT_PIP_SIZE = 0.0001

# See module docstring point 1 — these components have no honest historical
# value to feed most backtested bars and are stubbed at score=0/confidence=0
# (fuse() then gives them zero weight regardless of COMPONENT_WEIGHTS).
STUBBED_COMPONENTS = ["macro", "cross_market", "news", "session"]


def _spread_cost(instrument: str) -> float:
    pips = ASSUMED_SPREAD_PIPS.get(instrument, 1.5)
    pip_size = PIP_SIZE.get(instrument, DEFAULT_PIP_SIZE)
    return pips * pip_size


def _gap_aware_stop_fill(direction: int, stop_level: float, bar_open: float) -> float:
    """Real bug found 2026-09-23 (external review, P1-03, T04): a stop hit
    used to always fill at the exact stop_level, even when the bar's own
    OPEN had already gapped through it — no liquidity ever existed at
    stop_level in that bar, so a fill there is not achievable with OHLC-
    only data. Reproduced exactly: long at 100, stop 97, next bar opens 90
    and highs at 92 (the whole bar's range is 90-92, nowhere near 97) used
    to still report a 97 fill. Conservative gap-aware assumption: if the
    open is already past the stop level in the adverse direction, the
    realistic fill is the open price (the first price the market actually
    traded at); otherwise (the ordinary intrabar-touch case) the fill
    stays at stop_level exactly, unchanged from before."""
    if direction == 1:  # long stop (a sell) -- a worse fill is LOWER
        return min(stop_level, bar_open)
    return max(stop_level, bar_open)  # short stop (a buy) -- a worse fill is HIGHER


def _simulate_exit(
    candles_df: pd.DataFrame,
    entry_idx: int,
    direction: int,  # 1 = long, -1 = short
    entry_price: float,
    stop_distance: float,
    target_distance: float,
    max_hold_bars: int | None = None,
) -> tuple[int, float, str]:
    """Walks forward bar-by-bar from entry_idx+1, checking each bar's
    high/low against the stop/target levels fixed at entry — the same
    ATR-sized-once-at-decision-time mechanism fuse() uses for live trading
    (src/decision/fusion.py's ATR_STOP_MULTIPLIER/REWARD_RISK_MULTIPLE), not
    a fixed-bar-count exit. A single bar's range crossing BOTH levels is
    possible with OHLC-only data (no intrabar tick order) — the stop is
    assumed to hit first, the conservative assumption.

    Returns (exit_idx, exit_price, exit_reason): exit_reason is "stop",
    "target", "timeout" (max_hold_bars reached first) or "eod" (ran out of
    candles before either level was hit — a real, disclosed limitation of
    backtesting near the end of available history, not a bug: the trade is
    honestly closed at the last available close rather than silently
    dropped)."""
    if direction == 1:
        stop_level = entry_price - stop_distance
        target_level = entry_price + target_distance
    else:
        stop_level = entry_price + stop_distance
        target_level = entry_price - target_distance

    last_idx = len(candles_df) - 1
    end_idx = last_idx if max_hold_bars is None else min(last_idx, entry_idx + max_hold_bars)

    for idx in range(entry_idx + 1, end_idx + 1):
        bar = candles_df.iloc[idx]
        if direction == 1:
            stop_hit = bar["low"] <= stop_level
            target_hit = bar["high"] >= target_level
        else:
            stop_hit = bar["high"] >= stop_level
            target_hit = bar["low"] <= target_level
        if stop_hit:
            fill_price = _gap_aware_stop_fill(direction, stop_level, float(bar["open"]))
            return idx, float(fill_price), "stop"
        if target_hit:
            return idx, float(target_level), "target"

    reason = "timeout" if max_hold_bars is not None and end_idx < last_idx else "eod"
    return end_idx, float(candles_df.iloc[end_idx]["close"]), reason


def _simulate_trailing_exit(
    candles_df: pd.DataFrame,
    entry_idx: int,
    direction: int,  # 1 = long, -1 = short
    entry_price: float,
    stop_distance: float,
    target_distance: float,
    atr_multiplier: float = ATR_STOP_MULTIPLIER,
    max_hold_bars: int | None = None,
) -> tuple[int, float, str]:
    """Phase D1 — same bar-by-bar walk as _simulate_exit, but the stop
    ratchets in the trade's favor each bar using THAT bar's own atr_14
    (never against it — a long's stop only ever rises, a short's only ever
    falls), recomputed with the same ATR_STOP_MULTIPLIER fuse() uses for
    the initial stop. The target does not trail (Phase D's scope is a
    trailing STOP, not a trailing target).

    Stays causal (no lookahead) by ordering each bar's work as: (1) check
    the hit against the stop level as of the START of this bar (set by
    prior bars), (2) only then ratchet the stop using this bar's own
    close/atr_14, for the NEXT bar's check. A bar can never move its own
    stop and then get evaluated against the moved level in the same step —
    otherwise a big favorable move could "dodge" a stop-out that same bar's
    low/high should have triggered under the pre-ratchet level.

    Returns (exit_idx, exit_price, exit_reason) — same vocabulary as
    _simulate_exit ("stop", "target", "timeout", "eod")."""
    if direction == 1:
        stop_level = entry_price - stop_distance
        target_level = entry_price + target_distance
    else:
        stop_level = entry_price + stop_distance
        target_level = entry_price - target_distance

    last_idx = len(candles_df) - 1
    end_idx = last_idx if max_hold_bars is None else min(last_idx, entry_idx + max_hold_bars)

    for idx in range(entry_idx + 1, end_idx + 1):
        bar = candles_df.iloc[idx]
        if direction == 1:
            stop_hit = bar["low"] <= stop_level
            target_hit = bar["high"] >= target_level
        else:
            stop_hit = bar["high"] >= stop_level
            target_hit = bar["low"] <= target_level
        if stop_hit:
            fill_price = _gap_aware_stop_fill(direction, stop_level, float(bar["open"]))
            return idx, float(fill_price), "stop"
        if target_hit:
            return idx, float(target_level), "target"

        bar_atr = bar["atr_14"]
        if pd.notna(bar_atr) and bar_atr > 0:
            candidate_distance = atr_multiplier * bar_atr
            if direction == 1:
                stop_level = max(stop_level, bar["close"] - candidate_distance)
            else:
                stop_level = min(stop_level, bar["close"] + candidate_distance)

    reason = "timeout" if max_hold_bars is not None and end_idx < last_idx else "eod"
    return end_idx, float(candles_df.iloc[end_idx]["close"]), reason


@dataclass
class BacktestResult:
    instrument: str
    granularity: str
    horizon_bars: int
    n_trades: int
    net_return_pct: float
    max_drawdown_pct: float
    hit_rate: float
    payoff_ratio: float
    profit_factor: float
    calibration: pd.DataFrame
    trade_log: pd.DataFrame
    regime_distribution: dict[str, int]
    stubbed_components: list[str]
    equity_curve: pd.Series = field(repr=False)


def run_walk_forward_backtest(
    candles_df: pd.DataFrame,
    instrument: str,
    granularity: str,
    horizon_bars: int = 4,
    train_window: int = 250,
    test_window: int = 50,
    min_confidence: float = MIN_CONFIDENCE,
    max_hold_bars: int | None = None,
    use_trailing_stop: bool = False,
) -> BacktestResult | None:
    featured = feature_ready_frame(candles_df)
    classified = classify_regime(featured)
    labeled = add_forward_target(classified, horizon_bars)
    usable = labeled.dropna(subset=["target_up", *REGIME_FEATURE_COLUMNS]).reset_index(drop=True)

    splits = list(walk_forward_splits(len(usable), train_window, test_window))
    if not splits:
        return None

    spread = _spread_cost(instrument)
    trades = []
    all_oos_probs = []
    all_oos_actuals = []
    regime_counts: dict[str, int] = {}

    for split in splits:
        # Real bug found 2026-09-23 (external review, P1-03, T05): a row's
        # target_up label is close[row + horizon_bars] (see add_forward_
        # target), computed on the FULL frame before splitting — so the
        # last `horizon_bars` rows of every naive [train_start:train_end)
        # slice have labels that reach past train_end into the test
        # window's own prices. With the defaults (train_window=250,
        # horizon_bars=4), that's exactly rows 246-249, matching the
        # brief's own reproduction precisely. Standard "purged" walk-
        # forward practice: drop those rows from training rather than let
        # the model see labels informed by data it's about to be scored
        # against — this module's own docstring already claims "trained
        # only on data strictly before that window," which was false for
        # these rows until now.
        train_df = usable.iloc[split.train_start : split.train_end - horizon_bars]
        test_df = usable.iloc[split.test_start : split.test_end]

        model = fit(train_df)
        probs = predict_proba_up(model, test_df)
        all_oos_probs.extend(probs)
        all_oos_actuals.extend(test_df["target_up"].astype(int).to_numpy())

        for i, (_, row) in enumerate(test_df.iterrows()):
            p_up = float(probs[i])
            regime = str(row["regime"])
            regime_counts[regime] = regime_counts.get(regime, 0) + 1

            component_views = [
                price_component_view(p_up),
                *(ComponentView(name, 0.0, 0.0) for name in STUBBED_COMPONENTS),
            ]
            decision = fuse(
                instrument=instrument,
                horizon=f"{horizon_bars}bar",
                regime=regime,
                component_views=component_views,
                current_price=float(row["close"]),
                atr_14=float(row["atr_14"]),
                data_freshness={},  # nothing to be stale against in a backtest
                now=row["time"].to_pydatetime(),
            )

            if decision.action == "NO_TRADE" or decision.confidence < min_confidence:
                continue
            if not decision.stop_distance or not decision.take_profit_distance:
                continue

            direction = 1 if decision.action == "BUY" else -1
            entry_idx = int(row.name)
            entry_price = float(row["close"])
            exit_fn = _simulate_trailing_exit if use_trailing_stop else _simulate_exit
            exit_idx, exit_price, exit_reason = exit_fn(
                usable, entry_idx, direction, entry_price,
                decision.stop_distance, decision.take_profit_distance, max_hold_bars=max_hold_bars,
            )

            gross_return = direction * (exit_price - entry_price) / entry_price
            cost_return = spread / entry_price  # spread paid once, round-turn approximated as 1x
            net_return = gross_return - cost_return

            trades.append(
                {
                    "time": row["time"],
                    "exit_time": usable.iloc[exit_idx]["time"],
                    "direction": "BUY" if direction == 1 else "SELL",
                    "entry": entry_price,
                    "exit": exit_price,
                    "exit_reason": exit_reason,
                    "regime": regime,
                    "confidence": decision.confidence,
                    "p_up": p_up,
                    "net_return": net_return,
                    "win": net_return > 0,
                }
            )

    trade_log = pd.DataFrame(trades)
    if trade_log.empty:
        return BacktestResult(
            instrument=instrument,
            granularity=granularity,
            horizon_bars=horizon_bars,
            n_trades=0,
            net_return_pct=0.0,
            max_drawdown_pct=0.0,
            hit_rate=float("nan"),
            payoff_ratio=float("nan"),
            profit_factor=float("nan"),
            calibration=pd.DataFrame(),
            trade_log=trade_log,
            regime_distribution=regime_counts,
            stubbed_components=STUBBED_COMPONENTS,
            equity_curve=pd.Series(dtype=float),
        )

    equity_curve = (1 + trade_log["net_return"]).cumprod()
    running_max = equity_curve.cummax()
    drawdown = (equity_curve - running_max) / running_max
    max_drawdown_pct = float(drawdown.min() * 100)

    wins = trade_log[trade_log["net_return"] > 0]["net_return"]
    losses = trade_log[trade_log["net_return"] <= 0]["net_return"]
    hit_rate = len(wins) / len(trade_log)
    payoff_ratio = float(wins.mean() / abs(losses.mean())) if len(losses) and len(wins) else float("nan")
    profit_factor = float(wins.sum() / abs(losses.sum())) if losses.sum() != 0 else float("nan")
    net_return_pct = float((equity_curve.iloc[-1] - 1) * 100)

    calibration = _calibration_table(np.array(all_oos_probs), np.array(all_oos_actuals))

    return BacktestResult(
        instrument=instrument,
        granularity=granularity,
        horizon_bars=horizon_bars,
        n_trades=len(trade_log),
        net_return_pct=net_return_pct,
        max_drawdown_pct=max_drawdown_pct,
        hit_rate=hit_rate,
        payoff_ratio=payoff_ratio,
        profit_factor=profit_factor,
        calibration=calibration,
        trade_log=trade_log,
        regime_distribution=regime_counts,
        stubbed_components=STUBBED_COMPONENTS,
        equity_curve=equity_curve,
    )


def _calibration_table(probs: np.ndarray, actuals: np.ndarray, n_buckets: int = 5) -> pd.DataFrame:
    """sec. 14.1: 'do 70% predictions actually win about 70% in comparable
    conditions?' — bucket predicted P(up) and compare to realized frequency."""
    if len(probs) == 0:
        return pd.DataFrame()
    df = pd.DataFrame({"p_up": probs, "actual_up": actuals})
    df["bucket"] = pd.qcut(df["p_up"], q=min(n_buckets, df["p_up"].nunique()), duplicates="drop")
    grouped = df.groupby("bucket", observed=True).agg(
        predicted_mean=("p_up", "mean"),
        actual_rate=("actual_up", "mean"),
        n=("actual_up", "size"),
    )
    return grouped.reset_index()
