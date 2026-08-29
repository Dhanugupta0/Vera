"""Conversation state machine for /v1/reply.

Holds two kinds of memory:

* per conversation — turns, every body already sent (anti-repetition), whether
  we have moved from pitch mode into action mode, how many nudges have gone
  unanswered;
* per merchant — auto-reply fingerprints, opt-out and hostility flags, sends
  used in this test window, suppression keys already spent. Merchant-level
  memory is what makes an opt-out on one conversation stop *every* other
  conversation with that merchant, and what lets a canned auto-reply be
  recognised across separate conversation ids.
"""
from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from . import intent as intents
from .config import AUTO_REPLY_STRIKES_BEFORE_END
from .derive import build_ledger
from .guard import finalize
from .lang import Voice, customer_voice, merchant_voice, stable_seed, bi

OPEN, WAITING, ENDED = "open", "waiting", "ended"


@dataclass
class Turn:
    role: str
    text: str
    label: str = ""
    at: str = ""


@dataclass
class Conversation:
    conversation_id: str
    merchant_id: str = ""
    customer_id: str | None = None
    trigger_id: str = ""
    send_as: str = "vera"
    state: str = OPEN
    mode: str = "pitch"              # pitch -> action once the merchant commits
    turns: list[Turn] = field(default_factory=list)
    sent: list[str] = field(default_factory=list)
    sent_hashes: set[str] = field(default_factory=set)
    unanswered: int = 0
    apologised: bool = False
    end_reason: str = ""

    def record_send(self, body: str, digest: str) -> None:
        self.turns.append(Turn("bot", body))
        self.sent.append(body)
        self.sent_hashes.add(digest)
        self.unanswered += 1


@dataclass
class MerchantMemory:
    merchant_id: str
    auto_reply_fingerprints: Counter = field(default_factory=Counter)
    auto_reply_strikes: int = 0
    opted_out: bool = False
    hostile: bool = False
    sends_used: int = 0
    replies_received: int = 0
    spent_suppression: set[str] = field(default_factory=set)
    open_conversations: set[str] = field(default_factory=set)
    sent_hashes: set[str] = field(default_factory=set)


