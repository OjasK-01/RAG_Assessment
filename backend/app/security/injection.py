"""Heuristic prompt-injection detector, used on both user questions and KB documents at ingestion.
It is one layer only: the prompt treats all input as data, the model has no tools, and the output
guard checks the answer regardless of what got through here (including a canary that catches
system-prompt leaks)."""
import base64
import binascii
import re

from app.security.text import normalize

_RULE_SOURCES = {
    "override_instructions":
        r"\b(?:ignore|disregard|forget|override|bypass|skip)\b.{0,40}"
        r"\b(?:instructions?|rules?|guidelines?|context|(?:system|previous|prior|above|earlier|initial|your) prompts?)\b",
    "new_instructions":
        r"\b(?:new|updated|real|actual|hidden)\s+(?:instructions?|rules?|system\s+prompt)\b",
    "prompt_extraction":
        r"\b(?:reveal|print|show|repeat|output|display|leak|tell me)\b.{0,40}\b(?:system|developer|initial|hidden)\s*(?:prompt|message|instructions?)\b",
    "role_hijack":
        r"\b(?:you are now|from now on,? you|pretend (?:to be|you are)|roleplay as|you must now"
        r"|act as (?:if you|an? (?:unrestricted|unfiltered|different|new)))\b",
    "jailbreak_terms":
        r"\b(?:jailbreak|DAN mode|developer mode|god mode|do anything now)\b",
    "addressed_to_ai":
        r"\b(?:note|message|instructions?|attention)\s+(?:for|to)\s+(?:all\s+)?(?:ai|llm|assistants?|chatbots?|language models?)\b"
        r"|\b(?:ai|llm|language model|chatbot)s?\s+(?:assistants?\s+)?reading\s+this\b",
    "tag_smuggling":
        r"</?\s*(?:system|assistant|user|kb|kb_article|question|conversation|employee|reply|instructions?)\b[^>]*>"
        r"|\[/?(?:INST|SYS)\]|<\|im_(?:start|end)\|>",
    "always_tell_user":
        r"\balways (?:tell|instruct|advise|say to) (?:the )?users?\b",
    "output_control":
        r"\b(?:respond|reply|answer) (?:(?:only|just) with|with (?:exactly|only|just))\b"
        r"|\b(?:start|begin|end) (?:your|the) (?:answer|reply|response) with\b"
        r"|\bsay (?:exactly|verbatim|only)\b",
    "rules_probe":
        r"\bwhat (?:are|were) your (?:instructions|rules|guidelines|system prompt)\b"
        r"|\b(?:everything|all the text|the text|the words) (?:above|before) (?:this|my message)\b"
        r"|\b(?:print|repeat|show|output)\b.{0,20}\b(?:above|previous) (?:text|message|content)\b",
    "hinglish_override":
        r"\b(?:pichle|pehle (?:ke|wale)|saare|sab|upar (?:ke|wale))\s+(?:instructions?|rules?|niyam)\b.{0,30}"
        r"\b(?:bhool|ignore|chhod|hata)\w*",
}
_BASE64 = re.compile(r"[A-Za-z0-9+/]{24,}={0,2}")
_RULES = {name: re.compile(src, re.I | re.S) for name, src in _RULE_SOURCES.items()}


def _decoded_payloads(text: str) -> list[str]:
    """Base64 blobs that decode to readable text ("aWdub3JlIGFsbCBwcmV2aW91cy..." hides an attack)."""
    out = []
    for blob in _BASE64.findall(text):
        try:
            decoded = base64.b64decode(blob + "=" * (-len(blob) % 4), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        if decoded.isprintable():
            out.append(decoded)
    return out


def scan(text: str) -> list[str]:
    """Return the names of the injection rules the text triggers (empty list = clean)."""
    t = normalize(text)
    hits = [name for name, rule in _RULES.items() if rule.search(t)]
    for decoded in _decoded_payloads(t):
        if inner := [name for name, rule in _RULES.items() if rule.search(normalize(decoded))]:
            hits.append("encoded_payload")
            hits += [h for h in inner if h not in hits]
    return hits
