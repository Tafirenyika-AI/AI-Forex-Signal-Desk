"""Walk-forward backtest engine (blueprint sec. 6.2, sec. 14 Phase 2).

Rules enforced here, matching the blueprint directly:
- No random train/test shuffling — rolling-origin walk-forward only.
- Every model that scores a test window was trained only on data strictly
  before that window.
- Costs are applied: every simulated trade pays the spread on entry and exit.
- Reports the metrics sec. 14.1 asks for: net return, max drawdown, hit
  rate, payoff ratio, profit factor, and calibration.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.features.engine import FEATURE_COLUMNS, add_forward_target, feature_ready_frame
from src.models.price_model import fit, predict_proba_up, walk_forward_splits

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


def _spread_cost(instrument: str) -> float:
    pips = ASSUMED_SPREAD_PIPS.get(instrument, 1.5)
    pip_size = PIP_SIZE.get(instrument, DEFAULT_PIP_SIZE)
    return pips * pip_size


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
    equity_curve: pd.Series = field(repr=False)


def run_walk_forward_backtest(
    candles_df: pd.DataFrame,
    instrument: str,
    granularity: str,
    horizon_bars: int = 4,
    train_window: int = 250,
    test_window: int = 50,
    confidence_threshold: float = 0.58,
) -> BacktestResult | None:
    featured = feature_ready_frame(candles_df)
    labeled = add_forward_target(featured, horizon_bars)
    usable = labeled.dropna(subset=["target_up"]).reset_index(drop=True)

    splits = list(walk_forward_splits(len(usable), train_window, test_window))
    if not splits:
        return None

    spread = _spread_cost(instrument)
    trades = []
    all_oos_probs = []
    all_oos_actuals = []

    for split in splits:
        train_df = usable.iloc[split.train_start : split.train_end]
        test_df = usable.iloc[split.test_start : split.test_end]

        model = fit(train_df)
        probs = predict_proba_up(model, test_df)
        all_oos_probs.extend(probs)
        all_oos_actuals.extend(test_df["target_up"].astype(int).to_numpy())

        for i, (_, row) in enumerate(test_df.iterrows()):
            p_up = probs[i]
            if p_up >= confidence_threshold:
                direction = 1
            elif p_up <= (1 - confidence_threshold):
                direction = -1
            else:
                continue  # confidence gate: NO TRADE

            entry = row["close"]
            exit_idx = row.name + horizon_bars
            if exit_idx >= len(usable):
                continue
            exit_price = usable.iloc[exit_idx]["close"]

            gross_return = direction * (exit_price - entry) / entry
            cost_return = spread / entry  # spread paid once, round-turn approximated as 1x
            net_return = gross_return - cost_return

            trades.append(
                {
                    "time": row["time"],
                    "direction": "BUY" if direction == 1 else "SELL",
                    "entry": entry,
                    "exit": exit_price,
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
