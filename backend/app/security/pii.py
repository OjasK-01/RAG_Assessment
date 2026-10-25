"""Mask personal data before it is logged or sent to the LLM provider.

Structured identifiers (email, phone, Aadhaar, PAN, card, employee id, "password is ...") are regexes.
Names have no fixed shape, so they are found two ways:
  1. context cues: "I'm X", "my name is X", "Mr/Ms/Dr X", "my manager X", "on behalf of X", sign-offs
     ("Regards, X")
  2. capitalised word pairs/triples ("Rahul Mehta", "Anand Kumar Sharma") where no word is part of
     the KB's own vocabulary or a list of common product, place and sentence words, so "Company
     Portal", "Microsoft Teams" or "Windows Defender" stay readable for retrieval.
A lowercase name mentioned in passing ("ask priya") is still missed; production would add an NER model
(e.g. Presidio) behind the same mask() interface."""
import re
from functools import lru_cache

# Order matters: secrets and long digit runs are masked before phone numbers can claim them.
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")),
    ("SECRET", re.compile(r"(?i)\b(?:password|passcode|passwd|pwd|otp|pin)\b\s*(?:is|was|:|=)\s*\S+")),
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("AADHAAR", re.compile(r"\b\d{4}[ -]\d{4}[ -]\d{4}\b")),
    ("PAN", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    ("PHONE", re.compile(r"(?:\+?\d{1,3}[\s-]?)?\(?\b[6-9]\d{2}\)?[\s-]?\d{3}[\s-]?\d{4}\b")),
    ("EMPLOYEE_ID", re.compile(r"\bVX\d{5}\b", re.I)),
]

_NAME = r"(?P<name>[A-Z][a-z]+(?:[ -][A-Z][a-z]+){0,2})"
_NAME_CUES = [re.compile(p) for p in (
    r"(?i:\b(?:I'm|I am|my name is|this is|myself|mera naam|naam hai))\s+" + _NAME,
    r"(?i:\b(?:mr|mrs|ms|miss|dr|shri|smt|sri)\.?)\s+" + _NAME,
    r"(?i:\b(?:my|our)\s+(?:manager|colleague|teammate|team lead|lead|boss|supervisor|reporting manager"
    r"|co-?worker|director|hod))\s*,?\s+" + _NAME,
    r"(?i:\b(?:on behalf of|for user|user|employee|staff member|cc|assigned to|contact person))\s*:?\s+" + _NAME,
    r"(?i:\b(?:regards|thanks|thank you|cheers|best|sincerely|warm regards))\s*,?\s*\n?\s*-?\s*" + _NAME + r"\s*$",
)]
_CAP_RUN = re.compile(r"\b[A-Z][a-z]+(?: [A-Z][a-z]+){1,2}\b")

# Capitalised words that are not names. The KB's own vocabulary is added at runtime.
_COMMON = set("""
hi hello hey dear team sir madam thanks thank regards please can could would will how what when where why who
which is are was do does did my our the a an it this that these those there i im we you your yes no ok okay
monday tuesday wednesday thursday friday saturday sunday january february march april may june july august
september october november december today tomorrow yesterday morning evening
india gurugram gurgaon delhi noida mumbai bengaluru bangalore pune chennai hyderabad kolkata ahmedabad jaipur
microsoft google apple android samsung dell lenovo hp asus acer excel word powerpoint onedrive sharepoint
outlook teams zoom chrome edge firefox safari slack jira confluence adobe acrobat office mac macbook iphone
ipad ios windows linux ubuntu bluetooth wifi vpn sap tally autocad power desktop laptop mobile mail app
portal company service desk help support security network ops admin manager it
""".split())


@lru_cache(maxsize=1)
def _known_words() -> frozenset[str]:
    words = set(_COMMON)
    try:
        from app.config import settings
        for path in settings.kb_dir.glob("*.md"):
            words |= {w.lower() for w in re.findall(r"[A-Za-z]+", path.read_text(encoding="utf-8"))}
    except Exception:
        pass
    return frozenset(words)


def _mask_names(text: str) -> tuple[str, int]:
    known = _known_words()
    count = 0

    def cue(m: re.Match) -> str:
        nonlocal count
        name = m.group("name")
        if all(w.lower() in known for w in re.split(r"[ -]", name)):  # "this is Outlook"
            return m.group(0)
        count += 1
        start, end = m.start("name") - m.start(), m.end("name") - m.start()
        return m.group(0)[:start] + "[NAME]" + m.group(0)[end:]

    def run(m: re.Match) -> str:
        nonlocal count
        if any(w.lower() in known for w in m.group(0).split()):
            return m.group(0)
        count += 1
        return "[NAME]"

    for rx in _NAME_CUES:
        text = rx.sub(cue, text)
    text = _CAP_RUN.sub(run, text)
    return text, count


PLACEHOLDER = re.compile(r"\[(?:EMAIL|SECRET|CARD|AADHAAR|PAN|PHONE|EMPLOYEE_ID|NAME)\]")


def mask(text: str) -> tuple[str, list[str]]:
    """Return (masked_text, sorted list of PII types found)."""
    found = set()
    for label, pattern in _PATTERNS:
        text, n = pattern.subn(f"[{label}]", text)
        if n:
            found.add(label)
    text, n = _mask_names(text)
    if n:
        found.add("NAME")
    return text, sorted(found)


def contains_pii(text: str) -> bool:
    # Names are not checked here: answers legitimately contain capitalised KB terms.
    return any(p.search(text) for _, p in _PATTERNS) or bool(PLACEHOLDER.search(text))
