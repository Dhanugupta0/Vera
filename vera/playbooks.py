"""Per-trigger-kind composition playbooks.

Each playbook answers three questions in order, because that is the order a
merchant reads a WhatsApp message in:

    hook     why am I hearing from you *right now*  (the trigger, with a fact)
    insight  why does that matter to *me*           (this merchant's numbers)
    ask      what do you want me to do              (exactly one thing, last)

Every playbook has two paths. The rich path uses `trigger.payload`. The thin
path fires when the payload is a placeholder — 75 of the 100 dataset triggers
are — and rebuilds specificity from the performance snapshot and the category
pack, which are the only fields present on all 50 merchants. A playbook is only
allowed to state something the ledger can source.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from .derive import Ledger
from .lang import Voice, bi

DUR_HI = {"days": "din", "day": "din", "months": "mahine", "month": "mahina",
          "weeks": "hafte", "week": "hafta", "years": "saal", "year": "saal",
          "hours": "ghante", "hour": "ghanta"}


def dur(p: "P", value: str) -> str:
    """Render a duration the ledger already unit-tagged, in the active voice.

    The ledger says '38 days'; a code-mix message wants '38 din'. Templates must
    never append their own unit or you get '38 days din'.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if not p.voice.code_mix:
        return text
    parts = text.split()
    if len(parts) == 2 and parts[1].lower() in DUR_HI:
        return f"{parts[0]} {DUR_HI[parts[1].lower()]}"
    return text


def _days(value: str) -> str:
    """A bare integer out of a payload is a count, not a phrase."""
    text = str(value or "").strip()
    if text and text.replace(",", "").isdigit():
        return f"{text} days"
    return text


CTA_BINARY = "binary_yes_no"
CTA_CONFIRM = "binary_confirm_cancel"
CTA_OPEN = "open_ended"
CTA_SLOT = "multi_choice_slot"
CTA_NONE = "none"


@dataclass
class Draft:
    hook: str
    insight: str = ""
    ask: str = ""
    cta: str = CTA_OPEN
    citation: str = ""
    template: str = "vera_nudge_v1"
    levers: tuple = ()
    send_as: str | None = None
    suppression: str | None = None
    skip: bool = False           # playbook decided this trigger isn't worth a send
    skip_reason: str = ""


@dataclass
class P:
    led: Ledger
    voice: Voice
    trigger: dict
    merchant: dict
    category: dict
    customer: dict | None
    seed: int

    def f(self, key: str, default: str = "") -> str:
        return self.led.get(key, default)

    def has(self, *keys: str) -> bool:
        return self.led.has(*keys)

    def v(self, key: str, default=None):
        return self.led.val(key, default)

    def say(self, fragment) -> str:
        return self.voice.say(fragment)

    def pick(self, options, offset: int = 0) -> str:
        return self.voice.pick(options, offset)

    @property
    def thin(self) -> bool:
        return self.led.has("payload_thin")


# ---------------------------------------------------------------------------
# reusable specificity engines — these are what keep a thin trigger specific
# ---------------------------------------------------------------------------

def _issue_label(source: str) -> str:
    """'JIDA Oct 2026, p.14' -> 'JIDA Oct 2026'; 'ICMR, Apr 2026' -> 'ICMR Apr 2026'."""
    text = (source or "").strip()
    if not text:
        return ""
    parts = [seg.strip() for seg in text.split(",") if seg.strip()]
    parts = [seg for seg in parts if not re.match(r"^p\.?\s*\d+$", seg, re.I)]
    return " ".join(parts)


def peer_gap_line(p: P) -> str:
    """The universal fallback: two always-present numbers become a real claim."""
    if p.has("perf_views", "perf_ctr", "peer_ctr", "actions_at_peer", "actions_now", "action_gap"):
        scope = p.f("peer_scope", "your category")
        return p.say(bi(
            f"Your listing pulled {p.f('perf_views')} views in {p.f('perf_window')} but converted at "
            f"{p.f('perf_ctr')} — the peer median for {scope} is {p.f('peer_ctr')}. Same views at the "
            f"median would be {p.f('actions_at_peer')} actions instead of {p.f('actions_now')}, "
            f"a gap of {p.f('action_gap')}.",
            f"{dur(p, p.f('perf_window'))} mein {p.f('perf_views')} views aaye, par conversion {p.f('perf_ctr')} "
            f"raha — {scope} ka peer median {p.f('peer_ctr')} hai. Wahi views median par "
            f"{p.f('actions_at_peer')} actions dete, abhi {p.f('actions_now')} hain — "
            f"{p.f('action_gap')} ka gap.",
        ))
    if p.has("perf_views", "perf_ctr", "peer_ctr") and p.f("perf_ctr") != p.f("peer_ctr"):
        return p.say(bi(
            f"You're at {p.f('perf_ctr')} conversion on {p.f('perf_views')} views this month; "
            f"the peer median is {p.f('peer_ctr')}.",
            f"Is mahine {p.f('perf_views')} views par conversion {p.f('perf_ctr')} hai; "
            f"peer median {p.f('peer_ctr')} hai.",
        ))
    if p.has("perf_views", "perf_calls"):
        return p.say(bi(
            f"{p.f('perf_views')} views in {p.f('perf_window')} turned into {p.f('perf_calls')} calls.",
            f"{dur(p, p.f('perf_window'))} mein {p.f('perf_views')} views se {p.f('perf_calls')} calls aaye.",
        ))
    return ""


def traffic_line(p: P) -> str:
    """Views -> calls, with no peer comparison. Safe when a comparison would
    contradict the trigger (e.g. a 'spike' on a below-median listing)."""
    if p.has("perf_views", "perf_calls"):
        return p.say(bi(
            f"{p.f('perf_views')} views in {p.f('perf_window')} produced {p.f('perf_calls')} calls.",
            f"{dur(p, p.f('perf_window'))} mein {p.f('perf_views')} views se "
            f"{p.f('perf_calls')} calls aaye.",
        ))
    return ""


ROUND_MARKS = (100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000)


def crossed_mark(value) -> int | None:
    """The largest round threshold this number has passed — a real milestone
    derived from a real number, rather than one invented for a thin payload."""
    try:
        n = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    marks = [m for m in ROUND_MARKS if m <= n]
    return marks[-1] if marks else None


def strength_line(p: P) -> str:
    """For merchants above the peer median — social proof pointed back at them."""
    if p.has("action_surplus", "perf_ctr", "peer_ctr"):
        return p.say(bi(
            f"You're converting at {p.f('perf_ctr')} against a {p.f('peer_ctr')} peer median — "
            f"about {p.f('action_surplus')} extra actions a month on the same traffic.",
            f"Aapka conversion {p.f('perf_ctr')} hai, peer median {p.f('peer_ctr')} — "
            f"same traffic par lagbhag {p.f('action_surplus')} extra actions har mahine.",
        ))
    if p.has("calls_above_peer", "perf_calls"):
        return p.say(bi(
            f"{p.f('perf_calls')} calls this month — {p.f('calls_above_peer')} above the peer median.",
            f"Is mahine {p.f('perf_calls')} calls — peer median se {p.f('calls_above_peer')} zyada.",
        ))
    return ""


def offer_gap_line(p: P) -> str:
    if p.has("offer_none") and p.has("catalog_offer"):
        return p.say(bi(
            f"Your listing has no live offer right now. The format that moves in "
            f"{p.f('category_name').lower()} is service+price, not a flat discount — "
            f"something like {p.f('catalog_offer')}.",
            f"Abhi aapki listing par koi live offer nahi hai. "
            f"{p.f('category_name')} mein service+price chalta hai, flat discount nahi — "
            f"jaise {p.f('catalog_offer')}.",
        ))
    if p.has("offer_active"):
        return p.say(bi(
            f"Your {p.f('offer_active')} is the one doing the work on the listing.",
            f"Listing par abhi {p.f('offer_active')} hi kaam kar raha hai.",
        ))
    return ""


