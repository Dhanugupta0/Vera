"""LLM-powered message composer using Groq API.

Replaces template playbooks with natural language composition while keeping
the fact-ledger grounding layer. Every fact referenced in the prompt comes
from the ledger, so the guard can still prove every numeral traces back to a
pushed context.

Falls back to the template playbooks when:
  - LLM is disabled via VERA_LLM_ENABLED=false
  - The API call times out (>VERA_LLM_TIMEOUT seconds)
  - The API returns an error or unparseable response
  - The guard rejects the LLM output entirely
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
import urllib.error
from typing import Any

from . import config
from .derive import Ledger

log = logging.getLogger("vera.llm")

# ---------------------------------------------------------------------------
# Category-specific system prompt fragments
# ---------------------------------------------------------------------------

CATEGORY_VOICE = {
    "dentists": (
        "You are a clinical peer advisor for dental practices. "
        "Use professional, clinical vocabulary (fluoride varnish, caries, recall). "
        "Tone: peer-to-peer, not promotional. Address dentists as 'Dr.' always. "
        "NEVER use words: cure, guaranteed, 100% effective, painless. "
        "Source-cite research (journal, page) when available."
    ),
    "salons": (
        "You are a warm, practical business advisor for salons and beauty studios. "
        "Tone: friendly and grounded, operator-to-operator. "
        "Use service+price formats (e.g., 'Haircut @ ₹99'), never flat discounts. "
        "Mention real trends and seasons relevant to beauty services."
    ),
    "restaurants": (
        "You are a practical F&B operations advisor for restaurants. "
        "Tone: operator-to-operator, crisp, numbers-first. "
        "Focus on covers, orders, delivery metrics, listing visibility. "
        "Use service+price formats for offers, never generic '10% off'."
    ),
    "gyms": (
        "You are a coaching-style business advisor for gyms and fitness centres. "
        "Tone: motivational but data-backed. "
        "Focus on member retention, session attendance, trial conversions. "
        "Keep language English-primary even for Hindi-speaking merchants — "
        "the category register is English-primary."
    ),
    "pharmacies": (
        "You are a precise, trustworthy advisor for pharmacies and medical stores. "
        "Tone: authoritative, compliance-aware, no overclaims. "
        "Reference regulation sources (CDSCO, DCGI) when available. "
        "Use exact molecule names and batch numbers when provided."
    ),
}

SYSTEM_TEMPLATE = """You compose WhatsApp messages for Vera, magicpin's AI merchant assistant.

{category_voice}

RULES (CRITICAL — violating these loses points):
1. ONLY use facts listed below under AVAILABLE FACTS. Do NOT invent numbers, dates, names, offers, research, or statistics.
2. Keep it concise — WhatsApp messages should be 3-5 sentences max.
3. Structure: WHY NOW (the trigger) → WHY IT MATTERS TO YOU (merchant-specific) → WHAT TO DO (single CTA, last sentence).
4. Single CTA in the LAST sentence — binary (Reply YES/STOP) for action triggers, open-ended for information triggers.
5. NO preambles ("I hope you're doing well"), NO re-introductions, NO "AMAZING DEAL!" hype.
6. {language_instruction}
7. Anchor on a SPECIFIC, VERIFIABLE fact (a number, date, source citation) in the first sentence.
8. Use compulsion levers: loss aversion, social proof, curiosity, effort externalization, reciprocity.
9. Do NOT use URLs. Do NOT expose internal identifiers (merchant_id, context_id, etc.).
10. Service+price ("Dental Cleaning @ ₹299") beats generic discounts ("10% off") ALWAYS.

