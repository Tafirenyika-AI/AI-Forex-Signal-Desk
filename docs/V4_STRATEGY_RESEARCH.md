# V4 Strategy Research — Registry, Evidence, and Results

Source: brief Section 6 ("Advanced Strategy Research Laboratory") and Section 7
("Strategy Evidence and Research Agent"). Part of V4 Priority 3
(`docs/V4_ARCHITECTURE.md`'s prioritized plan). Every strategy here is a
**research candidate, not an assumed-profitable method** — the brief's own
words, repeated because it's the whole point of this document.

## 1. The Strategy Registry

The full, formal, field-by-field spec for all 10 named strategy families
(A–J) lives in `src/strategies/registry.py`'s `STRATEGY_REGISTRY` — a
structured `StrategySpec` per strategy, not prose duplicated here (so there
is exactly one source of truth; this document summarizes it). Each spec
defines, verbatim per the brief's own required fields: Hypothesis, Eligible
instruments, Timeframe, Entry conditions, Exit conditions, Position sizing
assumptions, Stop-loss logic, Invalidation conditions, Expected holding
period, Data requirements, Transaction costs, Failure conditions, Validation
criteria.

| Code | Name | Hypothesis (one line) | Status |
|---|---|---|---|
| A | Time-Series Momentum | Own trailing return predicts own forward return's sign | **HYPOTHESIS_TESTED** (real result below) |
| B | Cross-Sectional Momentum | Relative-strength ranking predicts relative forward performance | RESEARCH_SPEC_ONLY |
| C | Trend Following | Trend persists; trailing-stop captures more of it than a fixed horizon | RESEARCH_SPEC_ONLY (but its exit mechanism, the ATR-ratcheting trailing stop, is already built+backtested from earlier work) |
| D | Opening-Range Breakout | Volume-confirmed opening-range breaks persist through the session | RESEARCH_SPEC_ONLY |
| E | VWAP Mean Reversion | Statistically stretched price reverts to session VWAP under calm regimes | RESEARCH_SPEC_ONLY |
| F | Volatility Breakout | Volatility compression is followed by a directionally-persistent expansion | RESEARCH_SPEC_ONLY |
| G | Earnings/Event-Driven | Earnings surprises drift in the surprise's direction (PEAD) | RESEARCH_SPEC_ONLY |
| H | Sector Rotation | Regime-conditioned sector relative strength predicts continued rotation | RESEARCH_SPEC_ONLY |
| I | Statistical Pairs Trading | A cointegrated pair's stretched spread reverts | RESEARCH_SPEC_ONLY |
| J | Crypto Momentum and Volatility | A/F's hypotheses, re-validated separately for 24/7 crypto | RESEARCH_SPEC_ONLY |

`status` values (see `StrategySpec`'s own docstring for the full ladder):
`RESEARCH_SPEC_ONLY` → `HYPOTHESIS_TESTED` → `BACKTESTED` → `SHADOW` →
`PAPER_APPROVED`. Nothing in this registry has reached `SHADOW` or
`PAPER_APPROVED` — those require, respectively, real running shadow
infrastructure and the brief's own explicit Phase 7 human approval.

**Real, disclosed data gap found while testing Strategy A** (not fixed in
this pass — flagged for Priority 4's opportunity scanner, which needs the
same universe): of the brief's own named equity candidates (Section 5 —
NVDA, AMD, AAPL, MSFT, AMZN, META, GOOGL, TSLA), only **NVDA, AAPL, and
MSFT** currently have any backfilled H4 candle history in this system
(confirmed live, 2026-10-08) — the other five have never been traded by
this account and were never added to `BENCHMARK_INSTRUMENTS` or any user's
instrument list, so `src/scripts/backfill_candles.py` has never fetched
them. The hypothesis test below is therefore reported for 3 of 8 named
instruments, honestly, not silently narrowed.

## 2. Strategy Evidence Registry (brief Section 7)

Real, verified citations (looked up live via WebSearch on 2026-10-08, not
recalled from memory and not fabricated) for the strategy families the
brief explicitly named sources for:

### Time-Series Momentum (Strategy A)
**Moskowitz, T. J., Ooi, Y. H., & Pedersen, L. H. (2012). "Time Series
Momentum." *Journal of Financial Economics*, 104(2), 228–250.**
Found strong evidence that an asset's own past returns predict its future
returns, replicated across 58 futures markets spanning equities, bonds,
currencies, and commodities. — Our own independent result: see Section 3
below. Methodology adapted here: nearest-timestamp day-count lookback/
holding windows (not the original paper's futures-contract-roll
methodology, since this system trades spot equities/crypto, not futures).
Known limitation of our replication: no transaction-cost model applied yet
(this is the `HYPOTHESIS_TESTED`, not `BACKTESTED`, stage).

### Cross-Sectional Momentum (Strategy B)
**Jegadeesh, N., & Titman, S. (1993). "Returns to Buying Winners and
Selling Losers: Implications for Stock Market Efficiency." *The Journal of
Finance*, 48(1), 65–91.** The original, seminal cross-sectional momentum
paper — 3-to-12-month formation periods produce significant positive
returns buying past winners and selling past losers. — Our own backtest:
not yet run (`RESEARCH_SPEC_ONLY`). Known constraint: the short leg of this
strategy cannot currently be traded live — this project's own risk posture
disables equity short-selling without separate approval (brief Section 12).

### Statistical Pairs Trading (Strategy I)
**Gatev, E., Goetzmann, W. N., & Rouwenhorst, K. G. (2006). "Pairs Trading:
Performance of a Relative-Value Arbitrage Rule." *The Review of Financial
Studies*, 19(3), 797–827.** Daily data 1962–2002; a simple distance-based
pairs rule yielded average annualized excess returns up to 11% for
self-financing portfolios. — Our own backtest: not yet run. Known gap: this
project has no cointegration-testing code anywhere yet (checked
`src/features/` and `src/models/`) — genuinely new statistical
infrastructure needed before this strategy can even reach
`HYPOTHESIS_TESTED`.

### Earnings and Event-Driven Trading (Strategy G)
**Bernard, V. L., & Thomas, J. K. (1989). "Post-Earnings-Announcement
Drift: Delayed Price Response or Risk Premium?" *Journal of Accounting
Research*, 27, 1–36.** The paper that named post-earnings-announcement
drift (PEAD) — a documented tendency for price to continue drifting in an
earnings surprise's direction for weeks after the announcement, a
longstanding challenge to market efficiency. — Our own backtest: not yet
run. Known gap: no "surprise magnitude" feature (actual vs. consensus/
trend) is computed anywhere in this codebase yet, despite the raw SEC
EDGAR/company_events/news inputs it would be derived from already being
real and live (Equity V2 Phases 3/4/6).

### Trend Following, Opening-Range Breakout, VWAP Mean Reversion, Volatility
Breakout, Sector Rotation, Crypto Momentum (Strategies C, D, E, F, H, J)
The brief names these research AREAS generically ("market microstructure
studies," "volatility and regime-dependent trading research") rather than
specific papers, unlike A/B/G/I above. No single canonical citation is
claimed for these in this pass — each one's `StrategySpec.hypothesis` field
states its own testable claim plainly instead; a literature search specific
to each is real future work for this registry, not done here to avoid
citing something not actually verified.

**Do not accept YouTube demonstrations, social media comments, marketing
screenshots, or hypothetical performance claims as independently verified
profitability** (brief's own instruction, repeated here as this registry's
own standing rule for any future addition).

## 3. Strategy A — Real Hypothesis Test Results (live, 2026-10-08)

Ran `src/strategies/time_series_momentum.py`'s `evaluate_momentum_hypothesis()`
against this project's own real, backfilled H4 candle history (not
synthetic data — synthetic data was used only in `tests/
test_time_series_momentum.py` to prove the function's math is correct
before trusting it against anything real) for the 3 named instruments that
currently have history, across 3 lookback/holding pairs matching the
spec's own "multiple lookback periods, holding horizons" instruction:

| Instrument | Lookback | Holding | n | Hit rate | z-score | Mean move-in-favor |
|---|---|---|---|---|---|---|
| NVDA | 7d | 1d | 1,245 | 49.5% | -0.37 | -0.00085 |
| NVDA | 28d | 7d | 1,355 | 50.0% | 0.03 | -0.00143 |
| NVDA | 84d | 30d | 1,358 | **53.5%** | **2.55** | 0.00196 |
| AAPL | 7d | 1d | 1,163 | 52.0% | 1.38 | 0.00013 |
| AAPL | 28d | 7d | 1,273 | 49.3% | -0.53 | -0.00200 |
| AAPL | 84d | 30d | 1,271 | **54.7%** | **3.34** | 0.00912 |
| MSFT | 7d | 1d | 1,169 | 48.4% | -1.08 | -0.00030 |
| MSFT | 28d | 7d | 1,280 | 50.9% | 0.61 | 0.00093 |
| MSFT | 84d | 30d | 1,280 | **58.8%** | **6.32** | 0.00023 |

**Real, disclosed finding**: short (7d/1d) and medium (28d/7d) lookback/
holding combinations show NO significant momentum signal for any of the 3
instruments (all `|z| < 1.5`) — consistent with short-horizon efficiency.
The long (84d lookback / 30d holding) combination shows a statistically
significant positive momentum signal for **all three** instruments
(z = 2.55, 3.34, 6.32 — MSFT's result in particular is very strong), which
matches the cited Moskowitz/Ooi/Pedersen finding that time-series momentum
is a medium-to-long-horizon effect, not a short-horizon one.

**This is a HYPOTHESIS_TESTED result, not a BACKTESTED or tradeable one.**
No transaction costs, no position sizing, no stop-loss, no slippage are
modeled above — `mean_move_in_favor` is a raw log-return, not a dollar P&L,
and MSFT's small mean-move-in-favor (0.00023) alongside its strong z-score
illustrates exactly why a significant hit rate alone isn't sufficient
evidence of a tradeable edge (the brief's own Section 9 instruction: don't
let an arbitrary confidence score stand in for proof of profitability). The
next real step for this strategy (not done in this pass) is a full
`BACKTESTED` pass through `src/backtest/engine.py` with realistic costs —
only after that would `SHADOW` status even be considered.

## 4. Known limitations, disclosed not hidden

- 7 of 10 strategy families are `RESEARCH_SPEC_ONLY` — specs exist, nothing
  has been run against real data yet. This is deliberate, incremental
  scoping (confirmed with the user before starting Priority 3's largest
  item), not an oversight.
- Only 3 of the brief's 8 named equity candidates have any backfilled
  candle history at all (see Section 1 above) — a real gap for Priority 4.
- Strategy A's own result above covers equities only — Strategy J
  explicitly calls for a SEPARATE crypto validation, not done in this pass.
- No strategy in this registry has been wired into `src/decision/fusion.py`
  or any live decision path — that integration is explicitly Priority 5's
  job (the adaptive meta-model/strategy selector), not this one.
