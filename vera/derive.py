"""The Fact Ledger — the grounding layer.

Everything the composer is allowed to say about a merchant has to exist here
first. A `Fact` carries a display string, the raw value, and the dotted context
path it came from. Two consequences:

* the guard can prove every numeral in an outgoing message traces to a pushed
  context, so hallucination is structurally impossible rather than merely
  unlikely;
* the `rationale` the judge reads can cite real source paths.

The ledger also computes *derived* facts. This matters more than the raw ones:
40 of the 50 merchants in the dataset ship with empty `offers`, `signals`,
`review_themes` and `conversation_history`, and 75 of the 100 triggers carry a
placeholder payload. The only universally-present data is the performance
snapshot and the category pack — so the peer-gap arithmetic below is what keeps
a message specific when there is nothing else to say.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

NUM_RE = re.compile(r"\d+(?:,\d{2,3})*(?:\.\d+)?")

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------

def fmt_int(value: Any) -> str | None:
    n = to_num(value)
    return f"{int(round(n)):,}" if n is not None else None


def fmt_pct(value: Any, places: int = 1) -> str | None:
    """0.021 -> '2.1%'."""
    n = to_num(value)
    if n is None:
        return None
    return f"{round(n * 100, places):g}%"


def fmt_pct_points(value: Any, places: int = 1) -> str | None:
    """0.021 -> '2.1' (no % sign; for 'X points below')."""
    n = to_num(value)
    return f"{round(n * 100, places):g}" if n is not None else None


def fmt_money(value: Any) -> str | None:
    n = to_num(value)
    return f"₹{int(round(n)):,}" if n is not None else None


def to_num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "").replace("₹", "").strip().rstrip("%")
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def numerals(text: str) -> set[str]:
    """Normalised numeric tokens in a string: '2,410 views' -> {'2410'}."""
    out = set()
    for match in NUM_RE.findall(text or ""):
        token = match.replace(",", "")
        out.add(token)
        if token.endswith(".0"):
            out.add(token[:-2])
        if "." in token:
            out.add(token.split(".")[0])
    return out


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------

@dataclass
class Fact:
    key: str
    display: str
    value: Any
    source: str

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.display


@dataclass
class Ledger:
    facts: dict[str, Fact] = field(default_factory=dict)
    licensed: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)

    def add(self, key: str, display: Any, value: Any, source: str) -> Fact | None:
        if display is None or display == "":
            return None
        display = str(display)
        fact = Fact(key, display, value, source)
        self.facts[key] = fact
        self.licensed |= numerals(display)
        return fact

    def license(self, *tokens: Any) -> None:
        """Allow a structural numeral the templates introduce themselves."""
        for token in tokens:
            self.licensed |= numerals(str(token))

    def get(self, key: str, default: str = "") -> str:
        fact = self.facts.get(key)
        return fact.display if fact else default

    def val(self, key: str, default: Any = None) -> Any:
        fact = self.facts.get(key)
        return fact.value if fact else default

    def has(self, *keys: str) -> bool:
        return all(k in self.facts for k in keys)

    def any_of(self, *keys: str) -> str | None:
        for key in keys:
            if key in self.facts:
                return self.facts[key].display
        return None

    def sources(self, *keys: str) -> str:
        return ", ".join(self.facts[k].source for k in keys if k in self.facts)


def _dig(obj: Any, *path: str, default: Any = None) -> Any:
    cur = obj
    for part in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(part)
    return cur if cur is not None else default


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


# ---------------------------------------------------------------------------
# builder
# ---------------------------------------------------------------------------

def build_ledger(bundle: dict, now_month: int | None = None) -> Ledger:
    led = Ledger()
    category = bundle.get("category") or {}
    merchant = bundle.get("merchant") or {}
    trigger = bundle.get("trigger") or {}
    customer = bundle.get("customer") or {}

    _identity(led, merchant, category)
    _subscription(led, merchant)
    _performance(led, merchant)
    _peer_gap(led, merchant, category)
    _customers(led, merchant)
    _offers(led, merchant, category)
    _signals(led, merchant)
    _reviews(led, merchant)
    _category_knowledge(led, category, trigger, bundle, now_month)
    _trigger_payload(led, trigger)
    if customer:
        _customer(led, customer)
    return led


# -- merchant ---------------------------------------------------------------

def _identity(led: Ledger, merchant: dict, category: dict) -> None:
    ident = merchant.get("identity") or {}
    biz = ident.get("name")
    owner = ident.get("owner_first_name")
    slug = str(category.get("slug") or merchant.get("category_slug") or "")

    if owner:
        owner = str(owner).strip()
        if slug.startswith("dentist") and not owner.lower().startswith("dr"):
            owner = f"Dr. {owner}"
        led.add("owner", owner, ident.get("owner_first_name"), "merchant.identity.owner_first_name")
    if biz:
        led.add("biz", biz, biz, "merchant.identity.name")
    # who we address: owner if we have one, else the business name
    led.add("addressee", owner or biz, owner or biz, "merchant.identity")

    for key, path in (("locality", "locality"), ("city", "city")):
        if ident.get(path):
            led.add(key, ident[path], ident[path], f"merchant.identity.{path}")
    if ident.get("locality") and ident.get("city"):
        led.add("place", f"{ident['locality']}, {ident['city']}", None, "merchant.identity")

    langs = [str(x).lower() for x in _list(ident.get("languages"))]
    led.add("languages", ",".join(langs), langs, "merchant.identity.languages")

    if ident.get("verified") is False:
        led.add("unverified", "unverified", False, "merchant.identity.verified")
    elif ident.get("verified") is True:
        led.add("verified", "verified", True, "merchant.identity.verified")

    year = to_num(ident.get("established_year"))
    if year and 1900 < year < 2100:
        led.add("established_year", str(int(year)), int(year), "merchant.identity.established_year")
        years = 2026 - int(year)
        if years >= 1:
            led.add("years_running", f"{years} years", years, "derived(2026 - established_year)")

    display_name = category.get("display_name") or slug or "business"
    led.add("category_name", display_name, slug, "category.display_name")
    led.add("category_one", _singular(str(display_name).split("&")[0].split(",")[0].strip()),
            slug, "derived(category.display_name)")
    led.add("category_slug", slug, slug, "category.slug")

    # Customer-facing copy needs the right noun for the visit and the booking.
    # "Your check-up is due" is wrong for a pharmacy and "hold a slot" is wrong
    # for an order.
    nouns = CATEGORY_NOUNS.get(_noun_key(slug), CATEGORY_NOUNS["_default"])
    led.add("visit_noun", nouns["visit"], nouns, "derived(category.slug)")
    led.add("appt_noun", nouns["appt"], nouns, "derived(category.slug)")
    led.add("hold_noun", nouns["hold"], nouns, "derived(category.slug)")
    led.add("hold_noun_hi", nouns["hold_hi"], nouns, "derived(category.slug)")


CATEGORY_NOUNS = {
    "dentist": {"visit": "check-up", "appt": "appointment", "hold": "hold a slot",
                "hold_hi": "slot hold kar denge"},
    "salon": {"visit": "appointment", "appt": "appointment", "hold": "hold a slot",
              "hold_hi": "slot hold kar denge"},
    "gym": {"visit": "session", "appt": "session", "hold": "save a spot",
            "hold_hi": "spot save kar denge"},
    "restaurant": {"visit": "table", "appt": "booking", "hold": "hold a table",
                   "hold_hi": "table hold kar denge"},
    "pharmac": {"visit": "refill", "appt": "pickup", "hold": "keep it ready",
                "hold_hi": "ready rakh denge"},
    "_default": {"visit": "visit", "appt": "appointment", "hold": "hold a slot",
                 "hold_hi": "slot hold kar denge"},
}


def _noun_key(slug: str) -> str:
    for key in CATEGORY_NOUNS:
        if key != "_default" and slug.startswith(key):
            return key
    return "_default"


def _subscription(led: Ledger, merchant: dict) -> None:
    sub = merchant.get("subscription") or {}
    status = sub.get("status")
    if status:
        led.add("sub_status", status, status, "merchant.subscription.status")
    if sub.get("plan"):
        led.add("plan", sub["plan"], sub["plan"], "merchant.subscription.plan")
    days = to_num(sub.get("days_remaining"))
    if days is not None and status in ("active", "trial") and days > 0:
        led.add("days_remaining", f"{int(days)} days", int(days), "merchant.subscription.days_remaining")
    since = to_num(sub.get("days_since_expiry"))
    if since:
        led.add("days_since_expiry", f"{int(since)} days", int(since), "merchant.subscription.days_since_expiry")


def _performance(led: Ledger, merchant: dict) -> None:
    perf = merchant.get("performance") or {}
    window = to_num(perf.get("window_days")) or 30
    led.add("perf_window", f"{int(window)} days", int(window), "merchant.performance.window_days")

    for key, label in (("views", "views"), ("calls", "calls"), ("directions", "directions"), ("leads", "leads")):
        n = to_num(perf.get(key))
        if n is not None:
            led.add(f"perf_{key}", fmt_int(n), int(n), f"merchant.performance.{key}")

    ctr = to_num(perf.get("ctr"))
    if ctr is not None:
        led.add("perf_ctr", fmt_pct(ctr), ctr, "merchant.performance.ctr")
        led.add("perf_ctr_points", fmt_pct_points(ctr), ctr, "merchant.performance.ctr")

    delta = perf.get("delta_7d") or {}
    for key, label in (("views_pct", "views"), ("calls_pct", "calls"), ("ctr_pct", "CTR")):
        d = to_num(delta.get(key))
        if d is None:
            continue
        metric = label
        pct = f"{abs(round(d * 100)):g}%"
        direction = "up" if d > 0 else "down" if d < 0 else "flat"
        led.add(f"delta_{key}", pct, d, f"merchant.performance.delta_7d.{key}")
        led.add(f"delta_{key}_dir", direction, direction, f"merchant.performance.delta_7d.{key}")
        led.add(f"delta_{key}_phrase", f"{metric} {direction} {pct} week-on-week", d,
                f"merchant.performance.delta_7d.{key}")


def _peer_gap(led: Ledger, merchant: dict, category: dict) -> None:
    """The workhorse. Turns two always-present numbers into a specific claim."""
    perf = merchant.get("performance") or {}
    peer = category.get("peer_stats") or {}

    for key, fmt in (
        ("avg_rating", str), ("avg_review_count", fmt_int), ("avg_views_30d", fmt_int),
        ("avg_calls_30d", fmt_int), ("avg_photos", fmt_int), ("avg_post_freq_days", fmt_int),
    ):
        raw = peer.get(key)
        if raw is not None:
            led.add(f"peer_{key}", fmt(raw), to_num(raw), f"category.peer_stats.{key}")
    if peer.get("retention_6mo_pct") is not None:
        led.add("peer_retention", fmt_pct(peer["retention_6mo_pct"], 0), to_num(peer["retention_6mo_pct"]),
                "category.peer_stats.retention_6mo_pct")
    if peer.get("scope"):
        led.add("peer_scope", str(peer["scope"]).replace("_", " "), peer["scope"], "category.peer_stats.scope")

    peer_ctr = to_num(peer.get("avg_ctr"))
    ctr = to_num(perf.get("ctr"))
    views = to_num(perf.get("views"))

    if peer_ctr is not None:
        led.add("peer_ctr", fmt_pct(peer_ctr), peer_ctr, "category.peer_stats.avg_ctr")

    if peer_ctr is not None and ctr is not None:
        gap = round((peer_ctr - ctr) * 100, 1)
        if gap > 0.05:
            led.add("ctr_gap_points", f"{gap:g}", gap, "derived(peer.avg_ctr - merchant.ctr)")
            led.add("ctr_position", "below", "below", "derived")
        elif gap < -0.05:
            led.add("ctr_lead_points", f"{abs(gap):g}", abs(gap), "derived(merchant.ctr - peer.avg_ctr)")
            led.add("ctr_position", "above", "above", "derived")
        else:
            led.add("ctr_position", "at", "at", "derived")

    if peer_ctr is not None and ctr is not None and views:
        at_peer = int(round(views * peer_ctr))
        at_now = int(round(views * ctr))
        shortfall = at_peer - at_now
        led.add("actions_at_peer", fmt_int(at_peer), at_peer, "derived(views x peer.avg_ctr)")
        led.add("actions_now", fmt_int(at_now), at_now, "derived(views x merchant.ctr)")
        if shortfall >= 3:
            led.add("action_gap", fmt_int(shortfall), shortfall,
                    "derived(views x (peer.avg_ctr - merchant.ctr))")
        elif shortfall <= -3:
            led.add("action_surplus", fmt_int(abs(shortfall)), abs(shortfall),
                    "derived(views x (merchant.ctr - peer.avg_ctr))")

    peer_views = to_num(peer.get("avg_views_30d"))
    if peer_views and views:
        ratio = views / peer_views
        if ratio >= 1.15:
            led.add("views_vs_peer", f"{ratio:.1f}x", round(ratio, 1), "derived(views / peer.avg_views_30d)")
            led.add("views_position", "above", "above", "derived")
        elif ratio <= 0.85:
            led.add("views_vs_peer_short", f"{round((1 - ratio) * 100):g}%", round(1 - ratio, 2),
                    "derived(1 - views / peer.avg_views_30d)")
            led.add("views_position", "below", "below", "derived")

    peer_calls = to_num(peer.get("avg_calls_30d"))
    calls = to_num(perf.get("calls"))
    if peer_calls and calls is not None:
        diff = int(round(calls - peer_calls))
        if diff >= 2:
            led.add("calls_above_peer", fmt_int(diff), diff, "derived(calls - peer.avg_calls_30d)")
        elif diff <= -2:
            led.add("calls_below_peer", fmt_int(abs(diff)), abs(diff), "derived(peer.avg_calls_30d - calls)")


def _customers(led: Ledger, merchant: dict) -> None:
    agg = merchant.get("customer_aggregate") or {}
    mapping = {
        "total_unique_ytd": ("cust_total", fmt_int, "unique customers this year"),
        "lapsed_180d_plus": ("cust_lapsed", fmt_int, "lapsed 180+ days"),
        "high_risk_adult_count": ("cust_high_risk", fmt_int, "high-risk adults"),
        "active_members": ("cust_active", fmt_int, "active members"),
        "repeat_rate_pct": ("cust_repeat", lambda v: fmt_pct(v, 0), "repeat rate"),
    }
    for src, (key, fmt, _label) in mapping.items():
        if agg.get(src) is not None:
            led.add(key, fmt(agg[src]), to_num(agg[src]), f"merchant.customer_aggregate.{src}")
    if agg.get("retention_6mo_pct") is not None:
        led.add("cust_retention", fmt_pct(agg["retention_6mo_pct"], 0), to_num(agg["retention_6mo_pct"]),
                "merchant.customer_aggregate.retention_6mo_pct")
    # any other numeric aggregate the judge invents later is still licensed
    for src, value in agg.items():
        if to_num(value) is not None and src not in mapping and src != "retention_6mo_pct":
            led.add(f"cust_x_{src}", fmt_int(value), to_num(value), f"merchant.customer_aggregate.{src}")


def _offers(led: Ledger, merchant: dict, category: dict) -> None:
    offers = _list(merchant.get("offers"))
    active = [o for o in offers if isinstance(o, dict) and o.get("status") == "active" and o.get("title")]
    lapsed = [o for o in offers if isinstance(o, dict) and o.get("status") in ("expired", "paused") and o.get("title")]

    if active:
        led.add("offer_active", active[0]["title"], active[0], "merchant.offers[active][0]")
        led.add("offer_active_all", " and ".join(o["title"] for o in active[:2]), active, "merchant.offers[active]")
        led.add("offer_active_count", str(len(active)), len(active), "merchant.offers[active]")
    else:
        led.add("offer_none", "no live offer", 0, "merchant.offers (empty/none active)")
    if lapsed:
        led.add("offer_lapsed", lapsed[0]["title"], lapsed[0], "merchant.offers[expired][0]")

    catalog = [o for o in _list(category.get("offer_catalog")) if isinstance(o, dict) and o.get("title")]
    if not catalog:
        return
    led.add("catalog_size", str(len(catalog)), len(catalog), "category.offer_catalog")
    active_titles = {str(o.get("title", "")).lower() for o in active}

    def _pick(pred) -> dict | None:
        for o in catalog:
            if str(o.get("title", "")).lower() in active_titles:
                continue
            if pred(o):
                return o
        return None

    # service+price beats a discount every time — the brief is explicit about it
    entry = (_pick(lambda o: o.get("type") == "service_at_price" and o.get("audience") == "new_user")
             or _pick(lambda o: o.get("type") == "service_at_price")
             or _pick(lambda o: True))
    if entry:
        led.add("catalog_offer", entry["title"], entry, "category.offer_catalog[service_at_price]")
    free = _pick(lambda o: o.get("type") == "free_service")
    if free:
        led.add("catalog_free_offer", free["title"], free, "category.offer_catalog[free_service]")
    repeat = _pick(lambda o: o.get("audience") == "repeat_user")
    if repeat:
        led.add("catalog_repeat_offer", repeat["title"], repeat, "category.offer_catalog[repeat_user]")


SIGNAL_LABELS = {
    "stale_posts": "no Google post in {v}",
    "ctr_below_peer_median": "CTR below the peer median",
    "unverified_gbp": "Google listing still unverified",
    "no_active_offers": "no live offer on the listing",
    "dormant_with_vera": "quiet with me for {v}",
    "perf_dip_severe": "a sharp performance dip",
    "no_recent_post": "no recent Google post",
    "delivery_not_set_up": "delivery not set up",
    "trial_ending_soon": "trial ending soon",
    "winback_eligible": "eligible for win-back",
    "high_engagement": "consistently engaged",
    "above_peer_ctr": "CTR above the peer median",
    "above_peer_median_calls": "calls above the peer median",
    "above_peer_calls": "calls above the peer median",
    "high_retention": "strong retention",
    "growing_views_7d": "views growing week-on-week",
}


def _signals(led: Ledger, merchant: dict) -> None:
    raw = [str(s) for s in _list(merchant.get("signals")) if s]
    if not raw:
        return
    led.add("signals_raw", ",".join(raw), raw, "merchant.signals")
    for item in raw:
        name, _, value = item.partition(":")
        led.facts.setdefault(f"signal:{name}", Fact(f"signal:{name}", value or "on", value or True, "merchant.signals"))
        if value:
            led.license(value)
        label = SIGNAL_LABELS.get(name)
        if label:
            pretty = label.format(v=_humanise_duration(value)) if "{v}" in label else label
            led.add(f"signal_text:{name}", pretty, value or True, f"merchant.signals[{name}]")
    # the single most quotable problem signal, in priority order
    for name in ("unverified_gbp", "no_active_offers", "stale_posts", "no_recent_post",
                 "ctr_below_peer_median", "delivery_not_set_up", "perf_dip_severe", "dormant_with_vera"):
        key = f"signal_text:{name}"
        if key in led.facts:
            led.add("headline_gap", led.facts[key].display, name, led.facts[key].source)
            break
    for name in ("above_peer_ctr", "above_peer_median_calls", "above_peer_calls", "high_retention",
                 "high_engagement", "growing_views_7d"):
        key = f"signal_text:{name}"
        if key in led.facts:
            led.add("headline_strength", led.facts[key].display, name, led.facts[key].source)
            break


def _singular(phrase: str) -> str:
    """'Pharmacies & Medical Stores' -> 'pharmacy'. str.rstrip('s') eats the
    wrong letters ('Fitness' -> 'Fitne'), so singularise the head word only."""
    head = (phrase or "").strip().split()
    if not head:
        return "business"
    word = head[0]
    low = word.lower()
    if low.endswith("ies") and len(low) > 4:
        return low[:-3] + "y"
    if low.endswith(("ches", "shes", "sses", "xes")):
        return low[:-2]
    if low.endswith("s") and not low.endswith("ss"):
        return low[:-1]
    return low


def _humanise_duration(value: str) -> str:
    match = re.match(r"^(\d+)\s*d", str(value or ""))
    if match:
        return f"{match.group(1)} days"
    return str(value or "").replace("_", " ")


def _reviews(led: Ledger, merchant: dict) -> None:
    themes = [t for t in _list(merchant.get("review_themes")) if isinstance(t, dict)]
    if not themes:
        return
    neg = [t for t in themes if t.get("sentiment") == "neg"]
    pos = [t for t in themes if t.get("sentiment") == "pos"]
    if neg:
        t = max(neg, key=lambda x: to_num(x.get("occurrences_30d")) or 0)
        led.add("review_neg_theme", str(t.get("theme", "")).replace("_", " "), t, "merchant.review_themes[neg]")
        if t.get("occurrences_30d") is not None:
            led.add("review_neg_count", fmt_int(t["occurrences_30d"]), to_num(t["occurrences_30d"]),
                    "merchant.review_themes[neg].occurrences_30d")
        if t.get("common_quote"):
            led.add("review_neg_quote", t["common_quote"], t["common_quote"],
                    "merchant.review_themes[neg].common_quote")
    if pos:
        t = max(pos, key=lambda x: to_num(x.get("occurrences_30d")) or 0)
        led.add("review_pos_theme", str(t.get("theme", "")).replace("_", " "), t, "merchant.review_themes[pos]")
        if t.get("common_quote"):
            led.add("review_pos_quote", t["common_quote"], t["common_quote"],
                    "merchant.review_themes[pos].common_quote")


# -- category ---------------------------------------------------------------

DIGEST_KIND_FOR_TRIGGER = {
    "research_digest": ("research",),
    "regulation_change": ("compliance",),
    "compliance_alert": ("compliance",),
    "cde_opportunity": ("cde", "research"),
    "category_trend_movement": ("trend",),
    "category_seasonal": ("trend", "research"),
    "supply_alert": ("compliance", "supply"),
    "competitor_opened": ("trend", "research"),
    "curious_ask_due": ("trend", "research"),
}


def _category_knowledge(led: Ledger, category: dict, trigger: dict, bundle: dict, now_month: int | None) -> None:
    digest = [d for d in _list(category.get("digest")) if isinstance(d, dict)]
    if digest:
        led.add("digest_count", str(len(digest)), len(digest), "category.digest")

    fresh_ids: set[str] = set(bundle.get("fresh_digest_ids") or ())
    kind = str(trigger.get("kind", ""))
    payload = trigger.get("payload") if isinstance(trigger.get("payload"), dict) else {}
    wanted_id = payload.get("top_item_id") or payload.get("digest_item_id") or payload.get("alert_id")

    chosen = None
    if wanted_id:
        chosen = next((d for d in digest if d.get("id") == wanted_id), None)
    if chosen is None and fresh_ids:
        # Phase 3 injects new digest items mid-test; prefer them when relevant.
        preferred_kinds = DIGEST_KIND_FOR_TRIGGER.get(kind)
        fresh_items = [d for d in digest if d.get("id") in fresh_ids]
        if preferred_kinds:
            chosen = next((d for d in fresh_items if d.get("kind") in preferred_kinds), None)
        chosen = chosen or (fresh_items[0] if fresh_items else None)
        if chosen is not None:
            led.add("digest_is_new", "new since last sync", True, "store.version_diff(category.digest)")
    if chosen is None:
        for k in DIGEST_KIND_FOR_TRIGGER.get(kind, ()):
            chosen = next((d for d in digest if d.get("kind") == k), None)
            if chosen:
                break
    if chosen is None and digest:
        chosen = digest[0]

    if chosen:
        _digest_facts(led, chosen, prefix="digest")
    # keep a compliance item to hand — it is the highest-urgency thing we can raise
    compliance = next((d for d in digest if d.get("kind") == "compliance"), None)
    if compliance and compliance is not chosen:
        _digest_facts(led, compliance, prefix="compliance")

    beats = [b for b in _list(category.get("seasonal_beats")) if isinstance(b, dict)]
    beat = _match_seasonal_beat(beats, now_month)
    if beat:
        led.add("season_note", str(beat.get("note", "")).split("—")[0].strip(), beat,
                "category.seasonal_beats[current_month]")
        led.add("season_note_full", beat.get("note"), beat, "category.seasonal_beats[current_month]")
        led.add("season_window", beat.get("month_range"), beat, "category.seasonal_beats.month_range")
    elif beats:
        led.add("season_note_full", beats[0].get("note"), beats[0], "category.seasonal_beats[0]")
        led.add("season_window", beats[0].get("month_range"), beats[0], "category.seasonal_beats[0]")

    trends = [t for t in _list(category.get("trend_signals")) if isinstance(t, dict)]
    if trends:
        top = max(trends, key=lambda t: to_num(t.get("delta_yoy")) or 0)
        led.add("trend_query", top.get("query"), top, "category.trend_signals[max delta_yoy].query")
        d = to_num(top.get("delta_yoy"))
        if d is not None:
            led.add("trend_delta", f"{round(d * 100):g}%", d, "category.trend_signals[max].delta_yoy")
        if top.get("segment_age"):
            led.add("trend_segment", top["segment_age"], top["segment_age"],
                    "category.trend_signals[max].segment_age")

    library = [c for c in _list(category.get("patient_content_library")) if isinstance(c, dict)]
    if library:
        item = library[0]
        led.add("content_title", item.get("title"), item, "category.patient_content_library[0].title")
        if item.get("length_seconds"):
            led.add("content_length", f"{int(to_num(item['length_seconds']) or 0)}-second", item,
                    "category.patient_content_library[0].length_seconds")
        led.add("content_count", str(len(library)), len(library), "category.patient_content_library")

    voice = category.get("voice") or {}
    led.add("voice_tone", voice.get("tone"), voice.get("tone"), "category.voice.tone")
    taboo = [str(t).lower() for t in _list(voice.get("vocab_taboo"))]
    led.facts["voice_taboo"] = Fact("voice_taboo", "", taboo, "category.voice.vocab_taboo")
    allowed = [str(t) for t in _list(voice.get("vocab_allowed"))]
    led.facts["voice_allowed"] = Fact("voice_allowed", "", allowed, "category.voice.vocab_allowed")
    if allowed:
        led.add("vocab_term", allowed[0], allowed, "category.voice.vocab_allowed[0]")

    journals = _list(category.get("professional_journals"))
    if journals:
        led.add("journal", journals[0], journals, "category.professional_journals[0]")
    authorities = _list(category.get("regulatory_authorities"))
    if authorities:
        led.add("authority", authorities[0], authorities, "category.regulatory_authorities[0]")


def _digest_facts(led: Ledger, item: dict, prefix: str) -> None:
    led.add(f"{prefix}_id", item.get("id"), item.get("id"), f"category.digest[{item.get('id')}].id")
    led.add(f"{prefix}_title", item.get("title"), item, f"category.digest[{item.get('id')}].title")
    led.add(f"{prefix}_source", item.get("source"), item, f"category.digest[{item.get('id')}].source")
    led.add(f"{prefix}_summary", item.get("summary"), item, f"category.digest[{item.get('id')}].summary")
    led.add(f"{prefix}_actionable", item.get("actionable"), item, f"category.digest[{item.get('id')}].actionable")
    led.add(f"{prefix}_kind", item.get("kind"), item.get("kind"), f"category.digest[{item.get('id')}].kind")
    if item.get("trial_n") is not None:
        led.add(f"{prefix}_trial_n", fmt_int(item["trial_n"]), to_num(item["trial_n"]),
                f"category.digest[{item.get('id')}].trial_n")
    if item.get("credits") is not None:
        led.add(f"{prefix}_credits", fmt_int(item["credits"]), to_num(item["credits"]),
                f"category.digest[{item.get('id')}].credits")
    if item.get("date"):
        led.add(f"{prefix}_date", _pretty_date(item["date"]), item["date"],
                f"category.digest[{item.get('id')}].date")
    if item.get("patient_segment"):
        led.add(f"{prefix}_segment", str(item["patient_segment"]).replace("_", " "), item["patient_segment"],
                f"category.digest[{item.get('id')}].patient_segment")
    # the summary is quotable verbatim, so license its numerals too
    led.license(item.get("summary") or "", item.get("title") or "", item.get("actionable") or "")


def _match_seasonal_beat(beats: list[dict], now_month: int | None) -> dict | None:
    if not beats or not now_month:
        return None
    label = MONTHS[(now_month - 1) % 12]
    for beat in beats:
        rng = str(beat.get("month_range", ""))
        if "-" in rng:
            start, _, end = rng.partition("-")
            try:
                s = MONTHS.index(start.strip()[:3])
                e = MONTHS.index(end.strip()[:3])
            except ValueError:
                continue
            idx = now_month - 1
            inside = s <= idx <= e if s <= e else (idx >= s or idx <= e)
            if inside:
                return beat
        elif rng.strip()[:3] == label:
            return beat
    return None


# -- trigger ----------------------------------------------------------------

def _trigger_payload(led: Ledger, trigger: dict) -> None:
    led.add("trigger_kind", str(trigger.get("kind", "")).replace("_", " "), trigger.get("kind"), "trigger.kind")
    led.add("trigger_source", trigger.get("source"), trigger.get("source"), "trigger.source")
    urgency = to_num(trigger.get("urgency"))
    if urgency is not None:
        led.facts["trigger_urgency"] = Fact("trigger_urgency", "", int(urgency), "trigger.urgency")

    payload = trigger.get("payload") if isinstance(trigger.get("payload"), dict) else {}
    if not payload or payload.get("placeholder") is True:
        led.notes.append("trigger payload is a placeholder — composing from merchant + category facts")
        led.facts["payload_thin"] = Fact("payload_thin", "", True, "trigger.payload.placeholder")
        return

    for key, value in payload.items():
        if key in ("placeholder", "metric_or_topic"):
            continue
        path = f"trigger.payload.{key}"
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            led.add(f"tp_{key}", _fmt_payload_number(key, value), value, path)
            if key.endswith("_pct") or key.endswith("_yoy"):
                led.add(f"tp_{key}_dir", "up" if value > 0 else "down", value, path)
        elif isinstance(value, str):
            if _looks_iso(value):
                led.add(f"tp_{key}", _pretty_date(value), value, path)
            else:
                led.add(f"tp_{key}", _humanise_token(value), value, path)
        elif isinstance(value, list):
            texts = [str(v).replace("_", " ") for v in value if isinstance(v, (str, int, float))]
            if texts:
                led.add(f"tp_{key}", ", ".join(texts[:5]), value, path)
            slots = [v for v in value if isinstance(v, dict) and v.get("label")]
            if slots:
                led.add(f"tp_{key}_slots", " or ".join(s["label"] for s in slots[:2]), slots, path)
                led.add(f"tp_{key}_slot_count", str(len(slots)), len(slots), path)
        elif isinstance(value, dict):
            for sub, subval in value.items():
                if isinstance(subval, (str, int, float)) and not isinstance(subval, bool):
                    led.add(f"tp_{key}_{sub}", str(subval), subval, f"{path}.{sub}")

    # canonical aliases the playbooks reach for
    if "tp_season_note" in led.facts and "season_note_full" in led.facts:
        if " " not in str(payload.get("season_note", "")):
            led.facts["tp_season_note"] = led.facts["season_note_full"]

    _alias(led, "tp_slots", "tp_available_slots_slots", "tp_next_session_options_slots")
    _alias(led, "tp_slot_count", "tp_available_slots_slot_count", "tp_next_session_options_slot_count")
    _alias(led, "tp_delta", "tp_delta_pct")
    _alias(led, "tp_delta_dir", "tp_delta_pct_dir")


def _alias(led: Ledger, target: str, *candidates: str) -> None:
    if target in led.facts:
        return
    for cand in candidates:
        if cand in led.facts:
            led.facts[target] = led.facts[cand]
            return


MONEY_KEY_HINTS = ("amount", "price", "fee", "revenue", "ltv", "cost", "spend")
DAY_KEY_HINTS = ("days", "day_count", "duration_days")
MONTH_KEY_HINTS = ("months", "membership_months")


def _fmt_payload_number(key: str, value: float) -> str:
    """Numbers in a trigger payload carry units the key name implies."""
    low = key.lower()
    if low.endswith("_pct") or low.endswith("_yoy"):
        return f"{abs(round(value * 100)):g}%"
    if any(h in low for h in MONEY_KEY_HINTS):
        return fmt_money(value) or str(value)
    if any(h in low for h in DAY_KEY_HINTS):
        return f"{int(round(value))} days"
    if any(h in low for h in MONTH_KEY_HINTS):
        return f"{int(round(value))} months"
    if float(value).is_integer():
        return fmt_int(value) or str(value)
    return f"{round(float(value), 2):g}"


_WINDOW_RE = re.compile(r"^(\d+)\s*([dwmhy])$", re.I)
_UNITS = {"d": "days", "w": "weeks", "m": "months", "h": "hours", "y": "years"}


def _humanise_token(value: str) -> str:
    """'7d' -> '7 days'; 'weight_loss' -> 'weight loss'; leaves prose alone."""
    text = value.strip()
    m = _WINDOW_RE.match(text)
    if m:
        return f"{m.group(1)} {_UNITS[m.group(2).lower()]}"
    if "_" in text and " " not in text:
        text = text.replace("_", " ")
    return re.sub(r"\b(\d+)(day|week|month|min|hour)\b", r"\1-\2", text)


def _looks_iso(value: str) -> bool:
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}", value or ""))


def _pretty_date(value: str) -> str:
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(value))
    if not match:
        return str(value)
    year, month, day = match.groups()
    return f"{int(day)} {MONTHS[int(month) - 1]} {year}"


# -- customer ---------------------------------------------------------------

def _customer(led: Ledger, customer: dict) -> None:
    ident = customer.get("identity") or {}
    rel = customer.get("relationship") or {}
    prefs = customer.get("preferences") or {}

    if ident.get("name"):
        led.add("cx_name", ident["name"], ident["name"], "customer.identity.name")
    led.add("cx_lang", str(ident.get("language_pref") or "en").lower(), ident.get("language_pref"),
            "customer.identity.language_pref")
    if ident.get("age_band"):
        led.add("cx_age_band", ident["age_band"], ident["age_band"], "customer.identity.age_band")

    if customer.get("state"):
        led.add("cx_state", str(customer["state"]).replace("_", " "), customer["state"], "customer.state")
    visits = to_num(rel.get("visits_total"))
    if visits:
        led.add("cx_visits", fmt_int(visits), int(visits), "customer.relationship.visits_total")
        led.add("cx_visits_phrase", f"{fmt_int(visits)} time" + ("" if int(visits) == 1 else "s"),
                int(visits), "customer.relationship.visits_total")
    if rel.get("last_visit"):
        led.add("cx_last_visit", _pretty_date(rel["last_visit"]), rel["last_visit"],
                "customer.relationship.last_visit")
    if rel.get("first_visit"):
        led.add("cx_first_visit", _pretty_date(rel["first_visit"]), rel["first_visit"],
                "customer.relationship.first_visit")
    services = [str(s).replace("_", " ") for s in _list(rel.get("services_received"))]
    if services:
        led.add("cx_last_service", services[-1], services, "customer.relationship.services_received[-1]")
        led.add("cx_service_count", str(len(services)), len(services), "customer.relationship.services_received")
        top = max(set(services), key=services.count)
        led.add("cx_top_service", top, top, "customer.relationship.services_received[mode]")
    if rel.get("lifetime_value") is not None:
        led.add("cx_ltv", fmt_money(rel["lifetime_value"]), to_num(rel["lifetime_value"]),
                "customer.relationship.lifetime_value")

    slots = str(prefs.get("preferred_slots") or "")
    if slots:
        led.add("cx_pref_slots", slots.replace("_", " "), slots, "customer.preferences.preferred_slots")
    consent = customer.get("consent") or {}
    scope = [str(s).replace("_", " ") for s in _list(consent.get("scope"))]
    if scope:
        led.add("cx_consent_scope", ", ".join(scope), scope, "customer.consent.scope")
    if consent.get("opted_in_at"):
        led.add("cx_opted_in", _pretty_date(consent["opted_in_at"]), consent["opted_in_at"],
                "customer.consent.opted_in_at")
