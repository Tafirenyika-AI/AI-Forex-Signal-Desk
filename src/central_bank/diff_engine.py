"""Statement diffing and tone scoring (Autonomous Upgrade Spec sec. 9):
"Diff new statements against the previous statement sentence-by-sentence.
Score semantic changes rather than only absolute hawkish/dovish tone."

Same honesty note as every rule-based component in this project: sentence
splitting is regex-based (not a real NLP tokenizer) and hawkish/dovish
scoring is keyword density (not language understanding) — a coarse,
transparent proxy, not a claim of genuinely reading the statement.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

HAWKISH_KEYWORDS = [
    "restrictive", "further tightening", "elevated inflation", "vigilant",
    "upside risks to inflation", "raise rates", "rate increase", "firmly committed",
    "additional policy firming", "inflation remains elevated", "tighten",
]
DOVISH_KEYWORDS = [
    "accommodative", "downside risks", "patient", "cut rates", "rate cut",
    "ample reserves", "support the economy", "softening", "moderating",
    "lower rates", "ease", "easing",
]


@dataclass(frozen=True)
class StatementDiff:
    added_sentences: list[str]
    removed_sentences: list[str]
    unchanged_count: int
    change_ratio: float  # 0..1 — how much of the statement changed
    old_tone: float  # -1 (dovish) .. +1 (hawkish)
    new_tone: float
    tone_shift: float  # new_tone - old_tone


# Common abbreviations that would otherwise be misread as sentence
# boundaries by the period-then-capital heuristic below (e.g. "2:00 p.m.
# EDT" -> split after "p.m."). A well-known, cheap fix for naive splitters
# — not a substitute for real sentence tokenization, just enough to stop
# the most common false positives in this specific document type.
_ABBREVIATIONS = ["p.m.", "a.m.", "u.s.", "mr.", "ms.", "dr.", "vs.", "no.", "st."]


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    for abbr in _ABBREVIATIONS:
        text = re.sub(re.escape(abbr), abbr.replace(".", "․"), text, flags=re.IGNORECASE)
    raw = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text)
    return [s.strip().replace("․", ".") for s in raw if len(s.strip()) > 15]


def hawkish_dovish_score(text: str) -> float:
    lowered = text.lower()
    hawk = sum(lowered.count(kw) for kw in HAWKISH_KEYWORDS)
    dove = sum(lowered.count(kw) for kw in DOVISH_KEYWORDS)
    if hawk == 0 and dove == 0:
        return 0.0
    return max(-1.0, min(1.0, (hawk - dove) / (hawk + dove)))


def diff_statements(old_text: str, new_text: str) -> StatementDiff:
    old_sentences = split_sentences(old_text)
    new_sentences = split_sentences(new_text)

    matcher = SequenceMatcher(None, old_sentences, new_sentences, autojunk=False)
    added, removed, unchanged = [], [], 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            unchanged += i2 - i1
        elif tag == "replace":
            removed.extend(old_sentences[i1:i2])
            added.extend(new_sentences[j1:j2])
        elif tag == "delete":
            removed.extend(old_sentences[i1:i2])
        elif tag == "insert":
            added.extend(new_sentences[j1:j2])

    total_unique = unchanged + len(added) + len(removed)
    change_ratio = (len(added) + len(removed)) / total_unique if total_unique else 0.0

    old_tone = hawkish_dovish_score(old_text)
    new_tone = hawkish_dovish_score(new_text)

    return StatementDiff(
        added_sentences=added, removed_sentences=removed, unchanged_count=unchanged,
        change_ratio=change_ratio, old_tone=old_tone, new_tone=new_tone,
        tone_shift=new_tone - old_tone,
    )


_DISSENT_BLOCK = re.compile(
    r"Voting against.{0,400}?(?:were|was)\s+(.+?)(?=\s+(?:Implementation Note|In a related|The Board|$))",
    re.IGNORECASE | re.DOTALL,
)


def extract_dissenting_members(full_text: str) -> list[str]:
    """Regex over the raw text, not the sentence splitter — Fed statements
    list dissenters as 'Beth M. Hammack, Neel Kashkari, and Lorie K. Logan',
    and initials' embedded periods would confuse sentence-boundary logic.
    A best-effort name list, not guaranteed perfectly clean on every
    statement's exact phrasing."""
    match = _DISSENT_BLOCK.search(full_text)
    if not match:
        return []
    raw = re.split(r",?\s+who preferred", match.group(1))[0]
    names = re.split(r",\s+and\s+|,\s+|\s+and\s+", raw)
    return [n.strip().rstrip(".") for n in names if len(n.strip()) > 3]


def classify_policy_regime(new_tone: float, tone_shift: float) -> str:
    """Spec sec. 9: 'tightening, easing, transition, uncertainty,
    intervention risk, or neutral' — intervention risk isn't derivable from
    statement tone alone (it's a distinct, much rarer signal), so this
    classifier only ever returns the other five."""
    if abs(tone_shift) < 0.05:
        return "tightening" if new_tone > 0.15 else ("easing" if new_tone < -0.15 else "neutral")
    if tone_shift > 0.05:
        return "transition" if new_tone < 0.15 else "tightening"
    return "transition" if new_tone > -0.15 else "easing"
