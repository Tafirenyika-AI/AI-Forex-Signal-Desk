from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
)

metadata = MetaData()

# --- users / invitations / sessions / user_oanda_accounts / user_preferences:
# invite-only multi-user auth. Password hashing (bcrypt) and OANDA-token
# encryption (Fernet) live in src/auth/crypto.py, never in this file — this
# module only defines shape, never touches a plaintext secret. Registration
# is invite-only: a `users` row is only ever created once someone completes
# registration through a valid, unexpired, unused `invitations` token — there
# is no "invited but not yet registered" user row; that pending state is
# represented purely by an invitations row with used_at IS NULL. ---
users = Table(
    "users",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("email", String, nullable=False, unique=True, index=True),
    Column("username", String, nullable=False, unique=True, index=True),
    Column("password_hash", String, nullable=False),
    Column("is_admin", Boolean, nullable=False, default=False),
    Column("status", String, nullable=False, default="active"),  # active / disabled
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

invitations = Table(
    "invitations",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("email", String, nullable=False, index=True),
    Column("token", String, nullable=False, unique=True, index=True),
    Column("invited_by_user_id", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("used_at", DateTime(timezone=True), nullable=True),
    Column("used_by_user_id", Integer, nullable=True),
)

sessions = Table(
    "sessions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("session_token", String, nullable=False, unique=True, index=True),
    Column("user_id", Integer, nullable=False, index=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
)

# encrypted_api_token is Fernet ciphertext (src/auth/crypto.py), never the
# raw OANDA bearer token — the decryption key lives only in .env
# (AUTH_ENCRYPTION_KEY), never in this database, so a stolen forex.db file
# alone can't recover anyone's broker credentials.
user_oanda_accounts = Table(
    "user_oanda_accounts",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, nullable=False, unique=True, index=True),
    Column("encrypted_api_token", Text, nullable=False),
    Column("oanda_account_id", String, nullable=False),
    Column("oanda_environment", String, nullable=False, default="practice"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

user_preferences = Table(
    "user_preferences",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, nullable=False, unique=True, index=True),
    Column("instrument_list_json", Text, nullable=True),  # NULL = full curated OANDA universe
    Column("execution_mode_default", String, nullable=False, default="demo"),
    Column("auto_execute", Boolean, nullable=False, default=False),
    Column("onboarding_complete", Boolean, nullable=False, default=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


# --- market_prices: timestamped bid/ask/mid/spread by instrument (blueprint sec. 12) ---
market_prices = Table(
    "market_prices",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("instrument", String, nullable=False, index=True),
    Column("time", DateTime(timezone=True), nullable=False, index=True),
    Column("bid", Float, nullable=False),
    Column("ask", Float, nullable=False),
    Column("ingested_at", DateTime(timezone=True), nullable=False),
)

# --- candles: OHLC by instrument/granularity ---
candles = Table(
    "candles",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("instrument", String, nullable=False, index=True),
    Column("granularity", String, nullable=False, index=True),
    Column("time", DateTime(timezone=True), nullable=False, index=True),
    Column("open", Float, nullable=False),
    Column("high", Float, nullable=False),
    Column("low", Float, nullable=False),
    Column("close", Float, nullable=False),
    Column("volume", Integer, nullable=False),
    Column("complete", Boolean, nullable=False),
    UniqueConstraint("instrument", "granularity", "time", name="uq_candle"),
)

# --- positions_snapshots: periodic broker-reconciled exposure/equity ---
positions_snapshots = Table(
    "positions_snapshots",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("time", DateTime(timezone=True), nullable=False, index=True),
    Column("account_id", String, nullable=False),
    Column("balance", Float, nullable=False),
    Column("nav", Float, nullable=False),
    Column("unrealized_pl", Float, nullable=False),
    Column("margin_used", Float, nullable=False),
    Column("margin_available", Float, nullable=False),
    Column("open_trade_count", Integer, nullable=False),
    Column("open_position_count", Integer, nullable=False),
)


# --- economic_events: scheduled macro releases, point-in-time (blueprint sec. 12) ---
economic_events = Table(
    "economic_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("event_time", DateTime(timezone=True), nullable=False, index=True),
    Column("ingested_at", DateTime(timezone=True), nullable=False),
    Column("country", String, nullable=False),
    Column("currency", String, nullable=False, index=True),
    Column("event_name", String, nullable=False),
    Column("source", String, nullable=False),
    # consensus is nullable: FRED/BLS/ECB give actual+revisions, not analyst consensus.
    # Only a paid calendar (e.g. Trading Economics) can populate consensus reliably.
    Column("consensus", Float, nullable=True),
    Column("previous", Float, nullable=True),
    Column("actual", Float, nullable=True),
    Column("revised", Float, nullable=True),
    Column("importance", String, nullable=True),
    Column("unit", String, nullable=True),
    UniqueConstraint("event_time", "currency", "event_name", "source", name="uq_econ_event"),
)

# --- news_events: publish time, source, entities, sentiment, novelty ---
news_events = Table(
    "news_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("publish_time", DateTime(timezone=True), nullable=False, index=True),
    Column("ingest_time", DateTime(timezone=True), nullable=False),
    Column("source", String, nullable=False),
    Column("headline", String, nullable=False),
    Column("url", String, nullable=True),
    Column("currencies", String, nullable=False),  # comma-separated
    Column("event_type", String, nullable=True),
    Column("sentiment_score", Float, nullable=True),  # -1..1
    Column("novelty_score", Float, nullable=True),  # 0..1
    Column("confidence", Float, nullable=True),  # 0..1
    UniqueConstraint("url", name="uq_news_url"),
)

# --- features: point-in-time feature vectors keyed by timestamp/horizon ---
features = Table(
    "features",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("instrument", String, nullable=False, index=True),
    Column("horizon", String, nullable=False, index=True),
    Column("time", DateTime(timezone=True), nullable=False, index=True),
    Column("feature_json", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("instrument", "horizon", "time", name="uq_feature_vector"),
)

# --- predictions: per-component and final calibrated outputs ---
predictions = Table(
    "predictions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("time", DateTime(timezone=True), nullable=False, index=True),
    Column("instrument", String, nullable=False, index=True),
    Column("horizon", String, nullable=False),
    Column("component", String, nullable=False),  # price / macro / news / regime / meta
    Column("p_up", Float, nullable=True),
    Column("expected_move", Float, nullable=True),
    Column("confidence", Float, nullable=True),
    Column("raw_json", Text, nullable=True),
)

# --- trade_intents: proposed action before risk approval (decision object, sec. 5.2) ---
trade_intents = Table(
    "trade_intents",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, nullable=True, index=True),  # nullable: pre-multiuser rows backfilled to the admin
    Column("time", DateTime(timezone=True), nullable=False, index=True),
    Column("instrument", String, nullable=False, index=True),
    Column("action", String, nullable=False),  # BUY / SELL / NO_TRADE
    Column("confidence", Float, nullable=False),
    Column("horizon", String, nullable=False),
    Column("regime", String, nullable=True),
    Column("key_drivers_json", Text, nullable=True),
    Column("contrary_evidence_json", Text, nullable=True),
    Column("entry_condition", String, nullable=True),
    Column("invalidation", String, nullable=True),
    Column("stop_distance", Float, nullable=True),
    Column("take_profit_distance", Float, nullable=True),
    Column("target_logic", String, nullable=True),
    Column("data_freshness_json", Text, nullable=True),
    Column("explanation", Text, nullable=True),
    # PROPOSED -> RISK_REJECTED | AWAITING_AUTHORIZATION -> AUTHORIZED_EXECUTED
    # | REJECTED_BY_USER | EXECUTION_FAILED | EXPIRED
    Column("status", String, nullable=False, default="PROPOSED"),
    Column("execution_mode", String, nullable=True),  # paper / demo; set once risk-approved
    Column("reference_price", Float, nullable=True),  # mid price when the signal was generated
)

# --- risk_decisions: approved/rejected plus reason and limits ---
risk_decisions = Table(
    "risk_decisions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, nullable=True, index=True),
    Column("trade_intent_id", Integer, nullable=False, index=True),
    Column("time", DateTime(timezone=True), nullable=False, index=True),
    Column("approved", Boolean, nullable=False),
    Column("reason", String, nullable=False),
    Column("size_units", Integer, nullable=True),
    Column("gates_json", Text, nullable=True),
)

# --- orders_fills: broker request/response, fill, slippage, status ---
orders_fills = Table(
    "orders_fills",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, nullable=True, index=True),
    Column("time", DateTime(timezone=True), nullable=False, index=True),
    Column("client_order_id", String, nullable=False, unique=True),
    Column("broker_order_id", String, nullable=True),
    Column("broker_transaction_id", String, nullable=True),
    Column("instrument", String, nullable=False),
    Column("units", Integer, nullable=False),
    Column("status", String, nullable=False),
    Column("fill_price", Float, nullable=True),
    Column("spread", Float, nullable=True),
    Column("latency_ms", Float, nullable=True),
    Column("execution_mode", String, nullable=False),  # paper / demo
    Column("raw_json", Text, nullable=True),
)

# --- model_registry: model version, training period, validation stats, deployment status ---
model_registry = Table(
    "model_registry",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String, nullable=False),
    Column("version", String, nullable=False),
    Column("trained_at", DateTime(timezone=True), nullable=False),
    Column("train_start", DateTime(timezone=True), nullable=True),
    Column("train_end", DateTime(timezone=True), nullable=True),
    Column("validation_json", Text, nullable=True),
    Column("deployed", Boolean, nullable=False, default=False),
    UniqueConstraint("name", "version", name="uq_model_version"),
)


# --- authorizations: human sign-off audit log (sec. 1 core principle applied to
# execution itself, not just the model's own BUY/SELL/NO_TRADE) ---
authorizations = Table(
    "authorizations",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, nullable=True, index=True),
    Column("trade_intent_id", Integer, nullable=False, index=True),
    Column("decision", String, nullable=False),  # APPROVED / REJECTED
    Column("authorized_by", String, nullable=False, default="user"),
    Column("authorized_at", DateTime(timezone=True), nullable=False),
    Column("notes", String, nullable=True),
    Column("resulting_client_order_id", String, nullable=True),
)


# --- paper broker ledger, persisted so PaperBroker state survives across
# short-lived process invocations (each dashboard action is its own asyncio.run()
# with its own event loop — an in-memory-only ledger would reset every click) ---
paper_account_state = Table(
    "paper_account_state",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, nullable=False, unique=True, index=True),
    Column("starting_balance", Float, nullable=False),
    Column("realized_pl", Float, nullable=False, default=0.0),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

# instrument was the sole PK pre-multiuser (one paper ledger, one account) —
# now a composite PK, since two users can each hold their own paper
# position in the same instrument without colliding.
paper_positions = Table(
    "paper_positions",
    metadata,
    Column("user_id", Integer, primary_key=True),
    Column("instrument", String, primary_key=True),
    Column("units", Integer, nullable=False),
    Column("avg_price", Float, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

# client_order_id is already a UUID-derived string, globally unique
# regardless of user — user_id here is for querying/display, not dedup.
paper_order_ids_seen = Table(
    "paper_order_ids_seen",
    metadata,
    Column("client_order_id", String, primary_key=True),
    Column("user_id", Integer, nullable=True, index=True),
    Column("seen_at", DateTime(timezone=True), nullable=False),
)


# --- market_indicators: cross-market confirmation inputs (blueprint sec. 6,
# row 5 — rates/yields, dollar-index proxy, commodities) ---
market_indicators = Table(
    "market_indicators",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("indicator", String, nullable=False, index=True),
    Column("observation_date", DateTime(timezone=True), nullable=False, index=True),
    Column("value", Float, nullable=False),
    Column("source", String, nullable=False),
    Column("ingested_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("indicator", "observation_date", "source", name="uq_market_indicator"),
)


# --- trade_outcomes: realized results, linked back to the trade_intent that
# caused them — this is what meta-model training (src/models/train_meta_model.py)
# learns from ---
trade_outcomes = Table(
    "trade_outcomes",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, nullable=True, index=True),
    Column("trade_intent_id", Integer, nullable=True, index=True),  # nullable: link may be unresolvable
    Column("client_order_id", String, nullable=True, index=True),
    Column("broker_trade_id", String, nullable=False),
    Column("execution_mode", String, nullable=False),  # paper / demo
    Column("instrument", String, nullable=False),
    Column("action", String, nullable=False),  # BUY / SELL
    Column("units", Integer, nullable=False),
    Column("entry_price", Float, nullable=False),
    Column("exit_price", Float, nullable=False),
    Column("realized_pl_usd", Float, nullable=False),
    Column("opened_at", DateTime(timezone=True), nullable=True),
    Column("closed_at", DateTime(timezone=True), nullable=False),
    Column("outcome", String, nullable=False),  # WIN / LOSS / BREAKEVEN
    Column("synced_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("broker_trade_id", "execution_mode", name="uq_trade_outcome"),
)


# --- risk_state: per-user kill-switch / daily-loss tracking state, one row
# per (user, day). Unique constraint was `day` alone pre-multiuser (a true
# global singleton per day) — now `(user_id, day)`, since each user's kill
# switch and daily P/L baseline are independent. ---
risk_state = Table(
    "risk_state",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, nullable=False, index=True),
    Column("day", String, nullable=False),  # YYYY-MM-DD UTC
    Column("day_start_balance", Float, nullable=False),
    Column("kill_switch_active", Boolean, nullable=False, default=False),
    Column("kill_switch_reason", String, nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("user_id", "day", name="uq_risk_state_user_day"),
)

# --- risk_state_weekly: same shape as risk_state, one row per (user, ISO
# week) (Autonomous Upgrade Spec sec. 17: "Daily/weekly loss limits"). ---
risk_state_weekly = Table(
    "risk_state_weekly",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, nullable=False, index=True),
    Column("iso_week", String, nullable=False),  # e.g. "2026-W33"
    Column("week_start_balance", Float, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("user_id", "iso_week", name="uq_risk_state_weekly_user_week"),
)


# --- knowledge_documents / knowledge_chunks: the RAG knowledge library
# (Autonomous Upgrade Spec sec. 4-5). Source-quality scoring is rule-based
# (src/knowledge/scoring.py) — no LLM key is configured for this project, so
# nothing here can call out to one; see project_autonomous_upgrade_roadmap
# memory for why that's a deliberate choice, not a gap. ---
knowledge_documents = Table(
    "knowledge_documents",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("filepath", String, nullable=False, unique=True),
    Column("sha256", String, nullable=False, index=True),
    Column("title", String, nullable=True),
    Column("source_domain", String, nullable=True),
    Column("document_type", String, nullable=True),  # pdf/docx/txt/md/html
    Column("category", String, nullable=True),  # which forex_knowledge/ subfolder
    Column("publication_date", DateTime(timezone=True), nullable=True),
    Column("ingested_at", DateTime(timezone=True), nullable=False),
    Column("source_score_json", Text, nullable=True),  # sec. 4.2 dimensions
    Column("overall_score", Float, nullable=True),  # 0..1, aggregate of source_score_json
    Column("novelty_score", Float, nullable=True),  # vs existing indexed corpus at ingest time
    Column("chunk_count", Integer, nullable=False, default=0),
    Column("candidate_hypotheses_json", Text, nullable=True),
    UniqueConstraint("sha256", name="uq_knowledge_doc_hash"),
)

knowledge_chunks = Table(
    "knowledge_chunks",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("document_id", Integer, nullable=False, index=True),
    Column("chunk_index", Integer, nullable=False),
    Column("text", Text, nullable=False),
    Column("topic", String, nullable=True),  # rule-based topic classification
    UniqueConstraint("document_id", "chunk_index", name="uq_knowledge_chunk"),
)


# --- economic_surprises: matched FRED actual x Forex Factory consensus,
# plus measured price reaction (Autonomous Upgrade Spec sec. 7). Not yet
# wired into the live decision weight — src/models/surprise_engine.py and
# src/models/market_reaction.py are real and tested, but validating whether
# this actually improves decisions is P9's job (champion/challenger), not
# assumed here. ---
economic_surprises = Table(
    "economic_surprises",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("currency", String, nullable=False, index=True),
    Column("fred_event_name", String, nullable=False),
    Column("ff_event_name", String, nullable=False),
    Column("actual", Float, nullable=False),
    Column("comparable_actual", Float, nullable=False),
    Column("consensus", Float, nullable=False),
    Column("previous", Float, nullable=False),
    Column("surprise_vs_consensus", Float, nullable=False),
    Column("surprise_vs_previous", Float, nullable=False),
    Column("fred_event_time", DateTime(timezone=True), nullable=False),
    Column("ff_event_time", DateTime(timezone=True), nullable=False, index=True),
    Column("ff_importance", String, nullable=True),
    Column("price_reaction_60min", Float, nullable=True),
    Column("mismatch", String, nullable=True),
    Column("computed_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("currency", "fred_event_name", "ff_event_time", name="uq_economic_surprise"),
)


# --- central_bank_statements: archive + diff + tone (Autonomous Upgrade
# Spec sec. 9). Speaker-level baseline deviation isn't modeled yet — needs
# more accumulated history than exists on day one, same honest scoping as
# the meta-model's MIN_SAMPLES gate; dissent extraction below is a
# lightweight first step, not that full model. ---
central_bank_statements = Table(
    "central_bank_statements",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("central_bank", String, nullable=False, index=True),
    Column("statement_type", String, nullable=False),  # e.g. "FOMC statement", "Minutes"
    Column("title", String, nullable=False),
    Column("url", String, nullable=False, unique=True),
    Column("published_at", DateTime(timezone=True), nullable=False, index=True),
    Column("full_text", Text, nullable=False),
    Column("hawkish_dovish_tone", Float, nullable=True),
    Column("tone_shift_vs_previous", Float, nullable=True),
    Column("change_ratio_vs_previous", Float, nullable=True),
    Column("policy_regime", String, nullable=True),
    Column("dissenting_members_json", Text, nullable=True),
    Column("ingested_at", DateTime(timezone=True), nullable=False),
)


# --- cftc_positioning: weekly Commitments of Traders net positioning per
# currency (Autonomous Upgrade Spec sec. 6, sec. 10) ---
cftc_positioning = Table(
    "cftc_positioning",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("currency", String, nullable=False, index=True),
    Column("report_date", DateTime(timezone=True), nullable=False, index=True),
    Column("noncomm_long", Float, nullable=False),
    Column("noncomm_short", Float, nullable=False),
    Column("net_position", Float, nullable=False),
    Column("open_interest", Float, nullable=False),
    Column("ingested_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("currency", "report_date", name="uq_cftc_positioning"),
)


# --- trade_attribution: automated post-trade attribution (Autonomous
# Upgrade Spec sec. 13: "which assumptions were correct, which failed, and
# whether the loss was model error, execution error, noise, regime change
# or unavoidable event risk"). Rule-based classification, same honesty
# posture as source-quality scoring — no ML classifier behind this, and the
# module docstring (src/memory/attribution.py) is explicit about what each
# rule actually checks. One row per trade_outcome, computed once. ---
trade_attribution = Table(
    "trade_attribution",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, nullable=True, index=True),
    Column("trade_outcome_id", Integer, nullable=False, unique=True, index=True),
    Column("trade_intent_id", Integer, nullable=True, index=True),
    Column("r_multiple", Float, nullable=True),  # realized_pl_usd / dollar risk at entry; None if stop_distance unknown
    Column("primary_reason", String, nullable=False),  # model_correct/model_error/execution_error/event_risk/regime_change/noise
    Column("contributing_factors_json", Text, nullable=True),  # every rule that fired, with its evidence, not just the winner
    Column("computed_at", DateTime(timezone=True), nullable=False),
)

# --- trade_analogs: "before a new trade, retrieve statistically similar
# historical situations and compare outcomes" (Autonomous Upgrade Spec sec.
# 13, last bullet). Computed once per trade_intent at signal time (matching
# instrument+regime+action, falling back to regime+action across instruments
# if too few same-instrument samples — same self-relative sample-size
# handling philosophy as crowding_score/regime detection elsewhere in this
# project) and logged alongside it — informational only, never used to
# widen a risk limit (sec. 8's "model may never increase its own risk
# limit" applies here too; this is visibility, not a new gate). ---
trade_analogs = Table(
    "trade_analogs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, nullable=True, index=True),
    Column("trade_intent_id", Integer, nullable=False, unique=True, index=True),
    Column("basis", String, nullable=False),  # instrument_regime_action / regime_action_fallback / insufficient_history
    Column("matched_count", Integer, nullable=False),
    Column("win_rate", Float, nullable=True),
    Column("avg_r_multiple", Float, nullable=True),
    Column("avg_pl_usd", Float, nullable=True),
    Column("computed_at", DateTime(timezone=True), nullable=False),
)


# --- challenger_decisions / signal_evaluations / strategy_graveyard:
# Champion/Challenger framework (Autonomous Upgrade Spec sec. 14-15).
# Challengers receive the exact same market snapshot as the champion every
# cycle (src/challengers/definitions.py) and run fully in shadow — fuse()
# is called, but the result never reaches the risk governor or a broker.
# signal_evaluations scores BOTH the champion's real trade_intents and every
# challenger's shadow decisions with the identical methodology (does actual
# subsequent price move match the action's direction by the time the
# horizon elapses — src/scripts/evaluate_challengers.py), so they're
# comparable on equal footing. strategy_graveyard is where a challenger
# goes once it's collected enough samples (MIN_SAMPLES) to judge and hasn't
# beaten the champion — logged with a reason so run_loop.py can skip it on
# future cycles instead of "repeatedly rediscovering" a losing idea (spec's
# own phrase, sec. 14). Nothing here is ever auto-promoted to live
# trading — same posture as the meta-model's MIN_SAMPLES gate. ---
challenger_decisions = Table(
    "challenger_decisions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, nullable=True, index=True),
    Column("trade_intent_id", Integer, nullable=False, index=True),  # the champion cycle this ran alongside
    Column("challenger_name", String, nullable=False, index=True),
    Column("instrument", String, nullable=False),
    Column("horizon", String, nullable=False),
    Column("action", String, nullable=False),
    Column("confidence", Float, nullable=False),
    Column("reference_price", Float, nullable=False),
    Column("key_drivers_json", Text, nullable=True),
    Column("computed_at", DateTime(timezone=True), nullable=False, index=True),
    UniqueConstraint("trade_intent_id", "challenger_name", name="uq_challenger_decision"),
)

signal_evaluations = Table(
    "signal_evaluations",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, nullable=True, index=True),
    Column("source", String, nullable=False, index=True),  # "champion" or a challenger_name
    Column("trade_intent_id", Integer, nullable=False, index=True),
    Column("instrument", String, nullable=False),
    Column("horizon", String, nullable=False),
    Column("action", String, nullable=False),
    Column("signal_time", DateTime(timezone=True), nullable=False),
    Column("reference_price", Float, nullable=False),
    Column("expiry_price", Float, nullable=False),
    Column("hit", Boolean, nullable=False),
    Column("move_in_favor", Float, nullable=False),  # signed price move, positive = direction favored the action
    Column("evaluated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("source", "trade_intent_id", name="uq_signal_evaluation"),
)

strategy_graveyard = Table(
    "strategy_graveyard",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("strategy_name", String, nullable=False, unique=True),
    Column("reason", Text, nullable=False),
    Column("sample_size", Integer, nullable=False),
    Column("challenger_hit_rate", Float, nullable=True),
    Column("champion_hit_rate", Float, nullable=True),
    Column("buried_at", DateTime(timezone=True), nullable=False),
)


# --- promotion_gate_snapshots: periodic record of src/evaluation/
# promotion_gates.py's report (Autonomous Upgrade Spec sec. 18). Spec's
# own phase table requires evidence "across many trades, multiple market
# regimes and major scheduled events" — a single point-in-time report
# can't show that trend, only a history of them can, hence storing a
# snapshot each time rather than only ever computing on demand. ---
promotion_gate_snapshots = Table(
    "promotion_gate_snapshots",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, nullable=True, index=True),
    Column("current_phase", String, nullable=False),
    Column("target_phase", String, nullable=False),
    Column("ready_for_promotion", Boolean, nullable=False),
    Column("criteria_json", Text, nullable=False),
    Column("computed_at", DateTime(timezone=True), nullable=False, index=True),
)


def get_engine(db_path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    metadata.create_all(engine)
    return engine