def digest_line(p: P, prefix: str = "digest") -> str:
    title = p.f(f"{prefix}_title")
    if not title:
        return ""
    parts = [title.rstrip(".") + "."]
    trial = p.f(f"{prefix}_trial_n")
    summary = p.f(f"{prefix}_summary")
    if trial and summary:
        parts.append(f"{trial}-patient trial. {summary}")
    elif summary:
        parts.append(summary)
    return " ".join(parts)


def trend_line(p: P) -> str:
    if not p.has("trend_query", "trend_delta"):
        return ""
    seg = p.f("trend_segment")
    tail = f" — concentrated in the {seg} band" if seg else ""
    return p.say(bi(
        f"'{p.f('trend_query')}' searches are up {p.f('trend_delta')} year-on-year{tail}.",
        f"'{p.f('trend_query')}' searches saal-dar-saal {p.f('trend_delta')} badhe hain{tail}.",
    ))


def season_line(p: P) -> str:
    note = p.f("season_note_full")
    window = p.f("season_window")
    if not note:
        return ""
    return p.say(bi(
        f"Category pattern for {window}: {note}." if window else f"{note}.",
        f"{window} mein category ka pattern: {note}." if window else f"{note}.",
    ))


def citation_of(p: P, prefix: str = "digest") -> str:
    src = p.f(f"{prefix}_source")
    return f" — {src}" if src else ""


def best_insight(p: P) -> str:
    """Pick the strongest merchant-specific line available, in priority order."""
    for fn in (peer_gap_line, strength_line, offer_gap_line):
        line = fn(p)
        if line:
            return line
    return season_line(p) or trend_line(p)


# ---------------------------------------------------------------------------
# merchant-facing playbooks
# ---------------------------------------------------------------------------

def pb_research_digest(p: P) -> Draft:
    body = digest_line(p)
    if not body:
        return pb_default_merchant(p)
    fresh = "just landed" if p.has("digest_is_new") else "landed"
    issue = _issue_label(p.f("digest_source")) or p.f("journal") or "this week's digest"
    hook = p.say(bi(
        f"{issue} {fresh}. {body}",
        f"{issue} {fresh}. {body}",
    ))
    seg = p.f("digest_segment")
    if seg and p.has("cust_high_risk"):
        insight = p.say(bi(
            f"Directly relevant to the {p.f('cust_high_risk')} {seg} on your list.",
            f"Aapki list ke {p.f('cust_high_risk')} {seg} par seedha lagu hota hai.",
        ))
    elif seg:
        insight = p.say(bi(
            f"Applies to your {seg} cohort, not the low-risk ones.",
            f"Ye aapke {seg} cohort par lagta hai, low-risk par nahi.",
        ))
    else:
        insight = p.f("digest_actionable") and f"Practical read: {p.f('digest_actionable').rstrip('.')}." or best_insight(p)
    ask = p.pick([
        bi("Want the 2-minute abstract plus a patient-facing note you can forward? Reply YES.",
           "2-minute abstract aur ek patient-facing note bhej dun jo aap forward kar sakein? YES bolein."),
        bi("Should I pull the abstract and draft the customer version? Reply YES.",
           "Abstract nikaal kar customer version draft kar dun? YES bhej dijiye."),
    ])
    return Draft(hook, insight, ask, CTA_BINARY, citation_of(p), "vera_research_digest_v1",
                 ("specificity", "reciprocity", "effort_externalization"))


def pb_regulation_change(p: P) -> Draft:
    prefix = "digest" if p.f("digest_kind") == "compliance" else ("compliance" if p.has("compliance_title") else "digest")
    title = p.f(f"{prefix}_title")
    if not title:
        return pb_default_merchant(p)
    deadline = p.f("tp_deadline_iso") or p.f("tp_effective_date")
    hook = p.say(bi(
        f"Compliance item, not a promo: {title.rstrip('.')}.",
        f"Ek compliance baat hai, promo nahi: {title.rstrip('.')}.",
    ))
    summary = p.f(f"{prefix}_summary")
    detail = summary or p.f(f"{prefix}_actionable")
    insight = ""
    if detail:
        insight = detail.rstrip(".") + "."
    raw_deadline = str((p.trigger.get("payload") or {}).get("deadline_iso") or "")
    already_shown = bool(raw_deadline and raw_deadline in f"{title} {summary}")
    if deadline and not already_shown:
        insight += p.say(bi(f" That lands {deadline}.", f" Ye {deadline} se lagu hota hai."))
    action = p.f(f"{prefix}_actionable")
    if action and action not in insight:
        insight += p.say(bi(f" Your side: {action.rstrip('.')}.", f" Aapko: {action.rstrip('.')}."))
    ask = p.pick([
        bi("Want me to put a dated reminder and a one-page checklist in your dashboard? Reply YES.",
           "Ek dated reminder aur one-page checklist dashboard mein daal dun? YES bolein."),
        bi("Should I send the checklist so your staff can verify it this week? Reply YES.",
           "Checklist bhej dun taaki staff is hafte verify kar le? YES bhej dijiye."),
    ])
    return Draft(hook, insight, ask, CTA_BINARY, citation_of(p, prefix), "vera_compliance_v1",
                 ("specificity", "loss_aversion", "effort_externalization"))


def pb_supply_alert(p: P) -> Draft:
    molecule = p.f("tp_molecule")
    batches = p.f("tp_affected_batches")
    if not molecule:
        return pb_regulation_change(p) if p.has("compliance_title") or p.has("digest_title") else pb_default_merchant(p)
    hook = p.say(bi(
        f"Recall notice on {molecule} — batches {batches}." if batches
        else f"Recall notice on {molecule}.",
        f"{molecule} par recall notice hai — batches {batches}." if batches
        else f"{molecule} par recall notice hai.",
    ))
    mfr = p.f("tp_manufacturer")
    insight = p.say(bi(
        f"Manufacturer {mfr}. Pull those batches off the shelf before the next counter shift and "
        f"check your last dispense log for the same numbers." if mfr else
        "Pull those batches off the shelf before the next counter shift and check your dispense log.",
        f"Manufacturer {mfr}. Agli counter shift se pehle wo batches shelf se hata dein aur "
        f"dispense log check kar lein." if mfr else
        "Agli shift se pehle wo batches shelf se hata dein aur dispense log check kar lein.",
    ))
    ask = p.say(bi(
        "Want me to post a 'batch checked — safe stock' note on your Google listing so walk-ins see it? Reply YES.",
        "Aapki Google listing par 'batch checked — safe stock' note daal dun taaki walk-ins ko dikhe? YES bolein.",
    ))
    alert_id = (p.trigger.get("payload") or {}).get("alert_id")
    citation = citation_of(p) if alert_id and p.f("digest_id") == alert_id else ""
    return Draft(hook, insight, ask, CTA_BINARY, citation, "vera_supply_alert_v1",
                 ("specificity", "loss_aversion", "reciprocity"))


