"""
ISO 8583 dialect profiles.

Every acquirer speaks its own variant of ISO 8583, and the differences are not
cosmetic: a field declared four digits long and sent as twelve does not produce
a clean error, it shifts every byte after it and makes the whole message
unreadable. Guessing here is not a small risk.

The defaults in this module are not guesses. Each length below was checked
against four independent sources that agree:

  * jPOS packagers ``base1.xml`` and ``visapack.xml`` — Visa BASE I;
  * jPOS packagers ``europay.xml``, ``iso87ascii-binary-bitmap.xml``,
    ``base24.xml`` — other dialects, which agree on the fields that matter here;
  * the ISO 8583:1987 data element table;
  * Worldpay's publicly published ISO 8583 Reference Guide — a real acquirer,
    which also marks which fields are mandatory.

Copies of the packager definitions are vendored in ``docs/reference/`` so the
comparison can be repeated rather than taken on trust.

Two corrections came out of that check, and both would have been fatal:

  * **DE12** was being sent as a twelve-digit date-and-time. Every source says
    n6 — the *time* alone. The date belongs in **DE13** (n4), which was not
    being sent at all.
  * **DE22** was being sent as twelve characters, the Mastercard-style
    point-of-service *data code*. Visa expects n4 and the ISO 8583:1987
    dialects expect n3, for the point-of-service *entry mode*.

Three mandatory fields were missing outright: **DE7** (transmission date and
time), **DE13**, and **DE18** (merchant category code). DE7 matters beyond
being mandatory — the third component of DE90 is the *original* DE7 value, so
reversals were referencing a field that had never been sent, and no acquirer
could have matched them.

Nothing here mutates libfin's own configuration: these profiles derive from it
and are passed per call, so the MCI IPM parsers that share that config are
unaffected.
"""

import copy
import logging
from typing import Any, Dict

from libfin.config import config as _libfin_config

LOGGER = logging.getLogger(__name__)

#: Fields absent from libfin's configuration, added by every profile.
#: Formats from the sources above; all three are mandatory in an authorisation.
_MISSING_FIELDS: Dict[str, Dict[str, Any]] = {
    # n10, MMDDhhmmss. Mandatory, and referenced by DE90 in every reversal.
    "7": {
        "field_name": "Transmission date and time",
        "field_type": "FIXED",
        "field_length": 10,
        "field_python_type": "datetime",
        "field_date_format": "%m%d%H%M%S",
    },
    # n4, MMDD. The date half of what was wrongly packed into DE12.
    "13": {
        "field_name": "Date, local transaction",
        "field_type": "FIXED",
        "field_length": 4,
        "field_python_type": "datetime",
        "field_date_format": "%m%d",
    },
    # n4. The merchant category code; 6051 is the usual quasi-cash/crypto code,
    # but the acquirer assigns it — do not assume.
    "18": {
        "field_name": "Merchant type",
        "field_type": "FIXED",
        "field_length": 4,
    },
}

#: DE12 is the local transaction *time* only, n6, in every source consulted.
_DE12_TIME_ONLY: Dict[str, Any] = {
    "field_name": "Time, local transaction",
    "field_type": "FIXED",
    "field_length": 6,
    "field_python_type": "datetime",
    "field_date_format": "%H%M%S",
}


#: DE25, point of service condition code. Optional in the field table, but it
#: is what identifies a transaction as electronic commerce — and that
#: identification drives interchange rate, chargeback liability, and whether
#: some issuers accept a card-not-present transaction at all. Omitting it does
#: not fail loudly; it just prices and protects the transaction as something
#: other than what it is.
#:
#:   01  Customer not present
#:   08  Mail/telephone order
#:   59  Electronic commerce transaction    <- a web checkout
POS_CONDITION_ECOMMERCE = "59"

_POS_CONDITION_FIELD: Dict[str, Any] = {
    "field_name": "Point of service condition code",
    "field_type": "FIXED",
    "field_length": 2,
}


def _pos_entry_mode(length: int) -> Dict[str, Any]:
    return {
        "field_name": "Point of service entry mode",
        "field_type": "FIXED",
        "field_length": length,
    }


#: Which message types carry which step, per Worldpay's transaction type table:
#:
#:     00  0100/0110  0120/0130  0420/0430   POS Preauthorized Request
#:     00  0200/0210  0220/0230  0420/0430   POS Direct Debit, Credit Purchase
#:
#: The completion message differs by flow and is easy to get wrong: 0120 is the
#: *authorisation* advice that completes a preauthorisation, while 0220 is the
#: advice for a 0200 financial transaction. They are not interchangeable — an
#: acquirer answers the wrong one with 12 (Invalid Transaction), which until
#: recently this gateway would have recorded as success.
#:
#: Reversal is 0420/0430 in both flows. 0400/0410 exists but Worldpay lists it
#: only for Credit Purchase; it is a *request* the host may refuse, where 0420
#: is an *advice* the host records. For undoing a transaction that never
#: completed, the advice is the right semantics: we are not asking permission
#: to return money we should not have taken.
MESSAGE_TYPES = {
    "auth_capture": {
        "authorize": "0100",
        "capture": "0120",     # authorisation advice completes the preauth
        "reversal": "0420",
    },
    "purchase": {
        "authorize": "0200",
        "capture": "0220",     # financial transaction advice
        "reversal": "0420",
    },
}


def message_type(capture_mode: str, step: str) -> str:
    """The MTI for one step of one flow."""
    try:
        return MESSAGE_TYPES[capture_mode][step]
    except KeyError:
        raise ValueError(
            f"No message type for step {step!r} in {capture_mode!r} flow. "
            f"Known flows: {', '.join(MESSAGE_TYPES)}."
        )


class Dialect:
    """One acquirer's variant: field lengths, encoding, bitmap form."""

    def __init__(
        self,
        name: str,
        pos_entry_length: int,
        encoding: str,
        hex_bitmap: bool,
        default_pos_entry: str,
        source: str,
    ):
        self.name = name
        self.pos_entry_length = pos_entry_length
        self.encoding = encoding
        self.hex_bitmap = hex_bitmap
        self.default_pos_entry = default_pos_entry
        #: Where these values were checked. Kept so the next person can repeat
        #: the check rather than trusting this file.
        self.source = source

    def bit_config(self) -> Dict[str, Any]:
        """The field table to hand to ``iso8583.dumps`` and ``loads``."""
        cfg = copy.deepcopy(_libfin_config["bit_config"])
        cfg.update(copy.deepcopy(_MISSING_FIELDS))
        cfg["12"] = copy.deepcopy(_DE12_TIME_ONLY)
        cfg["22"] = _pos_entry_mode(self.pos_entry_length)
        cfg["25"] = copy.deepcopy(_POS_CONDITION_FIELD)
        return cfg

    def __repr__(self) -> str:
        return (
            f"<Dialect {self.name}: DE22=n{self.pos_entry_length}, "
            f"encoding={self.encoding}, hex_bitmap={self.hex_bitmap}>"
        )


DIALECTS: Dict[str, Dialect] = {
    # Visa BASE I. EBCDIC because base1.xml packs every character field with
    # IFE_CHAR, whose jPOS implementation uses EbcdicInterpreter.
    "visa_base1": Dialect(
        name="visa_base1",
        pos_entry_length=4,
        encoding="cp500",
        hex_bitmap=False,
        # 09 = PAN entry via electronic commerce, 00 = no PIN capability.
        # Not 01: manual entry means a person keyed the card into a terminal,
        # which is a different transaction type with different liability and a
        # different rate from a web checkout.
        default_pos_entry="0900",
        source="jPOS base1.xml, visapack.xml; Worldpay Field 022 value table",
    ),
    # The 1987 baseline, and what most acquirer host specifications start from.
    "iso87": Dialect(
        name="iso87",
        pos_entry_length=3,
        encoding="latin_1",
        hex_bitmap=False,
        # 09 = electronic commerce, 0 = no PIN capability.
        default_pos_entry="090",
        source="jPOS europay.xml, iso87ascii-binary-bitmap.xml; ISO 8583:1987",
    ),
    # Worldpay's published guide: n4 entry mode, ASCII on the wire.
    "worldpay": Dialect(
        name="worldpay",
        pos_entry_length=4,
        encoding="latin_1",
        hex_bitmap=False,
        default_pos_entry="0900",
        source="Worldpay ISO 8583 Reference Guide V2.57, Field 022 value table",
    ),
}

#: Fields an authorisation must carry, per Worldpay's guide. Checked at build
#: time so a message is refused here rather than by the acquirer — a rejection
#: from them costs a round trip and tells you far less about why.
MANDATORY_AUTHORIZATION_FIELDS = ("DE4", "DE7", "DE11", "DE12", "DE13", "DE18", "DE41")


def card_acceptor_name_location(
    name: str, city: str, state: str = "", country: str = ""
) -> str:
    """Build DE43 from its parts, in the positions the field actually has.

    ans40, and strictly positional — Worldpay's format 3:

        1-23   card acceptor name
        24-36  card acceptor city
        37-38  card acceptor state
        39-40  card acceptor alphabetic country

    Built rather than configured as one opaque forty-character string. A blob
    is silently wrong when it is off by a character: the acquirer reads the city
    out of the middle of the merchant's name and nothing rejects it. The city
    appears on the cardholder's statement, so the failure surfaces as a support
    call rather than an error.

    Values are truncated to their fields rather than allowed to overflow into
    the next one.
    """
    return (
        f"{name[:23]:<23}"
        f"{city[:13]:<13}"
        f"{state[:2]:<2}"
        f"{country[:2]:<2}"
    )


def get_dialect(name: str) -> Dialect:
    try:
        return DIALECTS[name]
    except KeyError:
        raise ValueError(
            f"Unknown ISO 8583 dialect {name!r}. Available: {', '.join(sorted(DIALECTS))}. "
            "Ask your acquirer which variant their host speaks; the wrong one is "
            "rejected outright rather than degraded."
        )


def missing_mandatory(message: Dict[str, Any]) -> list:
    """Which mandatory authorisation fields this message lacks."""
    return [f for f in MANDATORY_AUTHORIZATION_FIELDS if not message.get(f)]
