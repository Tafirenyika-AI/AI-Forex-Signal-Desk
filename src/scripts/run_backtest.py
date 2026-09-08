"""Phase 2 walk-forward backtest (blueprint sec. 14).

Run from the project root with the venv active:
    python -m src.scripts.run_backtest

Replays the real live decision path per bar (classify_regime + fuse, ATR
stop/target exit) via src/backtest/engine.py — see that module's docstring
for exactly which components are stubbed (macro/cross_market/news/session)
and why. Uses whatever history src/scripts/backfill_candles.py has already
stored; run that first if a pair here reports "not enough candles yet".
"""
from __future__ import annotations

from src.backtest.engine import run_walk_forward_backtest
from src.config import load_settings
from src.data.db import get_engine
from src.features.engine import load_candles_df
from src.run_loop import PAIRS  # single source of truth — was a second, silently-diverging copy before

GRANULARITY = "H1"
HORIZON_BARS = 4  # H1 candles -> 4h horizon, matching blueprint sec 6.1


def main() -> None:
    settings = load_settings()
    engine = get_engine(settings.db_path)

    print(f"{'pair':10} {'n_trades':>9} {'net_ret%':>9} {'max_dd%':>9} "
          f"{'hit_rate':>9} {'payoff':>8} {'p_factor':>9}")
    print("-" * 70)

    for pair in PAIRS:
        df = load_candles_df(engine, pair, GRANULARITY)
        result = run_walk_forward_backtest(
            df,
            instrument=pair,
            granularity=GRANULARITY,
            horizon_bars=HORIZON_BARS,
            train_window=200,
            test_window=40,
        )
        if result is None:
            print(f"{pair:10} not enough candles for a single walk-forward split yet")
            continue

        print(f"{pair:10} {result.n_trades:9d} {result.net_return_pct:9.2f} "
              f"{result.max_drawdown_pct:9.2f} {result.hit_rate:9.2%} "
              f"{result.payoff_ratio:8.2f} {result.profit_factor:9.2f}")
        print(f"  regime distribution: {result.regime_distribution}")
        print(f"  stubbed components (score=0/conf=0, not real historical data): "
              f"{', '.join(result.stubbed_components)}")

        if not result.trade_log.empty:
            print(f"  exit reasons: {result.trade_log['exit_reason'].value_counts().to_dict()}")

        if not result.calibration.empty:
            print(f"  calibration ({pair}):")
            print(result.calibration.to_string(index=False).replace("\n", "\n  "))
        print()


if __name__ == "__main__":
    main()