def pb_perf_dip(p: P) -> Draft:
    metric = p.f("tp_metric", "calls")
    delta = p.f("tp_delta") or p.f("tp_delta_pct")
    window = p.f("tp_window", "7d")
    baseline = p.f("tp_vs_baseline")
    if delta:
        hook = p.say(bi(
            f"Your {metric} are down {delta} over the last {window}"
            + (f", from a baseline of {baseline}." if baseline else "."),
            f"Pichle {dur(p, window)} mein aapke {metric} {delta} gire hain"
            + (f", baseline {baseline} tha." if baseline else "."),
        ))
    elif p.has("delta_calls_pct_phrase") and p.v("delta_calls_pct", 0) < 0:
        hook = p.say(bi(
            f"Your {p.f('delta_calls_pct_phrase')} — worth 30 seconds.",
            f"Aapke {p.f('delta_calls_pct_phrase')} — 30 second ki baat hai.",
        ))
    else:
        hook = p.say(bi(
            f"Your numbers moved this week and not the way you'd want.",
            f"Is hafte aapke numbers galat direction mein gaye hain.",
        ))
    if p.led.val("ctr_position") == "above" and p.has("perf_ctr", "peer_ctr"):
        insight = p.say(bi(
            f"Your conversion is not the problem — {p.f('perf_ctr')} against a {p.f('peer_ctr')} "
            f"peer median. Fewer people are reaching the listing, so this is a discovery drop.",
            f"Conversion problem nahi hai — aapka {p.f('perf_ctr')} hai, peer median {p.f('peer_ctr')}. "
            f"Log listing tak kam pahunch rahe hain, ye discovery ki dikkat hai.",
        ))
    else:
        insight = peer_gap_line(p) or offer_gap_line(p)
    gap = p.f("headline_gap")
    if gap:
        insight += p.say(bi(f" The likeliest cause on your listing: {gap}.",
                            f" Listing par sabse sambhavit wajah: {gap}."))
    ask = p.pick([
        bi("Want me to fix that one thing today and show you the before/after next week? Reply YES.",
           "Wo ek cheez aaj theek kar dun aur agle hafte before/after dikhaun? YES bolein."),
        bi("I can fix it from my side today — reply YES and I'll start.",
           "Main apni taraf se aaj hi theek kar deti hoon — YES bolein, shuru karti hoon."),
    ])
    return Draft(hook, insight, ask, CTA_BINARY, "", "vera_perf_dip_v1",
                 ("specificity", "loss_aversion", "effort_externalization"))


def pb_seasonal_perf_dip(p: P) -> Draft:
    metric = p.f("tp_metric", "views")
    delta = p.f("tp_delta") or p.f("tp_delta_pct")
    note = p.f("tp_season_note") or p.f("season_note_full")
    hook = p.say(bi(
        f"Your {metric} are down {delta} this week — and this one is seasonal, not broken."
        if delta else "This week's dip in your numbers looks seasonal, not broken.",
        f"Is hafte {metric} {delta} neeche hain — par ye seasonal hai, kuch toota nahi."
        if delta else "Is hafte ki dip seasonal lagti hai, kuch toota nahi hai.",
    ))
    insight = ""
    if note:
        insight = p.say(bi(f"The category pattern is {note}.", f"Category ka pattern: {note}."))
    insight = (insight + " " + (strength_line(p) or offer_gap_line(p))).strip()
    ask = p.pick([
        bi("The useful move in this window is retention, not acquisition — want me to draft a "
           "win-back note for members who've gone quiet? Reply YES.",
           "Is window mein retention kaam aata hai, acquisition nahi — quiet members ke liye "
           "win-back note draft kar dun? YES bolein."),
        bi("Want a retention push instead of an acquisition one this month? Reply YES.",
           "Is mahine acquisition ki jagah retention push karun? YES bhej dijiye."),
    ])
    return Draft(hook, insight, ask, CTA_BINARY, "", "vera_seasonal_dip_v1",
                 ("specificity", "reciprocity", "effort_externalization"))


def pb_perf_spike(p: P) -> Draft:
    metric = p.f("tp_metric", "calls")
    delta = p.f("tp_delta") or p.f("tp_delta_pct")
    driver = p.f("tp_likely_driver")
    baseline = p.f("tp_vs_baseline")
    if delta:
        hook = p.say(bi(
            f"Your {metric} are up {delta} this week"
            + (f", against a baseline of {baseline}." if baseline else "."),
            f"Is hafte aapke {metric} {delta} badhe hain"
            + (f", baseline {baseline} tha." if baseline else "."),
        ))
    else:
        hook = p.say(bi("Something is working on your listing this week.",
                        "Is hafte aapki listing par kuch sahi chal raha hai."))
    if driver:
        insight = p.say(bi(
            f"It traces back to your {driver.replace('_', ' ')} — that's a repeatable lever, not luck.",
            f"Ye aapke {driver.replace('_', ' ')} se aaya hai — ye repeat ho sakta hai, luck nahi.",
        ))
    else:
        insight = strength_line(p) or traffic_line(p)
    ask = p.pick([
        bi("Spikes decay in about a week. Want me to run the same play once more while it's live? Reply YES.",
           "Spike ek hafte mein thanda ho jata hai. Jab tak chal raha hai, wahi play dobara chala dun? YES bolein."),
        bi("Want me to repeat that play this week before the lift fades? Reply YES.",
           "Lift khatam hone se pehle wahi play is hafte dohra dun? YES bhej dijiye."),
    ])
    return Draft(hook, insight, ask, CTA_BINARY, "", "vera_perf_spike_v1",
                 ("specificity", "loss_aversion", "curiosity"))


def pb_milestone(p: P) -> Draft:
    if not p.has("tp_value_now") and not p.has("tp_milestone_value"):
        return _pb_milestone_thin(p)
    metric = {"review count": "reviews", "reviewcount": "reviews", "views": "views",
              "calls": "calls"}.get(str(p.f("tp_metric", "review_count")).replace("_", " "),
                                    str(p.f("tp_metric", "review_count")).replace("_", " "))
    now = p.f("tp_value_now")
    target = p.f("tp_milestone_value")
    if now and target:
        try:
            remaining = int(float(target.replace(",", ""))) - int(float(now.replace(",", "")))
        except ValueError:
            remaining = None
        if remaining and remaining > 0:
            p.led.license(remaining)
            hook = p.say(bi(
                f"You're at {now} {metric} — {remaining} short of {target}.",
                f"Aap {now} {metric} par hain — {target} se sirf {remaining} door.",
            ))
        else:
            hook = p.say(bi(f"You've crossed {target} {metric}.", f"Aapne {target} {metric} cross kar liye."))
    else:
        hook = p.say(bi("You've hit a milestone worth using.", "Ek milestone hit hua hai, ise use karna chahiye."))
    peer = p.f("peer_avg_review_count")
    insight = ""
    if peer and "review" in metric:
        insight = p.say(bi(
            f"Peer median in your category is {peer} reviews, so this is already a real advantage — "
            f"it just isn't visible on your listing yet.",
            f"Aapki category ka peer median {peer} reviews hai, to ye already ek advantage hai — "
            f"bas listing par dikh nahi raha.",
        ))
    insight = (insight or strength_line(p) or peer_gap_line(p))
    ask = p.pick([
        bi("Want me to draft the milestone post and the ask-for-review message that closes the gap? Reply YES.",
           "Milestone post aur review-ask message draft kar dun jo gap band kare? YES bolein."),
        bi("Should I put it on your listing as a post today? Reply YES.",
           "Aaj listing par post kar dun? YES bhej dijiye."),
    ])
    return Draft(hook, insight, ask, CTA_BINARY, "", "vera_milestone_v1",
                 ("social_proof", "specificity", "effort_externalization"))


def _pb_milestone_thin(p: P) -> Draft:
    """No milestone value in the payload. Rather than invent one (the -2
    fabrication penalty), derive a real threshold the merchant has actually
    crossed from their performance snapshot."""
    mark = crossed_mark(p.v("perf_views"))
    if not mark or not p.has("perf_views"):
        return pb_default_merchant(p)
    p.led.license(mark)
    hook = p.say(bi(
        f"Your listing crossed {mark:,} views in the last {p.f('perf_window')} — "
        f"{p.f('perf_views')} in total.",
        f"Aapki listing ne pichle {dur(p, p.f('perf_window'))} mein {mark:,} views cross kiye — "
        f"kul {p.f('perf_views')}.",
    ))
    insight = strength_line(p) or traffic_line(p)
    if p.has("peer_avg_review_count", "peer_avg_rating"):
        insight += p.say(bi(
            f" That traffic is worth more with proof next to it — the peer median in "
            f"{p.f('peer_scope', 'your category')} is {p.f('peer_avg_review_count')} reviews at "
            f"{p.f('peer_avg_rating')}★.",
            f" Us traffic ke saath proof zaroori hai — {p.f('peer_scope', 'aapki category')} ka "
            f"peer median {p.f('peer_avg_review_count')} reviews aur {p.f('peer_avg_rating')}★ hai.",
        ))
    ask = p.pick([
        bi("Want me to put that number on your listing as a post this week? Reply YES.",
           "Wo number is hafte listing par post kar dun? YES bolein."),
        bi("Should I turn that into a listing post today? Reply YES.",
           "Use aaj listing post bana dun? YES bhej dijiye."),
    ])
    return Draft(hook, insight, ask, CTA_BINARY, "", "vera_milestone_traffic_v1",
                 ("social_proof", "specificity", "effort_externalization"))


