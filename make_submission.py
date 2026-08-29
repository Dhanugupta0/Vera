#!/usr/bin/env python3
"""Write submission.jsonl for the canonical 30 (merchant, trigger) test pairs.

    python3 dataset/generate_dataset.py --out ./expanded
    python3 make_submission.py ./expanded
"""
from __future__ import annotations

import glob
import json
import os
import sys
from datetime import datetime, timezone

from bot import compose

EXPANDED = sys.argv[1] if len(sys.argv) > 1 else "./expanded"
NOW = datetime(2026, 4, 26, tzinfo=timezone.utc)


def load(pattern: str, key: str) -> dict:
    out = {}
    for path in sorted(glob.glob(pattern)):
        data = json.load(open(path, encoding="utf-8"))
        out[data[key]] = data
    return out


def main() -> int:
    cats = load(os.path.join(EXPANDED, "categories", "*.json"), "slug")
    merchants = load(os.path.join(EXPANDED, "merchants", "*.json"), "merchant_id")
    customers = load(os.path.join(EXPANDED, "customers", "*.json"), "customer_id")
    triggers = load(os.path.join(EXPANDED, "triggers", "*.json"), "id")

    pairs_path = os.path.join(EXPANDED, "test_pairs.json")
    if not os.path.exists(pairs_path):
        print(f"missing {pairs_path} — run dataset/generate_dataset.py first")
        return 1
    pairs = json.load(open(pairs_path, encoding="utf-8"))
    pairs = pairs.get("pairs", pairs) if isinstance(pairs, dict) else pairs

    written = 0
    with open("submission.jsonl", "w", encoding="utf-8") as fh:
        for i, pair in enumerate(pairs, start=1):
            trigger = triggers.get(pair.get("trigger_id") or pair.get("trigger", ""), {})
            merchant = merchants.get(pair.get("merchant_id") or trigger.get("merchant_id", ""), {})
            category = cats.get(merchant.get("category_slug", ""), {})
            customer = customers.get(trigger.get("customer_id") or "", None)
            result = compose(category, merchant, trigger, customer, now=NOW)
            fh.write(json.dumps({
                "test_id": pair.get("test_id", f"T{i:02d}"),
                "merchant_id": merchant.get("merchant_id", ""),
                "trigger_id": trigger.get("id", ""),
                **{k: result[k] for k in ("body", "cta", "send_as", "suppression_key", "rationale")},
            }, ensure_ascii=False) + "\n")
            written += 1
    print(f"wrote submission.jsonl with {written} lines")
    return 0


if __name__ == "__main__":
    sys.exit(main())
