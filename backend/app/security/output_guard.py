"""Deterministic checks on the model's answer before it reaches an employee.

Anything that fails here is replaced by the safe abstain response; the model never gets the final say."""
import re
from dataclasses import dataclass, field

from app.security import pii


def _prompt_parts():
    from app.prompts import CANARY, SYSTEM_PROMPT  # lazy: prompts imports settings
    return CANARY, SYSTEM_PROMPT

# Dangerous advice. Each match is ignored if a negation ("never", "do not"...) appears just before it,
# so faithful KB wording such as "Never disable antivirus" passes.
_UNSAFE = {
    "disable_security_software": re.compile(
        r"\b(?:disable|turn(?:ing)? off|switch(?:ing)? off|uninstall|deactivate|stop|pause)\b.{0,30}?"
        r"\b(?:anti-?virus|defender|endpoint protection|firewall|edr|security software)\b", re.I),
    "share_credentials": re.compile(
        r"\b(?:share|send|tell|give|provide|email|read out)\b.{0,30}?"
        r"\b(?:password|otp|one-time (?:code|password)|mfa code|verification code)\b", re.I),
    "local_admin_install": re.compile(r"\b(?:install|run)\b.{0,25}?\bas (?:a )?local admin", re.I),
    "deprecated_vpn": re.compile(
        r"\b(?:download|install|use|connect (?:with|via|using))\b.{0,25}?\bsecurelink\b"
        r"|\bvpn\.vectralogistics\.example\b", re.I),
}
_NEGATION = re.compile(
    r"\b(?:never|not|don't|do not|must not|mustn't|avoid|no longer|cannot|can't|won't)\b[^.!?\n]{0,40}$", re.I)

# Concrete facts that must be copied from the KB, never invented.
_HOST = re.compile(r"\b(?:https?://)?((?:[a-z0-9-]+\.)+(?:example|com|in|net|org|io|local))\b", re.I)
_CODE = re.compile(r"\b[A-Z]{1,4}-\d{2,5}\b|\b0x[0-9a-f]{4,}\b", re.I)
_NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w])")
_ANY_DIGITS = re.compile(r"\d+(?:\.\d+)?")  # source side: also "24x7", "14+", "3.x", "5GHz"
_NUMBER_WORDS = {w: str(i) for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty".split())} | {"thirty": "30", "forty": "40", "fifty": "50",
                                                            "sixty": "60", "hundred": "100"}
_LEAK_NGRAM = 8  # this many consecutive words of the system prompt in an answer = prompt leak
_LIST_MARKER = re.compile(r"(?m)^\s*\d+[.)]\s+")
_DOC_ID = re.compile(r"\bKB-\d{3}\b")


@dataclass
class GuardResult:
    violations: list[str] = field(default_factory=list)
    answer: str = ""
    pii_scrubbed: bool = False

    @property
    def ok(self) -> bool:
        return not self.violations


def _unsafe(answer: str) -> list[str]:
    hits = []
    for name, pattern in _UNSAFE.items():
        for m in pattern.finditer(answer):
            if not _NEGATION.search(answer[max(0, m.start() - 60): m.start()]):
                hits.append(f"unsafe:{name}")
                break
    return hits


def _source_numbers(text: str) -> set[str]:
    words = re.findall(r"[a-z]+", text)
    return set(_ANY_DIGITS.findall(text)) | {_NUMBER_WORDS[w] for w in words if w in _NUMBER_WORDS}


def _ungrounded(answer: str, source: str, question: str = "") -> list[str]:
    """Hosts, codes and numbers in the answer must come from the cited articles. Numbers the employee
    wrote themselves ("dock 3") may be repeated back."""
    src = source.lower()
    src_numbers = _source_numbers(src) | _source_numbers(question.lower())
    issues = []
    for host in _HOST.findall(answer):
        if host.lower() not in src:
            issues.append(f"ungrounded_host:{host}")
    for code in _CODE.findall(answer):
        if code.lower() not in src and not _DOC_ID.fullmatch(code.upper()):
            issues.append(f"ungrounded_code:{code}")
    for num in _NUMBER.findall(_LIST_MARKER.sub("", answer)):
        if num not in src_numbers:
            issues.append(f"ungrounded_number:{num}")
    return issues


def _scrub_pii(answer: str) -> str:
    masked, _ = pii.mask(answer)
    text = pii.PLACEHOLDER.sub("", masked)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"\s+([,.!?])", r"\1", text).strip(" ,")


def _prompt_leak(answer: str) -> list[str]:
    canary, system_prompt = _prompt_parts()
    if canary.lower() in answer.lower():
        return ["prompt_leak:canary"]
    # Example phrasings in the prompt (quoted or in brackets) are things good answers may legitimately say.
    instructions = re.sub(r'\([^)]*\)|"[^"]*"', " ", system_prompt.lower())
    words = re.findall(r"\w+", instructions)
    grams = {" ".join(words[i:i + _LEAK_NGRAM]) for i in range(len(words) - _LEAK_NGRAM + 1)}
    ans = re.findall(r"\w+", answer.lower())
    if any(" ".join(ans[i:i + _LEAK_NGRAM]) in grams for i in range(len(ans) - _LEAK_NGRAM + 1)):
        return ["prompt_leak:verbatim"]
    return []


def check(answer: str, cited_text: str, indexed_doc_ids: set[str], question: str = "") -> GuardResult:
    """cited_text: full text of the articles the answer cites (plus any server-added escalation text).
    question: the masked question (numbers in it may be echoed)."""
    violations = _unsafe(answer) + _ungrounded(answer, cited_text, question) + _prompt_leak(answer)
    for doc_id in set(_DOC_ID.findall(answer)) - indexed_doc_ids:
        violations.append(f"mentions_excluded_doc:{doc_id}")

    # PII echo is scrubbed rather than blocked: the rest of the answer is still useful.
    if pii.contains_pii(answer):
        return GuardResult(violations, _scrub_pii(answer), pii_scrubbed=True)
    return GuardResult(violations, answer)
