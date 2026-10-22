"""Spot clipboard text that shouldn't travel or be kept without a second look.

Errs on the side of asking: a false alarm costs one tap, a leaked password
costs much more. The same checks run in the browser (lib.js
``looksSensitive``) so the question comes before anything is sent.
"""

from __future__ import annotations

import re

_CARD = re.compile(r"(?:\d[ -]?){12,18}\d")
_CODE = re.compile(r"\d{4,8}")
_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_TOKENS = re.compile(
    r"""
      gh[pousr]_[A-Za-z0-9]{30,}            # GitHub
    | github_pat_[A-Za-z0-9_]{30,}
    | (?:sk|pk|rk)-[A-Za-z0-9_-]{20,}        # OpenAI, Stripe and friends
    | (?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{16,}
    | AKIA[0-9A-Z]{16}                       # AWS access key
    | xox[abposr]-[A-Za-z0-9-]{10,}          # Slack
    | AIza[0-9A-Za-z_-]{35}                  # Google API key
    | eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}  # JWT
    """,
    re.VERBOSE,
)
_LABELLED = re.compile(
    r"(?i)\b(?:password|passwd|pwd|passcode|pin|secret|api[ _-]?key|token)\s*[:=]\s*\S+"
)


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def _password_like(word: str) -> bool:
    """One 8 to 64 character word mixing letters, digits and symbols, like a password."""
    if not 8 <= len(word) <= 64 or any(ch.isspace() for ch in word) or "://" in word:
        return False
    kinds = sum(
        (
            any(ch.islower() for ch in word),
            any(ch.isupper() for ch in word),
            any(ch.isdigit() for ch in word),
            any(not ch.isalnum() for ch in word),
        )
    )
    return kinds >= 4 or (kinds == 3 and any(not ch.isalnum() for ch in word) and len(word) >= 10)


def looks_sensitive(text: str) -> str | None:
    """Why ``text`` looks private ("card", "code", "key", "token", "password"), or None."""
    value = text.strip()
    if not value:
        return None
    if _KEY.search(value):
        return "key"
    if len(value) > 20_000:  # a document, not a secret
        return None
    if _TOKENS.search(value):
        return "token"
    if _LABELLED.search(value):
        return "password"
    if _CARD.fullmatch(value):
        digits = re.sub(r"\D", "", value)
        if 13 <= len(digits) <= 19 and _luhn(digits):
            return "card"
        return None
    if _CODE.fullmatch(value) and not (len(value) == 4 and value.startswith(("19", "20"))):
        return "code"
    if _password_like(value):
        return "password"
    return None


REASONS = {
    "card": "a card number",
    "code": "a one-time code",
    "key": "a private key",
    "token": "an access token or API key",
    "password": "a password",
}
