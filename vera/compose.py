"""Composition orchestrator: bundle → ledger → LLM/playbook → guard → action."""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime

from . import playbooks
from .derive import build_ledger
from .guard import finalize
from .lang import customer_voice, merchant_voice, stable_seed
from .playbooks import P, Draft

log = logging.getLogger("vera.compose")


@dataclass
class Composed:
    body: str
    cta: str
    send_as: str
    suppression_key: str
    rationale: str
    template_name: str
    template_params: list[str]
    trigger_id: str
    merchant_id: str
    customer_id: str | None
    levers: tuple = ()
    issues: list[str] = field(default_factory=list)
    facts_used: list[str] = field(default_factory=list)

    def digest(self) -> str:
        return hashlib.sha1(self.body.encode("utf-8")).hexdigest()[:16]


NAME_FACT_KEYS = frozenset({
    "biz", "owner", "addressee", "locality", "city", "place", "plan", "journal", "authority",
    "vocab_term", "cx_name", "cx_top_service", "cx_last_service", "offer_active",
    "offer_active_all", "offer_lapsed", "catalog_offer", "catalog_free_offer",
    "catalog_repeat_offer", "digest_source", "compliance_source", "season_window",
    "trend_query", "category_name", "category_one",
})


def _proper_nouns(led) -> set[str]:
    """Words the contexts themselves treat as names — safe to keep capitalised.

    Restricted to name-bearing facts on purpose. Scanning every display leaks
    ordinary sentence-initial words (a content-library title like "Is keratin
    safe?" would otherwise register "Is" as a proper noun) and the salutation
    then reads "Sushma, Is hafte...".
    """
    words = set()
    for key, fact in led.facts.items():
        if key not in NAME_FACT_KEYS and not key.startswith("tp_"):
            continue
        for token in str(fact.display).split():
            clean = token.strip(".,;:'\"()[]")
            if clean and clean[0].isupper():
                words.add(clean)
    return words


def _lead_in(hook: str, led) -> str:
    """`Dr. Meera, ` + `Ek compliance baat...` reads wrong. Lowercase the first
    word unless the contexts say it is a name, an acronym, or an abbreviation."""
    hook = hook.lstrip()
    if not hook:
        return hook
    first = hook.split(" ", 1)[0]
    clean = first.strip(".,;:'\"()[]")
    if not clean or not clean[0].isalpha():
        return hook
    if any(ch.isupper() for ch in clean[1:]) or clean.isupper():
        return hook                      # JIDA, DCI, IPL, McDonald's
    if first.endswith(".") and len(clean) <= 3:
        return hook                      # Dr., Mr.
    if clean in _proper_nouns(led):
        return hook                      # Diwali, Smile Studio, Summer
    return hook[0].lower() + hook[1:]


def _assemble(draft: Draft, addressee: str, led) -> str:
    hook = draft.hook.strip()
    citation = draft.citation or ""
    if draft.send_as != "merchant_on_behalf" and addressee:
        hook = _lead_in(hook, led)
    parts = [hook, draft.insight.strip(), draft.ask.strip()]
    core = " ".join(p for p in parts if p)
    if citation:
        core = core.rstrip(". ") + "." + citation
    if draft.send_as == "merchant_on_behalf":
        return core
    prefix = f"{addressee}, " if addressee else ""
    return prefix + core


def _template_params(draft: Draft, addressee: str, led) -> list[str]:
    params = [addressee or led.get("biz", "there")]
    for part in (draft.hook, draft.insight, draft.ask):
        text = (part or "").strip()
        if text:
            params.append(text[:400])
    return params[:5]


