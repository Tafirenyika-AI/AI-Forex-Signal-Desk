# Equity Intelligence V2 — Implementation Log

Source brief: "AI TRADING DESK — EQUITY INTELLIGENCE V2" (pasted 2026-10-01). Companion to `docs/EQUITY_V2_AUDIT.md` (Phase 0). Each phase gets its own dated section below, in the format every prior section of this project's work has used (see `docs/REQUIREMENT_TRACKER.md` for the external-review-brief precedent this follows).

Per-phase entries record: files changed, schema changes, tests added, tests passed/failed, known limitations, execution-impact assessment (did this phase touch anything capable of creating/modifying/closing a broker order — see audit section 12/13 for the enumerated list to check against).

---

## Phase 0 — Safe Repository Audit — **DONE**

2026-10-01. Produced `docs/EQUITY_V2_AUDIT.md` (14 sections, all requested). Captured required pre-implementation read-only broker snapshot (`docs/EQUITY_V2_PRE_IMPLEMENTATION_SNAPSHOT.json`) before any inspection began — real state at capture: zero open positions on either broker (the one position open earlier that day, an MSFT short, independently confirmed to have closed via its own pre-existing protective stop order, not by anything in this audit).

**Files changed**: none (read-only phase, as instructed). Two new files created: `docs/EQUITY_V2_AUDIT.md`, `docs/EQUITY_V2_PRE_IMPLEMENTATION_SNAPSHOT.json`, this log.

**Schema changes**: none.

**Tests**: none added (no code changed). Confirmed no change to `src/config.py`'s live-trading refusal, no order placed, no position touched.

**Execution-impact assessment**: zero. Nothing in this phase is capable of reaching section 12/13's enumerated order-mutating paths — every action taken was a read (broker state queries, source file reads) or a new-file write under `docs/`.

**Known limitations carried forward** (full detail in the audit): no `EQUITY_V2_ACTIVATED_AT` boundary exists yet (recommended as a new singleton table, not yet created); the backtest engine's historical confidence numbers carry an unquantified dilution effect from the same bug fixed live today; `active_trading_users()`'s OANDA INNER JOIN remains a real structural risk for a future equity-only user.

**Next**: awaiting direction on which phase to pick up next — the brief itself frames this as a large, multi-week, phase-by-phase effort ("work incrementally... do not make one giant rewrite"), consistent with how this project's other large brief (the external code-review brief, see `docs/REQUIREMENT_TRACKER.md`) was handled.

---

## Phase 1 — Broker Reconciliation Engine — **DONE**

2026-10-01. Built `src/reconciliation/alpaca.py` — the brief's four required primitives (account/positions/open-orders/historical-orders, `ReconciliationIssue` records with the exact requested fields, VERIFIED/WARNING/UNRESOLVED/CRITICAL severities, the explicit INTERNAL-vs-BROKER-VERIFIED P&L distinction) plus the four checks it made possible: position reconciliation, protective-order coverage, unexplained-broker-order detection, and P&L reconciliation.

**Files changed**: `src/reconciliation/__init__.py` (new), `src/reconciliation/alpaca.py` (new, ~300 lines), `src/data/db.py` (+1 table), `src/dashboard/app.py` (consolidated a duplicated starting-deposit constant to import from the new module instead of keeping its own copy), `tests/test_reconciliation_alpaca.py` (new, 18 tests).

**Schema changes**: one new additive table, `reconciliation_issues` (persists every `ReconciliationIssue` a run produces — `severity`, `symbol`, `issue_type`, `broker_value`, `internal_value`, `detected_at`, `description`, `suggested_investigation`, plus `resolved_at`/`resolved_note` for a human to close one out later; never written by the reconciliation run re-checking itself). Also gave the existing, previously-dormant `positions_snapshots` table (defined in the schema since early in this project, never once written to by any code path) its first real writer — one row per reconciliation run, exactly the point-in-time account snapshot it was evidently built for.

