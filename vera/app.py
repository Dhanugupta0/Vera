"""HTTP surface — the five endpoints the judge harness calls.

Every handler reads the raw request body itself instead of leaning on pydantic
validation. A 422 from a schema mismatch is scored as a malformed response, and
the brief is explicit that any context can arrive at any time in any shape, so
the bot degrades rather than rejects.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import config
from .dialog import Brain
from .policy import plan_tick
from .store import ContextStore, VALID_SCOPES, iso, parse_iso, utcnow

log = logging.getLogger("vera")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI(title="Vera-G", version=config.MODEL)
STORE = ContextStore()
BRAIN = Brain(STORE)
START = time.time()

STATS = {"context_pushes": 0, "context_rejected": 0, "ticks": 0, "actions_sent": 0,
         "replies": 0, "ends": 0, "waits": 0}


# ---------------------------------------------------------------------------
# background workers: snapshot + self-keepalive
# ---------------------------------------------------------------------------

def _snapshot_loop() -> None:
    while True:
        time.sleep(max(5, config.SNAPSHOT_INTERVAL_SECONDS))
        try:
            STORE.snapshot(config.STATE_PATH)
        except Exception:                              # never take the app down
            log.exception("snapshot failed")


def _keepalive_loop() -> None:
    url = f"{config.PUBLIC_URL}/v1/healthz"
    while True:
        time.sleep(max(60, config.KEEPALIVE_INTERVAL_SECONDS))
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                resp.read(1)
        except Exception as exc:
            log.warning("keepalive ping failed: %s", exc)


@app.on_event("startup")
async def _startup() -> None:
    if config.STATE_PATH:
        restored = STORE.restore(config.STATE_PATH)
        if restored:
            log.info("restored %d contexts from %s after a restart", restored, config.STATE_PATH)
        threading.Thread(target=_snapshot_loop, daemon=True, name="vera-snapshot").start()
    if config.PUBLIC_URL:
        threading.Thread(target=_keepalive_loop, daemon=True, name="vera-keepalive").start()
        log.info("keepalive enabled against %s", config.PUBLIC_URL)


async def _json_body(request: Request) -> dict:
    try:
        raw = await request.body()
        if not raw:
            return {}
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        log.warning("unparseable body: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# 2.4 / 2.5 — liveness and identity
# ---------------------------------------------------------------------------

@app.get("/v1/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({
        "status": "ok",
        "uptime_seconds": int(time.time() - START),
        "contexts_loaded": STORE.counts(),
    })


@app.get("/v1/metadata")
async def metadata() -> JSONResponse:
    return JSONResponse({
        "team_name": config.TEAM_NAME,
        "team_members": config.TEAM_MEMBERS,
        "model": config.MODEL,
        "approach": config.APPROACH,
        "contact_email": config.CONTACT_EMAIL,
        "version": "1.0.0",
        "submitted_at": config.SUBMITTED_AT,
    })


# ---------------------------------------------------------------------------
# 2.1 — context push
# ---------------------------------------------------------------------------

@app.post("/v1/context")
async def push_context(request: Request) -> JSONResponse:
    body = await _json_body(request)
    scope = body.get("scope")
    context_id = body.get("context_id")
    payload = body.get("payload")
    version = body.get("version", 1)

    if scope not in VALID_SCOPES:
        STATS["context_rejected"] += 1
        return JSONResponse({"accepted": False, "reason": "invalid_scope",
                             "details": f"scope must be one of {list(VALID_SCOPES)}"}, status_code=400)
    if not isinstance(context_id, str) or not context_id:
        STATS["context_rejected"] += 1
        return JSONResponse({"accepted": False, "reason": "invalid_context_id",
                             "details": "context_id must be a non-empty string"}, status_code=400)
    if not isinstance(payload, dict):
        STATS["context_rejected"] += 1
        return JSONResponse({"accepted": False, "reason": "invalid_payload",
                             "details": "payload must be a JSON object"}, status_code=400)
    try:
        version = int(version)
    except (TypeError, ValueError):
        version = 1

    accepted, entry, current = STORE.put(scope, context_id, version, payload)
    if not accepted:
        return JSONResponse({"accepted": False, "reason": "stale_version",
                             "current_version": current}, status_code=409)

    STATS["context_pushes"] += 1
    return JSONResponse({"accepted": True,
                         "ack_id": f"ack_{context_id}_v{version}",
                         "stored_at": iso(entry.stored_at)})


# ---------------------------------------------------------------------------
# 2.2 — tick
# ---------------------------------------------------------------------------

@app.post("/v1/tick")
async def tick(request: Request) -> JSONResponse:
    body = await _json_body(request)
    now = parse_iso(body.get("now")) or utcnow()
    available = body.get("available_triggers")
    if not isinstance(available, list):
        available = []

    STATS["ticks"] += 1
    try:
        actions, skips = plan_tick(STORE, BRAIN, now, available)
    except Exception:                                  # never fail a tick
        log.exception("tick planning failed")
        return JSONResponse({"actions": []})

    STATS["actions_sent"] += len(actions)
    if skips:
        log.info("tick %s: %d action(s); skipped: %s", iso(now), len(actions), "; ".join(skips[:6]))
    return JSONResponse({"actions": actions})


# ---------------------------------------------------------------------------
# 2.3 — reply
# ---------------------------------------------------------------------------

@app.post("/v1/reply")
async def reply(request: Request) -> JSONResponse:
    body = await _json_body(request)
    STATS["replies"] += 1
    try:
        result = BRAIN.handle_reply(body)
    except Exception:
        log.exception("reply handling failed")
        return JSONResponse({"action": "end",
                             "rationale": "Internal error while composing the reply; closing the "
                                          "conversation rather than returning a malformed action."})
    if result.get("action") == "end":
        STATS["ends"] += 1
    elif result.get("action") == "wait":
        STATS["waits"] += 1
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# teardown (optional, §11 of the testing brief)
# ---------------------------------------------------------------------------

@app.post("/v1/teardown")
async def teardown() -> JSONResponse:
    STORE.clear()
    BRAIN.clear()
    if config.STATE_PATH:
        STORE.snapshot(config.STATE_PATH)
    return JSONResponse({"accepted": True, "wiped_at": iso(utcnow())})


@app.get("/v1/debug")
async def debug() -> JSONResponse:
    """Not part of the contract — useful while iterating locally."""
    return JSONResponse({
        "stats": STATS,
        "contexts": STORE.counts(),
        "conversations": {
            cid: {"state": c.state, "mode": c.mode, "turns": len(c.turns),
                  "merchant_id": c.merchant_id, "end_reason": c.end_reason}
            for cid, c in list(BRAIN.conversations.items())[:50]
        },
        "merchants": {
            mid: {"opted_out": m.opted_out, "hostile": m.hostile, "sends": m.sends_used,
                  "auto_reply_strikes": m.auto_reply_strikes}
            for mid, m in list(BRAIN.merchants.items())[:50]
        },
    })


@app.get("/")
async def root() -> JSONResponse:
    return JSONResponse({"service": config.TEAM_NAME,
                         "endpoints": ["/v1/healthz", "/v1/metadata", "/v1/context",
                                       "/v1/tick", "/v1/reply"]})
