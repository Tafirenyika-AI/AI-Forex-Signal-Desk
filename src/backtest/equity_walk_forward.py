"""Equity V2 Phase 11 — walk-forward / point-in-time model comparison for
Phase 10's equity challengers (FUNDAMENTAL/NEWS_EVENT/MARKET_REGIME/
CROSS_MARKET) and the existing price-only baseline.

Scope boundary, deliberate (see docs/EQUITY_V2_IMPLEMENTATION_LOG.md's own
Phase 11 entry for the full reasoning): this module evaluates RAW MODEL
SKILL — does a component's predicted probability actually anticipate the
real next-horizon price move — via a simple long/short-on-signal return,
NOT full trade execution (real stop/target/ATR exits, position sizing,
capital constraints). That level of realism already exists in
src/backtest/engine.py for the price-only forex case and will be extended
for multi-position/capital-aware equity portfolios in Phase 12 (the
brief's own next phase). Building a second full execution simulator here
would be the "one giant rewrite" duplication the brief's own instructions
warn against.

No future-information leakage: reuses src/models/price_model.py's own
walk_forward_splits (rolling-origin, never shuffled) and applies the same
purge src/backtest/engine.py's own module docstring documents — a row's
target_up label reaches horizon_bars past train_end, so those trailing
rows are dropped from the TRAINING slice, never just from the test
window. This is the exact T05 leakage class the brief's Phase 11 ask
explicitly names ("prevent any future-information leakage").

Metrics, deliberately matching this project's own existing vocabulary
(src/evaluation/promotion_gates.py) rather than introducing a differently-
named parallel standard: a simple (non-annualized) mean/std "Sharpe-like"
ratio and its downside-deviation-only "Sortino-like" counterpart, profit
factor, max drawdown, a worst-vs-mean tail-loss ratio, and a Brier score
(0=perfect, 0.25=baseline constant-0.5-guesser, same scale
src/models/calibration.py already uses) — not a textbook annualized
Sharpe/Sortino, which would imply a rigor this sample size and horizon
don't support. Regime stability is a straight per-regime breakdown of the
same win-rate/mean-return figures.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.models.equity_challengers import COMPONENT_COLUMNS, fit, fit_meta, predict_meta, predict_proba_up, usable_rows
from src.models.price_model import fit as fit_price, predict_proba_up as predict_proba_up_price, walk_forward_splits
from src.features.engine import FEATURE_COLUMNS


@dataclass(frozen=True)
class WalkForwardModelResult:
    model_name: str
    n_oos_predictions: int
    expectancy: float  # mean simple return per OOS prediction, sign-adjusted for the implied long/short
    sharpe_like: float  # mean / std of per-prediction returns, NOT annualized
    sortino_like: float  # mean / downside-deviation of per-prediction returns, NOT annualized
    profit_factor: float
    # CAUTION, real and material: this compounds one "trade" per bar over
    # a `horizon_bars`-long window that OVERLAPS with the next several
    # bars' own windows (a new prediction every bar, each looking
    # `horizon_bars` ahead) -- sequentially compounding thousands of these
    # as if one closed before the next began overstates real volatility/
    # drawdown far beyond what an actual position-sized portfolio would
    # ever see. Confirmed live: real NVDA numbers came back -74% to -86%
    # across every component, including the existing price baseline --
    # not a claim that trading this system loses that much, but an
    # artifact of this measurement convention. Useful ONLY for comparing
    # models RELATIVE to each other under an identical convention, never
    # as a standalone claim about real account risk. A true, non-
    # overlapping, capital-aware equity curve is explicitly Phase 12's job
    # ("portfolio-level backtester... true mark-to-market equity curve").
    max_drawdown_pct: float
    tail_loss_ratio: float  # worst single return / mean losing return
    brier_score: float
    regime_stability: dict[str, dict[str, float]] = field(default_factory=dict)  # regime -> {n, win_rate, mean_return}


def _brier(probs: np.ndarray, actuals: np.ndarray) -> float:
    if len(probs) == 0:
        return float("nan")
    return float(np.mean((probs - actuals) ** 2))


def _simple_returns_at(full_df: pd.DataFrame, index: pd.Index, probs: np.ndarray, horizon_bars: int) -> np.ndarray:
    """Sign-adjusted next-horizon simple return for exactly the rows in
    `index` (long, kept as-is, when the model implies p_up > 0.5; short,
    negated, otherwise). No spread/slippage cost applied — this is a pure
    skill measurement (see module docstring's scope boundary), not a claim
    of a realistically tradeable return.

    Real bug this signature guards against: a component's usable_rows()
    filter can drop individual rows (e.g. before a ticker's first real
    SEC filing), leaving a subset with internal gaps relative to the
    original bar sequence. Computing `.shift(-horizon_bars)` directly on
    that filtered subset would silently look up the wrong future bar --
    shifting N POSITIONS in a gappy frame is not the same as N real bars
    ahead. `full_df` must always be the fold's own gap-free reference
    frame (e.g. the full test_df, never a component-filtered subset); this
    function looks up each row's future close from THAT frame, then
    selects only `index`'s rows from the result."""
    future_close = full_df["close"].shift(-horizon_bars).loc[index].to_numpy()
    current_close = full_df["close"].loc[index].to_numpy()
    raw_return = (future_close - current_close) / current_close
    direction = np.where(probs > 0.5, 1.0, -1.0)
    return direction * raw_return


def _aggregate_metrics(model_name: str, returns: np.ndarray, probs: np.ndarray, actuals: np.ndarray, regimes: np.ndarray | None) -> WalkForwardModelResult:
    n = len(returns)
    if n == 0:
        return WalkForwardModelResult(
            model_name=model_name, n_oos_predictions=0, expectancy=float("nan"), sharpe_like=float("nan"),
            sortino_like=float("nan"), profit_factor=float("nan"), max_drawdown_pct=float("nan"),
            tail_loss_ratio=float("nan"), brier_score=float("nan"),
        )

    expectancy = float(np.mean(returns))
    std = float(np.std(returns))
    sharpe_like = expectancy / std if std > 0 else float("nan")

    downside = returns[returns < 0]
    downside_std = float(np.std(downside)) if len(downside) > 0 else 0.0
    sortino_like = expectancy / downside_std if downside_std > 0 else float("nan")

    wins = returns[returns > 0]
    losses = returns[returns <= 0]
    profit_factor = float(wins.sum() / abs(losses.sum())) if losses.sum() != 0 else float("nan")

    equity_curve = np.cumprod(1 + returns)
    running_max = np.maximum.accumulate(equity_curve)
    drawdown = (equity_curve - running_max) / running_max
    max_drawdown_pct = float(drawdown.min() * 100) if len(drawdown) else float("nan")

    if len(losses) > 0:
        mean_loss = float(losses.mean())
        tail_loss_ratio = float(losses.min() / mean_loss) if mean_loss != 0 else float("nan")
    else:
        tail_loss_ratio = float("nan")

    brier = _brier(probs, actuals)

    regime_stability: dict[str, dict[str, float]] = {}
    if regimes is not None:
        for regime in pd.unique(regimes):
            mask = regimes == regime
            regime_returns = returns[mask]
            if len(regime_returns) == 0:
                continue
            regime_stability[str(regime)] = {
                "n": float(len(regime_returns)),
                "win_rate": float((regime_returns > 0).mean()),
                "mean_return": float(regime_returns.mean()),
            }

    return WalkForwardModelResult(
        model_name=model_name, n_oos_predictions=n, expectancy=expectancy, sharpe_like=sharpe_like,
        sortino_like=sortino_like, profit_factor=profit_factor, max_drawdown_pct=max_drawdown_pct,
        tail_loss_ratio=tail_loss_ratio, brier_score=brier, regime_stability=regime_stability,
    )


_MIN_COMPONENT_TRAIN_ROWS = 30  # below this, a component's fold-level fit would be on too few real examples to trust


def run_equity_walk_forward(
    frame: pd.DataFrame, horizon_bars: int = 4, train_window: int = 250, test_window: int = 50,
) -> dict[str, WalkForwardModelResult]:
    """frame: src/features/equity_vectorized.py's build_training_frame()
    output, already labeled with target_up (src/features/engine.py's
    add_forward_target). Evaluates the existing price-only baseline
    alongside every Phase 10 challenger, walked forward identically and
    trained only on data strictly before the window it's scored against.

    META is evaluated SEPARATELY, after every fold's genuinely-OOS
    predictions are collected (never re-splitting within a fold, which
    would just shrink an already-small test window further): every row
    where price AND every challenger produced an OOS prediction is kept,
    then split in simple, non-shuffled, chronological order (first ~70%
    fits the stack, the rest evaluates it) -- the META model only ever
    sees OOS base-model outputs, never anything derived from its own
    evaluation rows."""
    results: dict[str, dict[str, list]] = {
        name: {"returns": [], "probs": [], "actuals": [], "regimes": []}
        for name in ("price", *COMPONENT_COLUMNS.keys())
    }
    # row index (position in price_usable) -> {component_name: oos_prob} --
    # built incrementally across folds, used only for the META pass below.
    oos_probs_by_row: dict[int, dict[str, float]] = {}

    price_usable = frame.dropna(subset=[*FEATURE_COLUMNS, "target_up"]).reset_index(drop=True)
    splits = list(walk_forward_splits(len(price_usable), train_window, test_window))

    for split in splits:
        # Same purge src/backtest/engine.py's own docstring documents: the
        # last horizon_bars rows of the naive training slice have labels
        # that reach into the test window's own future prices.
        train_df = price_usable.iloc[split.train_start: split.train_end - horizon_bars]
        test_df = price_usable.iloc[split.test_start: split.test_end]
        if train_df.empty or test_df.empty:
            continue

        price_model = fit_price(train_df)
        price_probs = predict_proba_up_price(price_model, test_df)
        results["price"]["returns"].extend(_simple_returns_at(test_df, test_df.index, price_probs, horizon_bars))
        results["price"]["probs"].extend(price_probs)
        results["price"]["actuals"].extend(test_df["target_up"].astype(int).to_numpy())
        results["price"]["regimes"].extend(test_df["regime"].to_numpy() if "regime" in test_df else [None] * len(test_df))
        for row_idx, prob in zip(test_df.index, price_probs):
            oos_probs_by_row.setdefault(row_idx, {})["price"] = float(prob)

        for component in COMPONENT_COLUMNS:
            component_train = usable_rows(train_df, component)
            component_test = usable_rows(test_df, component)
            if len(component_train) < _MIN_COMPONENT_TRAIN_ROWS or component_test.empty:
                continue
            model = fit(component, component_train)
            probs = predict_proba_up(component, model, component_test)
            results[component]["returns"].extend(_simple_returns_at(test_df, component_test.index, probs, horizon_bars))
            results[component]["probs"].extend(probs)
            results[component]["actuals"].extend(component_test["target_up"].astype(int).to_numpy())
            results[component]["regimes"].extend(
                component_test["regime"].to_numpy() if "regime" in component_test else [None] * len(component_test)
            )
            for row_idx, prob in zip(component_test.index, probs):
                oos_probs_by_row.setdefault(row_idx, {})["component_" + component] = float(prob)

    final: dict[str, WalkForwardModelResult] = {}
    for name, data in results.items():
        returns = np.array(data["returns"], dtype=float)
        probs = np.array(data["probs"], dtype=float)
        actuals = np.array(data["actuals"], dtype=float)
        regimes = np.array(data["regimes"], dtype=object) if data["regimes"] else None
        valid = ~np.isnan(returns)
        final[name] = _aggregate_metrics(name, returns[valid], probs[valid], actuals[valid],
                                          regimes[valid] if regimes is not None else None)

    final["meta"] = _evaluate_meta(oos_probs_by_row, price_usable, horizon_bars)
    return final


def _evaluate_meta(oos_probs_by_row: dict[int, dict[str, float]], price_usable: pd.DataFrame, horizon_bars: int) -> WalkForwardModelResult:
    expected_keys = {"price", *(f"component_{c}" for c in COMPONENT_COLUMNS)}
    complete_rows = {
        idx: vals for idx, vals in oos_probs_by_row.items() if expected_keys.issubset(vals.keys())
    }
    if len(complete_rows) < 20:
        return _aggregate_metrics("meta", np.array([]), np.array([]), np.array([]), None)

    ordered_index = sorted(complete_rows.keys())  # chronological, never shuffled
    component_frame = pd.DataFrame([complete_rows[i] for i in ordered_index], index=ordered_index)

    split_point = int(len(ordered_index) * 0.7)
    fit_idx, eval_idx = ordered_index[:split_point], ordered_index[split_point:]
    if len(fit_idx) < 10 or len(eval_idx) < 10:
        return _aggregate_metrics("meta", np.array([]), np.array([]), np.array([]), None)

    fit_targets = price_usable.loc[fit_idx, "target_up"].astype(int)
    eval_targets = price_usable.loc[eval_idx, "target_up"].astype(int)

    meta_model = fit_meta(component_frame.loc[fit_idx], fit_targets)
    meta_probs = predict_meta(meta_model, component_frame.loc[eval_idx])

    # price_usable (not component_frame) is the gap-free reference -- see
    # _simple_returns_at's own docstring for why the lookup must happen
    # against a contiguous frame, never the (here, possibly gappy)
    # subset of rows every component happened to have data for.
    returns = _simple_returns_at(price_usable, eval_idx, meta_probs, horizon_bars)
    eval_df = price_usable.loc[eval_idx]
    regimes = eval_df["regime"].to_numpy() if "regime" in eval_df else None
    valid = ~np.isnan(returns)
    return _aggregate_metrics(
        "meta", returns[valid], meta_probs[valid], eval_targets.to_numpy()[valid],
        regimes[valid] if regimes is not None else None,
    )