def _rationale(bundle: dict, draft: Draft, led, voice, issues: list[str], facts: list[str],
               llm_rationale: str = "") -> str:
    trigger = bundle.get("trigger") or {}
    kind = trigger.get("kind", "unknown")
    urgency = trigger.get("urgency", "?")
    src = trigger.get("source", "?")
    lang = {"en": "English", "hien": "Hindi-English code-mix", "hi": "Hindi (romanised)"}[voice.mode]
    why_lang = _language_reason(bundle, draft, voice)
    bits = [
        f"Trigger {kind} (source={src}, urgency={urgency}) is the reason to message now, and the "
        f"opening sentence states it explicitly.",
    ]
    if llm_rationale:
        bits.append(f"Composition rationale: {llm_rationale}")
    if facts:
        bits.append("Grounded on " + "; ".join(facts[:4]) + ".")
    if led.notes:
        bits.append(led.notes[0].capitalize() + ".")
    if draft.levers:
        bits.append("Compulsion levers: " + ", ".join(l.replace("_", " ") for l in draft.levers) + ".")
    bits.append(f"Single {draft.cta.replace('_', ' ')} CTA in the last sentence. Language: {lang} ({why_lang}).")
    if issues:
        bits.append("Guard actions: " + ", ".join(sorted(set(issues))) + ".")
    return " ".join(bits)


def _language_reason(bundle: dict, draft: Draft, voice) -> str:
    if draft.send_as == "merchant_on_behalf":
        pref = ((bundle.get("customer") or {}).get("identity") or {}).get("language_pref")
        return f"customer.identity.language_pref={pref!r}" if pref else "customer.identity.language_pref"
    langs = [str(x).lower() for x in
             ((bundle.get("merchant") or {}).get("identity") or {}).get("languages") or []]
    rule = str(((bundle.get("category") or {}).get("voice") or {}).get("code_mix") or "")
    if voice.mode != "en":
        return f"identity.languages={langs} and category.voice.code_mix={rule!r}"
    if "hi" in langs:
        return (f"identity.languages={langs} allows Hindi, but category.voice.code_mix={rule!r} "
                f"sets an English-primary register for this vertical")
    return f"identity.languages={langs} has no Hindi"


def _facts_used(body: str, led) -> list[str]:
    """Report which ledger facts actually made it into the body, with sources."""
    used = []
    for key, fact in led.facts.items():
        if not fact.display or len(fact.display) < 2:
            continue
        if key in ("languages", "signals_raw", "category_slug", "voice_tone", "digest_id"):
            continue
        if fact.display in body:
            used.append(f"{fact.display} ({fact.source})")
    used.sort(key=lambda s: -len(s))
    return used[:8]


# ---------------------------------------------------------------------------
# LLM-first composition with template fallback
# ---------------------------------------------------------------------------

def _try_llm_compose(led, trigger, merchant, category, customer, voice, is_customer_msg):
    """Attempt LLM composition. Returns (body, cta, rationale, send_as) or None."""
    try:
        from .llm import compose_with_llm
    except ImportError:
        return None
    
    addressee = led.get("addressee") or led.get("biz")
    result = compose_with_llm(
        led=led,
        trigger=trigger,
        merchant=merchant,
        category=category,
        customer=customer if is_customer_msg else None,
        voice_mode=voice.mode,
        addressee=addressee or "",
    )
    
    if result and result.get("body"):
        log.info("LLM composed %d chars for trigger %s", 
                 len(result["body"]), trigger.get("kind", "?"))
        return result
    return None