def pb_renewal_due(p: P) -> Draft:
    days = _days(p.f("tp_days_remaining")) or p.f("days_remaining")
    amount = p.f("tp_renewal_amount")
    plan = p.f("tp_plan") or p.f("plan", "your plan")
    hook = p.say(bi(
        f"{plan} renewal is {days} out." if days else f"{plan} renewal is coming up.",
        f"{plan} renewal {dur(p, days)} mein hai." if days else f"{plan} renewal aane wala hai.",
    ))
    insight = strength_line(p) or peer_gap_line(p)
    if amount:
        insight += p.say(bi(f" Renewal is {amount}.", f" Renewal amount {amount} hai."))
    ask = p.pick([
        bi("Want me to renew it now so nothing on the listing goes dark? Reply YES.",
           "Abhi renew kar dun taaki listing band na ho? YES bolein."),
        bi("Reply YES and I'll keep it running without a gap.",
           "YES bolein, bina gap ke chalta rahega."),
    ])
    return Draft(hook, insight, ask, CTA_BINARY, "", "vera_renewal_v1",
                 ("loss_aversion", "specificity", "effort_externalization"))


def pb_winback(p: P) -> Draft:
    since = _days(p.f("tp_days_since_expiry")) or p.f("days_since_expiry")
    dip = p.f("tp_perf_dip_pct")
    added = p.f("tp_lapsed_customers_added_since_expiry")
    if since:
        hook = p.say(bi(
            f"Your listing has been off for {since}" + (f", and views are down {dip} since." if dip else "."),
            f"Aapki listing {dur(p, since)} se band hai" + (f", tab se views {dip} gire hain." if dip else "."),
        ))
    else:
        hook = p.say(bi("Your subscription lapsed and the listing has gone quiet.",
                        "Aapka subscription lapse ho gaya aur listing shant ho gayi hai."))
    if added:
        insight = p.say(bi(
            f"{added} customers went quiet in that window — they're still reachable, "
            f"but that list ages fast.",
            f"Us window mein {added} customers shant ho gaye — abhi bhi reach kar sakte hain, "
            f"par ye list jaldi purani ho jati hai.",
        ))
    else:
        insight = peer_gap_line(p) or offer_gap_line(p)
    ask = p.pick([
        bi("Want me to switch the listing back on and send that group one win-back message? Reply YES.",
           "Listing wapas on kar dun aur us group ko ek win-back message bhej dun? YES bolein."),
        bi("Reply YES and I'll restart the listing plus draft the win-back note.",
           "YES bolein — listing restart karti hoon aur win-back note draft kar deti hoon."),
    ])
    return Draft(hook, insight, ask, CTA_BINARY, "", "vera_winback_v1",
                 ("loss_aversion", "specificity", "effort_externalization"))


def pb_gbp_unverified(p: P) -> Draft:
    path = str(p.f("tp_verification_path", "")).replace("_", " ")
    uplift = p.f("tp_estimated_uplift_pct")
    hook = p.say(bi(
        "Your Google listing is still unverified — that's the single biggest thing holding it back.",
        "Aapki Google listing abhi unverified hai — sabse bada rukawat yahi hai.",
    ))
    insight_bits = []
    if uplift:
        insight_bits.append(p.say(bi(
            f"Verified listings in your category see about {uplift} more discovery.",
            f"Verified listings ko aapki category mein lagbhag {uplift} zyada discovery milti hai.",
        )))
    if p.has("perf_views"):
        insight_bits.append(p.say(bi(
            f"On your current {p.f('perf_views')} views that is real traffic, not a rounding error.",
            f"Aapke abhi ke {p.f('perf_views')} views par ye asli traffic hai.",
        )))
    if path:
        insight_bits.append(p.say(bi(f"Verification is by {path}.", f"Verification {path} se hoti hai.")))
    insight = " ".join(insight_bits) or peer_gap_line(p)
    ask = p.say(bi(
        "Want me to start verification for you? Reply YES and I'll trigger it today.",
        "Verification shuru kar dun? YES bolein, aaj hi trigger kar deti hoon.",
    ))
    return Draft(hook, insight, ask, CTA_BINARY, "", "vera_gbp_verify_v1",
                 ("loss_aversion", "specificity", "effort_externalization"))


def pb_competitor_opened(p: P) -> Draft:
    name = p.f("tp_competitor_name")
    dist = p.f("tp_distance_km")
    their_offer = p.f("tp_their_offer")
    opened = p.f("tp_opened_date")
    if name:
        hook = p.say(bi(
            f"{name} opened {dist} km from you" + (f" on {opened}." if opened else "."),
            f"{name} aapse {dist} km door khula hai" + (f", {opened} ko." if opened else "."),
        ))
    else:
        hook = p.say(bi(
            f"A new {p.f('category_one', 'business')} listing went live in {p.f('locality', 'your area')}.",
            f"{p.f('locality', 'aapke area')} mein ek nayi {p.f('category_one', 'business')} listing live hui hai.",
        ))
    if their_offer:
        mine = p.f("offer_active") or p.f("catalog_offer")
        insight = p.say(bi(
            f"They're leading with {their_offer}. " +
            (f"You're showing {mine}." if mine else "Your listing has no live offer to answer it."),
            f"Wo {their_offer} se lead kar rahe hain. " +
            (f"Aap {mine} dikha rahe hain." if mine else "Aapki listing par jawab dene ko koi offer nahi hai."),
        ))
        insight += p.say(bi(
            " Matching on price is the losing move here; the review count and the response time are yours to win.",
            " Price match karna yahan galat move hai; review count aur response time aapke haath mein hai.",
        ))
    else:
        insight = peer_gap_line(p) or offer_gap_line(p)
    ask = p.pick([
        bi("Want me to show how your listing stacks up against theirs side by side?",
           "Dikhaun ki aapki listing unke saamne kaisi dikhti hai?"),
        bi("Should I put together the side-by-side so you can see where you actually lead?",
           "Side-by-side bana dun taaki dikhe ki aap kahan aage hain?"),
    ])
    return Draft(hook, insight, ask, CTA_OPEN, "", "vera_competitor_v1",
                 ("curiosity", "loss_aversion", "social_proof"))


def pb_review_theme(p: P) -> Draft:
    theme = str(p.f("tp_theme") or p.f("review_neg_theme", "")).replace("_", " ")
    count = p.f("tp_occurrences_30d") or p.f("review_neg_count")
    quote = p.f("tp_common_quote") or p.f("review_neg_quote")
    trend = p.f("tp_trend")
    if not theme:
        return _pb_review_thin(p)
    hook = p.say(bi(
        f"{count} reviews in the last 30 days mention {theme}"
        + (f", and it's {trend}." if trend else "."),
        f"Pichle 30 din mein {count} reviews mein {theme} ka zikr hai"
        + (f", aur ye {trend} hai." if trend else "."),
    )) if count else p.say(bi(
        f"A {theme} theme is showing up in your reviews.",
        f"Aapke reviews mein {theme} ka theme dikh raha hai.",
    ))
    insight = ""
    if quote:
        insight = p.say(bi(f'Typical wording: "{quote}".', f'Typical wording: "{quote}".'))
    pos = p.f("review_pos_theme")
    if pos:
        insight += p.say(bi(
            f" Worth saying: {pos} is the theme people praise you for, so this is a fixable "
            f"operations issue, not a reputation one.",
            f" Achhi baat: log aapko {pos} ke liye tareef karte hain, to ye operations ka issue hai, "
            f"reputation ka nahi.",
        ))
    ask = p.pick([
        bi("Want me to draft a short public reply for those reviews that names the fix? Reply YES.",
           "Un reviews ke liye ek chhota public reply draft kar dun jisme fix ka zikr ho? YES bolein."),
        bi("Should I draft the reply template your staff can reuse? Reply YES.",
           "Ek reply template bana dun jo staff dobara use kar sake? YES bhej dijiye."),
    ])
    return Draft(hook, insight, ask, CTA_BINARY, "", "vera_review_theme_v1",
                 ("specificity", "loss_aversion", "effort_externalization"))


