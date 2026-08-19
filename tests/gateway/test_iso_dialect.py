"""
Field definitions checked against published references.

Every length asserted here was verified against four independent sources that
agree: the jPOS packagers for Visa BASE I and for the ISO 8583:1987 dialects,
the ISO 8583:1987 data element table, and Worldpay's publicly published
reference guide. Copies of the packagers are vendored in ``docs/reference/`` so
the comparison can be repeated rather than believed.

These are not tests of our preferences. They are tests that our messages match
what acquirer hosts actually parse — and on a fixed-length field, a wrong length
does not fail cleanly, it shifts every byte after it.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from gateway.iso_dialect import (
    DIALECTS,
    MANDATORY_AUTHORIZATION_FIELDS,
    get_dialect,
    missing_mandatory,
)

#: What the sources say. The comment on each line names where it was checked.
REFERENCE_LENGTHS = {
    "3": 6,    # processing code — all sources
    "4": 12,   # amount — all sources
    "7": 10,   # transmission date and time, MMDDhhmmss — Worldpay (mandatory)
    "11": 6,   # STAN — all sources
    "12": 6,   # local transaction TIME only — all four sources agree
    "13": 4,   # local transaction DATE — Worldpay (mandatory), jPOS, ISO 1987
    "14": 4,   # expiration — all sources
    "18": 4,   # merchant category code — Worldpay (mandatory)
    "37": 12,  # retrieval reference number — all sources
    "41": 8,   # card acceptor terminal — jPOS base1
    "42": 15,  # card acceptor id — jPOS base1
    "49": 3,   # currency — all sources
    "90": 42,  # original data elements — jPOS base1, Worldpay
}


@pytest.mark.parametrize("dialect_name", sorted(DIALECTS))
@pytest.mark.parametrize("field,expected", sorted(REFERENCE_LENGTHS.items()))
def test_field_lengths_match_the_published_references(dialect_name, field, expected):
    """Every profile agrees with the references on the shared fields."""
    config = get_dialect(dialect_name).bit_config()
    assert field in config, f"DE{field} is absent from the {dialect_name} field table"

    # DE22 is the one field the dialects legitimately disagree on.
    if field == "22":
        return
    assert config[field]["field_length"] == expected, (
        f"DE{field} is {config[field]['field_length']} in {dialect_name}, "
        f"references say {expected}"
    )


def test_de12_carries_the_time_only_and_de13_the_date():
    """The correction that would have shifted every message.

    DE12 was being sent as a twelve-digit date-and-time — the 1993 form. Every
    source consulted says n6, the time alone, with the date in DE13.
    """
    config = get_dialect("iso87").bit_config()

    assert config["12"]["field_length"] == 6
    assert config["12"]["field_date_format"] == "%H%M%S"
    assert config["13"]["field_length"] == 4
    assert config["13"]["field_date_format"] == "%m%d"


def test_pos_entry_mode_length_follows_the_dialect():
    """DE22 is n4 for Visa and n3 for the 1987 dialects — never twelve.

    Twelve characters is the Mastercard point-of-service *data code*, a
    different field from the point-of-service *entry mode* this carries.
    """
    assert get_dialect("visa_base1").bit_config()["22"]["field_length"] == 4
    assert get_dialect("worldpay").bit_config()["22"]["field_length"] == 4
    assert get_dialect("iso87").bit_config()["22"]["field_length"] == 3

    for name in DIALECTS:
        length = get_dialect(name).bit_config()["22"]["field_length"]
        assert length in (3, 4), f"{name} declares DE22 as n{length}"


def test_visa_base1_is_ebcdic():
    """base1.xml packs character fields with IFE_CHAR, which is EBCDIC."""
    assert get_dialect("visa_base1").encoding == "cp500"
    assert get_dialect("iso87").encoding == "latin_1"


def test_libfin_defaults_are_left_alone():
    """Profiles derive from libfin's table; they must not mutate it.

    The MCI IPM parsers share that configuration, and they legitimately use the
    1993 lengths this module overrides for the acquirer link.
    """
    from libfin.config import config

    before = config["bit_config"]["12"]["field_length"]
    get_dialect("iso87").bit_config()
    get_dialect("visa_base1").bit_config()
    assert config["bit_config"]["12"]["field_length"] == before


def test_an_incomplete_authorisation_is_refused_before_it_is_sent():
    """A missing mandatory field costs a round trip and an opaque rejection."""
    complete = {f: "x" for f in MANDATORY_AUTHORIZATION_FIELDS}
    assert missing_mandatory(complete) == []

    without_de7 = dict(complete)
    del without_de7["DE7"]
    assert missing_mandatory(without_de7) == ["DE7"]


def test_an_authorisation_round_trips_in_every_dialect():
    """The whole message, encoded and parsed back, in each profile."""
    from libfin import iso8583

    sent_at = datetime(2026, 8, 18, 14, 30, 45, tzinfo=timezone.utc)

    for name, dialect in DIALECTS.items():
        config = dialect.bit_config()
        message = {
            "MTI": "0100",
            "DE2": "4111111111111111",
            "DE3": "000000",
            "DE4": 2500,
            "DE7": sent_at,
            "DE11": 123456,
            "DE12": sent_at,
            "DE13": sent_at,
            "DE14": "3012",
            "DE18": "6051",
            "DE22": dialect.default_pos_entry,
            "DE32": "12345678901",
            "DE37": "026230000001",
            "DE41": "TERM0001",
            "DE42": "MERCHANT0000001",
            "DE49": "840",
        }
        raw = iso8583.dumps(
            message, encoding=dialect.encoding, hex_bitmap=dialect.hex_bitmap, iso_config=config
        )
        back = iso8583.loads(
            raw, encoding=dialect.encoding, hex_bitmap=dialect.hex_bitmap, iso_config=config
        )

        assert back["DE2"] == "4111111111111111", f"{name}: PAN corrupted"
        assert back["DE4"] == 2500, f"{name}: amount corrupted"
        assert back["DE37"] == "026230000001", f"{name}: RRN corrupted"
        assert back["DE41"] == "TERM0001", f"{name}: terminal id corrupted"
        # The clearest sign the lengths line up: a field near the end survives.
        assert back["DE49"] == "840", f"{name}: fields after DE22 are shifted"


def test_original_data_elements_quote_the_transmission_timestamp():
    """DE90's third component is the original DE7, byte for byte.

    They are derived from the same instant rather than from two calls to the
    clock: an acquirer matches a reversal on exact equality, and a second's
    drift between them is enough to lose the match.
    """
    from gateway.acquirer import AcquirerService

    sent_at = datetime(2026, 8, 18, 14, 30, 45, tzinfo=timezone.utc)
    de90 = AcquirerService._original_data_elements("0100", "000123", sent_at)

    assert len(de90) == 42
    assert de90.isdigit()
    assert de90[:4] == "0100"
    assert de90[4:10] == "000123"
    assert de90[10:20] == sent_at.strftime("%m%d%H%M%S")


def test_every_dialect_names_where_it_was_checked():
    """A profile without a source is a guess wearing a profile's clothes."""
    for name, dialect in DIALECTS.items():
        assert dialect.source, f"{name} does not say where its values came from"


