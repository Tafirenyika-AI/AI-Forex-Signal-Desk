"""AI Trading Desk V4 Priority 3 (brief Section 6 — "Advanced Strategy
Research Laboratory"): the modular Strategy Registry.

Every strategy below is a RESEARCH CANDIDATE, not an assumed-profitable
method — the brief's own words. Each one's thirteen fields match the
brief's exact required spec fields verbatim (Hypothesis / Eligible
instruments / Timeframe / Entry conditions / Exit conditions / Position
sizing assumptions / Stop-loss logic / Invalidation conditions / Expected
holding period / Data requirements / Transaction costs / Failure conditions
/ Validation criteria).

`status` and `implementation_ref` track what's actually been DONE for each
spec, honestly, so this registry can't silently drift into claiming more
than has really been built and validated:
  - RESEARCH_SPEC_ONLY: the spec below exists; no code, no test, no result.
  - HYPOTHESIS_TESTED: a real, direct statistical test of the strategy's
    core hypothesis has been run against real historical data and the
    result is recorded in docs/V4_STRATEGY_RESEARCH.md — not yet a full
    backtested trading rule.
  - BACKTESTED: a full entry/exit/sizing/stop simulation has been run
    through src/backtest/engine.py or equivalent, with real results logged.
  - SHADOW: running live in shadow mode (V4_SHADOW_ONLY), no broker writes.
  - PAPER_APPROVED: cleared for V4_ALLOW_NEW_PAPER_ORDERS — not reachable
    without the explicit human approval the brief's own Phase 7 requires.

`data_requirements` is written against what THIS project already has
(src/equity/relationships.py's SIC_TO_SECTOR, src/equity/sec_edgar.py,
src/news/equity_news.py, src/models/regime.py, the 946K+ backfilled candle
rows) versus what it still needs — not a generic textbook list.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StrategySpec:
    code: str  # "A".."J", matching the brief's own lettering
    name: str
    hypothesis: str
    eligible_instruments: str
    timeframe: str
    entry_conditions: str
    exit_conditions: str
    position_sizing_assumptions: str
    stop_loss_logic: str
    invalidation_conditions: str
    expected_holding_period: str
    data_requirements: str
    transaction_costs: str
    failure_conditions: str
    validation_criteria: str
    status: str = "RESEARCH_SPEC_ONLY"
    implementation_ref: str | None = None


STRATEGY_REGISTRY: dict[str, StrategySpec] = {
    "A": StrategySpec(
        code="A", name="Time-Series Momentum",
        hypothesis="An instrument's own trailing return over a lookback window predicts the sign of its "
                    "return over the following holding window, independent of any cross-sectional ranking "
                    "(Moskowitz, Ooi & Pedersen 2012, Journal of Financial Economics 104(2):228-250).",
        eligible_instruments="Any instrument with sufficient backfilled candle history — equities, ETFs, crypto "
                              "alike (the hypothesis itself is asset-class-agnostic per the cited research).",
        timeframe="Multiple lookback/holding pairs tested in parallel (per the brief's own instruction): "
                   "1/4/12-week lookbacks against 1-day/1-week/1-month holding windows, using H1/H4/D candles.",
        entry_conditions="Trailing lookback-window return is non-zero and exceeds a noise floor "
                          "(> 0.5x trailing rolling_vol_20 over the same window) — direction = sign of that return.",
        exit_conditions="Fixed holding-period expiry (not a trailing stop) — momentum strategies in the cited "
                         "literature are evaluated on fixed horizons, not discretionary exits.",
        position_sizing_assumptions="Volatility-scaled (inverse of rolling_vol_20) per the cited paper's own "
                                     "methodology, not fixed notional — a high-vol instrument gets a smaller size "
                                     "for the same conviction.",
        stop_loss_logic="None in the pure research test (a fixed-horizon hypothesis test by design); a real "
                         "trading version would reuse this project's existing ATR-based stop (src/decision/"
                         "fusion.py's ATR_STOP_MULTIPLIER) — not yet wired in at RESEARCH_SPEC_ONLY/HYPOTHESIS_"
                         "TESTED status.",
        invalidation_conditions="A realized return opposite in sign and exceeding 2x the entry-time rolling_vol_20 "
                                 "before the holding window expires — the move the hypothesis predicted failed to "
                                 "happen and then some.",
        expected_holding_period="1 day to 1 month, matched to the lookback tested (shorter lookback -> shorter hold, "
                                 "per the cited research's own cross-horizon findings).",
        data_requirements="Already available: backfilled H1/H4/D candles (946K+ rows, src/scripts/"
                           "backfill_candles.py), rolling_vol_20/log_return features (src/features/engine.py). "
                           "Nothing new needed to test the hypothesis itself.",
        transaction_costs="Not modeled in the hypothesis test (see HYPOTHESIS_TESTED vs BACKTESTED distinction "
                           "above); a full backtest pass would need src/backtest/engine.py's existing cost model.",
        failure_conditions="Hit rate not statistically distinguishable from 50% (two-sided test) at the lookback/"
                            "holding combination tested, OR mean move-in-favor doesn't clear a realistic "
                            "round-trip cost estimate once a full backtest is run.",
        validation_criteria="A hit rate confidence interval that excludes 50% AND a positive mean move-in-favor, "
                             "both computed out-of-sample (chronological split, no shuffling) across multiple "
                             "instruments — a result on one ticker alone is not evidence.",
        status="HYPOTHESIS_TESTED",
        implementation_ref="src/strategies/time_series_momentum.py",
    ),
    "B": StrategySpec(
        code="B", name="Cross-Sectional Momentum",
        hypothesis="Ranking a universe of liquid instruments by relative trailing performance and buying the "
                    "top decile/selling the bottom decile produces positive relative returns after costs "
                    "(Jegadeesh & Titman 1993, The Journal of Finance 48(1):65-91).",
        eligible_instruments="A cross-section of liquid, comparable instruments — the brief's own named equity "
                              "universe (NVDA/AMD/AAPL/MSFT/AMZN/META/GOOGL/TSLA) is the natural starting set; "
                              "ETFs/crypto would need their own separate cross-sections (comparing a stock's "
                              "momentum against gold's isn't the cited methodology).",
        timeframe="Classic J/K formation: 3-12 month formation (ranking) period, 1-month holding period, "
                   "re-ranked periodically — adapted here to this project's available history (D/H4 candles).",
        entry_conditions="Instrument's trailing-formation-period return ranks in the top N (long candidates) or "
                          "bottom N (short candidates, equities-short currently disabled per this project's own "
                          "risk posture — see src/risk/governor.py) of the defined cross-section.",
        exit_conditions="End of the fixed holding period, at which point the cross-section is re-ranked and "
                         "positions rebalanced to the new ranking.",
        position_sizing_assumptions="Equal-weighted across the selected decile/quintile in the classic "
                                     "methodology — simplest, most-replicated version of the cited research.",
        stop_loss_logic="None in the classic academic methodology (same fixed-horizon-rebalance logic as "
                         "Strategy A); a trading adaptation would add this project's existing ATR stop.",
        invalidation_conditions="An instrument's relative rank crosses from top-decile to bottom-half before the "
                                 "holding period ends.",
        expected_holding_period="1 month (the cited paper's own best-replicated holding period), with 3-12 month "
                                 "formation.",
        data_requirements="Already available: backfilled candle history for the brief's named equity universe. "
                           "Missing: shorting equities is currently disabled by this project's own risk posture "
                           "(brief Section 12: 'Do not activate short selling unless separately approved') — this "
                           "strategy's short leg cannot be traded (only observed/backtested) without that "
                           "separate approval.",
        transaction_costs="Cross-sectional strategies rebalance their whole universe periodically — materially "
                           "higher turnover than Strategy A; must be modeled explicitly in any backtest, not "
                           "assumed away.",
        failure_conditions="Top-decile minus bottom-decile spread return is not significantly positive after "
                            "realistic costs, or the result is driven by 1-2 outlier names rather than a broad "
                            "effect across the cross-section.",
        validation_criteria="Positive, cost-adjusted long-short spread return, stable across multiple non-"
                             "overlapping re-ranking periods, not concentrated in a single name.",
        status="HYPOTHESIS_TESTED",
        implementation_ref="src/strategies/cross_sectional_momentum.py",
    ),
    "C": StrategySpec(
        code="C", name="Trend Following",
        hypothesis="Volatility-adjusted trend strength (moving-average structure) persists long enough that "
                    "entering in the direction of an established trend and trailing the exit captures more of "
                    "the move than a fixed-horizon approach.",
        eligible_instruments="Same universe as Strategy A — trend-following is asset-class-agnostic.",
        timeframe="H4/D, matched to this project's existing TREND regime classification "
                   "(src/models/regime.py, TREND_PERCENTILE=0.65).",
        entry_conditions="src/models/regime.py already classifies TREND (and, as of V4 Priority 2, "
                          "TREND_UP/TREND_DOWN via regime_direction) — entry triggers on a fresh transition into "
                          "TREND in the trade's intended direction, not merely 'currently in TREND' (avoids "
                          "entering late into an already-extended move).",
        exit_conditions="Trailing exit, not fixed-horizon — this project already has a backtested ATR-ratcheting "
                         "trailing stop (Phase D1/D2 of the earlier 'model intelligence' work, "
                         "src/execution/trailing_stop.py) directly reusable here rather than rebuilt.",
        position_sizing_assumptions="Volatility-adjusted (ATR-based), same convention as this project's existing "
                                     "live sizing.",
        stop_loss_logic="The existing ATR-ratcheting trailing stop (src/execution/trailing_stop.py) is the exact "
                         "mechanism this strategy's spec calls for — already built, backtested (Phase D1: "
                         "drawdown+payoff improve in every pair tested, hit rate drops, net return mixed) and "
                         "live-capable (Phase D2) for OANDA-demo/Alpaca-crypto. Reuse, don't rebuild.",
        invalidation_conditions="regime_direction flips against the open position's direction, or regime exits "
                                 "TREND into RANGE/SHOCK before the trailing stop itself is hit.",
        expected_holding_period="Open-ended, exit-driven rather than time-driven — trend-following's defining "
                                 "characteristic versus Strategies A/B.",
        data_requirements="Fully available already: src/models/regime.py's TREND/regime_direction, "
                           "src/execution/trailing_stop.py. The least new-data-dependent strategy in this set.",
        transaction_costs="Lower turnover than A/B (holds through the trend rather than rebalancing on a "
                           "schedule) but wider slippage risk on the trailing-stop exit itself in fast markets.",
        failure_conditions="Win rate materially below 50% AND average winner doesn't meaningfully exceed average "
                            "loser (trend-following strategies are expected to have a LOW hit rate but a high "
                            "payoff ratio — a high hit rate with a low payoff ratio would actually be the failure "
                            "signature here, the opposite of the naive read).",
        validation_criteria="Positive expectancy with a payoff ratio (avg win / avg loss) that compensates for a "
                             "sub-50% hit rate, consistent with Phase D1's own already-observed real pattern.",
        status="HYPOTHESIS_TESTED",
        implementation_ref="src/strategies/trend_following.py",
    ),
    "D": StrategySpec(
        code="D", name="Opening-Range Breakout",
        hypothesis="A liquid equity's first N minutes of regular-session trading establish a range whose "
                    "breakout (with volume confirmation) persists for the remainder of the session more often "
                    "than chance.",
        eligible_instruments="Liquid, actively-traded equities/ETFs only — this hypothesis is specifically about "
                              "regular-session open dynamics, meaningless for 24/7 crypto (see Strategy J instead).",
        timeframe="Intraday — opening range measured over the first 5-30 minutes of the regular session "
                   "(9:30-10:00 ET), breakout monitored through the rest of the session.",
        entry_conditions="Price closes outside the opening range's high/low on volume materially above that "
                          "instrument's own trailing average opening-range volume.",
        exit_conditions="End of regular session (no overnight hold), or an opposite-direction re-entry into the "
                         "opening range (a failed-breakout signal).",
        position_sizing_assumptions="Volatility-adjusted, sized to the opening range's own width (a wider range "
                                     "implies a wider stop, so smaller size for equal dollar risk).",
        stop_loss_logic="Opposite side of the opening range, or a fixed ATR multiple beyond the breakout level, "
                         "whichever is tighter.",
        invalidation_conditions="Price re-enters and closes back inside the opening range before a target is hit "
                                 "— the classic 'false breakout' failure mode this strategy must explicitly test "
                                 "for, not just assume away.",
        expected_holding_period="Intraday only (same session), typically under 4 hours.",
        data_requirements="PARTIALLY MISSING: this project has no explicit 'session open' feature or intraday "
                           "M15/M1 candle depth beyond M15's 60-day backfill window (src/scripts/"
                           "backfill_candles.py's TARGET_LOOKBACK) — a real gap for testing this specific "
                           "hypothesis properly; M15 depth is enough for a first-pass test but not a long "
                           "multi-year validation without a deeper intraday backfill.",
        transaction_costs="Higher turnover (one round-trip per eligible session) and real intraday slippage risk "
                           "at the breakout moment itself (a known weak point of this strategy family generally).",
        failure_conditions="False-breakout rate exceeds true-breakout persistence rate, or any edge found "
                            "disappears once realistic intraday slippage is modeled (this family is notoriously "
                            "sensitive to execution quality).",
        validation_criteria="True-breakout persistence rate statistically exceeds the false-breakout rate, net of "
                             "realistic slippage, across multiple liquid names and session types.",
    ),
    "E": StrategySpec(
        code="E", name="VWAP Mean Reversion",
        hypothesis="An intraday price that deviates statistically far from its own session VWAP tends to revert "
                    "toward VWAP within the same session, under suitable (non-trending, non-event) regimes.",
        eligible_instruments="Liquid equities/ETFs with reliable intraday volume data (VWAP is volume-weighted by "
                              "definition — illiquid names produce an unreliable VWAP).",
        timeframe="Intraday, M15 or finer.",
        entry_conditions="Price deviates from session VWAP by more than a volatility-scaled threshold (e.g. 2x "
                          "a trailing intraday ATR) AND src/models/regime.py's classification is RANGE/"
                          "regime_low_volatility, not TREND or SHOCK — this strategy explicitly should NOT fire "
                          "during a real trend (reversion against an established trend is exactly the failure "
                          "mode to guard against).",
        exit_conditions="Price reverts to within a smaller band of VWAP (target), a fixed time-stop if reversion "
                         "doesn't occur, or session close, whichever comes first.",
        position_sizing_assumptions="Sized to the stop distance (deviation-threshold-based), standard "
                                     "volatility-adjusted sizing.",
        stop_loss_logic="Beyond the entry deviation threshold by a further fixed multiple — if the price keeps "
                         "moving away from VWAP past entry, the reversion thesis has failed, not just paused.",
        invalidation_conditions="regime flips to TREND or SHOCK while the position is open — the regime "
                                 "precondition that justified the trade is no longer true.",
        expected_holding_period="Minutes to a few hours, same-session only.",
        data_requirements="PARTIALLY MISSING: no VWAP feature currently exists anywhere in this codebase (checked "
                           "src/features/engine.py and src/features/equity_vectorized.py — neither computes it); "
                           "needs a new, real, volume-weighted intraday feature, not a proxy.",
        transaction_costs="High turnover per session if multiple deviation events occur; the reversion target is "
                           "often narrow enough that costs can matter more here than in trend-following.",
        failure_conditions="Reversion rate is not meaningfully different between the RANGE-gated entries and an "
                            "ungated control group — i.e., the regime filter isn't actually adding anything, or "
                            "costs eat the whole edge given the narrow typical target.",
        validation_criteria="Reversion rate inside the regime-gated condition is both statistically better than "
                             "chance AND measurably better than an identical test with the regime gate removed — "
                             "proving the regime filter is pulling real weight, not decoration.",
    ),
    "F": StrategySpec(
        code="F", name="Volatility Breakout",
        hypothesis="A period of unusually compressed volatility (relative to an instrument's own trailing "
                    "history) is followed by a volatility expansion, and the expansion's initial direction "
                    "persists more often than chance.",
        eligible_instruments="Same broad universe as Strategy A — volatility compression/expansion is a "
                              "generic statistical property, not asset-class-specific.",
        timeframe="H1/H4, using the existing rolling_vol_20/vol_percentile features directly.",
        entry_conditions="src/models/regime.py's new (V4 Priority 2) regime_low_volatility flag was True within "
                          "a recent lookback window AND the current bar's vol_percentile has since risen sharply "
                          "— a direct, literal implementation of 'compression followed by expansion' using "
                          "infrastructure that already exists.",
        exit_conditions="Target set at a multiple of the pre-breakout compressed ATR, or trailing-stop exit "
                         "(reusing src/execution/trailing_stop.py, same as Strategy C) once the expansion is "
                         "confirmed under way.",
        position_sizing_assumptions="Sized to the POST-expansion ATR (not the compressed pre-breakout ATR, which "
                                     "would understate real risk the moment volatility actually expands).",
        stop_loss_logic="Beyond the compression range's own boundary — if price falls back inside the compressed "
                         "range, the expansion thesis failed.",
        invalidation_conditions="Price re-enters the pre-breakout compressed range before a target or trailing "
                                 "stop triggers.",
        expected_holding_period="Hours to a few days — shorter than Strategy C's open-ended trend hold, since "
                                 "this is testing the expansion's initial move, not a full trend's persistence.",
        data_requirements="Fully available already: rolling_vol_20/vol_percentile/regime_low_volatility "
                           "(src/models/regime.py, V4 Priority 2). No new data needed.",
        transaction_costs="Moderate — one round trip per detected compression/expansion cycle, less frequent "
                           "than Strategy E's intraday reversion attempts.",
        failure_conditions="Directional persistence immediately after a detected expansion is no better than "
                            "chance — i.e., compression reliably predicts a BIGGER move but not a predictable "
                            "DIRECTION, which would still be a real (if different) finding worth recording rather "
                            "than discarding.",
        validation_criteria="The initial expansion direction (first few bars after regime_low_volatility ends) "
                             "is directionally predictive at a rate statistically distinguishable from 50%.",
        status="HYPOTHESIS_TESTED",
        implementation_ref="src/strategies/volatility_breakout.py",
    ),
    "G": StrategySpec(
        code="G", name="Earnings and Event-Driven Trading",
        hypothesis="Earnings surprises (actual vs. consensus/trend) and abnormal post-announcement volume predict "
                    "continued price drift in the surprise's direction over the following days to weeks — the "
                    "post-earnings-announcement drift (PEAD) effect (Bernard & Thomas 1989, Journal of Accounting "
                    "Research 27:1-36).",
        eligible_instruments="Equities with reliable SEC EDGAR filing history and earnings-event coverage — the "
                              "brief's own named equity universe.",
        timeframe="Event-triggered, not calendar-scheduled — fires only around a real earnings/8-K event.",
        entry_conditions="A real company_events row (Equity V2 Phase 3) classified as an earnings/guidance event, "
                          "combined with abnormal volume in the surprise's direction in the following session(s).",
        exit_conditions="Fixed post-event holding window (PEAD is a drift effect measured over days-to-weeks, not "
                         "an immediate reversal/breakout play), or an opposing new event before then.",
        position_sizing_assumptions="Smaller than the baseline per-trade size — event-driven surprises carry "
                                     "materially higher idiosyncratic variance than a technical signal, and this "
                                     "project's own risk governor already has a dedicated earnings-lockout gate "
                                     "(src/risk/equity_governor_extensions.py's upcoming_earnings_lockout_gate) "
                                     "for the OPPOSITE case (blocking non-event trades ahead of a known earnings "
                                     "date) — this strategy is the deliberate, explicit exception that trades "
                                     "ON the event itself, not around it.",
        stop_loss_logic="Wider than a technical strategy's stop, sized to the event's own realized volatility "
                         "(the surprise itself materially repriced the instrument; a tight technical stop would "
                         "just get run over by the event's own normal noise).",
        invalidation_conditions="A subsequent, contradicting real event (e.g. guidance withdrawal after an "
                                 "initially positive earnings surprise) before the holding window expires.",
        expected_holding_period="Days to a few weeks — PEAD is specifically a MEDIUM-horizon drift effect, longer "
                                 "than Strategy D/E's intraday scope.",
        data_requirements="Already available: src/equity/sec_edgar.py (structured fundamentals, point-in-time), "
                           "src/data/db.py's company_events table (earnings/guidance/M&A calendar, Equity V2 "
                           "Phase 3), src/news/equity_news.py. A real 'surprise magnitude' feature (actual vs. "
                           "consensus/trend estimate) still needs to be derived from these — not yet computed as "
                           "its own feature anywhere in this codebase.",
        transaction_costs="Low turnover (event-triggered, not scheduled) but real event-day slippage risk — "
                           "entries right after an earnings print often face wider spreads than normal.",
        failure_conditions="Drift direction doesn't persist past the first 1-2 days (i.e., the surprise is "
                            "already fully priced by the time this project's own event-detection and order "
                            "placement could realistically act on it) — a real risk given this project's news/"
                            "event ingestion isn't sub-second.",
        validation_criteria="Statistically significant drift in the surprise's direction measured from AFTER this "
                             "project's own realistic event-detection latency, not from the event timestamp "
                             "itself — otherwise the validation would silently assume faster detection than the "
                             "system actually has.",
    ),
    "H": StrategySpec(
        code="H", name="Sector Rotation",
        hypothesis="Relative strength across sector ETFs, conditioned on the broad-market regime (src/models/"
                    "regime.py), predicts which sectors will continue to outperform/underperform over the next "
                    "rotation window.",
        eligible_instruments="The 11 SPDR sector ETFs already resolved via src/equity/relationships.py's "
                              "SIC_TO_SECTOR, plus SPY/IWM as broad-market/small-cap benchmarks (V4 Priority 2's "
                              "BENCHMARK_INSTRUMENTS addition).",
        timeframe="D/weekly — sector rotation is inherently a slower-moving effect than single-name momentum.",
        entry_conditions="A sector ETF's relative strength vs. SPY (already computed, Equity V2 Phase 8's "
                          "cross-market features) ranks in the top tier of the 11 sectors AND the broad-market "
                          "regime (classify_regime on SPY itself) is not SHOCK.",
        exit_conditions="Periodic re-ranking (matching Strategy B's rebalance cadence) or the sector's relative "
                         "rank falls out of the top tier.",
        position_sizing_assumptions="Equal-weighted across the selected top-tier sectors, same simple convention "
                                     "as Strategy B.",
        stop_loss_logic="Wide, ATR-based — sector ETFs are themselves diversified baskets, lower single-name "
                         "tail risk than Strategy A/B's individual-stock exposure.",
        invalidation_conditions="Broad-market regime flips to SHOCK (a market-wide event likely swamps any "
                                 "sector-relative signal).",
        expected_holding_period="Weeks to a couple of months.",
        data_requirements="Fully available already: Equity V2 Phase 7/8's SIC_TO_SECTOR + cross-market relative-"
                           "strength features, V4 Priority 2's expanded BENCHMARK_INSTRUMENTS. No new data needed.",
        transaction_costs="Low turnover (slow rotation, periodic rebalance) — one of the cheaper strategies in "
                           "this set to actually trade.",
        failure_conditions="Top-tier sector selection doesn't outperform an equal-weight-all-11-sectors baseline "
                            "after costs — i.e., the ranking itself adds nothing over just holding the whole "
                            "sector basket.",
        validation_criteria="Top-tier-selected sectors outperform the equal-weight-all-sectors baseline, net of "
                             "costs, across multiple non-overlapping rotation windows.",
        status="HYPOTHESIS_TESTED",
        implementation_ref="src/strategies/sector_rotation.py",
    ),
    "I": StrategySpec(
        code="I", name="Statistical Pairs Trading",
        hypothesis="Two related instruments' price spread is cointegrated (mean-reverting around a stable "
                    "long-run relationship) and a statistically stretched spread predicts reversion toward that "
                    "relationship (Gatev, Goetzmann & Rouwenhorst 2006, The Review of Financial Studies "
                    "19(3):797-827).",
        eligible_instruments="Related-pair candidates from this project's own existing universe: same-sector "
                              "equity pairs (via SIC_TO_SECTOR), a sector ETF vs. its own index/benchmark "
                              "(e.g. XLE vs. USO), or GLD vs. IAU (near-identical gold exposure, a cleaner "
                              "cointegration candidate than most equity pairs).",
        timeframe="D, cointegration relationships need enough history to test reliably — not a short-window "
                   "intraday signal.",
        entry_conditions="A formal cointegration test (not just correlation — correlation and cointegration are "
                          "different claims) passes on the candidate pair's own price history, AND the current "
                          "spread (in standard-deviation terms relative to its own historical spread) exceeds "
                          "an entry threshold.",
        exit_conditions="Spread reverts to within a narrower band of its historical mean, or a maximum holding "
                         "period expires without reversion.",
        position_sizing_assumptions="Dollar-neutral (long one leg, short the other in matched notional) — the "
                                     "classic pairs-trading construction; the short leg hits the same equities-"
                                     "short-disabled constraint as Strategy B unless the pair is ETF-vs-ETF "
                                     "(GLD/IAU) or one leg is already long-only compatible.",
        stop_loss_logic="Spread divergence beyond a wider threshold than entry — if the spread keeps widening "
                         "well past the entry trigger, the cointegration relationship itself may have broken "
                         "(a real risk this spec must test for, not assume away).",
        invalidation_conditions="A formal re-test of the cointegration relationship fails on a rolling basis — "
                                 "the pair's long-run relationship itself may no longer hold (corporate actions, "
                                 "business-model divergence, etc.).",
        expected_holding_period="Days to weeks, until reversion or the cointegration-break invalidation fires.",
        data_requirements="PARTIALLY MISSING: no cointegration-testing code exists anywhere in this codebase yet "
                           "(checked src/features/ and src/models/ — nothing). Needs a new statistical module "
                           "(e.g. an Engle-Granger or Johansen test) — genuinely new work, not a reuse of existing "
                           "infrastructure like most of the other strategies here.",
        transaction_costs="Two-legged (double the number of orders per round trip vs. a single-instrument "
                           "strategy) — materially higher cost drag per signal than Strategies A/C/F.",
        failure_conditions="The pair fails a genuine, pre-registered cointegration test (not cherry-picked after "
                            "seeing a profitable-looking spread), or the spread's historical mean/variance isn't "
                            "stable enough across the test period to define a meaningful entry threshold.",
        validation_criteria="A real, formal cointegration test passes on held-out data (not just the window used "
                             "to discover the pair), AND the reversion-based trading rule is profitable net of "
                             "two-legged transaction costs out-of-sample.",
    ),
    "J": StrategySpec(
        code="J", name="Crypto Momentum and Volatility",
        hypothesis="BTC/USD and ETH/USD's 24/7 trading removes the session-boundary effects Strategies D/E "
                    "depend on, so momentum/volatility-regime effects (per Strategies A/F) should be tested "
                    "SEPARATELY for crypto rather than assumed to transfer from equity-session results.",
        eligible_instruments="Alpaca-supported crypto pairs — BTC/USD, ETH/USD, plus any other liquid pairs "
                              "Alpaca's own list_instruments() confirms are tradable (checked, not assumed, per "
                              "this project's own 'confirm real symbols before relying on them' standard — see "
                              "V4 Priority 2's own live-verification of IWM/GLD/IAU/USO for the same discipline).",
        timeframe="H1/H4, continuously (no session gaps to align to, unlike D/E).",
        entry_conditions="Same mechanical entry logic as Strategy A (trailing-return momentum) and Strategy F "
                          "(volatility compression/expansion), but with regime thresholds re-fit on crypto's own "
                          "candle history rather than reusing equity-fitted percentile thresholds — crypto's "
                          "baseline volatility is materially higher, so e.g. src/models/regime.py's "
                          "HIGH_VOL_PERCENTILE=0.85 trailing-percentile approach is ALREADY self-relative per-"
                          "instrument (the module's own docstring: 'adapts automatically across pairs with very "
                          "different typical volatility') — genuinely reusable as-is, this needs confirming "
                          "empirically on crypto data specifically before assuming so.",
        exit_conditions="Same as Strategy A (fixed-horizon) or F (trailing-stop/target) depending on which "
                         "sub-hypothesis is being tested — crypto doesn't need its own separate exit logic, just "
                         "its own separate VALIDATION of the entry hypothesis.",
        position_sizing_assumptions="Crypto's fractional-unit sizing is already fully supported end to end "
                                     "(src/outcomes/alpaca_tracker.py, trade_outcomes.units is Float specifically "
                                     "for this) — no new sizing work needed.",
        stop_loss_logic="This project's existing live crypto trailing-stop code already exists and is live-"
                         "capable (Phase D2 of the earlier 'model intelligence' work, src/broker/alpaca.py's "
                         "crypto-only modify_stop_loss) — directly reusable.",
        invalidation_conditions="Same as Strategy A/F, re-evaluated against crypto's own regime thresholds rather "
                                 "than equity-fitted ones.",
        expected_holding_period="Hours to days — crypto's 24/7 trading means 'fixed calendar days' and 'fixed "
                                 "trading sessions' aren't the same unit, unlike equities.",
        data_requirements="Already available: this project already backfills BTC/USD and ETH/USD candle history "
                           "via Alpaca (confirmed live during the original Alpaca integration work). No new data "
                           "source needed — the work here is testing/validation, not data acquisition.",
        transaction_costs="Crypto spreads/fees on Alpaca differ materially from equities — must use crypto's own "
                           "real fee schedule in any backtest, not the equity cost model by default.",
        failure_conditions="Momentum/volatility-breakout effects that hold for equities don't replicate on crypto "
                            "at a statistically meaningful rate — itself a valid, useful finding (confirms "
                            "crypto needs its own validated strategy rather than inheriting equity results), not "
                            "a failure of the research process.",
        validation_criteria="Strategy A/F's own validation criteria, independently re-run on BTC/USD and ETH/USD "
                             "candle history specifically — not inferred from the equity-universe result.",
        status="HYPOTHESIS_TESTED",
        implementation_ref="src/strategies/time_series_momentum.py, src/strategies/volatility_breakout.py (reused directly, crypto args)",
    ),
}


def all_strategy_codes() -> list[str]:
    return sorted(STRATEGY_REGISTRY.keys())
