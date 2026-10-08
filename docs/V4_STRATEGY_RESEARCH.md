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
| B | Cross-Sectional Momentum | Relative-strength ranking predicts relative forward performance | **HYPOTHESIS_TESTED** (real result below — in-sample significant, FAILS a genuine holdout) |
| C | Trend Following | Trend persists; trailing-stop captures more of it than a fixed horizon | **HYPOTHESIS_TESTED** (real result below — important caveat: tested with a fixed holding period, not the strategy's own actual trailing-stop exit) |
| D | Opening-Range Breakout | Volume-confirmed opening-range breaks persist through the session | **HYPOTHESIS_TESTED** (real result below — consistently negative across every config tested, holdout inconclusive on tiny samples) |
| E | VWAP Mean Reversion | Statistically stretched price reverts to session VWAP under calm regimes | **HYPOTHESIS_TESTED** (real result below — suggestive full-sample, inconclusive once split) |
| F | Volatility Breakout | Volatility compression is followed by a directionally-persistent expansion | **HYPOTHESIS_TESTED** (real result below) |
| G | Earnings/Event-Driven | Earnings surprises drift in the surprise's direction (PEAD) | **HYPOTHESIS_TESTED** (real result below — sample too small to conclude either way) |
| H | Sector Rotation | Regime-conditioned sector relative strength predicts continued rotation | **HYPOTHESIS_TESTED** (real result below — the data says the OPPOSITE of the hypothesis) |
| I | Statistical Pairs Trading | A cointegrated pair's stretched spread reverts | **HYPOTHESIS_TESTED** (real result below — NO candidate pair is genuinely cointegrated; a dangerous false-positive trap found along the way) |
| J | Crypto Momentum and Volatility | A/F's hypotheses, re-validated separately for 24/7 crypto | **HYPOTHESIS_TESTED** (real result below — the equity config actively REVERSES sign on crypto; no stable crypto-specific config found either) |

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
returns buying past winners and selling past losers. — Our own
hypothesis test: see Section 8 below — an apparently significant
60-bar/20-bar spread (t=4.01 full-sample) did NOT replicate on a genuine
holdout (t=0.12). Known constraint: the short leg of this strategy cannot
currently be traded live regardless — this project's own risk posture
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
longstanding challenge to market efficiency. — Our own hypothesis test:
see Section 12 below — real, honest sample sizes (n=6-7 per instrument,
bounded by this project's own ~2 years of backfilled history) are too
small to confirm or deny the effect. Real correction: `company_events`
(Equity V2 Phase 3) is confirmed completely empty (0 rows) — a known,
already-disclosed gap, not a new one — and `equity_news`'s "EARNINGS" tag
turned out to match only multi-ticker roundup articles, not usable
per-ticker event triggers. Used `company_fundamentals.filed_at` directly
instead (real, point-in-time-correct SEC filing timestamps).

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

**⚠ Correction/update (2026-10-08, Priority 6's own holdout-testing item)**:
this table was built from the FULL sample tested at once, with nothing
held out. A genuine out-of-sample holdout re-test (Section 3's own
follow-up below) found this "significant for all three" result does NOT
robustly replicate — only AAPL's finding survives a real holdout; NVDA and
MSFT's do not. Read the full-sample numbers above as in-sample evidence
only, and see the holdout table further down before treating any of these
three as validated.

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

**Real follow-up finding (2026-10-08, V4 Priority 6 verification pass) — exactly the gap the paragraph above warned about, now confirmed**: ran a real portfolio-level backtest (`src/backtest/portfolio_engine.py`) using 129 real signals generated from this exact sign-prediction direction (every ~30 bars across NVDA/AAPL/MSFT's real H4 history, standard ATR-based stop/target sizing — `ATR_STOP_MULTIPLIER`/`REWARD_RISK_MULTIPLE` from `src/decision/fusion.py`, the project's own established convention). **Result: net return -4.08%, win rate 42.9%, profit factor 0.88, Sharpe -0.39, Sortino -0.35 — a naive implementation of Strategy A's own validated sign-prediction is UNPROFITABLE.** This does not contradict the earlier hit-rate finding (both are real, computed correctly) — it demonstrates precisely why `HYPOTHESIS_TESTED` and `BACKTESTED` are different rungs on this registry's own status ladder: correctly predicting a return's SIGN more often than chance does not by itself produce a profitable trading rule once a real entry cadence, stop distance, and target are attached. **Consequence, acted on, not just disclosed**: `src/models/strategy_selector.py`'s `ELIGIBLE_STRATEGIES` was changed from `("A",)` to `()` the same day — the selector was already shadow-only/no-broker-call by construction (zero live-safety impact), but its hit-rate-only eligibility bar was retired in favor of requiring BOTH a significant hypothesis test AND a profitable backtest.

**Second, even more direct follow-up finding (2026-10-08, V4 Priority 6's own "untouched final holdout" item) — the original full-sample claim does NOT robustly replicate out-of-sample**: `src/strategies/time_series_momentum.py`'s new `evaluate_momentum_hypothesis_with_holdout()` re-ran the exact validated 84d/30d configuration, splitting each instrument's real history chronologically into the earliest 80% ("development") and a genuinely untouched final 20% ("holdout") — the original Section 3 table above tested the FULL sample at once, with nothing held out.

| Instrument | Development n / hit_rate / z | Holdout n / hit_rate / z |
|---|---|---|
| NVDA | 1,095 / 54.8% / **3.17** | 274 / 48.5% / **-0.48** |
| AAPL | 1,024 / 56.0% / **3.81** | 255 / 57.3% / **2.32** |
| MSFT | 1,031 / 62.8% / **8.19** | 257 / 46.3% / **-1.19** |

**Only AAPL's finding replicates on genuinely held-out data.** NVDA and MSFT's original full-sample significance (z=2.55 and z=6.32 respectively) does not survive a real out-of-sample test — both flip to non-significant-or-negative on the untouched final 20%, consistent with having been, at least partly, an artifact of testing the entire sample at once rather than a replicated, robust effect. This is independent evidence pointing the same direction as the unprofitable-backtest finding above, and reinforces rather than merely coincides with the decision to pull Strategy A's selector eligibility — **the original "significant across all 3 instruments" claim should now be read as "significant in-sample for 3, replicated out-of-sample for only 1."**

## 4. Strategy F — Real Hypothesis Test Results (live, 2026-10-08)

Ran `src/strategies/volatility_breakout.py`'s `evaluate_volatility_breakout_hypothesis()`
against the same 3 instruments' real H4 history (`compression_window_bars=20`,
`expansion_vol_threshold=0.5`, `holding_bars=5`, `regime_lookback=250`) —
reusing `src/models/regime.py`'s `regime_low_volatility`/`vol_percentile`
wholesale, exactly as the strategy's own spec intends (no new feature work
needed). The event detector only counts the FIRST bar of each real
expansion episode (a rising-edge filter), not every bar while vol_percentile
stays elevated — see the "Real bugs found" note below for why that
distinction mattered.

| Instrument | n | Hit rate | z-score | Mean move-in-favor |
|---|---|---|---|---|
| NVDA | 15 | 46.7% | -0.26 | -0.00011 |
| AAPL | 8 | **87.5%** | **2.12** | 0.02381 |
| MSFT | 16 | 56.3% | 0.50 | 0.00590 |

**Real, disclosed finding**: NVDA and MSFT show no significant signal
(`|z| < 1`). AAPL's result (z=2.12) crosses the conventional significance
threshold, but **n=8 is a genuinely small sample** — a single flipped
outcome would materially change the hit rate, and 8 real compression/
expansion episodes over this instrument's available history is not enough
to treat this as a validated edge. Reported honestly as a `HYPOTHESIS_TESTED`
signal worth tracking as more history accumulates, not as a finding ready
for `BACKTESTED` status.

**Real bugs found and fixed while building this (not live-impacting — this
strategy was never run against real data until both were fixed)**:
1. The event detector's first version fired on every bar where vol_percentile
   stayed elevated after a compression ended, not just the first one — a
   deliberately-reversing synthetic fixture (compression → spike up →
   sustained decline) still produced a 100% "hit rate," because most
   "events" were really later continuation bars correlating with
   themselves rather than the genuine initial breakout. Fixed with a
   rising-edge filter (`qualifies & ~qualifies.shift(1)`).
2. That fix's own `~qualifies.shift(1)` silently did nothing at first:
   `shift()` on a bool-dtype pandas Series introduces a leading NaN,
   upcasting the whole Series to **object** dtype holding Python
   `True`/`False`/`NaN` — and `~` on an object-dtype Series of Python bools
   performs **integer bitwise-not** (`~True == -2`, `~False == -1`), not
   logical negation. Fixed by forcing `.astype(bool)` after `.fillna(False)`.
3. The same NaN-to-bool family struck a third time in `was_compressed_recently`:
   the very first row (no prior history at all) produced a NaN from
   `rolling().max()`, and `NaN.astype(bool)` evaluates `True` — incorrectly
   treating "no history yet" as "yes, recently compressed." Fixed by adding
   `.fillna(False)` before that `.astype(bool)` too.

All three were caught by directly fabricated, deterministic test fixtures
(`tests/test_volatility_breakout.py`) with a known-by-construction correct
answer — none were caught by eyeballing the code, and none were caught by
an initial end-to-end OHLC-series fixture attempt (abandoned after it
produced bizarre, hard-to-interpret results from interacting with
`classify_regime()`'s own long trailing-percentile window — see the
module's own docstring for why the test was restructured to target the
pure event-detection core directly instead).

## 5. Strategy C — Real Hypothesis Test Results (live, 2026-10-08)

Ran `src/strategies/trend_following.py`'s `evaluate_trend_following_hypothesis()`
against the same 3 instruments' real H4 history, entering only on a FRESH
transition into `TREND` (the spec's own "not merely 'currently in TREND'"
distinction), direction from `regime_direction`, across 3 fixed holding
periods:

| Instrument | Holding (bars) | n | Hit rate | z-score | Mean move-in-favor |
|---|---|---|---|---|---|
| NVDA | 10 | 23 | 52.2% | 0.21 | 0.00445 |
| NVDA | 20 | 23 | 30.4% | **-1.88** | -0.00905 |
| NVDA | 40 | 23 | 43.5% | -0.63 | -0.02102 |
| AAPL | 10 | 23 | 56.5% | 0.63 | -0.00347 |
| AAPL | 20 | 23 | 56.5% | 0.63 | 0.01928 |
| AAPL | 40 | 23 | 52.2% | 0.21 | 0.01509 |
| MSFT | 10 | 24 | 33.3% | -1.63 | -0.00882 |
| MSFT | 20 | 24 | 54.2% | 0.41 | -0.00658 |
| MSFT | 40 | 24 | 45.8% | -0.41 | -0.00757 |

**Real, disclosed finding — and an important interpretation caveat**: no
combination shows a significant POSITIVE signal, and two (NVDA at 20 bars,
MSFT at 10 bars) are mildly negative. This is **not** strong evidence that
trend-following fails on this data — it's evidence that a **fixed-horizon**
proxy is the wrong instrument to test it with. The strategy's own spec
(Section 1 above) is explicit that its real exit is a trailing stop, not a
calendar-time exit, specifically to avoid giving back gains during the
trend's later, choppier stages — and Equity V2's own earlier Phase D1 work
already found exactly that signature on a ratcheting ATR trailing stop
(drawdown and payoff ratio improved in every pair tested, hit rate
dropped, net return was mixed) applied in backtest. A real `BACKTESTED`-
stage test of Strategy C needs the actual trailing-stop exit wired in
(`src/execution/trailing_stop.py`), not this fixed-horizon substitute —
explicitly flagged as follow-up work, not done in this pass.

## 6. Strategy H — Real Hypothesis Test Results (live, 2026-10-08)

Ran `src/strategies/sector_rotation.py`'s `evaluate_sector_rotation_hypothesis()`
against real H4 history for SPY + the 11 SPDR sector ETFs
(`lookback_bars=20`, `holding_bars=20`, `top_tier_fraction=1/3`,
`regime_lookback=250`), gated on the broad market (SPY itself) not being
in a `SHOCK` regime:

**Result (CORRECTED 2026-10-08, see note below): n=3,366, mean forward
relative return = -0.145%, t = -2.63.**

**Real, disclosed finding — the data says the OPPOSITE of the hypothesis.**
Sector ETFs in the top tier of TRAILING relative strength vs. SPY tend to
UNDERPERFORM SPY over the following 20-bar window, not continue
outperforming — a mean-reversion signature, not the rotation-persistence
the strategy's own hypothesis predicted, and the effect is real and
significant (t = -2.63 on n=3,366). This is exactly the kind of result the
brief's own Section 6 instruction exists for ("these are research
candidates, not assumed profitable methods") — reported honestly as a
real negative/contrarian finding, not discarded or reframed as a win. A
genuinely interesting follow-up (not pursued in this pass): this result
structurally resembles Strategy E's own hypothesis (mean reversion)
rather than H's — worth a dedicated look at whether "fade the top-tier
sector, don't follow it" has real validation potential, which would be a
different strategy from the one actually specified here.

**⚠ Correction (2026-10-08, found via Priority 6's corporate-action
detector)**: this result was originally reported as t = -7.55, mean =
-0.76%. 5 of the 11 sector ETFs (XLB, XLE, XLK, XLU, XLY) had real,
unadjusted price data straddling a genuine 2-for-1 split (State Street,
2025-12-04/05) — a fake ~50% single-day "crash" in 5 of 11 cross-section
members, inflating the apparent effect size. Fixed via
`src/scripts/fix_spdr_2025_split.py` (a real, confirmed back-adjustment,
not a guess — see Priority 6's own implementation-log entry for the
citation). **The corrected effect is smaller but still real and
significant** — this is a correction to magnitude, not a reversal of the
conclusion.

**Real data gap found (at the time) — since resolved**: only 9 of 11
sector ETFs had any backfilled H4 history when this was originally run —
XLE and XLF had zero rows despite being in `BENCHMARK_INSTRUMENTS`.
Checked live against Alpaca directly at the time: real H1 data existed
for both, so this was a genuine backfill gap, not a "no data exists"
situation. **Resolved during Priority 4** (running the real backfill
script live materialized both) — the corrected result above now covers
all 11 of 11 sector ETFs (`n_etfs_covered=11`).

## 7. Strategy J — Real Hypothesis Test Results, WITH holdout (live, 2026-10-08)

Strategy J's own spec is explicit: equity-validated results must not be
assumed to transfer to crypto — this needed its own independent test, not
an inference. Reused Strategy A's `evaluate_momentum_hypothesis`/
`evaluate_momentum_hypothesis_with_holdout` and Strategy F's
`evaluate_volatility_breakout_hypothesis` directly against BTC/USD and
ETH/USD's real H4 history (4,575 real rows each) — no new code needed,
per the registry's own spec.

**Momentum, full sample, 3 lookback/holding configs** (same as Strategy A's
original equity test):

| Instrument | Lookback | Holding | n | Hit rate | z-score |
|---|---|---|---|---|---|
| BTC/USD | 7d | 1d | 4,572 | 49.6% | -0.56 |
| BTC/USD | 28d | 7d | 4,572 | 51.9% | **2.63** |
| BTC/USD | 84d | 30d | 4,572 | 47.2% | **-3.76** |
| ETH/USD | 7d | 1d | 4,572 | 47.6% | **-3.28** |
| ETH/USD | 28d | 7d | 4,572 | 52.4% | **3.22** |
| ETH/USD | 84d | 30d | 4,572 | 45.5% | **-6.03** |

**Real, striking finding**: the EXACT 84d/30d configuration that was
significantly POSITIVE for all 3 equities is significantly NEGATIVE for
both crypto pairs (BTC z=-3.76, ETH z=-6.03) — strong, direct evidence
that Strategy J's own premise is correct: crypto's 24/7 market genuinely
behaves differently at this horizon, and assuming the equity result would
transfer would have been actively wrong, not just unproven.

**Applying this session's own hard-won lesson immediately**: rather than
treating the 28d/7d config's apparent significance as a new finding,
holdout-tested it the same way Strategy A's own equity result was
corrected earlier today:

| Instrument | Development z | Holdout z |
|---|---|---|
| BTC/USD | **3.77** | **-1.65** |
| ETH/USD | 0.43 | **6.35** |

**Neither instrument replicates consistently** — BTC's development
significance flips to negative on holdout; ETH's development showed
NOTHING significant but its holdout alone is strongly significant. Two
instruments disagreeing this sharply between development and holdout is
itself the finding: this specific 28d/7d configuration is not a stable,
exploitable signal for crypto, just noise that happened to look
significant in one slice or the other. Correctly reported as a null
result, not a discovery.

**Volatility breakout**, same config as the equity test
(`compression_window_bars=20`, `holding_bars=5`, `regime_lookback=250`):
BTC/USD n=43 hit_rate=46.5% z=-0.46; ETH/USD n=59 hit_rate=50.8% z=0.13 —
no signal for either, consistent with (not contradicting) the equity
result.

**Conclusion for Strategy J**: real, separate crypto validation was
necessary and justified — the equity-validated momentum config actively
REVERSES sign on crypto, and no tested crypto-specific config survives a
genuine holdout either. No positive finding to report for crypto momentum
or volatility breakout at the configs tested.

## 8. Strategy B — Real Hypothesis Test Results, WITH holdout (live, 2026-10-08)

Cross-section: the 13 real, currently-backfilled US-equity-calendar
instruments (3 equities + 10 ETFs — same universe the opportunity scanner
uses, minus XLE/XLF's own gap at the time; `evaluate_cross_sectional_
momentum_hypothesis` found 12 covered once XLE/XLF's backfill completed).
Classic top-tier-minus-bottom-tier long-short spread
(`src/strategies/cross_sectional_momentum.py`), NOT relative-to-SPY
(that's Strategy H's distinct construction).

| Lookback | Holding | n | Mean spread | t-statistic |
|---|---|---|---|---|
| 5 bars | 5 bars | 1,152 | -0.00009 | -0.15 |
| 20 bars | 20 bars | 1,122 | -0.00111 | -1.05 |
| 60 bars | 20 bars | 1,082 | 0.00294 | **2.69** |

**Applying this session's holdout discipline immediately, before reporting
the 60/20 result as a finding**:

| | n | Mean spread | t-statistic |
|---|---|---|---|
| Development (earliest 80%) | 869 | 0.00357 | **3.11** |
| Holdout (final 20%, untouched) | 213 | 0.00035 | 0.12 |

**Does not replicate.** The apparently strong in-sample result (t=3.11 in
development, t=2.69 full-sample) collapses to essentially zero (t=0.12) on
genuinely held-out data — the third time this exact pattern has now shown
up this session (Strategy A's 84d/30d result, Strategy J's 28d/7d crypto
result, now this). **This recurring pattern is itself the most important
finding across Priority 3's hypothesis-testing work so far**: with only
~2 years of real backfilled history, several of these simple rank/sign-
based tests find apparent significance that does not survive genuine
out-of-sample validation — a strong argument for treating every full-
sample result in this document as provisional until holdout-checked, not
just the ones that happened to get checked first.

**⚠ Correction (2026-10-08, found via Priority 6's corporate-action
detector)**: the numbers above were originally reported as full-sample
t=4.01, development t=4.59 (holdout t=0.12 was already correct, since the
holdout window falls entirely after the split). 5 of the 13 cross-section
instruments (XLK, XLE, plus 3 more SPDR ETFs) had unadjusted data
straddling a real 2-for-1 split (see Strategy H's own correction note,
Section 6) — fixed via `src/scripts/fix_spdr_2025_split.py`. The
conclusion is UNCHANGED (still fails the holdout), but the development/
full-sample magnitudes were overstated and are corrected above.

## 9. Strategy E — Real Hypothesis Test Results, WITH holdout (live, 2026-10-08)

Reuses `src/features/equity_cross_market.py`'s `approx_vwap()` directly
(Equity V2 Phase 8's own disclosed Level-I rolling-VWAP approximation) —
no new feature was needed, correcting this registry's own earlier,
incomplete data-requirements note (Section 1). Entry gated on `regime ==
RANGE` only (never TREND/SHOCK/HIGH_VOLATILITY), real H1 history:

| Instrument | Threshold (std) | n | Hit rate | z-score |
|---|---|---|---|---|
| NVDA | 1.5 | 111 | 57.7% | 1.61 |
| NVDA | 2.0 | 24 | 66.7% | 1.63 |
| NVDA | 2.5 | 3 | 100% | 1.73 |
| AAPL | 1.5 | 72 | 41.7% | -1.41 |
| AAPL | 2.0 | 21 | 47.6% | -0.22 |
| AAPL | 2.5 | 7 | 28.6% | -1.13 |
| MSFT | 1.5 | 73 | **65.8%** | **2.69** |
| MSFT | 2.0 | 17 | 70.6% | 1.70 |
| MSFT | 2.5 | 4 | 50.0% | 0.00 |

**Holdout-tested MSFT's standout result (1.5 std threshold) immediately,
before reporting it**:

| | n | Hit rate | z-score |
|---|---|---|---|
| Development (earliest 80%) | 68 | 58.8% | 1.46 |
| Holdout (final 20%, untouched) | 5 | 100% | 2.24 |

**Inconclusive, not confirmed** — neither half independently clears
significance on its own terms (development alone is actually weaker than
the combined full-sample figure suggested; the holdout's apparent z=2.24
rests on only 5 real events, far too few to trust). AAPL shows no signal
at any threshold; NVDA is directionally suggestive but never reaches
significance at any threshold with a usable sample size. **No instrument
produces a robust, holdout-confirmed finding for Strategy E** — a fourth
consistent instance of this session's recurring theme (full-sample
figures that don't hold up once genuinely checked), though this one reads
more as "insufficient data to tell" than the clean contradictions seen in
A/B/J.

## 10. Strategy I — Real Cointegration Test Results (live, 2026-10-08)

Built `src/strategies/pairs_trading.py` with a real Augmented
Engle-Granger cointegration test (`statsmodels.tsa.stattools.coint`, a
newly added real dependency — see `requirements.txt` — not a hand-rolled
approximation of ADF critical values). Tested 6 real candidate pairs
against real H4 history, per the registry's own suggested candidates
(near-identical ETF pairs, sector-proxy pairs, same-broad-sector
equities):

| Pair | Cointegration p-value | Mechanical reversion n / hit_rate / z |
|---|---|---|
| GLD / IAU | 0.669 | 53 / 98.1% / **7.01** |
| XLE / USO | — (USO H4 gap, see Priority 4) | n=0 |
| SPY / QQQ | 0.178 | 210 / 51.4% / 0.41 |
| AAPL / MSFT | 0.897 | 171 / 44.4% / -1.45 |
| XLK / QQQ | 0.313 | 156 / 59.0% / **2.24** |
| AAPL / NVDA | 0.416 | 130 / 72.3% / **5.09** |
| MSFT / NVDA | 0.630 | 179 / 52.0% / 0.52 |

**None of the 6 testable pairs shows genuine cointegration** (every
p-value is well above the conventional 0.05 threshold — even GLD/IAU,
near-identical gold exposure, fails at p=0.669).

**A real, important, dangerous false-positive trap found along the way**:
GLD/IAU's MECHANICAL reversion test alone looks spectacular (z=7.01,
98.1% hit rate), AAPL/NVDA's looks very strong too (z=5.09), and even
XLK/QQQ now crosses the conventional threshold (z=2.24) — but ALL THREE
pairs fail the formal cointegration prerequisite. This is exactly why the
registry's own validation criteria require the cointegration test to pass
FIRST, not a reversion-test result alone: a rolling z-score of ANY two
price series' difference can look like a strong "reversion" signal purely
from the normalization itself, with no genuine statistical link
underneath it. Trading any of these three pairs on the mechanical signal
alone, without the cointegration gate, would have been a textbook
spurious-regression mistake — precisely the failure mode Gatev, Goetzmann
& Rouwenhorst's own methodology (and this registry's own validation
criteria) exists to prevent.

**⚠ Correction (2026-10-08, found via Priority 6's corporate-action
detector)**: XLK/QQQ's numbers were originally reported as p=0.549,
n=152, hit_rate=55.9%, z=1.46 — XLK had unadjusted data straddling a real
2-for-1 split (see Strategy H's correction note, Section 6), fixed via
`src/scripts/fix_spdr_2025_split.py`. The conclusion is UNCHANGED (still
fails cointegration), but the corrected numbers are a stronger, not
weaker, illustration of the same false-positive trap this section warns
about.

**Conclusion for Strategy I**: no validated pair exists in this project's
current real, backfilled universe. This is a genuine, useful null result,
not a failure of the research process — and the false-positive trap found
along the way is itself valuable, disclosed evidence for why this
strategy's own two-part validation criteria (cointegration AND profitable
reversion) must never be relaxed to "reversion signal alone."

## 12. Strategy G — Real Hypothesis Test Results (live, 2026-10-08)

Ran `src/strategies/earnings_drift.py`'s `evaluate_earnings_drift_hypothesis()`
against real company_fundamentals + H4 candle history for NVDA/AAPL/MSFT.
"Surprise" = YoY change in real, filed, single-quarter `EarningsPerShareDiluted`
(no analyst-consensus feed exists — a disclosed proxy, not a claim of
matching real consensus). Event anchor = `filed_at` (a few days after the
real earnings release, but a genuinely knowable, point-in-time-correct
timestamp — not the news-publish-time proxy this project's existing
earnings-lockout gate uses, which turned out not to have usable per-
ticker data for this purpose, see Section 2 above).

| Instrument | Holding (days) | n | Hit rate | z-score |
|---|---|---|---|---|
| NVDA | 5 | 6 | 33.3% | -0.82 |
| NVDA | 10 | 7 | 28.6% | -1.13 |
| NVDA | 20 | 7 | 28.6% | -1.13 |
| AAPL | 5 | 6 | 33.3% | -0.82 |
| AAPL | 10 | 6 | 50.0% | 0.00 |
| AAPL | 20 | 6 | 66.7% | 0.82 |
| MSFT | 5 | 6 | 16.7% | -1.63 |
| MSFT | 10 | 6 | 16.7% | -1.63 |
| MSFT | 20 | 6 | 16.7% | -1.63 |

**Real, disclosed finding**: no result reaches conventional significance
at any instrument/horizon (`|z| < 1.7` everywhere), and sample sizes
(n=6-7) are genuinely too small to trust either way — this project's own
~2 years of real backfilled candle history only covers 6-7 real quarterly
earnings events with a valid prior-year comparison per instrument.
MSFT's direction is at least consistently negative across all three
holding periods (worth noting, not over-interpreting given n=6). This is
an honest "insufficient data," not a confirmed null result the way
Strategy H's was — more real history accumulating over time would make
this test meaningfully more powerful without any code change.

**Real bug found and fixed while building this, before it ever ran
against real data incorrectly**: the first version of `_load_eps_events`
grouped only by `period_end`, which a Q4/fiscal-year-end shares with BOTH
the true single-quarter EPS fact AND SEC EDGAR's own cumulative annual
fact (same real XBRL ambiguity `src/data/db.py`'s own `company_fundamentals`
docstring already warns about). This produced nonsensical "quarterly" EPS
values when checked against real data (MSFT showing "17.95," its actual
annual figure, not one quarter's ~$3-5). Fixed by filtering to genuine
single-quarter facts (`period_end - period_start` between 80-100 days)
before grouping — caught by inspecting the real loaded values before
trusting the statistical test built on top of them, not by a failing
test (the synthetic test fixtures didn't happen to include this specific
annual/quarterly collision until a dedicated regression test was added
for it afterward).

## 13. Strategy D — Real Hypothesis Test Results, WITH holdout attempt (live, 2026-10-08)

Ran `src/strategies/opening_range_breakout.py`'s `evaluate_opening_range_breakout_hypothesis()`
against real M15 history (confirmed live: ~60-63 real trading days per
instrument, 2026-07-08 to 2026-10-08 — M15's 60-day backfill depth was
genuinely enough for this first-pass test, correcting this registry's own
earlier assumption that it wasn't). Opening range = first 2 or 4 M15 bars
(30/60 minutes) after the real 13:30 UTC session open; breakout requires
a volume-confirmed close outside that range.

| Instrument | Opening range | Volume mult. | n | Hit rate | z-score |
|---|---|---|---|---|---|
| NVDA | 2 bars | 1.2x | 16 | 43.8% | -0.50 |
| NVDA | 4 bars | 1.5x | 13 | 30.8% | -1.39 |
| AAPL | 2 bars | 1.2x | 20 | 30.0% | -1.79 |
| AAPL | 4 bars | 1.5x | 16 | 25.0% | **-2.00** |
| MSFT | 2 bars | 1.2x | 16 | 31.3% | -1.50 |
| MSFT | 4 bars | 1.5x | 6 | 0.0% | **-2.45** |

**Real, striking, consistent finding**: every single one of the 6
instrument/config combinations tested is NEGATIVE — volume-confirmed
opening-range breakouts in this real sample tend to falsely reverse more
often than persist, the OPPOSITE of the strategy's own hypothesis. This
is directionally consistent across every config, not just one lucky
combination, which is itself notable.

**Holdout attempt, honest about its own limits**: split each instrument's
real trading days chronologically (80/20). The holdout portions are too
small to mean anything on their own (AAPL 4-bar: holdout n=0; AAPL 2-bar:
holdout n=1; MSFT 4-bar: holdout n=1; NVDA 4-bar: holdout n=3) — this
project's own ~60 real trading days of M15 depth, combined with how rare
a volume-confirmed breakout actually is per day, leaves too few events
per instrument to properly holdout-test with this short a history. This
is an honest **inconclusive-by-sample-size** result for the holdout
check specifically, NOT a confirmation that the negative full-sample
finding is robust — the same caution this document has applied to every
other strategy's full-sample number applies here too.

**Conclusion for Strategy D**: a real, consistently negative signal in
the full sample (false-breakout risk may genuinely dominate for this
instrument set and window), but not independently holdout-confirmed due
to sample-size limits — more real M15 history accumulating over time
would make the holdout check meaningfully more powerful without any code
change, same situation as Strategy G.

## 14. Known limitations, disclosed not hidden

- **All 10 of 10 strategy families are now `HYPOTHESIS_TESTED`** — every
  one has a real result against real data, not just a spec. None has
  reached `BACKTESTED` status (full cost/sizing/stop simulation) or
  beyond.
- Holdout testing has only been applied to A/B/D/J so far — and in every
  one of those 4 cases, the apparent full-sample finding did NOT survive
  intact (only Strategy A's AAPL result held up; B and J did not
  replicate at all; D's holdout samples were too tiny to confirm OR deny
  its consistently-negative full-sample signal). C showed no signal even
  full-sample; E's one standout result was inconclusive once split; F's
  one significant result (AAPL, n=8) and H's contradicting result have
  NOT yet been holdout-checked and could have the same fragility; G's
  sample sizes (n=6-7) were too small to reach significance either way;
  I found no cointegrated pair at all, so there is nothing left to
  holdout-check for it in this universe. **The honest overall state of
  this registry today: no strategy has cleared a real, holdout-robust bar
  for trading** — a complete, real, first-pass validation of all 10 named
  strategy families, with the central finding being how FEW of them hold
  up under genuine scrutiny, not how many "work."
- Only 3 of the brief's 8 named equity candidates have any backfilled
  candle history at all (see Section 1 above) — a real gap for Priority 4.
- 2 of the 11 sector ETFs (XLE, XLF) have zero backfilled H4 history despite
  real Alpaca data existing for both — a real, disclosed backfill gap found
  via Strategy H's own test (Section 6 above), not fixed in this pass.
- Strategy F's AAPL result (z=2.12, n=8) is too small a sample to trust —
  disclosed explicitly in Section 4 above, not quietly treated as a win.
- Strategy C's fixed-horizon test is an acknowledged proxy, not a real test
  of the strategy's own trailing-stop-exit design (Section 5 above) — its
  flat/negative results should not be read as "trend-following doesn't
  work here."
- Strategy H's own result directly CONTRADICTS its hypothesis (Section 6) —
  reported honestly, not discarded; the contrarian finding is itself the
  useful output of doing real research rather than assuming an edge exists.
- No strategy in this registry has been wired into `src/decision/fusion.py`
  or any live decision path — that integration is explicitly Priority 5's
  job (the adaptive meta-model/strategy selector), not this one.
- Strategy A's original "significant for all 3 instruments" claim does NOT
  hold on a genuine out-of-sample holdout (Section 3 above) — only AAPL
  replicates; NVDA and MSFT do not. Combined with the unprofitable real
  backtest (also Section 3), Strategy A's selector eligibility was pulled
  (`src/models/strategy_selector.py`, `ELIGIBLE_STRATEGIES = ()`).
- Holdout testing (Priority 6) has only been applied to Strategy A so far
  — Strategies C/F/H's results above have NOT been re-checked against a
  genuine holdout and could have the same in-sample-only fragility.
