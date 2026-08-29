# Vera-G — a grounded merchant assistant for the magicpin AI Challenge

Live base URL: **`<paste your deployed https URL here>`**

```
POST /v1/context   POST /v1/tick   POST /v1/reply   GET /v1/healthz   GET /v1/metadata
```

---

## The bet

Reading the dataset before writing any code changed what I built. Two numbers decide this challenge:

- **40 of the 50 merchants** ship with empty `offers`, `signals`, `conversation_history`, and `review_themes`. Only `performance`, `identity`, `subscription`, and `customer_aggregate.total_unique_ytd` are present on all of them.
- **75 of the 100 triggers** carry `payload: {"placeholder": true, "metric_or_topic": <kind>}` — no data at all.

So a bot tuned on Dr. Meera scores beautifully on 10 merchants and produces "you should improve your profile" on the other 40. **Specificity when the context is thin is the whole game**, and it is what this bot is built around.

## Architecture

```
POST /v1/context ─► ContextStore ──┐  versioned, idempotent, diffs digest items
                                   │  across versions to know what is NEW
                                   ▼
POST /v1/tick ──► Policy ──► Fact Ledger ──► Playbook ──► Guard ──► action
                   │            │               │            │
              filter + rank  raw + derived   per-kind     numerals, URLs,
              (restraint)    verifiable      composition  taboos, identifiers,
                             facts           frames       one-CTA, repeats

POST /v1/reply ─► Intent router ─► Dialog state machine ─► send | wait | end
```

### 1. The Fact Ledger (`vera/derive.py`) — grounding as a data structure

Every context push is distilled into a ledger of `Fact(display, value, source_path)`. A playbook can only say something that exists in the ledger, and the ledger records where each fact came from, so the `rationale` the judge reads cites real paths (`merchant.customer_aggregate.high_risk_adult_count`, `derived(views × peer.avg_ctr)`).

The ledger also computes **derived facts**, which is what rescues the thin 40. Every merchant has views and a CTR; every category has a peer median. That is enough for a claim the merchant can verify on their own dashboard:

> *"Your listing pulled 3,185 views in 30 days but converted at 1.9% — the peer median for metro neighbourhood gyms 2026 is 4.5%. Same views at the median would be 143 actions instead of 61, a gap of 82."*

No trigger payload was involved. That line is pure arithmetic over two fields that are never missing.

### 2. Grounded generation is enforced, not hoped for

The guard (`vera/guard.py`) extracts every numeral from the finished body and checks it against the set the ledger licensed. An unlicensed number means the sentence containing it is **dropped**, not reworded. The same pass strips URLs (`-3` each per the API examples), removes any raw identifier or snake_case field name (`-1` internal jargon), drops sentences containing a `category.voice.vocab_taboo` term, and collapses every question but the last so there is exactly one CTA.

Hallucination is therefore structurally unavailable rather than merely discouraged. Where the payload has nothing to say, the playbook falls back to real data instead of inventing a fact — a `milestone_reached` trigger with an empty payload derives a threshold the merchant has *actually* crossed (`5,000 views in 30 days`) rather than making up a review count.

### 3. Playbooks with a thin path (`vera/playbooks.py`)

33 registered trigger kinds, each rendering `hook` (why now) → `insight` (why you) → `ask` (one thing, last). Every playbook has a rich path and a thin path, and several refuse their own frame when the data does not support it:

- `festival_upcoming` with no festival name becomes a seasonal-beat message, not "the festival is coming up".
- `chronic_refill_due` landing on a dentist (the generator scatters customer triggers across categories) routes to a category-appropriate recall instead of sending pharmacy copy.
- `review_theme_emerged` with no theme stays on the review topic using peer rating and review-count medians.
- A `perf_dip` on a merchant *above* the peer median is reframed as a discovery problem, because telling them their conversion is bad would be false.

Customer-facing copy picks its nouns from the category: a pharmacy customer gets "your refill is due … we'll keep it ready", a gym customer gets "your session … we'll save a spot".

### 4. Language

Merchant `identity.languages` **AND** `category.voice.code_mix` together decide the register. A gym owner who lists `hi` still gets English, because the gyms pack says `english_primary_some_hindi`. Where code-mix applies, the factual spine (numbers, prices, citations) stays English and the framing and CTA are Hindi-English — which is how the brief's own reference conversations read. Durations are localised (`38 days` → `38 din`) rather than concatenated.

### 5. Reply routing (`vera/intent.py`, `vera/dialog.py`)

A fixed precedence, because order matters more than accuracy here:

```
opt_out > hostile > auto_reply > commitment > objection > defer > off_topic > question > negative > ack
```

`"Ok lets do it. What's next?"` contains a question mark, and resolving it as a question instead of a commitment is exactly the Pattern D failure the brief calls out. Commitment therefore outranks question, and the action-mode reply states what is being done, at what scope, with a single CONFIRM — never another qualifying question.

