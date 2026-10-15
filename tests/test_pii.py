"""Unit tests for the shared PII rule (queryguard.pii).

The rule is also exercised end to end through the executor and the schema tools
(see tests/test_executor.py and tests/test_schema_tools.py); these are fast,
database-free checks of the rule itself, pinning the exact behaviour the Phase
4A.1 PII-gap work specifies: strong tokens as substrings (so ``admemail1`` is
caught), weak tokens as whole words only, and value-based email masking.
"""

from __future__ import annotations

import pytest

from queryguard.pii import MASK, is_pii_column, looks_like_email


@pytest.mark.parametrize(
    "name",
    [
        # Strong tokens: matched as a substring of the lower-cased name.
        "email",
        "Email",
        "customer_email",
        "admemail1",  # the real BIRD gap this rule closes
        "AdmEmail1",
        "admemail2",
        "phone",
        "phone_number",
        "telephone",  # contains the strong token "phone"
        "password",
        "passwordhash",
        "aadhaar",
        "ssn",
        "user_ssn",
        # Weak tokens: matched only as a whole word.
        "pan",
        "pan_card",
        "mobile",
        "token",
        "api_token",
        "secret",
        "client_secret",
    ],
)
def test_is_pii_column_masks(name: str) -> None:
    assert is_pii_column(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "company",  # not the whole word "pan"
        "pantry",
        "tokens_used",  # "tokens" is not the whole word "token"
        "panel",
        "japan",
        "spanish",
        "mobility",  # not the whole word "mobile"
        "name",
        "city",
        "description",
        "notes",
    ],
)
def test_is_pii_column_allows(name: str) -> None:
    assert is_pii_column(name) is False


@pytest.mark.parametrize(
    "value",
    ["a@b.com", "alice@example.com", "  bob@example.org  "],
)
def test_looks_like_email_true(value: str) -> None:
    assert looks_like_email(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "plain text",
        "no-at-sign.com",
        "a@b",  # no dot in the domain
        "@b.com",  # empty local part
        "a@.com",  # empty domain label before the dot
        "two@@at.com",  # two @ signs
        None,
        123,
    ],
)
def test_looks_like_email_false(value: object) -> None:
    assert looks_like_email(value) is False


def test_mask_token() -> None:
    assert MASK == "***"