def test_amounts_are_unaffected_by_the_dialect():
    """Minor-unit conversion is arithmetic, not encoding."""
    from gateway.acquirer import AcquirerService

    assert AcquirerService._to_cents(Decimal("1.15")) == 115


# ---------------------------------------------------------------------------
# Response codes (DE39)
# ---------------------------------------------------------------------------


def test_only_approval_codes_count_as_approved():
    """Transcribed from Worldpay's response code table, not inferred."""
    from gateway.action_codes import is_approved

    for code in ("00", "08", "09", "10", "11", "20"):
        assert is_approved(code), f"{code} is listed as an approval"

    for code in ("01", "05", "12", "14", "21", "30", "51", "54", "91", "96"):
        assert not is_approved(code), f"{code} is listed as a decline"


def test_a_refused_reversal_is_never_recorded_as_a_refund():
    """The bug this module exists to prevent.

    The gateway treated 12 and 21 as successful reversals, reasoning they might
    mean "nothing to reverse". Worldpay's table is explicit: 12 is "Invalid
    Transaction" and 21 is "Reversal Unsuccessful" — both declines. A refused
    refund was therefore recorded as REVERSED, no alert fired, and the
    cardholder was simply never paid back.
    """
    from gateway.action_codes import is_reversal_approved

    assert is_reversal_approved("00")

    assert not is_reversal_approved("12"), "12 means Invalid Transaction"
    assert not is_reversal_approved("21"), "21 means Reversal Unsuccessful"

    # The conditional approvals describe an authorisation being granted, not
    # money being returned. Accepting one would recreate the same silent loss.
    for code in ("08", "09", "10", "11", "20"):
        assert not is_reversal_approved(code), (
            f"{code} approves an authorisation; it does not confirm a refund"
        )


def test_reversal_approval_is_narrower_than_authorisation_approval():
    """Stated as a property, so neither set can drift into the other."""
    from gateway.action_codes import APPROVAL_CODES, REVERSAL_APPROVAL_CODES

    assert REVERSAL_APPROVAL_CODES < APPROVAL_CODES


def test_configuration_declines_are_distinguished_from_issuer_declines():
    """One is business as usual; the other fails every transaction until fixed."""
    from gateway.action_codes import indicates_misconfiguration

    assert indicates_misconfiguration("03"), "invalid merchant id is our setting"
    assert indicates_misconfiguration("30"), "format error is our dialect"
    assert not indicates_misconfiguration("51"), "insufficient funds is the cardholder"
    assert not indicates_misconfiguration("05"), "a generic decline is the issuer"


