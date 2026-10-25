"""Text normalisation applied before any scanning, so obfuscation tricks don't slip past regexes."""
import re
import unicodedata

# Zero-width and bidi-control characters used to hide or reorder text.
_INVISIBLE = re.compile(r"[​-‏‪-‮⁠-⁤﻿]")
# All C0/C1 control chars except tab and newline.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)  # folds full-width / stylised letters to ASCII
    text = _INVISIBLE.sub("", text)
    text = _CONTROL.sub(" ", text)
    return re.sub(r"[ \t]+", " ", text).strip()
