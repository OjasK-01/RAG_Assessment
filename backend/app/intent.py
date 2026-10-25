"""Rule-based intent detection: instant, free and deterministic.

Natural-language understanding of the question itself comes from embedding search (paraphrases
land near the right article); these rules only decide things that must never depend on a model:
small talk short-circuits, incident escalation, and whether a message is a follow-up to the
previous turn. Escalation patterns cover English, Hinglish (romanised Hindi) and Devanagari, since
Gurugram staff write all three."""
import re
from dataclasses import dataclass

_GREETING = re.compile(r"^(?:hi+|hello|hey|hiya|namaste|good (?:morning|afternoon|evening))(?: there)?[\s!.,]*$", re.I)
_THANKS = re.compile(r"^(?:thanks?(?: you)?(?: so much)?|thx|ty|ok(?:ay)?|great|cool|got it|perfect)[\s!.,]*$", re.I)

_SECURITY = re.compile(
    r"\b(?:phish\w*|ransom\w*|malware|virus|hacked|compromised|suspicious (?:link|email|mail|attachment|page)"
    r"|clicked (?:on )?(?:a |the |an )?(?:\w+ ){0,3}link"
    r"|(?:entered|typed|put) (?:in )?my (?:password|credentials) (?:on|into|in)"
    r"|files? (?:are |is )?(?:being )?(?:renamed|encrypted)|unknown extensions?"
    # Hinglish: "account hack ho gaya", "galat link pe click kiya", "password daal diya", "virus aa gaya"
    r"|hack (?:ho|kar) (?:gaya|gayi|liya|diya)|(?:link|mail) (?:pe|par) click"
    r"|password (?:daal|dal|daala|enter kar) (?:diya|di)|virus aa gaya|files? (?:ka|ke) naam badal)\b"
    r"|फ़िशिंग|फिशिंग|हैक|वायरस|मैलवेयर|रैनसमवेयर", re.I)

_P1 = re.compile(
    r"\b(?:all|every|whole|entire)\b[^.?!]{0,40}\b(?:printers?|scanners?|warehouse|site|dock|users?|staff|team|office)\b"
    r"[^.?!]{0,40}\b(?:down|stopped|stop|not working|offline|can'?t work|cannot work|blocked)\b"
    r"|\bdispatch (?:is )?(?:blocked|stopped|down|halted)\b"
    r"|\b(?:down|unreachable|not working) for everyone\b"
    r"|\b(?:nobody|no one) (?:can|is able to) (?:work|log ?in|connect|scan|print)\b"
    r"|\b(?:business|operations) (?:has |have )?(?:stopped|halted|down)\b"
    r"|\boutage\b"
    # Hinglish: "poora warehouse band hai", "sab printers ruk gaye", "koi kaam nahi kar pa raha", "dispatch ruk gaya"
    r"|\b(?:poora|pura|saare|sare|sab|sabhi)\b[^.?!]{0,40}\b(?:band|ruk gaye|ruk gaya|nahi chal rahe?|down)\b"
    r"|\b(?:koi bhi|koi) kaam nahi kar (?:pa )?(?:raha|rahe)\b|\bdispatch (?:ruk|band) (?:gaya|hai|ho gaya)\b"
    r"|सब बंद|पूरा .{0,20}बंद|काम रुक", re.I)

# Follow-up detection. A message is treated as a follow-up to the previous turn (and searched together
# with it) when it opens like one, or is short and refers back to something ("does it work on Linux?").
_FOLLOWUP_OPENER = re.compile(
    r"^(?:what|how) about\b|^and\b|^also\b|^what if\b|^what else\b|^then\b|^but\b|^same\b|^in that case\b"
    r"|^ok(?:ay)?,? (?:and|what|how|so)\b|^so\b|^for (?:p[1-4]|a p[1-4]|vip|mac|macos|windows|iphone|android)\b"
    r"|^aur\b|^phir\b|^uska\b|^iska\b", re.I)
_REFERS_BACK = re.compile(
    r"\b(?:it|its|that|this|those|these|them|they|there|same|above|previous|first|second|one|ones"
    r"|ye|yeh|woh|wo|uska|iska)\b", re.I)
_SELF_STATEMENT = re.compile(r"^(?:my|mine|i|i'm|im|i am|i've|our|we|mera|meri|mere|mujhe)\b", re.I)


@dataclass(frozen=True)
class Intent:
    kind: str  # "greeting" | "thanks" | "kb_question"
    security_incident: bool = False
    p1: bool = False


def is_followup(text: str) -> bool:
    t = text.strip()
    words = re.findall(r"\w+", t)
    if _FOLLOWUP_OPENER.search(t):
        return True
    # Elliptical: "first response?", "and P1?", "on macOS?". Not "My laptop is slow": a short message
    # about the employee's own situation is a new, self-contained question.
    if len(words) <= 4 and not _SELF_STATEMENT.match(t):
        return True
    return len(words) <= 10 and bool(_REFERS_BACK.search(t))


def classify(text: str) -> Intent:
    t = text.strip()
    if _GREETING.match(t):
        return Intent("greeting")
    if _THANKS.match(t):
        return Intent("thanks")
    return Intent("kb_question", security_incident=bool(_SECURITY.search(t)), p1=bool(_P1.search(t)))