Respond ONLY with this JSON (no markdown, no explanation):
{{"body": "the WhatsApp message", "cta": "binary_yes_no|open_ended|none", "rationale": "2-3 sentences explaining why this message, what facts it uses, which compulsion levers"}}"""

LANGUAGE_EN = "Write in English. Keep sentences crisp and professional."
LANGUAGE_HIEN = (
    "Write in Hindi-English code-mix (Hinglish) as used on WhatsApp in India. "
    "Keep numbers, prices, citations, and technical terms in English. "
    "Frame the ask and conversational bits in romanised Hindi. "
    "Example: 'Dr. Meera, JIDA ka Oct issue aaya hai. 2,100-patient trial mein...'"
)
LANGUAGE_HI = (
    "Write primarily in romanised Hindi with English for numbers and technical terms. "
    "Example: 'Aapki listing par pichle 30 din mein 2,410 views aaye...'"
)


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _build_facts_section(led: Ledger) -> str:
    """Dump all ledger facts the LLM is allowed to use."""
    lines = []
    for key, fact in sorted(led.facts.items()):
        if not fact.display or key in ("voice_taboo", "voice_allowed", "signals_raw",
                                        "voice_tone", "category_slug", "languages"):
            continue
        lines.append(f"- {key}: {fact.display}  (source: {fact.source})")
    return "\n".join(lines) if lines else "- No specific facts available"


def _build_trigger_section(trigger: dict) -> str:
    """Summarise the trigger for the prompt."""
    kind = trigger.get("kind", "unknown")
    source = trigger.get("source", "unknown")
    urgency = trigger.get("urgency", "?")
    payload = trigger.get("payload") or {}
    
    parts = [f"Kind: {kind}", f"Source: {source}", f"Urgency: {urgency}"]
    
    # Include key payload fields (skip placeholder markers)
    if payload and not payload.get("placeholder"):
        for k, v in payload.items():
            if k in ("merchant_id", "customer_id", "category", "placeholder"):
                continue
            if isinstance(v, (str, int, float)):
                parts.append(f"Payload.{k}: {v}")
            elif isinstance(v, list) and len(v) <= 3:
                parts.append(f"Payload.{k}: {v}")
    else:
        parts.append("Payload: placeholder (compose from merchant + category facts)")
    
    return "\n".join(parts)


def _build_merchant_section(merchant: dict) -> str:
    """Key merchant context for the prompt."""
    ident = merchant.get("identity") or {}
    perf = merchant.get("performance") or {}
    signals = merchant.get("signals") or []
    offers = [o.get("title") for o in (merchant.get("offers") or [])
              if isinstance(o, dict) and o.get("status") == "active"]
    
    parts = [
        f"Name: {ident.get('name', 'unknown')}",
        f"Owner: {ident.get('owner_first_name', 'unknown')}",
        f"Location: {ident.get('locality', '?')}, {ident.get('city', '?')}",
        f"Languages: {ident.get('languages', ['en'])}",
        f"Views (30d): {perf.get('views', '?')}, Calls: {perf.get('calls', '?')}, CTR: {perf.get('ctr', '?')}",
    ]
    if signals:
        parts.append(f"Signals: {signals[:5]}")
    if offers:
        parts.append(f"Active offers: {offers}")
    return "\n".join(parts)


def _build_customer_section(customer: dict | None) -> str:
    """Customer context if available."""
    if not customer:
        return "None (merchant-facing message)"
    ident = customer.get("identity") or {}
    rel = customer.get("relationship") or {}
    return "\n".join([
        f"Name: {ident.get('name', '?')}",
        f"Language: {ident.get('language_pref', 'en')}",
        f"State: {customer.get('state', '?')}",
        f"Last visit: {rel.get('last_visit', '?')}",
        f"Total visits: {rel.get('visits_total', '?')}",
        f"Services: {rel.get('services_received', [])}",
    ])


def build_prompt(led: Ledger, trigger: dict, merchant: dict, 
                 category: dict, customer: dict | None,
                 voice_mode: str, addressee: str) -> tuple[str, str]:
    """Build (system_prompt, user_prompt) for the LLM.
    
    Returns the system prompt and user prompt as a tuple.
    """
    slug = category.get("slug") or merchant.get("category_slug") or ""
    # Match category voice
    cat_voice = CATEGORY_VOICE.get(slug, "")
    if not cat_voice:
        for key, voice in CATEGORY_VOICE.items():
            if slug.startswith(key.rstrip("s")):
                cat_voice = voice
                break
    if not cat_voice:
        cat_voice = "You are a professional business advisor. Tone: helpful and data-backed."
    
    lang_instr = {
        "en": LANGUAGE_EN,
        "hien": LANGUAGE_HIEN,
        "hi": LANGUAGE_HI,
    }.get(voice_mode, LANGUAGE_EN)
    
    system = SYSTEM_TEMPLATE.format(
        category_voice=cat_voice,
        language_instruction=lang_instr,
    )
    
    is_customer = bool(customer and (trigger.get("scope") == "customer" or trigger.get("customer_id")))
    
    user_prompt = f"""Compose a message for {'the customer' if is_customer else 'the merchant'}.

ADDRESS AS: {addressee}
{'SEND AS: merchant_on_behalf (message appears from the merchant to their customer)' if is_customer else 'SEND AS: vera (message from Vera to the merchant)'}

TRIGGER (why we are messaging NOW):
{_build_trigger_section(trigger)}

MERCHANT:
{_build_merchant_section(merchant)}

CUSTOMER:
{_build_customer_section(customer if is_customer else None)}

AVAILABLE FACTS (you may ONLY reference these — do NOT invent any data):
{_build_facts_section(led)}