def compose(bundle: dict, now: datetime | None = None, attempt: int = 0,
            fresh_digest_ids: set[str] | None = None) -> Composed | None:
    trigger = bundle.get("trigger") or {}
    merchant = bundle.get("merchant") or {}
    category = bundle.get("category") or {}
    customer = bundle.get("customer")

    enriched = dict(bundle)
    enriched["fresh_digest_ids"] = fresh_digest_ids or set()
    led = build_ledger(enriched, now_month=now.month if now else None)

    merchant_id = merchant.get("merchant_id") or bundle.get("merchant_id") or ""
    trigger_id = trigger.get("id") or bundle.get("trigger_id") or ""
    m_rev = getattr(bundle.get("merchant_entry"), "revision", 0)
    c_rev = getattr(bundle.get("category_entry"), "revision", 0)
    seed = stable_seed(merchant_id, trigger_id, m_rev, c_rev, attempt)

    is_customer_msg = (trigger.get("scope") == "customer") or bool(customer and trigger.get("customer_id"))
    voice = (customer_voice(customer or {}, merchant, seed) if is_customer_msg
             else merchant_voice(merchant, category, seed))

    addressee = led.get("addressee") or led.get("biz")
    taboo = led.val("voice_taboo", []) or []
    llm_rationale = ""

    # ---- Path A: LLM composition (primary, attempt 0 only) ----
    if attempt == 0:
        llm_result = _try_llm_compose(led, trigger, merchant, category, customer, voice, is_customer_msg)
        if llm_result:
            body = llm_result["body"]
            cta = llm_result["cta"]
            send_as = llm_result.get("send_as", "vera")
            llm_rationale = llm_result.get("rationale", "")
            
            # Run through the guard — same rules as template path
            body, issues = finalize(body, led, taboo)
            if body and len(body) >= 40:
                suppression = trigger.get("suppression_key") or \
                    f"{trigger.get('kind', 'trigger')}:{merchant_id}:{trigger_id}"
                facts = _facts_used(body, led)
                
                # Build a minimal Draft for rationale generation
                draft = Draft(hook=body, cta=cta, send_as=send_as,
                              levers=("specificity", "loss_aversion", "engagement"))
                
                return Composed(
                    body=body,
                    cta=cta,
                    send_as=send_as,
                    suppression_key=suppression,
                    rationale=_rationale(bundle, draft, led, voice, issues, facts, llm_rationale),
                    template_name="llm_composed_v1",
                    template_params=[addressee or "", body[:400]],
                    trigger_id=trigger_id,
                    merchant_id=merchant_id,
                    customer_id=(customer or {}).get("customer_id") if send_as == "merchant_on_behalf" else None,
                    levers=draft.levers,
                    issues=issues + ["llm_composed"],
                    facts_used=facts,
                )
            else:
                log.warning("LLM output rejected by guard, falling back to template")

    # ---- Path B: Template playbook (fallback) ----
    p = P(led=led, voice=voice, trigger=trigger, merchant=merchant, category=category,
          customer=customer, seed=seed)
    draft = playbooks.select(trigger)(p)
    if draft.skip or not draft.hook:
        return None

    if draft.send_as is None:
        draft.send_as = "merchant_on_behalf" if is_customer_msg and customer else "vera"
    if draft.send_as == "merchant_on_behalf" and not customer:
        # never claim to speak for the merchant without a consented customer
        return None

    body = _assemble(draft, addressee, led)
    body, issues = finalize(body, led, taboo)
    if not body or len(body) < 40:
        return None

    suppression = draft.suppression or trigger.get("suppression_key") or \
        f"{trigger.get('kind', 'trigger')}:{merchant_id}:{trigger_id}"
    facts = _facts_used(body, led)
    return Composed(
        body=body,
        cta=draft.cta,
        send_as=draft.send_as,
        suppression_key=suppression,
        rationale=_rationale(bundle, draft, led, voice, issues, facts),
        template_name=draft.template,
        template_params=_template_params(draft, addressee, led),
        trigger_id=trigger_id,
        merchant_id=merchant_id,
        customer_id=(customer or {}).get("customer_id") if draft.send_as == "merchant_on_behalf" else None,
        levers=draft.levers,
        issues=issues,
        facts_used=facts,
    )


def compose_unique(bundle: dict, seen: set[str], now: datetime | None = None,
                   fresh_digest_ids: set[str] | None = None) -> Composed | None:
    """Compose, re-rolling deterministic variants until the body is not a repeat."""
    first = None
    for attempt in range(4):
        result = compose(bundle, now=now, attempt=attempt, fresh_digest_ids=fresh_digest_ids)
        if result is None:
            return first
        first = first or result
        if result.digest() not in seen:
            return result
    return None

