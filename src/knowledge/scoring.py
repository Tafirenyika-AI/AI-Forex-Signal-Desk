"""Source-quality scoring (Autonomous Upgrade Spec sec. 4.2).

Explicitly rule-based/heuristic, not LLM-based: this project has no
Anthropic/LLM API key configured (a deliberate earlier choice — see
project_autonomous_upgrade_roadmap memory), so nothing in the automated
ingestion pipeline can call out to one. These heuristics are honest
approximations of the spec's dimensions, not real natural-language
understanding — a keyword match is not the same as actually evaluating an
argument's rigor. Treat `overall_score` as a coarse triage signal (worth
reading vs. probably not), not a certified quality rating.

"Out-of-sample result" (spec's 8th dimension) isn't scored here — it only
applies once a claim has actually been backtested (sec. 14 Strategy Lab),
which happens after ingestion, not at it.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlparse

# Authority: domain-based. Official/institutional sources score highest;
# generic commercial content scores lowest. Spec sec. 4.2 "Authority" +
# "Primary vs secondary" both lean heavily on this signal.
HIGH_AUTHORITY_DOMAINS = (
    ".gov", ".edu", "federalreserve.gov", "fred.stlouisfed.org", "bls.gov",
    "bea.gov", "treasury.gov", "ecb.europa.eu", "bis.org", "imf.org",
    "worldbank.org", "cftc.gov", "oecd.org", "bankofengland.co.uk",
    "boj.or.jp", "bankofcanada.ca", "rba.gov.au", "rbnz.govt.nz", "snb.ch",
    "developer.oanda.com", "oanda.com", "ssrn.com", "arxiv.org", "nber.org",
    "gdeltproject.org",
)
MEDIUM_AUTHORITY_DOMAINS = (
    "reuters.com", "bloomberg.com", "ft.com", "wsj.com", "economist.com",
    "imf.org", "investopedia.com",
)

COMMERCIAL_BIAS_PATTERNS = [
    r"\bsign up\b", r"\bbuy now\b", r"\bour signals?\b", r"\blimited time\b",
    r"\bfree trial\b", r"\bdiscount\b", r"% off\b", r"\baffiliate\b",
    r"\bjoin our (course|community|academy)\b", r"\bguaranteed profits?\b",
    r"\bget rich\b", r"\bdm me\b", r"\btelegram (group|channel)\b",
]
EVIDENCE_PATTERNS = [
    r"\bmethodology\b", r"\bsample period\b", r"\bn\s*=\s*\d+\b",
    r"\bregression\b", r"\bstatistically significant\b", r"\bp\s*[<=]\s*0\.\d+\b",
    r"\bdataset\b", r"\bconfidence interval\b", r"\bstandard error\b",
]
PRIMARY_SOURCE_PATTERNS = [
    r"\boriginal (research|dataset|data)\b", r"\bofficial release\b",
    r"\bpress release\b", r"\bstatement\b", r"\bminutes of\b", r"\btranscript\b",
]
SECONDARY_SOURCE_PATTERNS = [
    r"\bsummary\b", r"\brecap\b", r"\broundup\b", r"\baccording to\b",
    r"\breported that\b",
]
TESTABLE_CLAIM_PATTERNS = [
    r"\d+(\.\d+)?\s*%", r"\bcorrelat(es|ion|ed)\b", r"\btends? to\b",
    r"\bhistorically\b", r"\bpredicts?\b", r"\bleads? to\b", r"\bprecedes?\b",
]

RECENCY_HALF_LIFE_DAYS = 730.0  # 2 years — macro/structural research ages slowly


def _keyword_density(text: str, patterns: list[str]) -> float:
    lowered = text.lower()
    hits = sum(len(re.findall(p, lowered)) for p in patterns)
    words = max(len(lowered.split()), 1)
    return min(1.0, hits / max(words / 300, 1))  # normalize per ~300 words


def authority_score(source_domain: str | None) -> float:
    if not source_domain:
        return 0.5  # user-supplied file with no URL — neutral, not penalized
    domain = source_domain.lower()
    if any(d in domain for d in HIGH_AUTHORITY_DOMAINS):
        return 1.0
    if any(d in domain for d in MEDIUM_AUTHORITY_DOMAINS):
        return 0.6
    return 0.3


def primary_vs_secondary_score(text: str) -> float:
    primary = _keyword_density(text, PRIMARY_SOURCE_PATTERNS)
    secondary = _keyword_density(text, SECONDARY_SOURCE_PATTERNS)
    if primary == 0 and secondary == 0:
        return 0.5
    return max(0.0, min(1.0, 0.5 + (primary - secondary)))


def evidence_score(text: str) -> float:
    return max(0.1, _keyword_density(text, EVIDENCE_PATTERNS))


def recency_score(publication_date: datetime | None, now: datetime | None = None) -> float:
    if publication_date is None:
        return 0.5  # unknown — neutral
    now = now or datetime.now(timezone.utc)
    if publication_date.tzinfo is None:
        publication_date = publication_date.replace(tzinfo=timezone.utc)
    age_days = max((now - publication_date).total_seconds() / 86400.0, 0.0)
    return 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)


def commercial_bias_score(text: str) -> float:
    """Higher = less commercial bias (i.e. more trustworthy)."""
    density = _keyword_density(text, COMMERCIAL_BIAS_PATTERNS)
    return max(0.0, 1.0 - density * 3)  # even light promotional language costs real points


def testability_score(text: str) -> float:
    return _keyword_density(text, TESTABLE_CLAIM_PATTERNS)


def score_document(
    text: str, source_domain: str | None, publication_date: datetime | None
) -> dict[str, float]:
    scores = {
        "authority": authority_score(source_domain),
        "primary_vs_secondary": primary_vs_secondary_score(text),
        "evidence": evidence_score(text),
        "recency": recency_score(publication_date),
        "commercial_bias": commercial_bias_score(text),
        "testability": testability_score(text),
    }
    # Authority and commercial-bias carry the most weight — an authoritative,
    # non-promotional source is worth reading even with weak evidence
    # density; a promotional source stays low-trust even if it happens to
    # use technical-sounding language.
    weights = {
        "authority": 0.30, "primary_vs_secondary": 0.15, "evidence": 0.20,
        "recency": 0.10, "commercial_bias": 0.20, "testability": 0.05,
    }
    overall = sum(scores[k] * weights[k] for k in weights)
    scores["overall"] = round(overall, 4)
    return scores


def extract_domain(source_url: str | None) -> str | None:
    if not source_url:
        return None
    try:
        return urlparse(source_url).netloc or None
    except ValueError:
        return None