Compose the message now. JSON only, no markdown."""

    return system, user_prompt


# ---------------------------------------------------------------------------
# API call
# ---------------------------------------------------------------------------

def _call_groq(system: str, user: str) -> dict | None:
    """Call Groq API and return parsed JSON response, or None on failure."""
    if not config.GROQ_API_KEY:
        return None
    
    body = json.dumps({
        "model": config.GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
        "max_tokens": 600,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {config.GROQ_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    
    t0 = time.time()
    try:
        resp = urllib.request.urlopen(req, timeout=config.LLM_TIMEOUT)
        data = json.loads(resp.read().decode("utf-8"))
        elapsed = time.time() - t0
        log.info("groq call ok in %.1fs, model=%s", elapsed, data.get("model", "?"))
        
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)
    except urllib.error.HTTPError as e:
        elapsed = time.time() - t0
        try:
            err_body = e.read().decode("utf-8")[:200]
        except Exception:
            err_body = ""
        log.warning("groq HTTP %s in %.1fs: %s", e.code, elapsed, err_body)
        return None
    except Exception as exc:
        elapsed = time.time() - t0
        log.warning("groq call failed in %.1fs: %s", elapsed, exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compose_with_llm(led: Ledger, trigger: dict, merchant: dict,
                     category: dict, customer: dict | None,
                     voice_mode: str, addressee: str) -> dict | None:
    """Compose a message using the LLM.
    
    Returns a dict with keys: body, cta, rationale, send_as
    or None if the LLM call fails.
    """
    if not config.LLM_ENABLED:
        return None
    
    system, user = build_prompt(led, trigger, merchant, category, customer, voice_mode, addressee)
    
    result = _call_groq(system, user)
    if not result:
        return None
    
    body = result.get("body", "").strip()
    if not body or len(body) < 30:
        log.warning("LLM returned too-short body: %r", body)
        return None
    
    cta = result.get("cta", "open_ended")
    if cta not in ("binary_yes_no", "binary_confirm_cancel", "open_ended", 
                    "multi_choice_slot", "none"):
        cta = "open_ended"
    
    is_customer = bool(customer and (trigger.get("scope") == "customer" or trigger.get("customer_id")))
    send_as = "merchant_on_behalf" if is_customer else "vera"
    
    rationale = result.get("rationale", "")
    if not rationale:
        rationale = f"LLM-composed for trigger {trigger.get('kind', '?')}"
    
    return {
        "body": body,
        "cta": cta,
        "rationale": rationale,
        "send_as": send_as,
    }


def compose_reply_with_llm(led: Ledger, merchant: dict, category: dict,
                            customer: dict | None, conversation_turns: list,
                            merchant_message: str, voice_mode: str,
                            addressee: str) -> dict | None:
    """Use LLM for reply composition (question/unknown intents only).
    
    Returns dict with body, cta, rationale or None.
    """
    if not config.LLM_ENABLED:
        return None
    
    slug = category.get("slug") or merchant.get("category_slug") or ""
    cat_voice = CATEGORY_VOICE.get(slug, "")
    if not cat_voice:
        for key, voice in CATEGORY_VOICE.items():
            if slug.startswith(key.rstrip("s")):
                cat_voice = voice
                break
    if not cat_voice:
        cat_voice = "You are a professional business advisor."
    
    lang_instr = {
        "en": LANGUAGE_EN,
        "hien": LANGUAGE_HIEN,
        "hi": LANGUAGE_HI,
    }.get(voice_mode, LANGUAGE_EN)
    
    system = f"""You are Vera, magicpin's AI merchant assistant, replying in an ongoing WhatsApp conversation.

{cat_voice}

RULES:
1. ONLY use facts from AVAILABLE FACTS below. NEVER fabricate data.
2. Keep reply to 2-3 sentences max. 
3. If the merchant asked a question you CAN answer from the facts, answer it directly and concisely, then state the concrete next step.
4. If you CANNOT answer from the facts, say so honestly ("I don't have that in front of me") and redirect to what you CAN do.
5. End with a single, clear next step or CTA.
6. NEVER ask another qualifying question after the merchant has committed.
7. {lang_instr}

Respond ONLY with JSON: {{"body": "your reply", "cta": "binary_yes_no|binary_confirm_cancel|open_ended|none", "rationale": "why this reply"}}"""

    history = ""
    for turn in conversation_turns[-6:]:
        role = getattr(turn, 'role', turn.get('role', '?')) if isinstance(turn, dict) else getattr(turn, 'role', '?')
        text = getattr(turn, 'text', turn.get('text', '')) if isinstance(turn, dict) else getattr(turn, 'text', '')
        history += f"\n[{role}]: {text[:200]}"
    
    user_prompt = f"""CONVERSATION SO FAR:{history}

MERCHANT'S LATEST MESSAGE: "{merchant_message}"

MERCHANT: {(merchant.get('identity') or {}).get('name', '?')}
ADDRESS AS: {addressee}

AVAILABLE FACTS:
{_build_facts_section(led)}

Reply to the merchant. JSON only."""

    result = _call_groq(system, user_prompt)
    if not result:
        return None
    
    body = result.get("body", "").strip()
    if not body or len(body) < 15:
        return None
    
    cta = result.get("cta", "open_ended")
    if cta not in ("binary_yes_no", "binary_confirm_cancel", "open_ended",
                    "multi_choice_slot", "none"):
        cta = "open_ended"
    
    return {
        "body": body,
        "cta": cta,
        "rationale": result.get("rationale", "LLM-composed reply"),
    }
