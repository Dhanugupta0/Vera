"""Output guard — the last thing that runs before a message leaves.

Nothing here is stylistic. Every rule maps to a scoring rule in the harness:

  ungrounded numeral   -> "Fabricating data not in context: -2"
  raw identifier       -> "Exposing internal jargon to merchant: -1"
  URL                  -> "Hard fail for that action ... Penalty: -3 per URL"
  more than one ask    -> "Multiple CTAs in one message" anti-pattern
  category taboo word  -> category-fit penalty (and, for dentists, a legal problem)
  repeated body        -> "-2 anti-repetition per repeat"

The guard repairs where a repair is safe and drops the offending clause where it
is not. It never invents replacement text.
"""
from __future__ import annotations

import re

from .derive import Ledger, numerals

URL_RE = re.compile(r"\b(?:https?://|www\.)\S+|\b[a-z0-9-]+\.(?:com|in|org|net|io|co)\b(?:/\S*)?", re.I)
ID_RE = re.compile(r"\b(?:m|c|trg|d|o|pc|den|sal|res|gym|pha)_[A-Za-z0-9][A-Za-z0-9_]{2,}\b")
SNAKE_RE = re.compile(r"\b[A-Za-z]+(?:_[A-Za-z0-9]+)+\b")
PLACE_ID_RE = re.compile(r"\bChIJ\w*\b")
FIELD_WORDS = re.compile(
    r"\b(suppression[_ ]key|send[_ ]as|conversation[_ ]id|merchant[_ ]id|customer[_ ]id|"
    r"context[_ ]id|place[_ ]id|category[_ ]slug|trigger[_ ]kind|payload|dataclass|json)\b", re.I)

# Numerals the templates introduce structurally (minutes, list counts) rather
# than as claims about the merchant.
STRUCTURAL_NUMERALS = {"1", "2", "3", "30"}

SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def split_sentences(text: str) -> list[str]:
    return [s for s in SENTENCE_SPLIT.split(text.strip()) if s.strip()]


def scrub(text: str) -> tuple[str, list[str]]:
    """Remove things that must never appear, regardless of context."""
    issues: list[str] = []
    if URL_RE.search(text):
        text = URL_RE.sub("", text)
        issues.append("stripped_url")
    if PLACE_ID_RE.search(text):
        text = PLACE_ID_RE.sub("", text)
        issues.append("stripped_place_id")
    if ID_RE.search(text):
        text = ID_RE.sub("", text)
        issues.append("stripped_internal_id")
    if FIELD_WORDS.search(text):
        text = FIELD_WORDS.sub("", text)
        issues.append("stripped_field_name")

    def _desnake(match: re.Match) -> str:
        return match.group(0).replace("_", " ")

    if SNAKE_RE.search(text):
        text = SNAKE_RE.sub(_desnake, text)
        issues.append("desnaked_identifier")
    return text, issues


def ungrounded(text: str, led: Ledger) -> set[str]:
    allowed = led.licensed | STRUCTURAL_NUMERALS
    found = numerals(text)
    return {n for n in found if n not in allowed}


def drop_ungrounded_sentences(text: str, led: Ledger) -> tuple[str, list[str]]:
    """Remove any sentence containing a numeral the ledger cannot source."""
    bad = ungrounded(text, led)
    if not bad:
        return text, []
    kept, dropped = [], []
    for sentence in split_sentences(text):
        if numerals(sentence) & bad:
            dropped.append(sentence)
        else:
            kept.append(sentence)
    return " ".join(kept), [f"dropped_ungrounded:{'|'.join(sorted(bad))}"] if dropped else []


def drop_taboo_sentences(text: str, taboo: list[str]) -> tuple[str, list[str]]:
    if not taboo:
        return text, []
    lowered = [t.lower().split("(")[0].strip() for t in taboo if t and len(str(t)) > 2]
    kept, hits = [], []
    for sentence in split_sentences(text):
        low = sentence.lower()
        found = [t for t in lowered if t and t in low]
        if found:
            hits.extend(found)
        else:
            kept.append(sentence)
    if not hits:
        return text, []
    return " ".join(kept), [f"dropped_taboo:{'|'.join(sorted(set(hits)))}"]


def enforce_single_ask(text: str) -> tuple[str, list[str]]:
    """One primary CTA. Earlier questions become statements."""
    if text.count("?") <= 1:
        return text, []
    parts = split_sentences(text)
    q_index = [i for i, s in enumerate(parts) if s.rstrip().endswith("?")]
    if len(q_index) <= 1:
        return text, []
    last = q_index[-1]
    for i in q_index[:-1]:
        parts[i] = parts[i].rstrip()[:-1].rstrip() + "."
    return " ".join(parts), ["collapsed_extra_questions"]


def tidy(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)
    text = re.sub(r"([.,;:])\1+", r"\1", text)
    text = re.sub(r"\.\s*\.", ".", text)
    text = re.sub(r",\s*\.", ".", text)
    text = re.sub(r"\(\s*\)", "", text)
    return text.strip(" ,;")


def finalize(body: str, led: Ledger, taboo: list[str] | None = None) -> tuple[str, list[str]]:
    issues: list[str] = []
    body, found = scrub(body)
    issues += found
    body, found = drop_taboo_sentences(body, taboo or [])
    issues += found
    body, found = drop_ungrounded_sentences(body, led)
    issues += found
    body, found = enforce_single_ask(body)
    issues += found
    body = tidy(body)
    if body and body[-1] not in ".!?":
        body += "."
    return body, issues
