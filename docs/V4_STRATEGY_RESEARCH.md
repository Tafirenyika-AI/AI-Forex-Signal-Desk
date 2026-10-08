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
| C | Trend Following | Trend persists; trailing-stop captures more of it than a fixed horizon | **HYPOTHESIS_TESTED** (real result below — important caveat: tested with a fixed holding period, not the strategy's own actual trailing-stop exit) |
| D | Opening-Range Breakout | Volume-confirmed opening-range breaks persist through the session | RESEARCH_SPEC_ONLY |
| E | VWAP Mean Reversion | Statistically stretched price reverts to session VWAP under calm regimes | RESEARCH_SPEC_ONLY |
| F | Volatility Breakout | Volatility compression is followed by a directionally-persistent expansion | **HYPOTHESIS_TESTED** (real result below) |
| G | Earnings/Event-Driven | Earnings surprises drift in the surprise's direction (PEAD) | RESEARCH_SPEC_ONLY |
| H | Sector Rotation | Regime-conditioned sector relative strength predicts continued rotation | **HYPOTHESIS_TESTED** (real result below — the data says the OPPOSITE of the hypothesis) |
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

**Result: n=3,366, mean forward relative return = -0.76%, t = -7.55.**

**Real, disclosed finding — the data says the OPPOSITE of the hypothesis,
strongly.** Sector ETFs in the top tier of TRAILING relative strength vs.
SPY tend to UNDERPERFORM SPY over the following 20-bar window, not
continue outperforming — a mean-reversion signature, not the rotation-
persistence the strategy's own hypothesis predicted, and the effect is
large and overwhelmingly significant (t = -7.55 on n=3,366). This is
exactly the kind of result the brief's own Section 6 instruction exists
for ("these are research candidates, not assumed profitable methods") —
reported honestly as a real negative/contrarian finding, not discarded or
reframed as a win. A genuinely interesting follow-up (not pursued in this
pass): this result structurally resembles Strategy E's own hypothesis
(mean reversion) rather than H's — worth a dedicated look at whether
"fade the top-tier sector, don't follow it" has real validation potential,
which would be a different strategy from the one actually specified here.

**Real data gap found**: only 9 of 11 sector ETFs have any backfilled H4
history — **XLE and XLF have zero rows** in this project's own database
despite being in `BENCHMARK_INSTRUMENTS` (confirmed, 2026-10-08). Checked
live against Alpaca directly: real H1 data exists for both right now
(XLE closed 65.07, XLF closed 53.615), so this is a genuine backfill gap
for these two specific symbols, not a "no data exists" situation — not
investigated further in this pass (root-causing exactly why the backfill
script skipped these two is separate work from the strategy research
itself).

## 7. Known limitations, disclosed not hidden

- 4 of 10 strategy families are `RESEARCH_SPEC_ONLY` — specs exist, nothing
  has been run against real data yet. This is deliberate, incremental
  scoping (confirmed with the user before starting Priority 3's largest
  item), not an oversight.
- Only 3 of the brief's 8 named equity candidates have any backfilled
  candle history at all (see Section 1 above) — a real gap for Priority 4.
- 2 of the 11 sector ETFs (XLE, XLF) have zero backfilled H4 history despite
  real Alpaca data existing for both — a real, disclosed backfill gap found
  via Strategy H's own test (Section 6 above), not fixed in this pass.
- Strategy A/F/C/H's own results above cover equities only — Strategy J
  explicitly calls for a SEPARATE crypto validation, not done in this pass.
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