def _pb_review_thin(p: P) -> Draft:
    """Review trigger, empty payload — stay on the review topic using peer stats."""
    rating = p.f("peer_avg_rating")
    count = p.f("peer_avg_review_count")
    if not (rating or count):
        return pb_default_merchant(p)
    hook = p.say(bi(
        f"Your review mix moved this week. For reference, the peer median in "
        f"{p.f('peer_scope', 'your category')} is {count} reviews at {rating}★.",
        f"Is hafte aapke reviews mein movement hai. Reference ke liye, "
        f"{p.f('peer_scope', 'aapki category')} ka peer median {count} reviews aur {rating}★ hai.",
    ))
    insight = p.say(bi(
        "Reviews are the one thing on a listing you can't buy, and they decay — "
        "a month with no new ones reads as a quiet business.",
        "Reviews hi ek cheez hai jo listing par kharidi nahi ja sakti, aur wo purani ho jati hai — "
        "ek mahina bina naye review ke matlab business shant lagta hai.",
    ))
    ask = p.pick([
        bi("Want me to draft the post-visit review ask your staff can send? Reply YES.",
           "Visit ke baad bhejne wala review-ask message draft kar dun? YES bolein."),
        bi("Should I draft a short review request your counter can send after each visit? Reply YES.",
           "Har visit ke baad counter se bhejne ke liye chhota review request bana dun? YES bhej dijiye."),
    ])
    return Draft(hook, insight, ask, CTA_BINARY, "", "vera_review_ask_v1",
                 ("social_proof", "specificity", "effort_externalization"))


def pb_festival(p: P) -> Draft:
    festival = p.f("tp_festival")
    if not festival:
        # No named festival in the payload — use the category's own seasonal beat,
        # which is a real dated pattern, rather than saying "the festival".
        return _pb_season_thin(p)
    days = p.f("tp_days_until")
    date = p.f("tp_date")
    hook = p.say(bi(
        f"{festival} is {days} out ({date})." if days and date else f"{festival} is coming up.",
        f"{festival} {dur(p, days)} door hai ({date})." if days and date else f"{festival} aane wala hai.",
    ))
    offer = p.f("catalog_offer") or p.f("offer_active")
    far_out = False
    try:
        far_out = int(str(p.f("tp_days_until")).split()[0]) > 45
    except (ValueError, IndexError):
        far_out = False
    if far_out:
        insight = p.say(bi(
            "Too early to post, but not too early to decide the format — the listings that win that "
            "week are built around one service at one price. "
            + (f"Yours would be {offer}." if offer else ""),
            "Post karne ka time nahi hai, par format decide karne ka hai — us hafte wahi listings "
            "chalti hain jo ek service, ek price par bani hoti hain. "
            + (f"Aapke liye {offer}." if offer else ""),
        ))
        ask = p.pick([
            bi("Want me to lock that format now and schedule the post for the right week? Reply YES.",
               "Format abhi lock kar dun aur post sahi hafte ke liye schedule kar dun? YES bolein."),
        ])
    else:
        insight = p.say(bi(
            "The bookings that convert around it get listed 2-3 weeks early, not on the day. "
            + (f"Your strongest format for that window is {offer}." if offer else ""),
            "Us mauke par jo bookings convert hoti hain wo 2-3 hafte pehle list hoti hain, us din nahi. "
            + (f"Us window ke liye aapka best format {offer} hai." if offer else ""),
        ))
        p.led.license(2, 3)
        ask = p.pick([
            bi("Want me to put the festival offer live with a dated Google post? Reply YES.",
               "Festival offer ko dated Google post ke saath live kar dun? YES bolein."),
            bi("Should I schedule the festival post now so it's indexed in time? Reply YES.",
               "Festival post abhi schedule kar dun taaki time par index ho? YES bhej dijiye."),
        ])
    insight = (insight + " " + season_line(p)).strip()
    return Draft(hook, insight, ask, CTA_BINARY, "", "vera_festival_v1",
                 ("specificity", "loss_aversion", "effort_externalization"))


def _pb_season_thin(p: P) -> Draft:
    note = p.f("season_note_full")
    window = p.f("season_window")
    if not note:
        return pb_default_merchant(p)
    hook = p.say(bi(
        f"Seasonal window coming up for your category. {window}: {note}.",
        f"Aapki category ka seasonal window aa raha hai. {window}: {note}.",
    ))
    offer = p.f("offer_active") or p.f("catalog_offer")
    insight = p.say(bi(
        f"Listings that win that window lead with one service at one price"
        + (f" — yours would be {offer}." if offer else "."),
        f"Us window mein wahi listings chalti hain jo ek service, ek price par bani hoti hain"
        + (f" — aapke liye {offer}." if offer else "."),
    ))
    material_gap = (p.v("action_gap") or 0) >= 20
    insight = (insight + " " + (peer_gap_line(p) if material_gap else "")).strip()
    ask = p.pick([
        bi("Want me to line that up now so it's indexed before the window opens? Reply YES.",
           "Window khulne se pehle index ho jaye, iske liye abhi laga dun? YES bolein."),
        bi("Should I schedule it for the start of that window? Reply YES.",
           "Us window ke shuru ke liye schedule kar dun? YES bhej dijiye."),
    ])
    return Draft(hook, insight, ask, CTA_BINARY, "", "vera_seasonal_beat_v1",
                 ("specificity", "loss_aversion", "effort_externalization"))


def pb_local_event(p: P) -> Draft:
    match = p.f("tp_match")
    venue = p.f("tp_venue")
    when = p.f("tp_match_time_iso") or p.f("tp_event_time_iso")
    city = p.f("tp_city") or p.f("city")
    if match:
        hook = p.say(bi(
            f"{match} is on tonight at {venue}" + (f", {city}." if city else "."),
            f"Aaj raat {match} hai {venue} par" + (f", {city}." if city else "."),
        ))
    else:
        topic = p.f("tp_headline") or p.f("tp_event") or p.f("tp_metric_or_topic")
        if not topic:
            return pb_default_merchant(p)
        hook = p.say(bi(f"Local event tonight: {topic}.", f"Aaj shaam ka local event: {topic}."))
    offer = p.f("offer_active") or p.f("catalog_offer")
    insight = p.say(bi(
        f"Order volume in your locality clusters in the two hours around start. "
        + (f"{offer} is the item to lead with." if offer else ""),
        f"Aapke area mein order volume start ke aas-paas do ghante mein bunch hota hai. "
        + (f"{offer} se lead karna chahiye." if offer else ""),
    ))
    p.led.license(2)
    ask = p.say(bi(
        "Want me to put that live as a listing post before the first innings? Reply YES.",
        "Pehli innings se pehle listing post live kar dun? YES bolein.",
    ))
    return Draft(hook, insight, ask, CTA_BINARY, "", "vera_local_event_v1",
                 ("specificity", "loss_aversion", "effort_externalization"))


