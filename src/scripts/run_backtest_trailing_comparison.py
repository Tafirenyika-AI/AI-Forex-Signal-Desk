"""Phase D1 — compares a ratcheting ATR-trailing stop against the fixed
ATR stop/target baseline, on the exact same walk-forward folds and
decisions (only the exit mechanic differs) via src/backtest/engine.py's
use_trailing_stop flag.

This is the evidence D2 (live trailing-stop broker code) is gated on —
review these numbers before touching any live broker code, per the
project's own plan.

Run from the project root with the venv active:
    python -m src.scripts.run_backtest_trailing_comparison
"""
from __future__ import annotations

from src.backtest.engine import run_walk_forward_backtest
from src.config import load_settings
from src.data.db import get_engine
from src.features.engine import load_candles_df
from src.run_loop import PAIRS

GRANULARITY = "H1"
HORIZON_BARS = 4


def main() -> None:
    settings = load_settings()
    engine = get_engine(settings.db_path)

    print(f"{'pair':10} {'exit':9} {'n_trades':>9} {'net_ret%':>9} {'max_dd%':>9} "
          f"{'hit_rate':>9} {'payoff':>8} {'p_factor':>9}")
    print("-" * 80)

    for pair in PAIRS:
        df = load_candles_df(engine, pair, GRANULARITY)
        for label, use_trailing in [("fixed", False), ("trailing", True)]:
            result = run_walk_forward_backtest(
                df,
                instrument=pair,
                granularity=GRANULARITY,
                horizon_bars=HORIZON_BARS,
                train_window=200,
                test_window=40,
                use_trailing_stop=use_trailing,
            )
            if result is None:
                print(f"{pair:10} {label:9} not enough candles for a single walk-forward split yet")
                continue
            print(f"{pair:10} {label:9} {result.n_trades:9d} {result.net_return_pct:9.2f} "
                  f"{result.max_drawdown_pct:9.2f} {result.hit_rate:9.2%} "
                  f"{result.payoff_ratio:8.2f} {result.profit_factor:9.2f}")
            if not result.trade_log.empty:
                print(f"           exit reasons: {result.trade_log['exit_reason'].value_counts().to_dict()}")
        print()


if __name__ == "__main__":
    main()
