"""Offline composition entry point (challenge brief §7.1).

The graded surface is the HTTP service in `vera/app.py`; this module exposes the
same composer through the plain function signature the brief specifies, so the
same code can be replayed offline against any (category, merchant, trigger,
customer) tuple.

    from bot import compose
    compose(category_dict, merchant_dict, trigger_dict, customer_dict_or_None)

Deterministic: the same inputs always produce the same output, on any machine,
in any process. No LLM call, no network, no clock dependence beyond the `now`
you pass in.
"""
from __future__ import annotations

from datetime import datetime, timezone

from vera.compose import compose as _compose


def compose(category: dict, merchant: dict, trigger: dict,
            customer: dict | None = None, now: datetime | None = None) -> dict:
    """Returns {body, cta, send_as, suppression_key, rationale, ...}."""
    bundle = {
        "category": category or {},
        "merchant": merchant or {},
        "trigger": trigger or {},
        "customer": customer,
        "merchant_id": (merchant or {}).get("merchant_id", ""),
        "trigger_id": (trigger or {}).get("id", ""),
    }
    result = _compose(bundle, now=now or datetime.now(timezone.utc))
    if result is None:
        return {
            "body": "",
            "cta": "none",
            "send_as": "vera",
            "suppression_key": (trigger or {}).get("suppression_key", ""),
            "rationale": "No verifiable fact was available in these contexts, so nothing was sent. "
                         "Silence beats a generic nudge.",
            "template_name": "",
            "template_params": [],
        }
    return {
        "body": result.body,
        "cta": result.cta,
        "send_as": result.send_as,
        "suppression_key": result.suppression_key,
        "rationale": result.rationale,
        "template_name": result.template_name,
        "template_params": result.template_params,
        "customer_id": result.customer_id,
        "facts_used": result.facts_used,
    }
