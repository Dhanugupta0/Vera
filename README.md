# Vera-G — a grounded merchant assistant

**Team SmoothOperator** · Dhanu Gupta · dhanugupta.dev@gmail.com · v1.0.0
**Live base URL:** `https://vera-ex06.onrender.com`
**Interactive walkthrough:** https://claude.ai/code/artifact/0afa0ff8-b830-4375-a205-765a59e3b64a

```
POST /v1/context   POST /v1/tick   POST /v1/reply   GET /v1/healthz   GET /v1/metadata
```

## Approach

Reading the dataset first changed what I built. In the expanded set, **40 of 50 merchants**
ship with empty `offers`, `signals`, `conversation_history` and `review_themes`, and **75 of
100 triggers** carry `payload: {"placeholder": true}`. A bot tuned on Dr. Meera scores well on
ten merchants and says "improve your profile" to the rest. Specificity when the context is thin
is the whole game.

**Fact Ledger** (`vera/derive.py`) — every context push is distilled into
`Fact(display, value, source_path)`. A playbook can only say what the ledger holds, and each
`rationale` cites real paths. Derived facts rescue the thin 40: every merchant has views and a
CTR, every category a peer median, which is enough for a claim the merchant can check on their
own dashboard.

**The guard** (`vera/guard.py`) extracts every numeral from the finished body and checks it
against the licensed set. An unlicensed number means the sentence is *dropped*, not reworded.
The same pass strips URLs and internal identifiers, drops `vocab_taboo` sentences, and collapses
every question but the last. Hallucination is structurally unavailable, not merely discouraged.

**Playbooks** (`vera/playbooks.py`) — 33 trigger kinds, each rendering hook → insight → ask.
Every one has a rich path and a thin path, and several refuse their own frame when the data
won't support it (a `perf_dip` on a merchant *above* the peer median is reframed as a discovery
problem, because telling them their conversion is bad would be false).

**Reply routing** (`vera/intent.py`, `vera/dialog.py`) — fixed precedence:
`opt_out > hostile > auto_reply > commitment > objection > defer > off_topic > question >
negative > ack`. Commitment outranks question, because `"Ok lets do it. What's next?"` is not a
question. Auto-reply fingerprints live in *merchant* memory, so a repeat counts even across a
fresh `conversation_id`. Follow-through is scoped to the thread's own trigger and each plan
sentence is spent once, so a thread never repeats itself.

**No LLM in the request path.** The brief requires determinism; the judge caps a call at 30s and
a tick can carry 20 actions. Measured p95 **2 ms**. Contexts snapshot to disk every 20s and
restore at boot. Run **one worker** — state is in-process.

## Trade-offs

- **No LLM.** Buys determinism, millisecond latency, zero hallucination; costs linguistic
  surprise. The scored dimensions are functions of *which fact you choose*, not prose novelty.
- **Restraint costs scored surface.** Refusing to send when nothing is groundable means fewer
  messages to score. The brief says restraint is rewarded; I took it at its word.
- **Hand-written playbooks don't generalise for free.** An unknown kind routes by scope to a
  default that still composes from the peer-gap engine.

## What context would have helped most

1. **Search-impression data per listing.** Nothing in the dataset supports "6,777 missed searches
   in Sector 14" — impressions and the discovery-vs-conversion split are the missing half of
   every performance message.
2. **What the merchant did after each past message.** `conversation_history.engagement` has tags
   but no outcome; knowing which lever earned a reply would make variant selection a learned policy.
3. **The merchant's own service list and prices.** I fall back to the category catalog when
   `offers` is empty — correct, but generic.

## Running it

```bash
pip install -r requirements.txt
uvicorn vera.app:app --host 0.0.0.0 --port 8080 --workers 1
python3 selftest.py http://localhost:8080     # 84 assertions, no API key needed
```

Offline entry points: `bot.py::compose(category, merchant, trigger, customer)` (§7.1) and
`conversation_handlers.py::respond(state, message)` (§7.4).
Deploy: `Dockerfile`, `render.yaml`, `fly.toml`, `Procfile`.

**Verification.** `selftest.py` drives the bot over real HTTP through the judge's lifecycle:
84/84 pass. Across the full expanded dataset (100 triggers × 50 merchants, all 75 placeholder
payloads) the bot composes **100/100** with zero crashes, zero messages without a verifiable
number, and a median body of 273 characters.