**Auto-reply detection is per merchant, not per conversation.** A WhatsApp Business responder answers every thread with the same text, so the fingerprint lives in merchant memory and a repeat counts even when the judge opens a fresh `conversation_id`. Strike 1: one prompt aimed at the owner. Strike 2: back off 24h. Strike 3: end. Production Vera burns 2–3 turns per auto-reply; this caps it at one.

An opt-out on any conversation suppresses **every** queued trigger for that merchant.

### 6. Restraint

`vera/policy.py` gates before it ranks: opted-out, hostile, expired, suppression key already spent, merchant already messaged, too many open threads. Merchant-facing and customer-facing budgets are separate — a message to Priya is not a message to Dr. Meera. Ranking then weights urgency, how recently the context arrived, whether this merchant actually replies, expiry proximity, and a bonus for customer-scoped sends.

### 7. Adapting to injected context

The store diffs `digest` item ids between category versions. When Phase 3 pushes a new research item mid-test, the composer prefers it and says so ("just landed"). Verified in `selftest.py`: after injecting a fresh digest item, the next message quotes the new 640-patient trial and its journal, not the stale one.

## Operational choices

**No LLM in the request path.** The brief requires determinism (§7.1); the API examples budget 10s for a tick and the judge caps at 30s; a tick can carry up to 20 actions. A live model call per action is a timeout and a hallucination risk for variety I do not need — the interesting variation here comes from the contexts, not from sampling. Measured p50 **1 ms**, p95 **3 ms**, max **13 ms** across 83 calls. Deterministic variant selection uses CRC32, not `hash()`, which Python randomises per process.

**State durability.** Contexts are snapshotted to disk every 20s and restored at boot, so a container restart mid-test does not silently empty the store. Set `VERA_PUBLIC_URL` and the bot keeps itself warm — free tiers idle a service out after ~15 minutes, which would drop all 255 warmup contexts. Run **one worker**: state is in-process.

**Never 422.** Handlers parse the request body themselves and degrade instead of rejecting, because a schema rejection is scored as malformed. An unknown trigger returns `{"actions": []}`; an exception in tick planning returns `{"actions": []}`; an exception in reply returns a clean `end`.

## Running it

```bash
pip install -r requirements.txt
uvicorn vera.app:app --host 0.0.0.0 --port 8080 --workers 1

python3 selftest.py http://localhost:8080     # 84 assertions, no API key needed
```

Offline entry points: `bot.py::compose(category, merchant, trigger, customer)` (§7.1) and
`conversation_handlers.py::respond(state, message)` (§7.4).

```bash
python3 dataset/generate_dataset.py --out ./expanded
python3 make_submission.py ./expanded          # writes submission.jsonl (30 pairs)
```

Deploy: `Dockerfile`, `render.yaml`, `fly.toml`, `Procfile` are all provided.

## Verification

`selftest.py` drives the bot over real HTTP through the judge's own lifecycle and asserts 84 properties — contract shape, `(context_id, version)` idempotency including the 409, the four replay scenarios, and the content rules (no URLs, no internal identifiers, one CTA, a numeral in every message, no repeated body, opt-out suppression). All 84 pass.

Across the full expanded dataset — 100 triggers × 50 merchants, including all 75 placeholder payloads — the bot composes **100/100** with zero crashes, zero messages without a verifiable number, and a median body of 273 characters.

## Trade-offs

- **No LLM in the loop.** Buys determinism, 1 ms latency, and zero hallucination; costs the linguistic surprise a frontier model would bring. The hooks come from 33 hand-written frames rather than free generation. Given that the scored dimensions are specificity, category fit, merchant fit, trigger relevance, and compulsion — all functions of *which fact you choose*, not of prose novelty — I think this is the right side of the trade, and it is the only side compatible with §7.1's determinism requirement.
- **Restraint costs scored surface.** Refusing to send when nothing is groundable means fewer messages for the judge to score. The brief says restraint is rewarded; I took it at its word.
- **Hand-written playbooks don't generalise to unseen kinds for free.** An unknown `kind` routes by scope to a default that still composes from the peer-gap engine, so it degrades to "specific but generic-framed" rather than to nothing.

## What context would have helped most

1. **Search-impression data per listing.** The strongest real Vera line in the brief is "6,777 missed searches in Sector 14". Nothing in the dataset supports a claim like that — impressions, query terms, and the discovery-vs-conversion split are the missing half of every performance message, and CTR alone forces me to infer.
2. **What the merchant did after each past message.** `conversation_history.engagement` has tags but no outcome. Knowing which of the eight compulsion levers actually earned a reply *from this merchant* would turn variant selection from deterministic rotation into a learned policy.
3. **The merchant's own service list and price points.** I fall back to the category catalog when `offers` is empty, which is right but generic. Their real menu would make every offer line theirs instead of the category's.
