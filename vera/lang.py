"""Voice + language selection.

Two jobs:

* Decide whether this merchant/customer gets English or Hindi-English code-mix,
  and render bilingual fragments accordingly. Real Vera keeps the factual spine
  (numbers, citations, prices) in English and code-mixes the framing and the
  ask — that is what actually reads naturally on WhatsApp in India, and it is
  what the brief's own worked examples do.
* Give the composer deterministic variety. `stable_seed` uses CRC32, not
  `hash()`, because Python randomises string hashing per process — a bot that
  is "deterministic" only within one boot is not deterministic.
"""
from __future__ import annotations

import zlib
from dataclasses import dataclass
from typing import Any, Sequence

# Devanagari-script languages we can code-mix into romanised Hindi for.
HINDI_LANGS = {"hi", "hi-en", "hi-en mix", "hinglish"}


def stable_seed(*parts: Any) -> int:
    joined = "|".join(str(p) for p in parts)
    return zlib.crc32(joined.encode("utf-8"))


def choose(options: Sequence[Any], seed: int, offset: int = 0):
    if not options:
        return None
    return options[(seed + offset) % len(options)]


def bi(en: str, hi: str) -> dict:
    """A fragment with an English and a Hindi-English code-mix rendering."""
    return {"en": en, "hi": hi}


@dataclass
class Voice:
    mode: str = "en"          # "en" | "hien" | "hi"
    seed: int = 0
    tone: str = ""
    formal: bool = True

    @property
    def code_mix(self) -> bool:
        return self.mode in ("hien", "hi")

    def say(self, fragment: Any) -> str:
        """Render a str or a bi() fragment in this voice."""
        if fragment is None:
            return ""
        if isinstance(fragment, dict):
            return fragment.get("hi" if self.code_mix else "en", fragment.get("en", ""))
        return str(fragment)

    def pick(self, options: Sequence[Any], offset: int = 0) -> str:
        return self.say(choose(options, self.seed, offset))


def merchant_voice(merchant: dict, category: dict, seed: int) -> Voice:
    """Merchant language ANDed with what the category says reads naturally.

    A merchant listing `hi` still gets pure English if the category pack says
    its register is english-primary (the gyms pack does exactly that).
    """
    langs = {str(x).lower() for x in (merchant.get("identity", {}).get("languages") or [])}
    voice_cfg = category.get("voice") or {}
    code_mix_rule = str(voice_cfg.get("code_mix") or "").lower()
    mode = "hien" if langs & HINDI_LANGS else "en"
    if code_mix_rule and ("english_primary" in code_mix_rule or "english_only" in code_mix_rule
                          or code_mix_rule in ("none", "no")):
        mode = "en"
    tone = str(voice_cfg.get("tone") or "")
    return Voice(mode=mode, seed=seed, tone=tone, formal=tone.startswith("peer") or "clinical" in tone)


def customer_voice(customer: dict, merchant: dict, seed: int) -> Voice:
    pref = str((customer.get("identity") or {}).get("language_pref") or "").lower().strip()
    if pref in ("hi", "hindi"):
        mode = "hi"
    elif pref in HINDI_LANGS or "hi" in pref.split("-"):
        mode = "hien"
    elif pref in ("en", "english", ""):
        # fall back to what the merchant's own customers are likely to read
        langs = {str(x).lower() for x in (merchant.get("identity", {}).get("languages") or [])}
        mode = "en" if pref else ("hien" if langs & HINDI_LANGS else "en")
    else:
        mode = "en"
    return Voice(mode=mode, seed=seed, formal=False)
