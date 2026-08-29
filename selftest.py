#!/usr/bin/env python3
"""Offline self-test: drives the bot over real HTTP exactly the way the judge
harness does, and asserts the contract plus the behaviours the replay phase
scores. Needs no LLM key.

    python3 selftest.py [http://localhost:8080]
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
import time
from urllib import error as urlerror, request as urlrequest

BASE = (sys.argv[1] if len(sys.argv) > 1 else os.getenv("BOT_URL", "http://localhost:8080")).rstrip("/")
DATASET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dataset")

G, R, Y, D, B, RS = "\033[92m", "\033[91m", "\033[93m", "\033[2m", "\033[1m", "\033[0m"
PASS = FAIL = 0
LATENCIES: list[float] = []


def call(method: str, path: str, body: dict | None = None, timeout: int = 30):
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urlrequest.Request(url, data=data, method=method,
                             headers={"Content-Type": "application/json"})
    start = time.time()
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            out = json.loads(resp.read().decode())
            code = resp.status
    except urlerror.HTTPError as exc:
        code = exc.code
        try:
            out = json.loads(exc.read().decode())
        except Exception:
            out = {}
    latency = (time.time() - start) * 1000
    LATENCIES.append(latency)
    return code, out, latency


def check(name: str, ok: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  {G}PASS{RS} {name}")
    else:
        FAIL += 1
        print(f"  {R}FAIL{RS} {name}" + (f"  {D}{detail}{RS}" if detail else ""))
    return ok


def section(title: str) -> None:
    print(f"\n{B}--- {title} ---{RS}")


def load_dataset():
    cats, merch, cust, trig = {}, {}, {}, {}
    for path in sorted(glob.glob(os.path.join(DATASET, "categories", "*.json"))):
        d = json.load(open(path))
        cats[d["slug"]] = d
    for name, key, box in (("merchants_seed.json", "merchant_id", merch),
                           ("customers_seed.json", "customer_id", cust),
                           ("triggers_seed.json", "id", trig)):
        path = os.path.join(DATASET, name)
        if not os.path.exists(path):
            continue
        blob = json.load(open(path))
        items = next((v for v in blob.values() if isinstance(v, list)), [])
        for item in items:
            box[item[key]] = item
    return cats, merch, cust, trig


REQUIRED_ACTION_FIELDS = ("conversation_id", "merchant_id", "send_as", "trigger_id",
                          "body", "cta", "suppression_key", "rationale")
URL_RE = re.compile(r"https?://|www\.")
JARGON_RE = re.compile(r"\b(?:m|c|trg|d)_[a-z0-9_]{4,}\b|suppression_key|send_as|ChIJ")


def audit_action(action: dict, label: str) -> None:
    missing = [f for f in REQUIRED_ACTION_FIELDS if f not in action or action[f] in (None, "")]
    check(f"{label}: all required fields present", not missing, f"missing {missing}")
    body = action.get("body", "")
    check(f"{label}: body non-empty", bool(body.strip()))
    check(f"{label}: no URL in body", not URL_RE.search(body), body[:80])
    check(f"{label}: no internal identifiers in body", not JARGON_RE.search(body), body[:80])
    check(f"{label}: at most one question", body.count("?") <= 1, body[:120])
    check(f"{label}: send_as is valid",
          action.get("send_as") in ("vera", "merchant_on_behalf"), str(action.get("send_as")))
    if action.get("send_as") == "merchant_on_behalf":
        check(f"{label}: customer-facing send names a customer", bool(action.get("customer_id")))
    check(f"{label}: rationale is substantive", len(action.get("rationale", "")) > 60)


def main() -> int:
    cats, merch, cust, trig = load_dataset()
    call("POST", "/v1/teardown")   # start from a clean slate so runs are repeatable
    print(f"{B}Vera-G self-test{RS}  bot={BASE}  "
          f"dataset={len(cats)}cat/{len(merch)}mx/{len(cust)}cx/{len(trig)}trg")

    # ---------------- Phase 1: warmup -------------------------------------
    section("Phase 1 — warmup")
    code, health, _ = call("GET", "/v1/healthz", timeout=5)
    check("healthz returns 200 with status ok", code == 200 and health.get("status") == "ok")
    check("healthz reports contexts_loaded for all four scopes",
          set(health.get("contexts_loaded", {})) >= {"category", "merchant", "customer", "trigger"})
    code, meta, _ = call("GET", "/v1/metadata", timeout=5)
    check("metadata returns the required identity fields",
          code == 200 and all(k in meta for k in
                              ("team_name", "team_members", "model", "approach",
                               "contact_email", "version", "submitted_at")))

    pushed = 0
    for slug, payload in cats.items():
        code, out, _ = call("POST", "/v1/context",
                            {"scope": "category", "context_id": slug, "version": 1,
                             "payload": payload, "delivered_at": "2026-04-26T09:45:00Z"})
        pushed += 1 if out.get("accepted") else 0
    for mid, payload in merch.items():
        code, out, _ = call("POST", "/v1/context",
                            {"scope": "merchant", "context_id": mid, "version": 1,
                             "payload": payload, "delivered_at": "2026-04-26T09:45:00Z"})
        pushed += 1 if out.get("accepted") else 0
    for cid, payload in cust.items():
        code, out, _ = call("POST", "/v1/context",
                            {"scope": "customer", "context_id": cid, "version": 1,
                             "payload": payload, "delivered_at": "2026-04-26T09:45:00Z"})
        pushed += 1 if out.get("accepted") else 0
    check("every base context accepted", pushed == len(cats) + len(merch) + len(cust),
          f"{pushed}/{len(cats) + len(merch) + len(cust)}")

    first = next(iter(merch))
    code, out, _ = call("POST", "/v1/context",
                        {"scope": "merchant", "context_id": first, "version": 1,
                         "payload": merch[first], "delivered_at": "2026-04-26T09:46:00Z"})
    check("re-push of the same version is rejected as stale (409)",
          code == 409 and out.get("accepted") is False and out.get("reason") == "stale_version",
          f"code={code} out={out}")

    bumped = json.loads(json.dumps(merch[first]))
    bumped["performance"]["views"] = 9111
    code, out, _ = call("POST", "/v1/context",
                        {"scope": "merchant", "context_id": first, "version": 2,
                         "payload": bumped, "delivered_at": "2026-04-26T10:30:00Z"})
    check("higher version replaces the stored context", code == 200 and out.get("accepted") is True)

    code, out, _ = call("POST", "/v1/context",
                        {"scope": "nonsense", "context_id": "x", "version": 1, "payload": {}})
    check("invalid scope rejected with 400", code == 400 and out.get("reason") == "invalid_scope")
    code, out, _ = call("POST", "/v1/context",
                        {"scope": "merchant", "context_id": "x", "version": 1, "payload": "notadict"})
    check("non-object payload rejected with 400", code == 400)

    code, health, _ = call("GET", "/v1/healthz", timeout=5)
    counts = health.get("contexts_loaded", {})
    check("healthz counts match what was pushed",
          counts.get("category") == len(cats) and counts.get("merchant") >= len(merch)
          and counts.get("customer") == len(cust), str(counts))

    # ---------------- Phase 2: ticks --------------------------------------
    section("Phase 2 — tick + composition")
    for tid, payload in trig.items():
        call("POST", "/v1/context", {"scope": "trigger", "context_id": tid, "version": 1,
                                     "payload": payload, "delivered_at": "2026-04-26T10:32:00Z"})

    code, out, latency = call("POST", "/v1/tick",
                              {"now": "2026-04-26T10:35:00Z", "available_triggers": []})
    check("tick with no triggers responds fast", latency < 2000, f"{latency:.0f}ms")

    all_actions = []
    ids = list(trig)
    for i in range(0, len(ids), 5):
        batch = ids[i:i + 5]
        code, out, latency = call("POST", "/v1/tick",
                                  {"now": "2026-04-26T10:40:00Z", "available_triggers": batch})
        check(f"tick batch {i // 5 + 1} returns 200 under budget",
              code == 200 and latency < 10000, f"{latency:.0f}ms")
        all_actions.extend(out.get("actions", []))

    check("bot composed messages for the trigger set", len(all_actions) >= 15, f"{len(all_actions)}")
    for action in all_actions[:6]:
        audit_action(action, action.get("trigger_id", "?")[:34])

    bodies = [a["body"] for a in all_actions]
    check("no two sends share an identical body", len(set(bodies)) == len(bodies),
          f"{len(bodies) - len(set(bodies))} duplicate(s)")
    conv_ids = [a["conversation_id"] for a in all_actions]
    check("conversation ids are unique per send", len(set(conv_ids)) == len(conv_ids))
    per_merchant = {}
    for a in all_actions:
        per_merchant[a["merchant_id"]] = per_merchant.get(a["merchant_id"], 0) + 1
    check("no merchant is over-messaged in one window", max(per_merchant.values()) <= 3,
          str(per_merchant))
    cx = [a for a in all_actions if a["send_as"] == "merchant_on_behalf"]
    check("customer-scoped triggers produce merchant_on_behalf sends", len(cx) >= 3, f"{len(cx)}")
    with_numbers = [b for b in bodies if re.search(r"\d", b)]
    check("every message carries at least one verifiable number",
          len(with_numbers) == len(bodies), f"{len(bodies) - len(with_numbers)} without")

    # ---------------- Phase 3: adaptive injection -------------------------
    section("Phase 3 — mid-test context injection")
    dentists = json.loads(json.dumps(cats["dentists"]))
    dentists["digest"].insert(0, {
        "id": "d_2026W18_injected_perio",
        "kind": "research",
        "title": "Single-visit perio therapy matches staged therapy at 12 months",
        "source": "Indian Journal of Dental Research, May 2026, p.31",
        "trial_n": 640,
        "patient_segment": "moderate_periodontitis",
        "summary": "640-patient randomised trial found no difference in pocket depth at 12 months "
                   "between single-visit full-mouth disinfection and staged quadrant therapy.",
        "actionable": "Consider single-visit protocols for patients who struggle to return",
    })
    code, out, _ = call("POST", "/v1/context", {"scope": "category", "context_id": "dentists",
                                                "version": 2, "payload": dentists,
                                                "delivered_at": "2026-04-26T10:50:00Z"})
    check("new category version accepted", out.get("accepted") is True)

    new_trigger = {
        "id": "trg_900_injected_digest", "scope": "merchant", "kind": "research_digest",
        "source": "external", "merchant_id": "m_001_drmeera_dentist_delhi", "customer_id": None,
        "payload": {"category": "dentists"}, "urgency": 3,
        "suppression_key": "research:dentists:2026-W18", "expires_at": "2026-06-30T00:00:00Z",
    }
    call("POST", "/v1/context", {"scope": "trigger", "context_id": new_trigger["id"],
                                 "version": 1, "payload": new_trigger})
    code, out, _ = call("POST", "/v1/tick", {"now": "2026-04-26T10:55:00Z",
                                             "available_triggers": [new_trigger["id"]]})
    injected = out.get("actions", [])
    if check("bot acted on the injected trigger", len(injected) == 1, str(out)):
        body = injected[0]["body"]
        check("injected message uses the NEW digest item, not the stale one",
              "640" in body or "perio" in body.lower(), body[:150])
        check("injected message does not invent facts",
              "Indian Journal" in body or "640" in body, body[:150])
        print(f"    {D}{body}{RS}")

    # ---------------- Phase 4: replay scenarios ---------------------------
    section("Phase 4 — replay: auto-reply hell")
    canned = "Thank you for contacting Dr. Meera's Dental Clinic! Our team will respond shortly."
    seen_end = False
    for turn in range(1, 5):
        code, out, _ = call("POST", "/v1/reply",
                            {"conversation_id": f"conv_auto_{turn}",
                             "merchant_id": "m_001_drmeera_dentist_delhi", "customer_id": None,
                             "from_role": "merchant", "message": canned,
                             "received_at": "2026-04-26T11:00:00Z", "turn_number": turn + 1})
        act = out.get("action")
        print(f"    {D}turn {turn}: {act} {out.get('body', out.get('wait_seconds', ''))}{RS}"[:180])
        if turn == 1:
            check("auto-reply turn 1: one owner-directed nudge, not a re-pitch", act == "send")
        if turn == 2:
            check("auto-reply turn 2: backs off instead of replying to a machine", act == "wait")
        if act == "end":
            seen_end = True
            check(f"auto-reply detected and closed by turn {turn}", turn <= 3, f"turn {turn}")
            break
    check("bot exits auto-reply loop within 3 turns", seen_end)

    section("Phase 4 — replay: intent transition")
    call("POST", "/v1/reply", {"conversation_id": "conv_intent", "merchant_id": "m_002_bharat_dentist_mumbai",
                               "from_role": "merchant", "message": "What exactly would you change?",
                               "received_at": "2026-04-26T11:05:00Z", "turn_number": 2})
    code, out, _ = call("POST", "/v1/reply",
                        {"conversation_id": "conv_intent", "merchant_id": "m_002_bharat_dentist_mumbai",
                         "from_role": "merchant", "message": "Ok lets do it. Whats next?",
                         "received_at": "2026-04-26T11:06:00Z", "turn_number": 3})
    body = out.get("body", "")
    print(f"    {D}{body}{RS}")
    low = body.lower()
    qualifying = ["would you", "do you", "can you tell", "what if", "how about"]
    actioning = ["done", "sending", "draft", "here", "confirm", "proceed", "next", "starting", "on it"]
    check("commitment produces a send", out.get("action") == "send")
    check("reply switches to action verbs", any(w in low for w in actioning), body[:120])
    check("reply contains no fresh qualifying question",
          not any(w in low for w in qualifying), body[:120])

    section("Phase 4 — replay: hostile then off-topic")
    code, out, _ = call("POST", "/v1/reply",
                        {"conversation_id": "conv_hostile", "merchant_id": "m_003_studio11_salon_hyderabad",
                         "from_role": "merchant", "message": "Why are you bothering me. This is useless.",
                         "received_at": "2026-04-26T11:10:00Z", "turn_number": 2})
    print(f"    {D}{out.get('action')}: {out.get('body', '')}{RS}")
    check("hostility handled with an apology or a clean exit",
          out.get("action") == "end" or
          any(w in out.get("body", "").lower() for w in ("sorry", "apolog", "won't", "maafi")))
    code, out, _ = call("POST", "/v1/reply",
                        {"conversation_id": "conv_offtopic", "merchant_id": "m_006_southindiancafe_restaurant_bangalore",
                         "from_role": "merchant",
                         "message": "Btw can you also help me with my GST filing this month?",
                         "received_at": "2026-04-26T11:11:00Z", "turn_number": 2})
    body = out.get("body", "")
    print(f"    {D}{body}{RS}")
    check("off-topic request declined and redirected, not answered",
          out.get("action") == "send" and "gst" in body.lower(), body[:120])

    section("Phase 4 — replay: hard opt-out")
    code, out, _ = call("POST", "/v1/reply",
                        {"conversation_id": "conv_optout", "merchant_id": "m_004_glamour_salon_pune",
                         "from_role": "merchant", "message": "Not interested. Stop messaging me.",
                         "received_at": "2026-04-26T11:12:00Z", "turn_number": 2})
    check("explicit opt-out ends the conversation", out.get("action") == "end", str(out))
    code, out, _ = call("POST", "/v1/tick",
                        {"now": "2026-04-26T11:15:00Z",
                         "available_triggers": ["trg_009_winback_glamour", "trg_025_dormancy_glamour"]})
    check("opt-out suppresses that merchant on later ticks",
          all(a["merchant_id"] != "m_004_glamour_salon_pune" for a in out.get("actions", [])),
          str(out.get("actions")))

    section("Robustness")
    code, out, _ = call("POST", "/v1/tick", {"available_triggers": ["does_not_exist"]})
    check("unknown trigger id yields an empty action list, not an error",
          code == 200 and out.get("actions") == [])
    code, out, _ = call("POST", "/v1/reply", {"conversation_id": "conv_brand_new",
                                              "merchant_id": "m_005_pizzajunction_restaurant_delhi",
                                              "from_role": "merchant", "message": "yes go ahead",
                                              "turn_number": 2})
    check("reply on an unknown conversation still produces a valid action",
          out.get("action") in ("send", "wait", "end") and bool(out.get("rationale")))
    code, out, _ = call("POST", "/v1/reply", {})
    check("empty reply body handled without a 500", code == 200)

    section("Latency")
    p95 = sorted(LATENCIES)[int(len(LATENCIES) * 0.95)]
    check("p95 latency well inside the 30s judge timeout", p95 < 1000, f"p95={p95:.0f}ms")
    print(f"  {D}calls={len(LATENCIES)} p50={sorted(LATENCIES)[len(LATENCIES)//2]:.0f}ms "
          f"p95={p95:.0f}ms max={max(LATENCIES):.0f}ms{RS}")

    total = PASS + FAIL
    colour = G if FAIL == 0 else R
    print(f"\n{B}{colour}{PASS}/{total} checks passed{RS}\n")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
