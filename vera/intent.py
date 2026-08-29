"""Priority intent router for merchant/customer replies.

Order matters more than accuracy here. "Ok lets do it. What's next?" contains a
question mark, but treating it as a question instead of a commitment is the
exact failure the brief calls out as Pattern D. So the router resolves in a
fixed precedence and the first match wins:

    opt_out > hostile > auto_reply > commitment > objection > defer
            > off_topic > question > negative > ack > unknown

Auto-reply detection is deliberately not per-conversation. A WhatsApp Business
canned responder answers every thread with the same text, so the fingerprint is
kept per merchant and a repeat counts as a strike even when the judge opens a
fresh conversation_id for it.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

OPT_OUT = "opt_out"
HOSTILE = "hostile"
AUTO_REPLY = "auto_reply"
COMMITMENT = "commitment"
OBJECTION = "objection"
DEFER = "defer"
OFF_TOPIC = "off_topic"
QUESTION = "question"
NEGATIVE = "negative"
ACK = "ack"
UNKNOWN = "unknown"


@dataclass
class Intent:
    label: str
    evidence: str = ""
    topic: str = ""

    def __str__(self) -> str:  # pragma: no cover
        return self.label


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fingerprint(text: str) -> str:
    """Stable signature for 'is this the same canned text again'."""
    words = normalise(text).split()
    return " ".join(words[:14])


# --- lexicons ---------------------------------------------------------------

OPT_OUT_PHRASES = (
    "stop messaging", "stop sending", "stop contacting", "do not message", "dont message",
    "don t message", "do not contact", "dont contact", "unsubscribe", "remove me", "opt out",
    "not interested", "no longer interested", "leave me alone", "never message",
    "band karo", "band kar do", "mat bhejo", "mat bheje", "message mat", "nahi chahiye",
    "koi zarurat nahi", "please stop",
)
OPT_OUT_EXACT = {"stop", "stop.", "unsubscribe", "band", "no thanks", "no thank you"}

HOSTILE_PHRASES = (
    "useless", "spam", "nonsense", "rubbish", "bakwas", "bekar", "faltu", "waste of time",
    "wasting my time", "why are you bothering", "stop bothering", "bothering me", "annoying",
    "irritating", "shut up", "idiot", "stupid", "fraud", "scam", "cheat", "pareshan mat",
    "tang mat", "get lost", "bloody", "damn you",
)

AUTO_REPLY_PHRASES = (
    "thank you for contacting", "thanks for contacting", "thank you for reaching out",
    "thank you for your message", "thanks for your message", "we have received your message",
    "message received", "our team will", "we will get back", "we will revert",
    "someone will get back", "will respond shortly", "will reply shortly", "respond as soon as",
    "back to you as soon as", "automated message", "automated response", "automated assistant",
    "this is an auto", "auto reply", "out of office", "away message", "our business hours",
    "office hours are", "working hours are", "currently unavailable", "not available right now",
    "for further assistance", "for any queries please",
    "aapki jaankari ke liye", "sampark karne ke liye dhanyavaad", "dhanyavaad", "shukriya",
    "hamari team", "humari team", "team tak pahuncha", "jaldi jawab", "sampark karenge",
    "aapka message mil gaya",
)

COMMITMENT_PHRASES = (
    "lets do it", "let s do it", "let us do it", "go ahead", "go for it", "please do",
    "please go", "do it", "yes please", "sounds good", "sign me up", "count me in",
    "i want to join", "i wanna join", "want to join", "join karna", "join karna hai",
    "judna hai", "judrna hai", "jurna hai", "jodna hai", "jud na hai", "juadna hai",
    "register karna", "shuru karna hai", "chalu karna hai",
    "send it", "send me", "share it", "share the", "pull it", "draft it", "set it up",
    "set up", "make it", "start it", "start now", "proceed", "confirm", "confirmed",
    "book it", "activate", "turn it on", "kar do", "kar dijiye", "kar dijiyega", "karo",
    "shuru karo", "shuru kar", "bhej do", "bhej dijiye", "bhejo", "theek hai karo",
    "haan bhej", "ok do", "okay do", "ready", "im in", "i am in", "chalu karo", "chalega",
)
COMMITMENT_EXACT = {
    "yes", "yes.", "y", "yep", "yeah", "yup", "ok", "ok.", "okay", "sure", "haan", "han",
    "haan ji", "ji haan", "theek hai", "thik hai", "done", "confirm", "confirmed", "go",
    "start", "proceed", "next", "acha", "achha", "ha", "ji", "yes do it", "ok yes",
}

OBJECTION_PHRASES = (
    "too expensive", "too costly", "very costly", "mehenga", "mehanga", "budget nahi",
    "no budget", "cant afford", "can t afford", "too much money", "price is high",
    "already have", "already using", "we do it ourselves", "not useful", "doesn t help",
    "does not help", "kaam nahi", "fayda nahi",
)

DEFER_PHRASES = (
    "later", "call me later", "call back", "next week", "next month", "baad mein", "abhi nahi",
    "not now", "busy", "vyast", "some other time", "will let you know", "let you know",
    "i will check", "will check", "think about it", "sochta hoon", "sochti hoon", "sochenge",
    "dekhta hoon", "dekhenge", "give me time", "thoda time",
)

OFF_TOPIC_TOPICS = {
    "gst": "GST filing", "income tax": "income tax", "itr": "tax filing", "tax": "tax filing",
    "loan": "business loans", "insurance": "insurance", "visa": "visas",
    "electricity bill": "utility bills", "recruit": "hiring", "hiring": "hiring",
    "staff salary": "payroll", "payroll": "payroll", "rent agreement": "rent agreements",
    "fssai": "FSSAI licensing", "trade licence": "trade licences", "trade license": "trade licences",
    "accountant": "accounting", "audit": "your audit", "legal notice": "legal matters",
    "police": "legal matters", "weather": "the weather", "cricket score": "cricket scores",
}

QUESTION_STARTS = (
    "what", "how", "why", "when", "where", "which", "who", "can you", "could you", "do you",
    "does it", "is it", "are you", "will you", "kya", "kaise", "kab", "kahan", "kaun", "kitna",
    "kitne", "kyun", "kyu",
)

NEGATIVE_EXACT = {"no", "no.", "nope", "nahi", "nahin", "na", "no need", "not required"}

ACK_PHRASES = ("thanks", "thank you", "thx", "ty", "noted", "got it", "shukriya", "dhanyavaad",
               "okay thanks", "ok thanks", "great", "nice", "good")


def _has(text: str, phrases) -> str:
    for phrase in phrases:
        if phrase in text:
            return phrase
    return ""


def looks_auto_reply(text: str) -> str:
    """Phrase-based detection, independent of repetition."""
    norm = normalise(text)
    hit = _has(norm, AUTO_REPLY_PHRASES)
    if hit:
        return hit
    # long, formal, no question, third-person about "the team" — the canned shape
    words = norm.split()
    if len(words) >= 12 and "?" not in text and re.search(r"\b(team|staff|executive|representative)\b", norm):
        if re.search(r"\b(will|shall|would)\b", norm):
            return "canned_third_person_shape"
    return ""


def classify(message: str, *, repeat_count: int = 0, in_action_mode: bool = False) -> Intent:
    raw = (message or "").strip()
    norm = normalise(raw)
    if not norm:
        return Intent(UNKNOWN, "empty message")

    hit = _has(norm, OPT_OUT_PHRASES)
    if hit or norm in OPT_OUT_EXACT:
        return Intent(OPT_OUT, hit or norm)

    auto = looks_auto_reply(raw)
    if repeat_count >= 1 and len(norm.split()) >= 4:
        return Intent(AUTO_REPLY, auto or f"identical text repeated {repeat_count + 1}x from this merchant")
    if auto:
        return Intent(AUTO_REPLY, auto)

    hit = _has(norm, HOSTILE_PHRASES)
    if hit:
        return Intent(HOSTILE, hit)

    hit = _has(norm, COMMITMENT_PHRASES)
    if hit or norm in COMMITMENT_EXACT:
        return Intent(COMMITMENT, hit or norm)

    hit = _has(norm, OBJECTION_PHRASES)
    if hit:
        return Intent(OBJECTION, hit)

    hit = _has(norm, DEFER_PHRASES)
    if hit:
        return Intent(DEFER, hit)

    for key, topic in OFF_TOPIC_TOPICS.items():
        if re.search(rf"\b{re.escape(key)}\b", norm):
            return Intent(OFF_TOPIC, key, topic)

    if "?" in raw or norm.startswith(QUESTION_STARTS):
        return Intent(QUESTION, "interrogative")

    if norm in NEGATIVE_EXACT:
        return Intent(NEGATIVE, norm)

    hit = _has(norm, ACK_PHRASES)
    if hit and len(norm.split()) <= 5:
        return Intent(ACK, hit)

    return Intent(UNKNOWN, "no lexical match")