class Brain:
    def __init__(self, store) -> None:
        self.store = store
        self._lock = threading.RLock()
        self.conversations: dict[str, Conversation] = {}
        self.merchants: dict[str, MerchantMemory] = {}

    # -- memory -------------------------------------------------------------
    def memory(self, merchant_id: str) -> MerchantMemory:
        with self._lock:
            mem = self.merchants.get(merchant_id)
            if mem is None:
                mem = MerchantMemory(merchant_id)
                self.merchants[merchant_id] = mem
            return mem

    def conversation(self, conversation_id: str, merchant_id: str = "",
                     customer_id: str | None = None) -> Conversation:
        with self._lock:
            conv = self.conversations.get(conversation_id)
            if conv is None:
                conv = Conversation(conversation_id, merchant_id or "", customer_id)
                self.conversations[conversation_id] = conv
            if merchant_id and not conv.merchant_id:
                conv.merchant_id = merchant_id
            if customer_id and not conv.customer_id:
                conv.customer_id = customer_id
            return conv

    def register_send(self, action, conv_id: str, merchant_id: str, customer_id, trigger_id,
                      send_as: str, digest: str, suppression_key: str) -> None:
        conv = self.conversation(conv_id, merchant_id, customer_id)
        conv.trigger_id = trigger_id
        conv.send_as = send_as
        conv.record_send(action, digest)
        mem = self.memory(merchant_id)
        if send_as != "merchant_on_behalf":
            mem.sends_used += 1
            mem.open_conversations.add(conv_id)
        mem.sent_hashes.add(digest)
        if suppression_key:
            mem.spent_suppression.add(suppression_key)

    def clear(self) -> None:
        with self._lock:
            self.conversations.clear()
            self.merchants.clear()

    # -- the reply decision -------------------------------------------------
    def handle_reply(self, body: dict) -> dict:
        conv_id = str(body.get("conversation_id") or "conv_unknown")
        merchant_id = body.get("merchant_id") or ""
        customer_id = body.get("customer_id")
        message = str(body.get("message") or "")
        from_role = str(body.get("from_role") or "merchant")

        conv = self.conversation(conv_id, merchant_id, customer_id)
        mem = self.memory(conv.merchant_id or merchant_id or "unknown")

        fp = intents.fingerprint(message)
        repeats = mem.auto_reply_fingerprints.get(fp, 0) if fp else 0
        verdict = intents.classify(message, repeat_count=repeats,
                                   in_action_mode=(conv.mode == "action"))
        conv.turns.append(Turn(from_role, message, verdict.label))
        conv.unanswered = 0
        mem.replies_received += 1

        ctx = self._context_for(conv, merchant_id, customer_id, from_role)
        handler = getattr(self, f"_on_{verdict.label}", self._on_unknown)
        result = handler(conv, mem, verdict, ctx, message)
        return self._emit(conv, mem, result, ctx)

    # -- context ------------------------------------------------------------
    def _context_for(self, conv: Conversation, merchant_id: str, customer_id, from_role: str) -> dict:
        mid = conv.merchant_id or merchant_id
        merchant = self.store.get("merchant", mid) or {}
        category = self.store.category_for_merchant(merchant) or {}
        customer = self.store.get("customer", conv.customer_id or customer_id) or {}
        trigger = self.store.get("trigger", conv.trigger_id) or {}
        bundle = {"merchant": merchant, "category": category, "trigger": trigger,
                  "customer": customer or None, "merchant_id": mid}
        led = build_ledger(bundle)
        seed = stable_seed(mid, conv.conversation_id, len(conv.turns))
        to_customer = (conv.send_as == "merchant_on_behalf") or from_role == "customer"
        voice = (customer_voice(customer, merchant, seed) if to_customer and customer
                 else merchant_voice(merchant, category, seed))
        return {"led": led, "voice": voice, "merchant": merchant, "category": category,
                "customer": customer, "to_customer": to_customer, "bundle": bundle}

    # -- handlers -----------------------------------------------------------
    def _on_opt_out(self, conv, mem, verdict, ctx, message) -> dict:
        mem.opted_out = True
        conv.state = ENDED
        conv.end_reason = f"merchant opted out ('{verdict.evidence}')"
        return {"action": "end",
                "rationale": f"Explicit opt-out detected ('{verdict.evidence}'). Closing this "
                             f"conversation and suppressing every queued trigger for this merchant "
                             f"for the rest of the window — no further sends on any conversation."}

    def _on_hostile(self, conv, mem, verdict, ctx, message) -> dict:
        mem.hostile = True
        if conv.apologised:
            conv.state = ENDED
            conv.end_reason = "sustained frustration after one apology"
            return {"action": "end",
                    "rationale": "Second hostile turn after an apology. Nothing useful is left to "
                                 "send; closing rather than spending another turn."}
        conv.apologised = True
        voice: Voice = ctx["voice"]
        body = voice.pick([
            bi("Apologies — I won't message again unless you ask. If anything changes, just reply "
               "here and I'll pick it up.",
               "Maafi chahti hoon — bina kahe dobara message nahi karungi. Kabhi zaroorat ho to "
               "yahin reply kar dijiye."),
            bi("Understood, and sorry for the interruption. I'll stop here.",
               "Samajh gayi, disturb karne ke liye maafi. Main yahin ruk jati hoon."),
        ])
        return {"action": "send", "body": body, "cta": "none",
                "rationale": f"Merchant is frustrated ('{verdict.evidence}') but has not asked to "
                             f"unsubscribe. One short apology with an opt-back-in path, no ask, no "
                             f"defence. Any further hostility ends the conversation."}

    def _on_auto_reply(self, conv, mem, verdict, ctx, message) -> dict:
        fp = intents.fingerprint(message)
        if fp:
            mem.auto_reply_fingerprints[fp] += 1
        mem.auto_reply_strikes += 1
        strikes = mem.auto_reply_strikes
        voice: Voice = ctx["voice"]

        if strikes >= AUTO_REPLY_STRIKES_BEFORE_END:
            conv.state = ENDED
            conv.end_reason = "auto-responder only"
            return {"action": "end",
                    "rationale": f"Auto-reply #{strikes} from this merchant ({verdict.evidence}). "
                                 f"Three canned responses and no human turn means the owner is not "
                                 f"at this handset; closing instead of burning more turns. Production "
                                 f"Vera spends 2-3 turns per auto-reply — this caps it at one."}
        if strikes == 2:
            conv.state = WAITING
            return {"action": "wait", "wait_seconds": 86400,
                    "rationale": f"Same canned text twice ({verdict.evidence}) across this merchant's "
                                 f"threads. Backing off 24h so the retry lands when a human is at the "
                                 f"phone rather than replying to a machine."}

        led = ctx["led"]
        hook = self._one_line_value(ctx)
        body = voice.pick([
            bi(f"Looks like an auto-reply. {hook} When the owner sees this, a single YES is enough.",
               f"Ye auto-reply lag raha hai. {hook} Jab owner dekhein, sirf YES kaafi hai."),
            bi(f"That reads like a canned response. {hook} Reply YES whenever the owner is on the phone.",
               f"Ye canned response lagta hai. {hook} Owner phone par ho tab YES bhej dijiye."),
        ])
        return {"action": "send", "body": body, "cta": "binary_yes_no",
                "rationale": f"Auto-reply detected on turn 1 ({verdict.evidence}). One explicit, "
                             f"low-effort prompt aimed at the owner rather than the responder, with "
                             f"the value restated in a single line. No further re-pitching."}

    def _on_commitment(self, conv, mem, verdict, ctx, message) -> dict:
        """The merchant said yes. Execute — never ask another qualifying question."""
        conv.mode = "action"
        voice: Voice = ctx["voice"]
        plan = self._action_plan(ctx)
        body = voice.pick([
            bi(f"Done — starting now. {plan['doing']} {plan['confirm']}",
               f"Theek hai — abhi shuru. {plan['doing_hi']} {plan['confirm_hi']}"),
            bi(f"On it. {plan['doing']} {plan['confirm']}",
               f"Kar rahi hoon. {plan['doing_hi']} {plan['confirm_hi']}"),
        ])
        return {"action": "send", "body": body, "cta": "binary_confirm_cancel",
                "rationale": f"Explicit commitment ('{verdict.evidence}'). Switching from pitch to "
                             f"execution in the same turn — stating what is being done, with what "
                             f"scope, and a single CONFIRM. No re-qualification (the Pattern D failure)."}

    def _on_question(self, conv, mem, verdict, ctx, message) -> dict:
        voice: Voice = ctx["voice"]
        answer = self._answer_from_context(ctx, message)
        plan = self._action_plan(ctx)
        if answer:
            body = voice.say(bi(f"{answer} {plan['confirm']}", f"{answer} {plan['confirm_hi']}"))
            why = "Answered from the pushed contexts, then restated the single next step."
        else:
            body = voice.say(bi(
                f"I don't have that in front of me, so I won't guess. {plan['doing']} {plan['confirm']}",
                f"Wo mere paas nahi hai, to andaza nahi lagaungi. {plan['doing_hi']} {plan['confirm_hi']}"))
            why = ("The answer is not in any pushed context, so the bot says so rather than "
                   "fabricating, and keeps the thread moving.")
        return {"action": "send", "body": body, "cta": "binary_confirm_cancel",
                "rationale": f"Merchant asked a question. {why}"}

    def _on_off_topic(self, conv, mem, verdict, ctx, message) -> dict:
        voice: Voice = ctx["voice"]
        topic = verdict.topic or "that"
        plan = self._action_plan(ctx)
        topic = topic[0].upper() + topic[1:] if topic else "That"
        body = voice.say(bi(
            f"{topic} is outside what I can do — that one's for your CA. "
            f"Back to what I can. {plan['doing']} {plan['confirm']}",
            f"{topic} mere haath mein nahi hai — wo aapke CA ka kaam hai. "
            f"Jo main kar sakti hoon wo ye. {plan['doing_hi']} {plan['confirm_hi']}"))
        return {"action": "send", "body": body, "cta": "binary_confirm_cancel",
                "rationale": f"Out-of-scope request ({verdict.evidence}). Declined in one clause "
                             f"without over-apologising, then redirected to the original thread so "
                             f"the turn is not wasted."}

    def _on_objection(self, conv, mem, verdict, ctx, message) -> dict:
        if conv.mode == "objection_answered":
            conv.state = ENDED
            conv.end_reason = "objection restated after one answer"
            return {"action": "end",
                    "rationale": "Objection repeated after being answered once. Pushing a third time "
                                 "costs goodwill; closing while the relationship is intact."}
        conv.mode = "objection_answered"
        voice: Voice = ctx["voice"]
        led = ctx["led"]
        anchor = self._one_line_value(ctx)
        body = voice.say(bi(
            f"Fair. {anchor} Nothing to pay and nothing to install — I do it from my side and you "
            f"see the numbers next week. Reply YES only if that's worth a week.",
            f"Sahi baat. {anchor} Na kuch dena hai, na install karna — main apni taraf se karti hoon, "
            f"agle hafte numbers dikh jayenge. Agar ek hafta dene layak lage to YES bolein."))
        return {"action": "send", "body": body, "cta": "binary_yes_no",
                "rationale": f"Objection ('{verdict.evidence}') answered once, with the cost and "
                             f"effort removed rather than the pitch repeated. If it comes back, the "
                             f"conversation ends."}

    def _on_defer(self, conv, mem, verdict, ctx, message) -> dict:
        conv.state = WAITING
        return {"action": "wait", "wait_seconds": 14400,
                "rationale": f"Merchant asked for time ('{verdict.evidence}'). Backing off 4 hours "
                             f"rather than answering into a busy moment; the trigger is still live "
                             f"so the thread can resume."}

    def _on_negative(self, conv, mem, verdict, ctx, message) -> dict:
        conv.state = ENDED
        conv.end_reason = "declined"
        return {"action": "end",
                "rationale": "Merchant declined. Taking the no at face value and closing without a "
                             "counter-offer — one more push here is what turns a no into an opt-out."}

    def _on_ack(self, conv, mem, verdict, ctx, message) -> dict:
        if len(conv.turns) >= 5:
            conv.state = ENDED
            conv.end_reason = "acknowledged, nothing pending"
            return {"action": "end", "rationale": "Merchant acknowledged and nothing is pending. "
                                                  "Closing rather than manufacturing another turn."}
        voice: Voice = ctx["voice"]
        plan = self._action_plan(ctx)
        body = voice.say(bi(f"{plan['doing']} {plan['confirm']}",
                            f"{plan['doing_hi']} {plan['confirm_hi']}"))
        return {"action": "send", "body": body, "cta": "binary_confirm_cancel",
                "rationale": "Acknowledgement with no direction. Advancing to the concrete next step "
                             "instead of asking what they'd like."}

    def _on_unknown(self, conv, mem, verdict, ctx, message) -> dict:
        if conv.mode == "action":
            return self._on_commitment(conv, mem, verdict, ctx, message)
        voice: Voice = ctx["voice"]
        plan = self._action_plan(ctx)
        body = voice.say(bi(f"{plan['doing']} {plan['confirm']}",
                            f"{plan['doing_hi']} {plan['confirm_hi']}"))
        return {"action": "send", "body": body, "cta": "binary_confirm_cancel",
                "rationale": "Reply did not match a known intent, so the bot advances the one thread "
                             "it already opened with a concrete step rather than guessing."}

    # -- content helpers ----------------------------------------------------
    def _action_plan(self, ctx: dict) -> dict:
        """The concrete deliverable for this merchant, from their own signals."""
        led = ctx["led"]
        biz_gap = led.val("headline_gap")
        offer = led.get("catalog_offer") or led.get("offer_active")
        scope = scope_hi = ""
        if led.has("cust_high_risk"):
            scope = f"{led.get('cust_high_risk')} patients on your high-risk list"
            scope_hi = f"aapki high-risk list ke {led.get('cust_high_risk')} patients"
        elif led.has("cust_lapsed"):
            scope = f"{led.get('cust_lapsed')} customers who have gone quiet"
            scope_hi = f"{led.get('cust_lapsed')} shant ho chuke customers"
        elif led.has("cust_total"):
            scope = f"your {led.get('cust_total')} customers this year"
            scope_hi = f"is saal ke aapke {led.get('cust_total')} customers"

        if biz_gap == "unverified_gbp":
            doing = "Starting Google verification on your listing today."
            doing_hi = "Aaj aapki listing ki Google verification shuru kar rahi hoon."
        elif biz_gap in ("no_active_offers",) and offer:
            doing = f"Putting {offer} live on your listing."
            doing_hi = f"{offer} aapki listing par live kar rahi hoon."
        elif biz_gap in ("stale_posts", "no_recent_post"):
            doing = "Drafting 3 Google posts for your listing now."
            doing_hi = "Abhi aapki listing ke liye 3 Google posts draft kar rahi hoon."
        elif led.has("action_gap"):
            doing = (f"Working the {led.get('action_gap')}-action gap between your "
                     f"{led.get('perf_ctr')} and the {led.get('peer_ctr')} peer median.")
            doing_hi = (f"Aapke {led.get('perf_ctr')} aur peer median {led.get('peer_ctr')} ke beech "
                        f"{led.get('action_gap')}-action gap par kaam shuru.")
        elif offer:
            doing = f"Putting {offer} live on your listing."
            doing_hi = f"{offer} aapki listing par live kar rahi hoon."
        else:
            doing = "Drafting the listing update now."
            doing_hi = "Listing update abhi draft kar rahi hoon."

        confirm = (f"Reply CONFIRM and it goes live for {scope}." if scope
                   else "Reply CONFIRM and it goes live today.")
        confirm_hi = (f"CONFIRM bolein, {scope_hi} ke liye live ho jayega." if scope_hi
                      else "CONFIRM bolein, aaj hi live ho jayega.")
        return {"doing": doing, "doing_hi": doing_hi,
                "confirm": confirm, "confirm_hi": confirm_hi}

    def _one_line_value(self, ctx: dict) -> str:
        led = ctx["led"]
        voice: Voice = ctx["voice"]
        if led.has("action_gap", "perf_ctr", "peer_ctr"):
            return voice.say(bi(
                f"Short version: {led.get('action_gap')} actions a month sit between your "
                f"{led.get('perf_ctr')} and the {led.get('peer_ctr')} peer median.",
                f"Chhoti baat: aapke {led.get('perf_ctr')} aur peer median {led.get('peer_ctr')} ke "
                f"beech har mahine {led.get('action_gap')} actions ka farak hai."))
        if led.has("headline_gap"):
            return voice.say(bi(f"Short version: {led.get('headline_gap')}.",
                                f"Chhoti baat: {led.get('headline_gap')}."))
        if led.has("digest_title"):
            return voice.say(bi(f"Short version: {led.get('digest_title')}.",
                                f"Chhoti baat: {led.get('digest_title')}."))
        return ""

    def _answer_from_context(self, ctx: dict, message: str) -> str:
        """Answer only what the pushed contexts actually contain."""
        led = ctx["led"]
        voice: Voice = ctx["voice"]
        norm = intents.normalise(message)
        if any(w in norm for w in ("price", "cost", "kitna", "charge", "fee", "rate")):
            offer = led.get("offer_active") or led.get("catalog_offer")
            if offer:
                return voice.say(bi(f"On price: {offer} is the listed format.",
                                    f"Price ki baat: {offer} listed format hai."))
        if any(w in norm for w in ("source", "study", "paper", "research", "citation", "proof")):
            if led.has("digest_source"):
                return voice.say(bi(f"Source is {led.get('digest_source')}.",
                                    f"Source hai {led.get('digest_source')}."))
        if any(w in norm for w in ("view", "call", "number", "performance", "ctr", "traffic")):
            if led.has("perf_views", "perf_calls"):
                return voice.say(bi(
                    f"Last {led.get('perf_window')}: {led.get('perf_views')} views and "
                    f"{led.get('perf_calls')} calls.",
                    f"Pichle {led.get('perf_window')}: {led.get('perf_views')} views aur "
                    f"{led.get('perf_calls')} calls."))
        if any(w in norm for w in ("competitor", "other", "peer", "compare")):
            if led.has("peer_ctr"):
                return voice.say(bi(f"Peer median in your category is {led.get('peer_ctr')}.",
                                    f"Aapki category ka peer median {led.get('peer_ctr')} hai."))
        return ""

    # -- emit ---------------------------------------------------------------
    def _emit(self, conv: Conversation, mem: MerchantMemory, result: dict, ctx: dict) -> dict:
        action = result.get("action")
        if action != "send":
            if action == "end":
                conv.state = ENDED
                mem.open_conversations.discard(conv.conversation_id)
            return {k: v for k, v in result.items() if v is not None}

        led = ctx["led"]
        taboo = led.val("voice_taboo", []) or []
        body, issues = finalize(result.get("body", ""), led, taboo)
        if not body:
            conv.state = ENDED
            return {"action": "end",
                    "rationale": "Nothing groundable left to say on this thread; closing rather "
                                 "than sending filler."}
        import hashlib
        digest = hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]
        if digest in conv.sent_hashes:
            conv.state = ENDED
            return {"action": "end",
                    "rationale": "The only remaining reply would repeat a message already sent in "
                                 "this conversation. Closing instead of triggering anti-repetition."}
        conv.record_send(body, digest)
        conv.unanswered = 0
        mem.sent_hashes.add(digest)
        rationale = result.get("rationale", "")
        if issues:
            rationale += " Guard actions: " + ", ".join(sorted(set(issues))) + "."
        return {"action": "send", "body": body, "cta": result.get("cta", "open_ended"),
                "rationale": rationale}