def pb_cde_opportunity(p: P) -> Draft:
    title = p.f("digest_title") or p.f("tp_title")
    credits = p.f("tp_credits") or p.f("digest_credits")
    fee = str(p.f("tp_fee", "")).replace("_", " ")
    date = p.f("digest_date")
    if not title:
        return pb_default_merchant(p)
    hook = p.say(bi(
        f"{title}" + (f", {date}" if date else "") + (f" — {credits} credits" if credits else "") + ".",
        f"{title}" + (f", {date}" if date else "") + (f" — {credits} credits" if credits else "") + ".",
    ))
    summary = p.f("digest_summary")
    insight = summary.rstrip(".") + "." if summary else best_insight(p)
    if fee:
        insight += p.say(bi(f" {fee.capitalize()}.", f" {fee.capitalize()}."))
    ask = p.pick([
        bi("Want me to hold a seat and put it in your calendar? Reply YES.",
           "Ek seat hold kar ke aapke calendar mein daal dun? YES bolein."),
        bi("Should I register you and send the joining link on the day? Reply YES.",
           "Register kar dun aur us din joining detail bhej dun? YES bhej dijiye."),
    ])
    return Draft(hook, insight, ask, CTA_BINARY, citation_of(p), "vera_cde_v1",
                 ("specificity", "reciprocity", "effort_externalization"))


def pb_trend_movement(p: P) -> Draft:
    line = trend_line(p)
    if not line:
        return pb_default_merchant(p)
    hook = line
    offer = p.f("catalog_offer")
    insight = p.say(bi(
        f"Nothing on your listing answers that query yet"
        + (f" — {offer} is the closest thing in your catalog." if offer else "."),
        f"Abhi aapki listing par is query ka koi jawab nahi hai"
        + (f" — catalog mein {offer} sabse kareeb hai." if offer else "."),
    ))
    insight = (insight + " " + (peer_gap_line(p) if p.has("action_gap") else "")).strip()
    ask = p.pick([
        bi("Want me to add that as a listed service so you show up for it? Reply YES.",
           "Use ek listed service ke roop mein add kar dun taaki aap us par dikhein? YES bolein."),
        bi("Should I put it on the listing this week? Reply YES.",
           "Is hafte listing par daal dun? YES bhej dijiye."),
    ])
    return Draft(hook, insight, ask, CTA_BINARY, "", "vera_trend_v1",
                 ("curiosity", "specificity", "loss_aversion"))


def pb_category_seasonal(p: P) -> Draft:
    trends = p.f("tp_trends")
    season = str(p.f("tp_season", "")).replace("_", " ")
    if trends:
        readable = trends.replace("_+", " up ").replace("_-", " down ").replace("_", " ")
        hook = p.say(bi(
            f"{season.title()} demand shift in your category: {readable}." if season
            else f"Demand shift in your category: {readable}.",
            f"{season.title()} mein aapki category ki demand shift: {readable}." if season
            else f"Aapki category mein demand shift: {readable}.",
        ))
    else:
        line = season_line(p) or trend_line(p)
        if not line:
            return pb_default_merchant(p)
        hook = line
    insight = p.say(bi(
        "Shelf and listing should move before the demand does, not after.",
        "Shelf aur listing demand se pehle badalni chahiye, baad mein nahi.",
    ))
    insight = (insight + " " + (offer_gap_line(p) or "")).strip()
    ask = p.pick([
        bi("Want me to reorder your listed items to match and post the change? Reply YES.",
           "Listed items ko us hisaab se reorder kar ke post kar dun? YES bolein."),
        bi("Should I update the listing to lead with what's moving? Reply YES.",
           "Jo chal raha hai usse lead karne ke liye listing update kar dun? YES bhej dijiye."),
    ])
    return Draft(hook, insight, ask, CTA_BINARY, "", "vera_category_seasonal_v1",
                 ("specificity", "loss_aversion", "effort_externalization"))


def pb_active_planning(p: P) -> Draft:
    topic = str(p.f("tp_intent_topic", "")).replace("_", " ")
    last = p.f("tp_merchant_last_message")
    if not topic:
        return pb_default_merchant(p)
    # The merchant already said yes. Do not re-qualify — deliver a first draft.
    hook = p.say(bi(
        f"Picking up your {topic} — here's the first cut, not another question.",
        f"Aapke {topic} par aage badh rahi hoon — ye pehla draft hai, ek aur sawaal nahi.",
    ))
    offer = p.f("catalog_repeat_offer") or p.f("catalog_offer")
    bits = []
    if offer:
        bits.append(p.say(bi(f"Anchor from your category catalog: {offer}.",
                             f"Category catalog se anchor: {offer}.")))
    if p.has("cust_total"):
        bits.append(p.say(bi(
            f"Sized against your {p.f('cust_total')} customers this year.",
            f"Aapke is saal ke {p.f('cust_total')} customers ke hisaab se size kiya hai.",
        )))
    season = season_line(p)
    if season:
        bits.append(season)
    insight = " ".join(bits) or best_insight(p)
    ask = p.pick([
        bi("I'll have the full draft in your dashboard today — reply CONFIRM and I'll publish it.",
           "Aaj hi poora draft aapke dashboard mein hoga — CONFIRM bolein, main publish kar dungi."),
        bi("Reply CONFIRM and I'll put the draft live for your review today.",
           "CONFIRM bolein, aaj hi draft aapke review ke liye live kar deti hoon."),
    ])
    return Draft(hook, insight, ask, CTA_CONFIRM, "", "vera_planning_handoff_v1",
                 ("effort_externalization", "specificity", "single_binary_commitment"))


def pb_curious_ask(p: P) -> Draft:
    """The family production Vera barely fires — asking the merchant something."""
    peer_bit = ""
    if p.has("peer_avg_ctr") and p.has("perf_ctr"):
        peer_bit = p.say(bi(
            f"Across {p.f('peer_scope', 'your category')} the median listing converts at {p.f('peer_ctr')}; "
            f"you're at {p.f('perf_ctr')}.",
            f"{p.f('peer_scope', 'aapki category')} mein median listing {p.f('peer_ctr')} par convert hoti hai; "
            f"aap {p.f('perf_ctr')} par hain.",
        ))
    hook = peer_bit or trend_line(p) or season_line(p)
    if not hook:
        return pb_default_merchant(p)
    insight = p.say(bi(
        "The numbers tell me what happened but not why, and you're the one who can see the counter.",
        "Numbers batate hain kya hua, par kyun nahi — wo aap counter par dekh sakte hain.",
    ))
    ask = p.pick([
        bi("What did people actually ask for most this week? I'll build next week's listing around it.",
           "Is hafte log sabse zyada kya maang rahe the? Agle hafte ki listing usi ke around bana dungi."),
        bi("Which service did customers ask for most this week? Whatever you say, I'll put it on the listing.",
           "Is hafte kaunsi service sabse zyada poochi gayi? Jo aap bolein, main listing par daal dungi."),
    ])
    return Draft(hook, insight, ask, CTA_OPEN, "", "vera_curious_ask_v1",
                 ("asking_the_merchant", "social_proof", "curiosity"))


def pb_dormant(p: P) -> Draft:
    days = p.f("tp_days_since_last_merchant_message")
    topic = str(p.f("tp_last_topic", "")).replace("_", " ")
    if days:
        d = dur(p, days)
        hook = p.say(bi(
            f"It's been {days} since we last spoke" + (f" — we'd stopped at {topic}." if topic else "."),
            f"Humari aakhri baat ko {d} ho gaye" + (f" — hum {topic} par ruke the." if topic else "."),
        ))
    else:
        hook = p.say(bi("We've been quiet a while, so one thing only.",
                        "Kaafi din se baat nahi hui, to sirf ek baat."))
    insight = best_insight(p)
    ask = p.pick([
        bi("If it's still useful I'll fix that one number this week — reply YES. If not, reply STOP "
           "and I'll leave it.",
           "Agar abhi bhi kaam ka hai to is hafte wo ek number theek kar deti hoon — YES bolein. "
           "Nahi to STOP bhej dijiye, main ruk jaungi."),
    ])
    return Draft(hook, insight, ask, CTA_BINARY, "", "vera_reengage_v1",
                 ("loss_aversion", "specificity", "single_binary_commitment"))


