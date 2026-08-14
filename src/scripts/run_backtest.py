"""Phase 2 walk-forward backtest (blueprint sec. 14).

Run from the project root with the venv active:
    python -m src.scripts.run_backtest

Only ~500 candles per pair/granularity are stored right now (one
download_candles.py run's worth), so these results are a plumbing check,
not a statistically meaningful evaluation — sec. 14 requires multiple
market regimes before a backtest result means anything. Run
download_candles.py repeatedly over time (or fetch a larger historical
window) before trusting these numbers for a go/no-go decision.
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

        if not result.calibration.empty:
            print(f"  calibration ({pair}):")
            print(result.calibration.to_string(index=False).replace("\n", "\n  "))
        print()


if __name__ == "__main__":
    main()
