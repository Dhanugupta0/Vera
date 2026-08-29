"""Tick policy — which triggers earn a message this tick, and which do not.

The harness rewards restraint explicitly ("Restraint is rewarded; spam is
penalised"), so this is a filter first and a ranker second. Everything a
merchant has already told us — opted out, hostile, auto-responder only, already
messaged — is a hard gate before any composition work happens.
"""
from __future__ import annotations

from datetime import datetime

from .compose import Composed, compose_unique
from .config import (MAX_ACTIONS_PER_MERCHANT_PER_TICK, MAX_ACTIONS_PER_TICK,
                     MAX_OPEN_CONVERSATIONS_PER_MERCHANT, MAX_SENDS_PER_MERCHANT_PER_WINDOW,
                     MIN_URGENCY_WHEN_MERCHANT_BUSY, UNANSWERED_NUDGES_BEFORE_END)
from .store import parse_iso

ENGAGED_SIGNALS = ("engaged_in_last_24h", "engaged_in_last_48h", "high_engagement",
                   "active_planning", "high_volume")
QUIET_SIGNALS = ("dormant_with_vera", "no_recent_conversation", "winback_eligible")


def _urgency(trigger: dict) -> int:
    try:
        return int(trigger.get("urgency") or 1)
    except (TypeError, ValueError):
        return 1


def _expired(trigger: dict, now: datetime) -> bool:
    expires = parse_iso(trigger.get("expires_at"))
    return bool(expires and expires < now)


def _score(bundle: dict, now: datetime, max_seq: int) -> float:
    trigger = bundle["trigger"]
    merchant = bundle["merchant"]
    score = _urgency(trigger) * 10.0

    entry = bundle.get("trigger_entry")
    if entry is not None and max_seq:
        score += 4.0 * (entry.seq / max_seq)          # newer context pushes go first

    signals = [str(s) for s in (merchant.get("signals") or [])]
    if any(any(sig.startswith(e) for e in ENGAGED_SIGNALS) for sig in signals):
        score += 3.0                                   # they reply; spend the turn here
    if any(any(sig.startswith(q) for q in QUIET_SIGNALS) for sig in signals):
        score += 1.5

    if bundle.get("customer"):
        score += 2.0                                   # customer-facing sends are scarce
    if str(trigger.get("source")) == "external":
        score += 0.5                                   # external events decay fastest

    expires = parse_iso(trigger.get("expires_at"))
    if expires:
        hours_left = (expires - now).total_seconds() / 3600.0
        if 0 < hours_left < 48:
            score += 3.0
    return score


def _consented(customer: dict | None) -> bool:
    if not customer:
        return False
    prefs = customer.get("preferences") or {}
    if prefs.get("reminder_opt_in") is False:
        return False
    consent = customer.get("consent") or {}
    return bool(consent.get("opted_in_at") or consent.get("scope"))


def _conversation_id(brain, merchant_id: str, trigger_id: str) -> str:
    base = f"conv_{merchant_id}_{trigger_id}"[:120]
    if base not in brain.conversations:
        return base
    n = 2
    while f"{base}_{n}" in brain.conversations:
        n += 1
    return f"{base}_{n}"