def pb_default_merchant(p: P) -> Draft:
    """No usable trigger payload and no specialised playbook — still be specific."""
    insight = best_insight(p)
    if not insight:
        return Draft("", skip=True, skip_reason="no grounded fact available for this merchant")
    gap = p.f("headline_gap")
    kind = p.f("trigger_kind", "an update")
    if gap:
        hook = p.say(bi(
            f"One thing on your listing is costing you more than the rest: {gap}.",
            f"Aapki listing par ek cheez sabse zyada nuksan kar rahi hai: {gap}.",
        ))
    else:
        hook = p.say(bi(
            f"Ran your {p.f('perf_window', '30 day')} numbers against the category median.",
            f"Aapke {dur(p, p.f('perf_window', '30 days'))} ke numbers category median se compare kiye.",
        ))
    ask = p.pick([
        bi("Want me to close that gap this week? Reply YES and I'll start today.",
           "Ye gap is hafte band kar dun? YES bolein, aaj se shuru."),
        bi("Reply YES and I'll fix it from my side this week.",
           "YES bolein, is hafte apni taraf se theek kar deti hoon."),
    ])
    return Draft(hook, insight, ask, CTA_BINARY, "", "vera_gap_nudge_v1",
                 ("specificity", "loss_aversion", "effort_externalization"))


# ---------------------------------------------------------------------------
# customer-facing playbooks (send_as = merchant_on_behalf)
# ---------------------------------------------------------------------------

def _cx_signoff(p: P) -> str:
    biz = p.f("biz", "we")
    return biz


def pb_recall_due(p: P) -> Draft:
    name = p.f("cx_name", "there")
    biz = p.f("biz")
    service = (str(p.f("tp_service_due", "")).replace("_", " ")
               or p.f("cx_top_service") or p.f("visit_noun", "visit"))
    last = p.f("cx_last_visit") or p.f("tp_last_service_date")
    slots = p.f("tp_slots")
    if p.voice.code_mix:
        slots = slots.replace(" or ", " ya ")
    offer = p.f("offer_active") or p.f("catalog_offer")

    hook = p.say(bi(
        f"Hi {name}, {biz} here. Your {service} is due" + (f" — last visit was {last}." if last else "."),
        f"Hi {name}, {biz} se. Aapka {service} due hai" + (f" — pichli visit {last} thi." if last else "."),
    ))
    bits = []
    if p.has("cx_visits_phrase"):
        bits.append(p.say(bi(
            f"You've been with us {p.f('cx_visits_phrase')}.",
            f"Aap {p.f('cx_visits')} baar aa chuke hain.",
        )))
    if offer:
        bits.append(p.say(bi(f"{offer} applies.", f"{offer} lagu hai.")))
    insight = " ".join(bits)

    if slots:
        pref = p.f("cx_pref_slots")
        pref_note = p.say(bi(
            f"Both are {pref} slots, the way you usually book.",
            f"Dono {pref} slots hain, jaise aap aam taur par book karte hain.",
        )) if pref else ""
        insight = (insight + " " + pref_note).strip()
        ask = p.say(bi(
            f"We've held {slots}. Reply 1 or 2, or tell us a time that suits you.",
            f"Humne {slots} hold kiye hain. 1 ya 2 reply karein, ya apna time bata dein.",
        ))
        cta = CTA_SLOT
        p.led.license(1, 2)
    else:
        ask = p.say(bi(
            f"Reply YES and we'll {p.f('hold_noun', 'hold a slot')} for you this week.",
            f"YES bhej dein, is hafte aapke liye {p.f('hold_noun_hi', 'slot hold kar denge')}.",
        ))
        cta = CTA_BINARY
    return Draft(hook, insight, ask, cta, "", "merchant_recall_reminder_v1",
                 ("specificity", "reciprocity", "single_binary_commitment"),
                 send_as="merchant_on_behalf")


def pb_customer_winback(p: P) -> Draft:
    name = p.f("cx_name", "there")
    biz = p.f("biz")
    days = p.f("tp_days_since_last_visit")
    focus = str(p.f("tp_previous_focus", "")).replace("_", " ")
    months = p.f("tp_previous_membership_months")
    last = p.f("cx_last_visit")
    hook = p.say(bi(
        f"Hi {name}, {biz} here. It's been {days} since your last session." if days else
        f"Hi {name}, {biz} here." + (f" Your last visit with us was {last}." if last else ""),
        f"Hi {name}, {biz} se. Aapki pichli session ko {dur(p, days)} ho gaye." if days else
        f"Hi {name}, {biz} se." + (f" Aapki pichli visit {last} thi." if last else ""),
    ))
    bits = []
    if focus and months:
        bits.append(p.say(bi(
            f"You were {months} in on {focus} — that progress doesn't disappear, "
            f"but it does get harder to restart the longer it waits.",
            f"Aap {focus} par {dur(p, months)} kar chuke the — wo progress khatam nahi hoti, "
            f"par jitna rukega, restart utna mushkil hoga.",
        )))
    elif p.has("cx_top_service"):
        bits.append(p.say(bi(
            f"You'd been coming in for {p.f('cx_top_service')}.",
            f"Aap {p.f('cx_top_service')} ke liye aate the.",
        )))
    offer = p.f("catalog_repeat_offer") or p.f("offer_active") or p.f("catalog_offer")
    if offer:
        bits.append(p.say(bi(f"{offer} is open to you.", f"Aapke liye {offer} khula hai.")))
    insight = " ".join(bits)
    ask = p.say(bi(
        f"Want us to {p.f('hold_noun', 'hold a slot')} this week? Reply YES.",
        f"Is hafte {p.f('hold_noun_hi', 'slot hold kar denge')} — YES bhej dein.",
    ))
    return Draft(hook, insight, ask, CTA_BINARY, "", "merchant_winback_v1",
                 ("loss_aversion", "specificity", "single_binary_commitment"),
                 send_as="merchant_on_behalf")


def pb_chronic_refill(p: P) -> Draft:
    # The generator scatters customer triggers across every category, so a
    # refill trigger can land on a dentist. Never send pharmacy copy to a
    # non-pharmacy merchant's customer.
    if not p.f("tp_molecule_list") and not str(p.f("category_slug")).startswith("pharmac"):
        return pb_default_customer(p)
    name = p.f("cx_name", "there")
    biz = p.f("biz")
    molecules = p.f("tp_molecule_list")
    runs_out = p.f("tp_stock_runs_out_iso")
    delivery = p.v("tp_delivery_address_saved")
    hook = p.say(bi(
        f"Hi {name}, {biz} here. Your {molecules} refill runs out on {runs_out}."
        if molecules and runs_out else f"Hi {name}, {biz} here. Your monthly refill is due.",
        f"Hi {name}, {biz} se. Aapki {molecules} ki dawai {runs_out} ko khatam ho rahi hai."
        if molecules and runs_out else f"Hi {name}, {biz} se. Aapki monthly refill due hai.",
    ))
    last = p.f("tp_last_refill")
    insight = p.say(bi(
        f"Last refill was {last}, so we've kept the same strip count ready." if last else
        "We've kept the same strip count ready.",
        f"Pichli refill {last} thi, wahi strip count ready rakhi hai." if last else
        "Wahi strip count ready rakhi hai.",
    ))
    if delivery:
        ask = p.say(bi(
            "Reply YES and we'll deliver to your saved address today.",
            "YES bhej dein, aaj hi aapke saved address par pahuncha denge.",
        ))
    else:
        ask = p.say(bi(
            "Reply YES and we'll keep it aside for pickup today.",
            "YES bhej dein, aaj pickup ke liye alag rakh denge.",
        ))
    return Draft(hook, insight, ask, CTA_BINARY, "", "merchant_refill_reminder_v1",
                 ("specificity", "effort_externalization", "single_binary_commitment"),
                 send_as="merchant_on_behalf")


