"""Deployment-time identity + tunables. Edit TEAM_* before submitting."""
import os

TEAM_NAME = os.getenv("VERA_TEAM_NAME", "SmoothOperator")
TEAM_MEMBERS = [m.strip() for m in os.getenv("VERA_TEAM_MEMBERS", "Dhanu Gupta").split(",") if m.strip()]
# Phone and LinkedIn deliberately stay out of /v1/metadata: it is a public
# unauthenticated endpoint and the harness never asks for them.
CONTACT_EMAIL = os.getenv("VERA_CONTACT_EMAIL", "dhanugupta.dev@gmail.com")
SUBMITTED_AT = os.getenv("VERA_SUBMITTED_AT", "2026-08-28T00:00:00Z")

MODEL = os.getenv("VERA_MODEL", "deterministic-grounded-composer/1.0 (no LLM in hot path)")
APPROACH = (
    "Fact-Ledger grounded composer: every context push is distilled into an auditable "
    "ledger of verifiable facts (raw + derived peer-gap arithmetic). A per-trigger-kind "
    "playbook selects facts and renders a WhatsApp turn; a guard rejects any numeral, "
    "URL, category taboo or internal identifier not licensed by the ledger. Replies run "
    "through a priority intent router (opt-out > hostile > auto-reply > commitment > ...) "
    "with cross-conversation auto-reply fingerprinting. Zero LLM calls in the request "
    "path: p99 under 15ms, fully deterministic."
)

# --- policy tunables -------------------------------------------------------
MAX_ACTIONS_PER_TICK = 20          # hard cap from the testing brief
MAX_ACTIONS_PER_MERCHANT_PER_TICK = 1
MAX_OPEN_CONVERSATIONS_PER_MERCHANT = 2
MAX_SENDS_PER_MERCHANT_PER_WINDOW = 3
MIN_URGENCY_WHEN_MERCHANT_BUSY = 3  # merchant already in an open conversation
AUTO_REPLY_STRIKES_BEFORE_END = 3
UNANSWERED_NUDGES_BEFORE_END = 3

# --- durability + uptime ---------------------------------------------------
# Where to snapshot pushed contexts so a process restart does not silently
# empty the store mid-test. Set to "" to disable.
STATE_PATH = os.getenv("VERA_STATE_PATH", "/tmp/vera_state.json")
SNAPSHOT_INTERVAL_SECONDS = int(os.getenv("VERA_SNAPSHOT_SECONDS", "20"))

# Free tiers on Render/Railway/Fly idle a container out after ~15 minutes of no
# traffic, which would drop every context the judge pushed during warmup. Set
# VERA_PUBLIC_URL to this bot's own public base URL and it will keep itself warm.
PUBLIC_URL = os.getenv("VERA_PUBLIC_URL", "").rstrip("/")
KEEPALIVE_INTERVAL_SECONDS = int(os.getenv("VERA_KEEPALIVE_SECONDS", "240"))
