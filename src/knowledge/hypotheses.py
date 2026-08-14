"""Candidate hypothesis / topic extraction (Autonomous Upgrade Spec sec. 5.1).

Same honesty note as scoring.py: this is pattern matching, not language
understanding. It flags sentences that *look* like empirical claims
(numeric, causal, or correlational language) as candidates for the research
queue (spec sec. 14 Strategy Lab) — a human, or a future LLM-backed pass,
still has to actually read and formalize them before they mean anything.
"""
from __future__ import annotations

import re

HYPOTHESIS_PATTERNS = [
    r"[^.]*\btends? to\b[^.]*\.",
    r"[^.]*\bhistorically\b[^.]*\.",
    r"[^.]*\bcorrelat(es|ion|ed)\b[^.]*\.",
    r"[^.]*\bpredicts?\b[^.]*\.",
    r"[^.]*\bleads? to\b[^.]*\.",
    r"[^.]*\bprecedes?\b[^.]*\.",
    r"[^.]*\d+(\.\d+)?\s*%[^.]*\.",
]

TOPIC_KEYWORDS = {
    "central_banks": ["fomc", "federal reserve", "ecb", "central bank", "monetary policy", "rate decision"],
    "macroeconomics": ["gdp", "inflation", "cpi", "unemployment", "payrolls", "labor market"],
    "market_microstructure": ["spread", "liquidity", "order book", "microstructure", "slippage"],
    "risk_management": ["drawdown", "position sizing", "risk management", "stop loss", "value at risk"],
    "execution": ["order execution", "market order", "limit order", "broker", "latency"],
    "technical_and_quant_methods": ["regression", "time series", "backtest", "machine learning", "model"],
    "positioning": ["commitments of traders", "cot report", "net positioning", "crowded"],
}


def extract_candidate_hypotheses(text: str, max_candidates: int = 15) -> list[str]:
    candidates: list[str] = []
    for pattern in HYPOTHESIS_PATTERNS:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            sentence = match.group(0).strip()
            if 30 <= len(sentence) <= 400 and sentence not in candidates:
                candidates.append(sentence)
            if len(candidates) >= max_candidates:
                return candidates
    return candidates


def classify_topic(text: str) -> str | None:
    lowered = text.lower()
    scores = {topic: sum(lowered.count(kw) for kw in kws) for topic, kws in TOPIC_KEYWORDS.items()}
    best_topic = max(scores, key=scores.get)
    return best_topic if scores[best_topic] > 0 else None
