"""Versioned, thread-safe context store with derived indexes.

The judge pushes four scopes of context at any time, in any order, at any
version. This module is the single source of truth the composer reads from.

Two things it does beyond a plain dict:

1. `novelty` tracking — when a category is re-pushed at a higher version, we
   diff the digest item ids and remember which ones are *new*. Phase 3 of the
   harness injects fresh digest items mid-test and rewards bots that use them,
   so "what changed since I last looked" has to be a first-class query.
2. `revision` counters — a monotonic per-context counter the composer folds
   into its variant selection, so a re-push of a merchant's performance
   snapshot produces a visibly different message instead of a repeat.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Any

VALID_SCOPES = ("category", "merchant", "customer", "trigger")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: Any) -> datetime | None:
    """Tolerant ISO-8601 parse. The judge sends several shapes; never raise."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(value.strip()[:19], fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Entry:
    __slots__ = ("scope", "context_id", "version", "payload", "stored_at", "seq", "revision")

    def __init__(self, scope: str, context_id: str, version: int, payload: dict, seq: int, revision: int):
        self.scope = scope
        self.context_id = context_id
        self.version = version
        self.payload = payload
        self.stored_at = utcnow()
        self.seq = seq          # global arrival order — "how fresh is this"
        self.revision = revision  # how many times this context_id has been replaced


class ContextStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[tuple[str, str], Entry] = {}
        self._seq = 0
        self._started = time.time()
        # category slug -> set of digest ids that arrived in the latest version
        self._new_digest_ids: dict[str, set[str]] = {}
        self._dirty = False

    # -- writes -------------------------------------------------------------
    def put(self, scope: str, context_id: str, version: int, payload: dict) -> tuple[bool, Entry | None, int]:
        """Returns (accepted, entry, current_version). Idempotent per (id, version)."""
        key = (scope, context_id)
        with self._lock:
            current = self._entries.get(key)
            if current is not None and version <= current.version:
                return False, current, current.version
            self._seq += 1
            revision = (current.revision + 1) if current else 0
            entry = Entry(scope, context_id, version, payload, self._seq, revision)
            if scope == "category":
                self._track_digest_novelty(context_id, current, payload)
            self._entries[key] = entry
            self._dirty = True
            return True, entry, version

    def _track_digest_novelty(self, slug: str, previous: Entry | None, payload: dict) -> None:
        new_ids = {d.get("id") for d in _as_list(payload.get("digest")) if isinstance(d, dict) and d.get("id")}
        if previous is None:
            self._new_digest_ids[slug] = set()
            return
        old_ids = {d.get("id") for d in _as_list(previous.payload.get("digest")) if isinstance(d, dict)}
        fresh = new_ids - old_ids
        if fresh:
            self._new_digest_ids[slug] = fresh

    # -- reads --------------------------------------------------------------
    def get(self, scope: str, context_id: str | None) -> dict | None:
        if not context_id:
            return None
        with self._lock:
            entry = self._entries.get((scope, context_id))
            return entry.payload if entry else None

    def entry(self, scope: str, context_id: str | None) -> Entry | None:
        if not context_id:
            return None
        with self._lock:
            return self._entries.get((scope, context_id))

    def all(self, scope: str) -> list[Entry]:
        with self._lock:
            return [e for (s, _), e in self._entries.items() if s == scope]

    def counts(self) -> dict[str, int]:
        counts = {s: 0 for s in VALID_SCOPES}
        with self._lock:
            for (scope, _) in self._entries:
                counts[scope] = counts.get(scope, 0) + 1
        return counts

    def new_digest_ids(self, slug: str | None) -> set[str]:
        if not slug:
            return set()
        with self._lock:
            return set(self._new_digest_ids.get(slug, ()))

    def uptime(self) -> int:
        return int(time.time() - self._started)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._new_digest_ids.clear()
            self._dirty = True

    # -- durability ---------------------------------------------------------
    # The judge pushes 255 contexts during warmup and expects them to still be
    # there an hour later. In-memory is allowed, but a process restart (an OOM,
    # a host recycling a free-tier container) would silently empty the store and
    # every later tick would compose nothing. A periodic snapshot makes a
    # restart recoverable; it is best-effort and never blocks a request.

    def snapshot(self, path: str) -> bool:
        with self._lock:
            if not self._dirty:
                return False
            payload = {
                "seq": self._seq,
                "new_digest_ids": {k: sorted(v) for k, v in self._new_digest_ids.items()},
                "entries": [
                    {"scope": e.scope, "context_id": e.context_id, "version": e.version,
                     "revision": e.revision, "seq": e.seq, "payload": e.payload}
                    for e in self._entries.values()
                ],
            }
            self._dirty = False
        try:
            directory = os.path.dirname(os.path.abspath(path)) or "."
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp, path)     # atomic; a torn file is never read back
            return True
        except OSError:
            self._dirty = True
            return False

    def restore(self, path: str) -> int:
        try:
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return 0
        restored = 0
        with self._lock:
            for row in payload.get("entries", []):
                try:
                    entry = Entry(row["scope"], row["context_id"], int(row["version"]),
                                  row["payload"], int(row.get("seq", 0)), int(row.get("revision", 0)))
                    self._entries[(entry.scope, entry.context_id)] = entry
                    restored += 1
                except (KeyError, TypeError, ValueError):
                    continue
            self._seq = max(int(payload.get("seq", 0)), self._seq)
            self._new_digest_ids = {k: set(v) for k, v in (payload.get("new_digest_ids") or {}).items()}
            self._dirty = False
        return restored

    # -- resolution ---------------------------------------------------------
    def category_for_merchant(self, merchant: dict | None) -> dict | None:
        if not merchant:
            return None
        slug = merchant.get("category_slug") or merchant.get("category")
        cat = self.get("category", slug)
        if cat is None and slug:
            # tolerate slug drift ("dentist" vs "dentists")
            for entry in self.all("category"):
                if str(entry.context_id).rstrip("s") == str(slug).rstrip("s"):
                    return entry.payload
        return cat

    def resolve(self, trigger_id: str) -> dict | None:
        """Expand a trigger id into the full 4-context bundle, or None if unusable."""
        trigger = self.get("trigger", trigger_id)
        if not isinstance(trigger, dict):
            return None
        merchant_id = trigger.get("merchant_id") or (trigger.get("payload") or {}).get("merchant_id")
        merchant = self.get("merchant", merchant_id)
        if not isinstance(merchant, dict):
            return None
        category = self.category_for_merchant(merchant)
        if not isinstance(category, dict):
            return None
        customer_id = trigger.get("customer_id") or (trigger.get("payload") or {}).get("customer_id")
        customer = self.get("customer", customer_id)
        if trigger.get("scope") == "customer" and not isinstance(customer, dict):
            customer = self._pick_customer_for(merchant_id, trigger)
        return {
            "trigger_id": trigger_id,
            "trigger": trigger,
            "merchant": merchant,
            "merchant_id": merchant_id,
            "category": category,
            "customer": customer if isinstance(customer, dict) else None,
            "trigger_entry": self.entry("trigger", trigger_id),
            "merchant_entry": self.entry("merchant", merchant_id),
            "category_entry": self.entry("category", category.get("slug")),
        }

    def _pick_customer_for(self, merchant_id: str | None, trigger: dict) -> dict | None:
        """A customer-scoped trigger with no customer_id still deserves a real send.

        Pick this merchant's most trigger-appropriate customer deterministically
        (stable ordering by id, preferring a state that matches the trigger kind).
        """
        if not merchant_id:
            return None
        candidates = [
            e.payload for e in self.all("customer")
            if isinstance(e.payload, dict) and e.payload.get("merchant_id") == merchant_id
        ]
        if not candidates:
            return None
        kind = str(trigger.get("kind", ""))
        preferred = {
            "recall_due": ("lapsed_soft", "active"),
            "customer_lapsed_soft": ("lapsed_soft",),
            "customer_lapsed_hard": ("lapsed_hard", "churned"),
            "winback": ("lapsed_hard", "churned"),
            "chronic_refill_due": ("active",),
            "chronic_refill": ("active",),
            "appointment_tomorrow": ("active",),
            "trial_followup": ("new", "active"),
        }.get(kind, ())
        candidates.sort(key=lambda c: str(c.get("customer_id", "")))
        for state in preferred:
            for c in candidates:
                if c.get("state") == state and _consented(c):
                    return c
        for c in candidates:
            if _consented(c):
                return c
        return None


def _consented(customer: dict) -> bool:
    consent = customer.get("consent") or {}
    prefs = customer.get("preferences") or {}
    if prefs.get("reminder_opt_in") is False:
        return False
    return bool(consent.get("opted_in_at") or consent.get("scope"))


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []
