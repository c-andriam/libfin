"""
ISO 8583 response codes (DE39).

Which codes mean "yes" is not a matter of taste, and getting it wrong is silent.
This gateway previously treated 12 and 21 as successful reversals on the
reasoning that they might mean "nothing to reverse". Worldpay's published
reference guide is unambiguous:

    00  Approve   Transaction Approved
    12  Decline   Invalid Transaction
    21  Decline   Reversal Unsuccessful

So a refused refund was being recorded as REVERSED. No alert fired, no
transaction was flagged, and the cardholder was simply never paid back — the
exact shape of loss this system is built to prevent, produced by a guess about
three characters.

The classification below is transcribed from that guide. Anything not listed as
an approval is treated as a refusal, which errs toward telling a human rather
than assuming a customer was made whole.
"""

import logging
from typing import Optional

LOGGER = logging.getLogger(__name__)

#: Codes that mean the request succeeded. Source: Worldpay ISO 8583 Reference
#: Guide V2.57, response code table.
APPROVAL_CODES = frozenset(
    {
        "00",  # Transaction approved
        "08",  # Honour with identification
        "09",  # Approved, special conditions
        "10",  # Approved for partial amount
        "11",  # VIP approval
        "20",  # Approved with overdraft
    }
)

#: The only approval a *reversal* or *void* may legitimately return. The partial
#: and conditional approvals above describe an authorisation being granted; none
#: of them describes money being given back, and accepting one would again mean
#: recording a refund that did not happen.
REVERSAL_APPROVAL_CODES = frozenset({"00"})

#: Refusals worth naming, because the operator response differs.
DECLINE_MEANINGS = {
    "01": "refer to card issuer",
    "03": "invalid merchant id — check ACQUIRER_MERCHANT_ID",
    "05": "generic decline",
    "12": "invalid transaction",
    "13": "invalid amount",
    "14": "invalid account number",
    "21": "reversal unsuccessful — the money was NOT returned",
    "30": "message format error — check the ISO 8583 dialect and field lengths",
    "51": "insufficient funds",
    "54": "expired card",
    "91": "issuer unavailable",
    "96": "system malfunction",
}

#: A refusal here means the gateway itself is misconfigured, not that the
#: cardholder's bank said no. Worth separating: one is business as usual, the
#: other is every transaction failing until someone changes a setting.
CONFIGURATION_DECLINES = frozenset({"03", "30"})


def is_approved(action_code: Optional[str]) -> bool:
    """Whether an authorisation response is an approval."""
    return (action_code or "").strip() in APPROVAL_CODES


def is_reversal_approved(action_code: Optional[str]) -> bool:
    """Whether a reversal or void actually returned the money.

    Deliberately narrower than :func:`is_approved`. Treating anything else as
    success means recording a refund that never happened.
    """
    return (action_code or "").strip() in REVERSAL_APPROVAL_CODES


def indicates_misconfiguration(action_code: Optional[str]) -> bool:
    """Whether this refusal points at our setup rather than the cardholder's bank."""
    return (action_code or "").strip() in CONFIGURATION_DECLINES


def describe(action_code: Optional[str]) -> str:
    """A short, human-readable reading of a response code."""
    code = (action_code or "").strip()
    if not code:
        return "no response code"
    if code in APPROVAL_CODES:
        return f"{code} (approved)"
    meaning = DECLINE_MEANINGS.get(code)
    return f"{code} ({meaning})" if meaning else f"{code} (declined)"