def pb_trial_followup(p: P) -> Draft:
    # A "trial session" only exists where the merchant sells sessions.
    if not p.has("tp_trial_date") and not str(p.f("category_slug")).startswith(("gym", "salon")):
        return pb_default_customer(p)
    name = p.f("cx_name", "there")
    biz = p.f("biz")
    trial = p.f("tp_trial_date")
    slots = p.f("tp_slots") or p.f("tp_next_session_options_slots")
    hook = p.say(bi(
        f"Hi {name}, {biz} here. Hope the {trial} trial session went well." if trial else
        f"Hi {name}, {biz} here. Hope the trial session went well.",
        f"Hi {name}, {biz} se. Umeed hai {trial} ka trial session accha raha." if trial else
        f"Hi {name}, {biz} se. Umeed hai trial session accha raha.",
    ))
    offer = p.f("offer_active") or p.f("catalog_offer")
    insight = p.say(bi(f"{offer} applies if you'd like to continue.",
                       f"Agar continue karna ho to {offer} lagu hai.")) if offer else ""
    if slots:
        ask = p.say(bi(
            f"Next session is {slots} — reply YES and we'll save the spot.",
            f"Agla session {slots} hai — YES bhej dein, spot save kar denge.",
        ))
    else:
        ask = p.say(bi("Want us to save a spot for the next session? Reply YES.",
                       "Agle session ke liye spot save kar dein? YES bhej dein."))
    return Draft(hook, insight, ask, CTA_BINARY, "", "merchant_trial_followup_v1",
                 ("reciprocity", "specificity", "single_binary_commitment"),
                 send_as="merchant_on_behalf")


def pb_wedding_followup(p: P) -> Draft:
    if not p.has("tp_wedding_date") and not p.has("tp_days_to_wedding"):
        return pb_default_customer(p)
    name = p.f("cx_name", "there")
    biz = p.f("biz")
    wedding = p.f("tp_wedding_date")
    days = p.f("tp_days_to_wedding")
    trial = p.f("tp_trial_completed")
    window = str(p.f("tp_next_step_window_open", "")).replace("_", " ")
    hook = p.say(bi(
        f"Hi {name}, {biz} here. {days} to {wedding}." if days and wedding else
        f"Hi {name}, {biz} here — checking in on your wedding prep.",
        f"Hi {name}, {biz} se. {wedding} mein {dur(p, days)} bache hain." if days and wedding else
        f"Hi {name}, {biz} se — wedding prep ke liye check kar rahe hain.",
    ))
    bits = []
    if trial:
        bits.append(p.say(bi(f"Your trial was on {trial}.", f"Aapka trial {trial} ko hua tha.")))
    if window:
        bits.append(p.say(bi(
            f"The {window} is the piece that has to start now to land on the day.",
            f"{window} abhi shuru hona chahiye taaki us din tak result mile.",
        )))
    insight = " ".join(bits)
    ask = p.say(bi(
        "Want us to block your prep dates now? Reply YES.",
        "Aapki prep dates abhi block kar dein? YES bhej dein.",
    ))
    return Draft(hook, insight, ask, CTA_BINARY, "", "merchant_bridal_followup_v1",
                 ("loss_aversion", "specificity", "single_binary_commitment"),
                 send_as="merchant_on_behalf")


def pb_appointment_tomorrow(p: P) -> Draft:
    name = p.f("cx_name", "there")
    biz = p.f("biz")
    appt = p.f("appt_noun", "appointment")
    slots = p.f("tp_slots")
    when = p.f("tp_appointment_time") or p.f("tp_slot_iso") or slots
    hook = p.say(bi(
        f"Hi {name}, {biz} here — reminder for your {appt} {when}." if when else
        f"Hi {name}, {biz} here — reminder for your {appt} tomorrow.",
        f"Hi {name}, {biz} se — aapke {appt} ka reminder {when}." if when else
        f"Hi {name}, {biz} se — kal ke {appt} ka reminder.",
    ))
    bits = []
    if p.has("cx_top_service"):
        svc = p.f("cx_top_service")
        bits.append(p.say(bi(f"{svc[0].upper()}{svc[1:]} as before.",
                             f"{svc[0].upper()}{svc[1:]} pehle jaisa.")))
    if (p.v("cx_visits") or 0) >= 2:
        bits.append(p.say(bi(f"You've been in {p.f('cx_visits_phrase')} before this.",
                             f"Isse pehle aap {p.f('cx_visits')} baar aa chuke hain.")))
    elif p.has("cx_last_visit"):
        bits.append(p.say(bi(f"Your last visit was {p.f('cx_last_visit')}.",
                             f"Aapki pichli visit {p.f('cx_last_visit')} thi.")))
    offer = p.f("offer_active")
    if offer and not bits:
        bits.append(p.say(bi(f"{offer} applies.", f"{offer} lagu hai.")))
    insight = " ".join(bits)
    ask = p.say(bi(
        "Reply YES to confirm, or tell us a better time.",
        "Confirm karne ke liye YES bhejein, ya behtar time bata dein.",
    ))
    return Draft(hook, insight, ask, CTA_BINARY, "", "merchant_appointment_reminder_v1",
                 ("specificity", "single_binary_commitment"),
                 send_as="merchant_on_behalf")


def pb_default_customer(p: P) -> Draft:
    if not p.has("cx_name"):
        return Draft("", skip=True, skip_reason="customer-scoped trigger without a consented customer context")
    state = p.v("cx_state")
    if state in ("lapsed_hard", "churned", "lapsed_soft"):
        return pb_customer_winback(p)
    return pb_recall_due(p)


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

REGISTRY: dict[str, Callable[[P], Draft]] = {
    # merchant-facing
    "research_digest": pb_research_digest,
    "category_research_digest_release": pb_research_digest,
    "regulation_change": pb_regulation_change,
    "compliance_alert": pb_regulation_change,
    "supply_alert": pb_supply_alert,
    "perf_dip": pb_perf_dip,
    "seasonal_perf_dip": pb_seasonal_perf_dip,
    "perf_spike": pb_perf_spike,
    "milestone_reached": pb_milestone,
    "renewal_due": pb_renewal_due,
    "winback_eligible": pb_winback,
    "gbp_unverified": pb_gbp_unverified,
    "competitor_opened": pb_competitor_opened,
    "review_theme_emerged": pb_review_theme,
    "festival_upcoming": pb_festival,
    "ipl_match_today": pb_local_event,
    "local_news_event": pb_local_event,
    "weather_heatwave": pb_local_event,
    "cde_opportunity": pb_cde_opportunity,
    "category_trend_movement": pb_trend_movement,
    "category_seasonal": pb_category_seasonal,
    "active_planning_intent": pb_active_planning,
    "curious_ask_due": pb_curious_ask,
    "scheduled_recurring": pb_curious_ask,
    "dormant_with_vera": pb_dormant,
    # customer-facing
    "recall_due": pb_recall_due,
    "customer_lapsed_soft": pb_recall_due,
    "customer_lapsed_hard": pb_customer_winback,
    "chronic_refill_due": pb_chronic_refill,
    "chronic_refill": pb_chronic_refill,
    "trial_followup": pb_trial_followup,
    "wedding_package_followup": pb_wedding_followup,
    "appointment_tomorrow": pb_appointment_tomorrow,
}

CUSTOMER_SCOPED = {
    "recall_due", "customer_lapsed_soft", "customer_lapsed_hard", "chronic_refill_due",
    "chronic_refill", "trial_followup", "wedding_package_followup", "appointment_tomorrow",
}


def select(trigger: dict) -> Callable[[P], Draft]:
    kind = str(trigger.get("kind", ""))
    if kind in REGISTRY:
        return REGISTRY[kind]
    # unseen kind: route by scope, still compose from merchant + category facts
    if trigger.get("scope") == "customer" or kind in CUSTOMER_SCOPED:
        return pb_default_customer
    for known, fn in REGISTRY.items():
        if known in kind or kind in known:
            return fn
    return pb_default_merchant
