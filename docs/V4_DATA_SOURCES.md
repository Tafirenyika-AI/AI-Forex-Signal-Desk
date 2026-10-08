# V4 Phase 0 — Required/Optional API Integrations & Costs

Source brief: Section 22 deliverable #6 ("A list of required and optional API integrations with estimated costs") and Section 19's instruction to "report subscription and API costs before introducing paid services." Pricing below was checked live via web search on 2026-10-08, not recalled from memory, and is cited — re-verify directly on each provider's own pricing page before committing any real spend, since these change.

## Already integrated, already free, already in use — no action needed

| Source | What it provides | Cost | Status |
|---|---|---|---|
| Alpaca (Basic/free tier) | Paper trading, account/positions/orders, IEX-only real-time equity quotes, full crypto data, historical bars | **$0** | Live, in use since Equity V2. This account is confirmed (by this session's own code read) to be on the free Basic tier — recent-data requests against the paid SIP feed return a real 403, which is why `feed="iex"` is used explicitly. |
| Alpaca News API | Ticker-aware news articles | **$0**, bundled with any Alpaca account regardless of tier | Live, in use since Equity V2 Phase 6 (50+ real articles fetched and classified). |
| SEC EDGAR (XBRL company-facts API) | Official structured fundamentals, point-in-time | **$0**, no key required, fair-access User-Agent policy only | Live, in use since Equity V2 Phase 4 (6,200+ real rows). |
| FRED (St. Louis Fed) | Treasury yields, macro series | **$0** with a free API key (already configured) | Live, pre-dates Equity V2. |

## Required for V4 sections as currently specified, with a real cost

| Source | Needed for | Cost (verified live 2026-10-08) | Recommendation |
|---|---|---|---|
| **Alpaca "Algo Trader Plus" plan** | Section 4's SIP (consolidated-tape) market data, and — separately but on the same plan — **corporate actions data** (splits/dividends), needed for Section 14/15's realistic backtesting ask | **$99/month** (per Alpaca's own current pricing; a legacy $9/month rate exists only for accounts grandfathered in before a 2026 price increase — this account is not on that legacy rate) [Alpaca Data pricing](https://alpaca.markets/data) | **Defer.** The existing system already runs correctly on free IEX data with an honest, disclosed limitation (never mislabeled as consolidated volume — confirmed by this audit). Corporate-actions awareness (Section 15) can be scoped around free alternatives first (e.g. detecting a split via a sudden, exact-ratio price/volume discontinuity, or a free source like Alpaca's own `/v2/corporate_actions` if it turns out to be included at the free tier for historical-only queries — **not yet confirmed either way, verify directly before assuming it needs the paid tier**). Revisit only if a specific V4 feature genuinely cannot proceed without it. |
| **TradingView paid plan** (Essential or higher) | Section 17's entire TradingView webhook integration | **Essential $12.95/mo, Plus $24.95/mo, Premium $49.95/mo, Ultimate $199.95/mo** (billed annually; webhooks are NOT available on TradingView's free plan) [pricing summary](https://www.tv-hub.org/guide/tradingview-alerts-setup) | **Explicitly the lowest priority in the whole brief** (Section 17 is its own, clearly-marked-optional, last-numbered section; `V4_TRADINGVIEW_ENABLED=false` by default). Build the RECEIVER (webhook endpoint, validation, dedup) at $0 cost first — it can be fully built and tested with synthetic/manually-sent webhook payloads without ever paying for a TradingView plan. Only subscribe once the receiver is built and the user actually wants to send real alerts from a real TradingView chart. |

## Optional, not currently needed, no immediate recommendation to add

| Source | Would help with | Notes |
|---|---|---|
| A real economic-calendar-with-consensus provider (e.g. Trading Economics) | A true "surprise vs. consensus" macro signal (FRED gives actuals/revisions only, never analyst consensus — a pre-existing, already-disclosed limitation of this project's macro component, unrelated to V4) | Not requested anywhere in the V4 brief; flagged only because it's a known pre-existing gap this session's audit happened to notice again. Not a V4 priority. |
| A licensed GICS sector/industry classification feed | Would replace Equity V2 Phase 7's own disclosed SIC-to-sector *approximation* with the real Wall Street standard | Real cost unresearched — not requested by V4, no action needed unless sector-accuracy becomes a measured problem. |
| An options-chain data provider | V4 doesn't ask for options trading; Alpaca's own OPRA options data is bundled into the same $99/mo Algo Trader Plus tier if ever needed | Out of scope — V4 Section 1 scopes this platform to equities/ETFs/crypto, not options. |

## Zero-cost, non-API costs worth naming

- **Academic literature for the Strategy Evidence Registry (Section 7)**: reading and citing publicly-available papers (Jegadeesh & Titman, Moskowitz/Ooi/Pedersen, etc.) is free — many of the brief's named papers have free SSRN/NBER working-paper versions. No subscription needed to start the registry.
- **Compute**: everything in this project already runs on the user's own machine (Windows Task Scheduler) plus a Neon Postgres free/low tier (already in use) — no new compute cost anticipated for Phase 0–6 (research/shadow) work. Phase 7 (controlled paper execution) doesn't change this either, since Alpaca paper trading itself is free.

## Bottom line for Phase 0

**No new paid service is required to begin V4 Phases 1–6** (market intelligence extensions, strategy research, adaptive AI, learning/performance, dashboard, shadow evaluation) using only the free sources already integrated. The two paid options above (Alpaca SIP+corporate-actions, TradingView webhooks) are each tied to a specific, deferrable V4 ask — neither blocks getting started, and both should wait for an explicit decision closer to the point they're actually needed, consistent with the brief's own "report costs before introducing paid services" instruction.

### Sources
- [Alpaca — Unlimited Access, Real-time Market Data API](https://alpaca.markets/data)
- [Price of unlimited plan — Alpaca Community Forum](https://forum.alpaca.markets/t/price-of-unlimited-plan/9957)
- [What Are the True Costs and Fees of Using the Alpaca Trading API?](https://trading-strategies.academy/archives/46666)
- [TradingView Alerts Setup: Free Plan Limits (2026)](https://www.tv-hub.org/guide/tradingview-alerts-setup)
- [TradingView Plan You Need For Webhook Automated Trading](https://blog.pickmytrade.trade/tradingview-plan-you-need-for-webhook-automated-trading/)
