"""Multi-turn handling (challenge brief §7.4).

Wraps the same state machine the live `/v1/reply` endpoint uses, so an offline
replay scores identically to the hosted bot.

    state = ConversationState(merchant=merchant_dict, category=category_dict,
                              trigger=trigger_dict, customer=None)
    respond(state, "Ok lets do it, whats next?")
"""
from __future__ import annotations

from dataclasses import dataclass, field

from vera.dialog import Brain
from vera.store import ContextStore


@dataclass
class ConversationState:
    merchant: dict
    category: dict
    trigger: dict | None = None
    customer: dict | None = None
    conversation_id: str = "conv_offline_001"
    turn: int = 1
    _brain: Brain | None = field(default=None, repr=False)

    def _ensure(self) -> Brain:
        if self._brain is None:
            store = ContextStore()
            slug = self.category.get("slug") or self.merchant.get("category_slug")
            store.put("category", slug, 1, self.category)
            store.put("merchant", self.merchant["merchant_id"], 1, self.merchant)
            if self.customer:
                store.put("customer", self.customer["customer_id"], 1, self.customer)
            if self.trigger:
                store.put("trigger", self.trigger["id"], 1, self.trigger)
            brain = Brain(store)
            conv = brain.conversation(self.conversation_id, self.merchant["merchant_id"],
                                      (self.customer or {}).get("customer_id"))
            if self.trigger:
                conv.trigger_id = self.trigger["id"]
            self._brain = brain
        return self._brain


def respond(state: ConversationState, merchant_message: str) -> dict:
    """Returns {action: send|wait|end, body?, cta?, wait_seconds?, rationale}."""
    brain = state._ensure()
    state.turn += 1
    return brain.handle_reply({
        "conversation_id": state.conversation_id,
        "merchant_id": state.merchant.get("merchant_id"),
        "customer_id": (state.customer or {}).get("customer_id"),
        "from_role": "customer" if state.customer else "merchant",
        "message": merchant_message,
        "turn_number": state.turn,
    })