**Real finding on the first live run** (not a hypothetical — ran it against the real production account immediately after writing it, per this project's own verification standard): position reconciliation flagged BTC/USD as CRITICAL — broker reports exactly 0, but FIFO-walking Alpaca's own order history reconstructed a nonzero 5.181e-6 BTC "still open." Investigated rather than loosened the check blindly: summed buy-minus-sell across this account's 7 real BTC/USD round trips by hand and got exactly 5.181e-6 (matching to 9 decimal places) — confirmed via a fresh live `/positions` call that the broker's own number is genuinely, exactly flat, not a display-rounded dust entry. Root cause: Alpaca deducts crypto trading fees from the position itself (already documented in `src/outcomes/alpaca_tracker.py`'s own module docstring from earlier the same day), so a round trip's sell leg is always a little less than its buy leg — real fee consumption, not drift or a bug. Fixed with an asset-class-aware tolerance (`asset_class_for`) rather than a single shared number, which would have either false-alarmed on routine crypto fee dust or masked a real equity mismatch under a too-loose shared threshold. Added two tests locking this in: the exact real 7-round-trip pattern must stay VERIFIED, and an equivalently tiny equity gap must still be CRITICAL (no comparable fee mechanic exists there).

**Second real finding, already known but now permanently tracked**: 13 "unexplained broker order" findings (UNRESOLVED), all real — Alpaca order history containing client_order_ids like `test-361e72f4ddcb`, `synctest-crypto-944826ea`, `trailing-verify-crypto-test` from this and earlier sessions' own ad-hoc live-verification scripts (see memory: Phase D2's "verification-script mishap" note), plus the one genuinely unexplained NVDA close from `docs/EQUITY_V2_AUDIT.md` section 12. None of these are a surprise, but none were discoverable in one place before this — now they're queryable (`reconciliation_issues` where `issue_type='unexplained_broker_order'`) instead of living only in scattered memory files and one-off investigations.

**P&L reconciliation threshold bug caught by the test-first discipline, not by inspection**: the original implementation required the dollar-amount AND percent-of-NAV thresholds to both be exceeded before flagging WARNING/CRITICAL — a test with a realistic small-account gap ($300 on $100,500 NAV, comfortably over the $100 floor but under the 0.5%-of-NAV floor) proved this would silently pass as VERIFIED. Changed to OR: either threshold alone is enough, since a small-dollar gap can still be a large fraction of a small account, and a large-dollar gap can still be a tiny fraction of a large one. Verified via the standard revert-and-confirm-the-test-fails cycle before restoring.

**Tests**: `tests/test_reconciliation_alpaca.py`, 18 tests — pure-function coverage for `_net_position`/`_is_known_client_order_id`, each of the four checks' VERIFIED/WARNING/CRITICAL/UNRESOLVED paths including the two real-pattern cases above, and one full end-to-end test of `reconcile()` itself against a fake `AlpacaBroker` (`_request`/`account_state`/`positions` monkeypatched, same pattern `tests/test_alpaca_crypto_protection.py` established) + an isolated in-memory SQLite engine, confirming both the returned `ReconciliationReport` and the persisted `reconciliation_issues`/`positions_snapshots` rows. Full suite: 146/146 passing.

**Execution-impact assessment**: zero. Every broker call in this module is a GET — confirmed by reading every call site (`account_state()`, `positions()`, `_request(..., "GET", ...)` twice for open/all orders). No `place_order`, `cancel_order`, or `modify_stop_loss` call exists anywhere in the file. Ran twice against the real production account as part of verification; both runs were pure reads plus the intended new-table writes (`reconciliation_issues`, `positions_snapshots`) — no order placed, no position touched, confirmed by the position-reconciliation check itself reporting all three real symbols as matched both times.

**Known limitations, disclosed not hidden**:
- The "executed trade_intents have a matching broker fill" cross-check from the original design sketch was not implemented this pass (would need live `get_order()` calls per intent, meaningfully more complex/rate-limit-sensitive than the four checks that shipped) — scoped out, not forgotten.
- `STARTING_DEPOSIT` is still a single hardcoded constant (now in one place instead of two, but still not a real deposit/withdrawal ledger) — same disclosed gap the Phase 0 audit already named.
- OANDA has no reconciliation module yet — this phase is Alpaca-only, matching the brief's own Phase 1 heading ("Broker Reconciliation Engine... src/reconciliation/alpaca.py").

**Scheduled, same day**: `src/scripts/run_reconciliation.py` (thin entrypoint, same shape as `sync_outcomes.py` — iterates `active_trading_users()`, calls `reconcile()` per user, logs a summary plus every CRITICAL finding loudly) + `run_reconciliation.bat` + a new `AIForex_Reconciliation` Windows Scheduled Task, every 30 minutes, matching `AIForex_SyncOutcomes`'s own cadence.

**Real bug caught registering the task, not by inspection**: the first registration (via `New-ScheduledTaskTrigger`/`-Principal` with no explicit `-Settings`) reported `LastTaskResult: 0` (success) on every manual trigger, twice, with a 20-second wait — but produced no log file and no effect at all. Running the exact same `.bat` file directly (outside Task Scheduler) worked perfectly, isolating the problem to the task's own settings, not the script. Diffed the new task's XML against the known-working `AIForex_SyncOutcomes` task (`schtasks /query ... /xml`) and found it was missing `StartWhenAvailable` and `WakeToRun` — the EXACT setting class already diagnosed as a real multi-day outage cause earlier in this project (see memory: "Sleep/wake fix... WakeToRun=False... not a code bug"), plus an inverted `DisallowStartIfOnBatteries`/`StopIfGoingOnBatteries` pair. Re-registered with `New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -StartWhenAvailable -WakeToRun`, confirmed the XML now matches the working task line-for-line except for the expected cosmetic differences (name, description, filenames, start time), then re-triggered and confirmed a real log entry appeared. A `LastTaskResult: 0` was not trusted as proof of real execution at any point — the log file's actual contents were the verification, consistent with this project's own standard.

**Execution-impact assessment (scheduling)**: zero — the scheduled task runs the exact same read-only `reconcile()` already assessed above, just on a timer instead of manually.

**Next**: dashboard surfacing of `reconciliation_issues` (part of Phase 16's own ask, but small enough to consider pulling forward), or continue to another phase per the brief's own ordering — awaiting direction.

---

## Phase 2 — Real Equity/Crypto Market Data Engine — **DONE**

2026-10-01. Built `src/market_data/alpaca_stream.py` — a real push-stream WebSocket client (`AlpacaMarketStream`) for Alpaca's live market-data feeds, as the brief's own architecture requires (`WebSocket -> normalized event -> event store -> feature engine -> ...`) before any later-phase feature/model/decision consumer can be built on top of it.

**Verified the real protocol live before writing any parsing code** (not inferred from docs, per this project's standing verification discipline): connected, authenticated, and subscribed against this account's real credentials on both `wss://stream.data.alpaca.markets/v2/iex` (equities) and `wss://stream.data.alpaca.markets/v1beta3/crypto/us` (crypto) — same auth/subscribe message shape for both — and received real live BTC/USD quote ticks confirming the exact message shape (`{"T":"q","S":...,"bp":...,"bs":...,"ap":...,"as":...,"t":...}`) used in `_parse_message`.

**Files changed**: `src/market_data/__init__.py` (new, empty), `src/market_data/alpaca_stream.py` (new, ~240 lines), `tests/test_alpaca_market_stream.py` (new, 13 tests), `requirements.txt` (+`websockets>=13.0`).

**Schema changes**: none — this phase has no event-store/persistence layer yet (disclosed limitation below).

**Design**: `MarketEvent` (frozen dataclass; one shape for trade/quote/bar) + `_parse_message`/`_parse_time` pure functions + `AlpacaMarketStream` (one instance per asset class — Alpaca uses separate WS hosts for equity vs. crypto, confirmed live). `listen()` is a single `async for` entry point handling reconnect (capped exponential backoff, same shape as `AlpacaBroker`'s own rate-limit retry), a heartbeat/watchdog (treats an idle connection as dead and reconnects even if the TCP socket hasn't noticed), re-subscription across reconnects, out-of-order rejection (per-instrument, by feed timestamp), and exact-duplicate-event dedup (per instrument+event-type, by a value signature). `is_stale()` lets a future caller fall back to `AlpacaBroker.stream_prices()`'s existing 5-second-poll implementation (preserved untouched, exactly as the brief's own "preserve it initially as a fallback" instruction asks) when the push stream goes quiet.

**Real test-design bug caught and fixed before trusting this phase's own tests**: `test_stream_dedups_exact_repeat_events` queued 3 identical inbound messages but broke out of its consuming loop immediately after the FIRST yielded event — meaning the 2nd/3rd duplicate messages could never reach the dedup check at all regardless of whether dedup worked, a vacuously-passing test. Caught by deliberately disabling the dedup condition (`if self._last_seen_key.get(dedup_key) == sig:` → `if False:`) as a sanity check and finding the test passed anyway — the expected signal of a real test-design flaw, not a false reassurance. Rewrote the test to collect under a short overall `asyncio.wait_for` deadline instead of breaking after one event, so all 3 queued messages are actually processed before the assertion runs. Re-verified both directions: with dedup disabled, the rewritten test correctly fails (`assert 3 == 1`); restored, it correctly passes (`assert len(events) == 1`).

**Also fixed while writing tests**: the fake connection's queued-message sequence didn't account for the extra subscribe-confirmation round-trip `_connect_and_auth()` performs when `subscribe()` is called before `listen()` starts — this silently consumed a queued data message as if it were that confirmation, then blocked on an empty queue inside a 60-second `asyncio.wait_for` that exceeded a tool timeout and looked like a hang. Fixed by adding a `pre_subscribed` flag to the test helper that builds the fake message sequence, and adding a `heartbeat_timeout_seconds` constructor parameter to `AlpacaMarketStream` (default unchanged for production) so tests can fail fast instead of risking a long wait on any future similar mismatch.

**Tests**: `tests/test_alpaca_market_stream.py`, 13 tests — pure-function coverage (`_parse_time` nanosecond truncation, `_parse_message` for quote/trade/bar/control/unknown-type), and `AlpacaMarketStream` driven by a fake WebSocket connection (same monkeypatch-the-transport philosophy this project already uses for broker adapters): connect/auth/subscribe/yield, dedup (now genuinely exercised, see above), out-of-order rejection, staleness detection, and reconnect-after-`ConnectionClosed` with re-subscription. Full project suite: **159/159 passing** (146 prior + 13 new).

**Execution-impact assessment**: zero, by construction, not just by promise. This file imports nothing from `src/execution` or `src/broker.alpaca`'s order-placing surface, and connects only to Alpaca's separate read-only market-data host (`stream.data.alpaca.markets`) — never the trading host (`paper-api.alpaca.markets`) `AlpacaBroker.place_order()` talks to. "DO NOT place orders from the WebSocket handler" / "Never: WebSocket -> order" holds structurally.

**Known limitations, disclosed not hidden**:
- Not yet wired into `src/run_loop.py`'s live decision cycle — this phase is the brief's first box only (`WebSocket -> normalized event`); the event-store/feature-engine/model/decision consumer chain is later-phase work (Phase 9 onward per the brief's own ordering).
- No event-store persistence layer exists yet — `MarketEvent`s are produced in-process only; nothing durable is written.
- Live verification was performed during off-market hours — real live data was only actually observed flowing on the **crypto** endpoint (BTC/USD, trades 24/7); the **equity** endpoint's connect/auth/subscribe round-trip was verified live but no real equity tick was observed flowing (US equity markets were closed at verification time). The equity parsing path is exercised only by the fake-connection unit tests, not a live tick, pending a future off-hours-aware re-check.

**Next**: per "go on until you finish" — continuing to the next phase in the brief's dependency order (equity data model tables / historical data layer), treating OANDA as permanently out of scope throughout.

---

## Phase 3 — Equity Data Model — **DONE**

2026-10-01. Added five new additive tables to `src/data/db.py`: `equity_entities` (ticker→company/sector/industry/index reference data), `company_fundamentals` (SEC EDGAR structured financials, EAV-shaped per metric), `company_events` (earnings/guidance/M&A/split/dividend calendar), `equity_news` (ticker-aware news, parallel to but never merged with the existing currency-keyed `news_events`), `market_context` (cross-market readings: SPY/QQQ/sector ETFs/yields/VIX, EAV-shaped per metric).

**Point-in-time discipline, per the brief's own no-look-ahead-bias requirement (sec. 11)**: every table separates the real-world period a value *describes* from the timestamp it actually became *knowable* — `company_fundamentals.filed_at` vs. `period_end`, `company_events.announced_at` vs. `event_time`, `equity_news.publish_time`/`ingest_time`, `market_context.time`. A historical backtest filtering on these timestamps can never see a value from its own simulated future.

**Real schema bug caught by its own test before shipping**: the first version of `company_fundamentals`'s unique constraint was `(ticker, period_end, metric, source)` — a test modeling a genuine restatement (a 10-K correcting an earlier 10-Q's revenue figure, same ticker/period/metric/source, later `filed_at`) failed to insert, colliding with the original filing. A restatement is a new point-in-time fact, not an overwrite of the old one — a replay "as of" an earlier date must still see only the filing(s) that existed by then. Fixed by adding `filed_at` to the constraint; the restatement test (and a sibling same-filing-twice duplicate-rejection test) now both pass correctly.

**Files changed**: `src/data/db.py` (+5 tables), `tests/test_equity_data_model.py` (new, 11 tests).

**Schema changes**: additive only, confirmed both via `metadata.create_all()`'s own additive semantics (an existing `candles` insert/select round-trip is exercised in the same test file to confirm the pre-existing schema is untouched) and by running `get_engine()` directly against the real production Postgres database — all 5 new tables created cleanly, table count went from 38 to 43, zero existing tables altered.

**Tests**: `tests/test_equity_data_model.py`, 11 tests — all 5 tables exist in metadata, additive creation doesn't disturb a pre-existing table, each table's unique constraint (including the restatement fix above), and the "missing fundamental metric is an explicit NULL row, never fabricated" case. Full suite: **170/170 passing** (159 prior + 11 new).

**Execution-impact assessment**: zero — schema-only change, no broker call anywhere in this phase.

**Known limitations, disclosed not hidden**:
- No ingestion code yet — these are empty tables awaiting Phase 4 (SEC EDGAR) / Phase 6 (equity news) / Phase 8 (cross-market features) to actually write into them.
- `equity_entities` is a single current-state row per ticker (upsert-shaped), not an append-only history — a sector reclassification overwrites in place, which the brief doesn't ask to be point-in-time (unlike fundamentals/events), so this is a deliberate, disclosed asymmetry in the schema's design, not an oversight.

**Next**: Phase 4 (SEC EDGAR intelligence, `src/equity/sec_edgar.py`) — the first real consumer of `company_fundamentals`/`equity_entities`.

---

## Phase 3 follow-up — `company_fundamentals` nullable-column bug (same day, before Phase 4)

2026-10-01, while writing Phase 4's real SEC EDGAR client and discovering live that a single XBRL tag can report both a single-quarter fact and a cumulative year-to-date fact sharing the same `end` date (only `start` differs) — found while verifying NVDA's real `Revenues` facts live against `data.sec.gov`. Added `period_start` to `company_fundamentals` to disambiguate, initially as nullable (instant/balance-sheet metrics like `Assets` have no real duration). **A new test caught a real bug in that nullable design before any real data was written**: ANSI SQL treats every NULL as distinct from every other NULL inside a unique constraint — so an instant metric (always `period_start=NULL`) could be inserted as an unbounded duplicate without ever tripping `uq_company_fundamental`, on both SQLite and Postgres. Fixed by making `period_start` `NOT NULL`, with the ingester required to set `period_start = period_end` as an honest "this period is a single point" sentinel for instant metrics, never a NULL placeholder.

**Production schema repaired directly** (not just in code): confirmed `company_fundamentals` was still genuinely empty (0 rows — Phase 4's ingestion hadn't run yet), then applied an additive `ALTER TABLE` migration (add `period_start NOT NULL`, drop and recreate `uq_company_fundamental` with the corrected column set) directly against the real production Postgres database — no data existed to lose, and this was confirmed before the migration ran. The auto-mode permission classifier flagged a considered `DROP TABLE` + `metadata.create_all()` recreate approach as a destructive action; the user explicitly declined it ("No, find another way") and the additive `ALTER TABLE` approach was used instead, with zero risk given the confirmed-empty table.

**Tests added**: `test_company_fundamentals_instant_metric_duplicate_is_rejected_not_silently_allowed` (locks in the fix), `test_company_fundamentals_quarterly_and_ytd_facts_coexist_distinct_period_start` (locks in the real quarterly-vs-YTD coexistence case). Full suite: **172/172 passing**.

**Execution-impact assessment**: zero — schema-only change to an empty, not-yet-written-to table; no broker call anywhere in this work.

---

## Phase 4 — SEC EDGAR Intelligence — **DONE**

2026-10-02. Built `src/equity/sec_edgar.py` (`SecEdgarClient`) — official-source-only structured financials from SEC's own XBRL API, not an LLM summary. Verified live before writing the parser: the real ticker→CIK mapping (`company_tickers.json`) and a real `companyconcept` response (NVDA's `Revenues` tag) — this is exactly where the Phase 3 follow-up's quarterly-vs-YTD-same-`end`-date discovery came from, so the schema and the client were co-developed against real data, not assumptions.

**SEC's fair-access policy requires a descriptive User-Agent (real name + contact)**, not a generic one. Asked the user explicitly (2026-10-02) whether to use their real email or a placeholder, since this would be sent to a third-party service on every scheduled run going forward, not just a one-off verification call — user chose their real email.

**Files changed**: `src/equity/__init__.py` (new, empty), `src/equity/sec_edgar.py` (new, ~190 lines), `src/scripts/sync_sec_edgar.py` (new, scheduled entrypoint), `run_sync_sec_edgar.bat` (new), `tests/test_sec_edgar.py` (new, 12 tests).

**Design**: `KEY_METRICS` — a deliberately small, high-signal set of `us-gaap` tags (Revenues, NetIncomeLoss, OperatingIncomeLoss, GrossProfit, operating cash flow, Assets, Liabilities, StockholdersEquity, diluted EPS) rather than every XBRL concept a company might report; a tag a company never reports 404s and is skipped, never fabricated. `_fact_to_row` is a pure transform (network-free, directly testable) that converts one SEC fact into a `company_fundamentals`-ready row, setting `period_start = period_end` for instant/balance-sheet metrics that have no real duration (the exact sentinel the Phase 3 follow-up's schema fix requires). `sync_sec_edgar.py` iterates every equity ticker any active user actually trades (via `active_trading_users()` + `asset_class_for()`, same pattern `backfill_candles.py` already established for Alpaca-only instruments), upserts `equity_entities` (current-state, never destructively overwritten with worse data) and inserts `company_fundamentals` with `on_conflict_do_nothing` (idempotent re-run).

**Live-verified against real production data, not just unit tests**: ran the sync script for real against this account's actual configured equity tickers (AAPL, MSFT, NVDA, QQQ) — 3 of 4 are real SEC filers and together wrote 6,231 real fundamentals rows plus 3 upserted entities; QQQ correctly returned no CIK (confirmed directly against SEC's own ticker file — an ETF/unit investment trust, genuinely absent from the company-tickers dataset, not a bug). Re-ran the script a second time and confirmed the row count was byte-for-byte identical (6,231) — true idempotency, not just "doesn't crash." Directly queried the real NVDA rows afterward and confirmed the quarterly-vs-YTD same-`period_end`-different-`period_start` case the Phase 3 follow-up fixed actually occurs multiple times in real data (e.g. period_end 2026-07-26 has 2 distinct `Revenues` facts) — the schema fix was validated against reality, not just a synthetic test case.

**Tests**: `tests/test_sec_edgar.py`, 12 tests — pure `_fact_to_row`/`_parse_sec_date` coverage (quarterly fact, the real YTD-coexistence case, the instant-metric sentinel, missing-field defensiveness) and `SecEdgarClient` driven by `httpx.MockTransport` (a real httpx feature, first use of this mocking style in the project) covering ticker lookup (found/not-found), concept fetch (found/404), a mixed-tags `fundamentals_rows` call, and the required-contact-email guard. Full suite: **184/184 passing** (172 prior + 12 new).

**Scheduled, same day**: `run_sync_sec_edgar.bat` + a new `AIForex_SyncSecEdgar` Windows Scheduled Task, once daily at 06:30 local — filings don't change intraday, so this runs far less often than the trading cycle or reconciliation. Registered with the exact settings the Phase 1 scheduling bug already diagnosed as required (`-AllowStartIfOnBatteries -StartWhenAvailable -WakeToRun`), confirmed correct in the task's own XML (not assumed), manually triggered, and confirmed via the real log file's contents (not the exit code) that it actually ran and completed.

**Execution-impact assessment**: zero, by construction — `src/equity/sec_edgar.py` imports nothing from `src.execution` or any broker module, and every HTTP call in it is a GET against SEC's own public, unauthenticated, read-only API. No broker was touched by writing, running, or scheduling this phase.

**Known limitations, disclosed not hidden**:
- `equity_entities.sector`/`industry`/`exchange`/`sector_etf`/`index_membership` are left `None` by this phase — SEC's `companyconcept`/`company_tickers` endpoints don't provide them; Phase 7's relationship graph is the intended filler.
- No point-in-time backfill depth limit was applied — `fetch_concept` returns a company's ENTIRE reported history for each tag (NVDA alone: ~2,094 rows across ~10 tags going back years), which is appropriate for Phase 11's walk-forward validation but means the first sync per ticker is notably heavier than subsequent daily deltas (SEC doesn't publish an incremental/delta endpoint, so a full history is a natural cost of staying always-additive via `on_conflict_do_nothing` rather than tracking a separate watermark).
- Only run against the 3 real equity tickers currently configured by this account's active users — not yet exercised against a ticker with a materially different filing shape (e.g. a bank, REIT, or insurer with a non-standard statement structure) to see how gracefully `KEY_METRICS`' assumptions degrade.

**Next**: Phase 5 (fundamental feature engine, `src/features/equity_fundamentals.py`) — the first real consumer of the `company_fundamentals` rows this phase now writes.

---

## Phase 5 — Fundamental Feature Engine — **DONE**

2026-10-02. Built `src/features/equity_fundamentals.py` — `compute_fundamental_features()`, a pure point-in-time function (same "causal, no future information" discipline as `src/features/engine.py`) turning Phase 4's raw `company_fundamentals` rows into growth/margin/cash-flow ratios: revenue, YoY revenue growth, gross/operating/net margin, operating-cash-flow margin, leverage ratio (Liabilities/Assets), diluted EPS.

**Point-in-time correctness**: every lookup filters to `filed_at <= as_of` internally (never trusted to the caller) — a feature computed "as of" a historical date can never see a filing that hadn't happened yet, which Phase 11's walk-forward validation depends on.

**Never fabricate missing data (the brief's own Phase 5 rule)**: every ratio is `None`, never a fabricated 0 or an imputed value, when an input is unavailable as-of the requested date; `FundamentalFeatures.missing_metrics` names exactly which inputs were absent so a downstream caller can make an informed choice.

**Two real bugs found and fixed, both via the test-first/live-verify discipline, before this was trusted**:
1. **Restatement tie-break bug (caught by a synthetic test)**: when two facts tied on both `period_end` and duration (the same quarter restated with a later `filed_at`), the original tie-break fell through to whichever happened to sort first, ignoring which one was actually the more recently filed, correct value. Fixed to always prefer the latest `filed_at` among ties.
2. **Fiscal-Q4 annual-figure leak (caught live against real MSFT production data, not by any synthetic test)**: a company's fiscal Q4 very often has NO standalone XBRL fact at all — a 10-K's income statement reports the full fiscal year, not a discrete fourth quarter — so the only fact at that period_end is a ~365-day annual cumulative one. The original function used duration only as a *tie-break* among facts sharing the latest `period_end`, so when that period_end had just one (annual) fact, it was accepted outright as "this quarter's revenue." Live run surfaced $331.8B as MSFT's "quarterly" revenue — its entire fiscal year's total, roughly 4x the real number. Fixed by filtering to quarter-length duration (`<=100` days) *before* selecting the latest `period_end`, so a Q4-only annual disclosure is correctly skipped and the function falls back to the most recent genuinely quarterly fact (Q3) instead — deriving a true Q4 figure via FY-minus-9-months subtraction is a disclosed gap for a later pass, not faked here.

**Files changed**: `src/features/equity_fundamentals.py` (new, ~165 lines), `tests/test_equity_fundamentals.py` (new, 10 tests).

**Live-verified against real production data, not just synthetic tests**: ran `compute_fundamental_features()` against the real NVDA/AAPL/MSFT rows Phase 4 already wrote. Before the Q4 fix, MSFT's figures were visibly wrong (revenue 4x too high, due to the bug above) while NVDA/AAPL happened to look plausible by coincidence (their most recent period_end genuinely had a quarterly fact available) — a reminder that "looks plausible for 2 of 3 real tickers" is not the same as "correct," and the third ticker's anomaly was the one that mattered. After the fix, all three show realistic, internally-consistent growth/margin/leverage/EPS figures (e.g. MSFT: $82.9B quarterly revenue, 18.3% YoY growth, 67.6% gross margin, 38.3% net margin — all in line with real-world expectations).

**Tests**: `tests/test_equity_fundamentals.py`, 10 tests — no-data-means-everything-missing, the real quarterly-vs-YTD same-`period_end` disambiguation, YoY growth (both the happy path and the honest-None-when-no-comparable-quarter-exists path), point-in-time filtering (a filing dated after `as_of` must be invisible), the restatement tie-break fix, margin-missing-when-numerator-absent, leverage ratio from instant metrics, the fiscal-Q4-annual-only-fact fix (locks in the real MSFT bug), and the dual-revenue-tag fallback. Full suite: **194/194 passing** (184 prior + 10 new).

**Execution-impact assessment**: zero — pure computation over already-stored rows, no DB write, no network call, no broker import anywhere in this module.

**Known limitations, disclosed not hidden**:
- No derived Q4 figure (FY minus 9-month YTD) — a company's fiscal Q4 is reported as "missing" rather than computed, consistent with "never fabricate," but means roughly 1-in-4 quarters per company currently has no standalone revenue/margin figure from this function. A credible candidate for a later pass once Phase 9's feature engine defines how it wants to handle derived (vs. directly-filed) values.
- `KEY_METRICS` (from Phase 4) is a small, fixed tag list — companies with materially different statement shapes (banks, REITs, insurers) were not tested and may report under entirely different XBRL tags this function doesn't look for yet.
- No caching/memoization — `compute_fundamental_features` re-scans the full row list on every call; fine at today's per-ticker row counts (~2,000), but a future caller computing features for many (ticker, as_of) pairs in a backtest loop may want a precomputed index.

**Next**: Phase 6 (equity news intelligence) or Phase 7 (relationship graph) — both are natural next consumers; awaiting no further input per "go on until you finish," picking whichever best unblocks Phase 9's feature engine.

---

## Phase 6 — Equity News Intelligence — **DONE**

2026-10-02. Built `src/news/equity_news.py` — ticker-aware news, kept entirely separate from the existing currency-keyed `news_events` path (`src/news/alpha_vantage.py`/`gdelt.py` untouched). Chose Alpaca's own News API (`data.alpaca.markets/v1beta1/news`) over Alpha Vantage for equities: verified live that it works with credentials this project already has (no second key to manage) and has a far more generous rate limit.

**The brief's own Phase 6 correction, implemented as two distinct jobs**: (1) classify a coarse event type via a deliberately simple, disclosed keyword heuristic (`classify_event_type` — EARNINGS/GUIDANCE/M&A/SEC_FILING/DIVIDEND/SPLIT/LEGAL/ANALYST_RATING/PRODUCT, same vocabulary as `company_events.event_type`; `None`, not a forced catch-all, when nothing matches); (2) `compute_reaction()` measures what *actually happened* to price after a headline, using this project's own already-stored candles, never estimated at publish time and never treated as known before enough real time has passed (`_REACTION_MIN_AGE = 27h`, so both the 1h and 1d windows are guaranteed to have genuine data before a row is even considered).

**Files changed**: `src/news/equity_news.py` (new, ~195 lines), `src/scripts/sync_equity_news.py` (new, scheduled entrypoint), `run_sync_equity_news.bat` (new), `tests/test_equity_news.py` (new, 14 tests).

**Live-verified against real production data**: ran the sync script for real — fetched 50 real articles across AAPL/MSFT/NVDA/QQQ (QQQ, confirmed in Phase 4 to have no SEC CIK, still gets real news coverage as an ETF — a useful real-world confirmation that `equity_news` correctly doesn't depend on `equity_entities`/CIK at all). 8 of 50 real headlines/summaries classified correctly (EARNINGS, ANALYST_RATING, PRODUCT, LEGAL all genuinely matched); spot-checked two EARNINGS matches that looked surprising from the headline alone and confirmed both were legitimate — the real article *summary* text contained "earnings" even though the headline didn't, a defensible (if coarse) outcome of a headline+summary keyword match, not a bug.

**Real bug caught by live verification, not a test**: the sync script's own "N newly inserted" log line reported 0 after a run that had genuinely just inserted all 50 real articles — the same psycopg bulk-`executemany` `rowcount` unreliability already documented elsewhere in this project (`src/outcomes/alpaca_tracker.py`'s `rowcount == -1` fix). Fixed by counting rows before/after the insert in the same transaction instead of trusting the driver's reported count — purely a log-accuracy fix (the insert itself was always correct), but a misleading operational log is still worth not shipping. Re-ran afterward and confirmed it then correctly reported 0 new (true idempotency) on a genuine no-new-articles re-run.

**Tests**: `tests/test_equity_news.py`, 14 tests — `classify_event_type` (each category, no-match, and a keyword-priority-ordering case), `_article_to_row` against the real captured Alpaca News shape (including the no-ticker-tag skip case), `AlpacaNewsClient` via `httpx.MockTransport`, and `compute_reaction`/`pending_reaction_rows` against an isolated in-memory engine with synthetic candles (happy path, no-baseline-candle-returns-None, missing-1d-only, age-gating, already-computed exclusion). Full suite: **208/208 passing** (194 prior + 14 new).

**Scheduled, same day**: `run_sync_equity_news.bat` + a new `AIForex_SyncEquityNews` Windows Scheduled Task, every 30 minutes (news changes faster than SEC filings but not every few minutes) — registered with the same proven `-AllowStartIfOnBatteries -StartWhenAvailable -WakeToRun` settings, confirmed correct in the task's own XML, manually triggered, confirmed via real log content.

**Execution-impact assessment**: zero — every network call is a GET (Alpaca's News endpoint, never the trading host), and reaction computation only reads from the already-maintained `candles` table; no broker/order/position code is imported anywhere in this phase.

**Known limitations, disclosed not hidden**:
- `sentiment_score`/`novelty_score`/`confidence` are left `None` — Alpaca's News API doesn't provide sentiment, and this phase deliberately doesn't run an LLM sentiment pass (consistent with "never fabricate"; a credible candidate for a later pass if the brief wants it).
- A multi-ticker article (common — the live "most-searched tickers" roundup touched 12 symbols) has its price reaction computed against only the FIRST listed ticker, since there's no per-article primary-ticker signal to prefer one symbol's reaction over another's — disclosed in the code, not silently treated as precise.
- The keyword classifier is coarse by design — matches against headline+summary combined, so a passing mention (e.g. "...citing valuation, earnings outlook...") can tag an article EARNINGS even when the real subject is an analyst rating. Acceptable for this phase's scope; an ML classifier would be a deliberate, separate upgrade, not an oversight.

**Next**: Phase 7 (equity relationship/knowledge graph) — ticker→company→industry→sector→sector ETF→index→peers, the natural filler for `equity_entities`' still-`None` sector/industry/exchange columns (Phase 4's own disclosed gap).
