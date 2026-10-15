"""The single PII-masking rule, shared by the executor and the schema tools.

Masking fires in three ways:

* strong tokens — matched as a *substring* of the lower-cased column name, so
  mashed-together names like ``admemail1`` are caught: email, phone, password,
  aadhaar, ssn.
* weak tokens — matched only as a *whole word* (the name split on
  non-alphanumerics), so they do not fire on innocent names: pan (not
  "company"/"pantry"), mobile, token (not "tokens_used"), secret.
* value-based — any cell whose value looks like an email address is masked
  regardless of its column name.

This lives in one place so the executor (row output) and the schema tools
(sample values) can never drift apart.
"""

from __future__ import annotations

import re

MASK = "***"

# Strong: substring match against the whole lower-cased column name.
_STRONG_TOKENS = ("email", "phone", "password", "aadhaar", "ssn")
# Weak: whole-word match only (too short/ambiguous to match as substrings).
_WEAK_TOKENS = frozenset({"pan", "mobile", "token", "secret"})
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")

# Deliberately simple: local@domain.tld with no spaces. Good enough to catch an
# email sitting in a blandly-named column without masking ordinary text.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_pii_column(name: str) -> bool:
    """True if a column's name marks it as PII (strong substring or weak word)."""
    low = name.lower()
    if any(token in low for token in _STRONG_TOKENS):
        return True
    words = [word for word in _TOKEN_SPLIT.split(low) if word]
    return any(word in _WEAK_TOKENS for word in words)


def looks_like_email(value: object) -> bool:
    """True if a cell value looks like an email address (value-based masking)."""
    return isinstance(value, str) and _EMAIL_RE.match(value.strip()) is not None