def plan_tick(store, brain, now: datetime, available_triggers: list[str]) -> tuple[list[dict], list[str]]:
    """Returns (actions, skip_log)."""
    skips: list[str] = []
    trigger_ids = [t for t in (available_triggers or []) if isinstance(t, str)]
    if not trigger_ids:
        # No hint from the judge: consider at most one high-urgency stored trigger.
        stored = sorted(store.all("trigger"), key=lambda e: -e.seq)
        for entry in stored:
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            if _urgency(payload) >= MIN_URGENCY_WHEN_MERCHANT_BUSY and not _expired(payload, now):
                trigger_ids = [entry.context_id]
                break

    all_triggers = store.all("trigger")
    max_seq = max((e.seq for e in all_triggers), default=1)

    candidates: list[tuple[float, dict]] = []
    for trigger_id in trigger_ids:
        bundle = store.resolve(trigger_id)
        if bundle is None:
            skips.append(f"{trigger_id}: no merchant/category context pushed yet")
            continue
        trigger = bundle["trigger"]
        merchant_id = bundle["merchant_id"]
        mem = brain.memory(merchant_id)

        if mem.opted_out:
            skips.append(f"{trigger_id}: merchant opted out")
            continue
        if mem.hostile:
            skips.append(f"{trigger_id}: merchant signalled frustration")
            continue
        if _expired(trigger, now):
            skips.append(f"{trigger_id}: expired at {trigger.get('expires_at')}")
            continue
        key = trigger.get("suppression_key")
        if key and key in mem.spent_suppression:
            skips.append(f"{trigger_id}: suppression key already spent ({key})")
            continue
        to_customer = trigger.get("scope") == "customer"
        if to_customer and not _consented(bundle.get("customer")):
            skips.append(f"{trigger_id}: no consented customer context for a customer-scoped trigger")
            continue
        # A message to the merchant's customer is not a message to the merchant.
        # The two budgets are separate because the recipient is.
        if not to_customer:
            if mem.sends_used >= UNANSWERED_NUDGES_BEFORE_END and mem.replies_received == 0:
                skips.append(f"{trigger_id}: {mem.sends_used} nudges with no reply — standing down")
                continue
            if mem.sends_used >= MAX_SENDS_PER_MERCHANT_PER_WINDOW:
                skips.append(f"{trigger_id}: merchant already had {mem.sends_used} sends this window")
                continue
            if (len(mem.open_conversations) >= MAX_OPEN_CONVERSATIONS_PER_MERCHANT
                    and _urgency(trigger) < MIN_URGENCY_WHEN_MERCHANT_BUSY):
                skips.append(f"{trigger_id}: merchant has {len(mem.open_conversations)} open threads "
                             f"and this is urgency {_urgency(trigger)}")
                continue
        bundle["to_customer"] = to_customer
        candidates.append((_score(bundle, now, max_seq), bundle))

    candidates.sort(key=lambda pair: (-pair[0], pair[1]["trigger_id"]))

    actions: list[dict] = []
    used_merchants: dict[str, int] = {}
    used_customers: set[str] = set()
    for _score_value, bundle in candidates:
        if len(actions) >= MAX_ACTIONS_PER_TICK:
            break
        merchant_id = bundle["merchant_id"]
        if bundle.get("to_customer"):
            customer_id = (bundle.get("customer") or {}).get("customer_id")
            if customer_id in used_customers:
                skips.append(f"{bundle['trigger_id']}: already messaging this customer this tick")
                continue
        elif used_merchants.get(merchant_id, 0) >= MAX_ACTIONS_PER_MERCHANT_PER_TICK:
            skips.append(f"{bundle['trigger_id']}: already sending to this merchant this tick")
            continue

        mem = brain.memory(merchant_id)
        slug = (bundle.get("category") or {}).get("slug")
        composed: Composed | None = compose_unique(
            bundle, seen=mem.sent_hashes, now=now, fresh_digest_ids=store.new_digest_ids(slug))
        if composed is None:
            skips.append(f"{bundle['trigger_id']}: no grounded, non-repeating message available")
            continue

        conversation_id = _conversation_id(brain, merchant_id, composed.trigger_id)
        actions.append({
            "conversation_id": conversation_id,
            "merchant_id": merchant_id,
            "customer_id": composed.customer_id,
            "send_as": composed.send_as,
            "trigger_id": composed.trigger_id,
            "template_name": composed.template_name,
            "template_params": composed.template_params,
            "body": composed.body,
            "cta": composed.cta,
            "suppression_key": composed.suppression_key,
            "rationale": composed.rationale,
        })
        brain.register_send(composed.body, conversation_id, merchant_id, composed.customer_id,
                            composed.trigger_id, composed.send_as, composed.digest(),
                            composed.suppression_key)
        if composed.send_as == "merchant_on_behalf" and composed.customer_id:
            used_customers.add(composed.customer_id)
        else:
            used_merchants[merchant_id] = used_merchants.get(merchant_id, 0) + 1
    return actions, skips