def test_response_codes_are_described_rather_than_echoed():
    """An operator reading a log should not need the specification open."""
    from gateway.action_codes import describe

    assert "approved" in describe("00")
    assert "NOT returned" in describe("21")
    assert "dialect" in describe("30")
    assert describe("") == "no response code"
    assert "99" in describe("99")


# ---------------------------------------------------------------------------
# Message types
# ---------------------------------------------------------------------------


def test_message_types_match_the_published_flow_table():
    """From Worldpay's transaction type table, not from what looked plausible.

        00  0100/0110  0120/0130  0420/0430   POS Preauthorized Request
        00  0200/0210  0220/0230  0420/0430   POS Direct Debit, Credit Purchase
    """
    from gateway.iso_dialect import message_type

    assert message_type("auth_capture", "authorize") == "0100"
    assert message_type("auth_capture", "capture") == "0120"
    assert message_type("auth_capture", "reversal") == "0420"

    assert message_type("purchase", "authorize") == "0200"
    assert message_type("purchase", "capture") == "0220"
    assert message_type("purchase", "reversal") == "0420"


def test_a_preauthorisation_is_completed_with_0120_not_0220():
    """The two advices belong to different flows and are not interchangeable.

    0120 is the authorisation advice that completes a 0100 preauthorisation;
    0220 is the advice for a 0200 financial transaction. Sending the wrong one
    earns a 12 (Invalid Transaction) — which, before the response codes were
    corrected, this gateway would have recorded as a successful capture.
    """
    from gateway.iso_dialect import message_type

    assert message_type("auth_capture", "capture") != message_type("purchase", "capture")


def test_reversals_use_the_advice_not_the_request():
    """0420 is an advice the host records; 0400 is a request it may refuse.

    Undoing a transaction that never completed is a notification, not a
    negotiation — and Worldpay lists 0420/0430 for both of the flows used here.
    """
    from gateway.iso_dialect import MESSAGE_TYPES

    for flow, steps in MESSAGE_TYPES.items():
        assert steps["reversal"] == "0420", f"{flow} does not send the reversal advice"


def test_an_unknown_step_is_refused_rather_than_guessed():
    from gateway.iso_dialect import message_type

    with pytest.raises(ValueError, match="No message type"):
        message_type("auth_capture", "settlement")

    with pytest.raises(ValueError):
        message_type("no_such_flow", "authorize")


def test_no_message_type_is_hard_coded_in_the_acquirer():
    """Every MTI comes from the table, so correcting one corrects all senders."""
    import pathlib

    source = pathlib.Path("src/gateway/acquirer.py").read_text()
    for literal in ('"MTI": "0400"', '"MTI": "0220"', '"MTI": "0420"', '"MTI": "0100"'):
        assert literal not in source, f"{literal} is hard-coded rather than looked up"


# ---------------------------------------------------------------------------
# Transaction identification
# ---------------------------------------------------------------------------


def test_the_entry_mode_describes_electronic_commerce():
    """09, not 01. They are different transactions with different liability.

    01 is manual entry: a person keyed a card into a terminal. 09 is PAN entry
    via electronic commerce: a cardholder typed it on a website. Sending the
    wrong one does not fail — it prices the transaction at the wrong
    interchange and places the chargeback liability somewhere else.
    """
    for name, dialect in DIALECTS.items():
        assert dialect.default_pos_entry.startswith("09"), (
            f"{name} declares entry mode {dialect.default_pos_entry}, which is "
            "not electronic commerce"
        )
        # Length has to match the field, or every byte after it shifts.
        assert len(dialect.default_pos_entry) == dialect.pos_entry_length


def test_the_condition_code_marks_the_transaction_as_e_commerce():
    """DE25=59, from Worldpay's value table."""
    from gateway.iso_dialect import POS_CONDITION_ECOMMERCE

    assert POS_CONDITION_ECOMMERCE == "59"
    for name in DIALECTS:
        assert "25" in get_dialect(name).bit_config(), f"{name} cannot carry DE25"


def test_card_acceptor_location_lands_in_the_declared_positions():
    """DE43 is positional, and a blob that drifts is silently wrong.

        1-23 name, 24-36 city, 37-38 state, 39-40 country
    """
    from gateway.iso_dialect import card_acceptor_name_location

    value = card_acceptor_name_location("ACME CRYPTO", "PARIS", "", "FR")

    assert len(value) == 40
    assert value[0:23].strip() == "ACME CRYPTO"
    assert value[23:36].strip() == "PARIS"
    assert value[38:40] == "FR"


def test_overlong_components_are_truncated_rather_than_shifting_the_rest():
    """An over-long name must not push the city out of its positions."""
    from gateway.iso_dialect import card_acceptor_name_location

    value = card_acceptor_name_location(
        "A MERCHANT NAME FAR LONGER THAN TWENTY THREE", "SOMEWHERE VERY LONG", "CA", "US"
    )

    assert len(value) == 40, "the field overflowed its own length"
    assert value[38:40] == "US", "the country was pushed out of its positions"
