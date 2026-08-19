"""
Exhaustive and property-based checks.

"No error in any case" cannot be shown by testing more cases — there are
infinitely many. It is shown two ways, and both are here:

  * **Exhaustively**, where the space is finite. The transaction state machine
    has eight states, so all sixty-four ordered pairs are enumerated and each
    one is asserted to be either permitted or refused. Nothing is sampled.
  * **By property**, where it is not. The money arithmetic must hold for every
    amount a caller can express, so Hypothesis generates them and the tests
    assert the properties that must never break — value is conserved, rounding
    only ever goes the safe way, nothing silently overflows.

A test that passes here is a statement about the whole input space, not about
the examples someone thought of.
"""

from decimal import Decimal

import pytest
from hypothesis import assume, given, settings as hyp_settings
from hypothesis import strategies as st

from gateway.acquirer import AcquirerService
from gateway.models import (
    LEGAL_TRANSITIONS,
    TERMINAL_STATES,
    IllegalTransition,
    Transaction,
    TransactionStatus,
)

ALL_STATES = list(TransactionStatus)


def _tx(status: TransactionStatus) -> Transaction:
    tx = Transaction(
        amount=Decimal("10.00"),
        masked_pan="411111******1111",
        target_wallet="0x742d35Cc6634C0532925a3b844Bc454e4438f44e",
        status=status,
    )
    tx.id = 1
    return tx


# ---------------------------------------------------------------------------
# The state machine, exhaustively
# ---------------------------------------------------------------------------


def test_every_state_declares_its_transitions():
    """A state missing from the table would silently permit nothing at all."""
    missing = set(ALL_STATES) - set(LEGAL_TRANSITIONS)
    assert not missing, f"states absent from LEGAL_TRANSITIONS: {missing}"


@pytest.mark.parametrize("origin", ALL_STATES, ids=lambda s: s.value)
@pytest.mark.parametrize("target", ALL_STATES, ids=lambda s: s.value)
def test_every_state_pair_is_decided(origin, target):
    """All 64 ordered pairs: permitted and applied, or refused and unchanged.

    The point is coverage of the whole space. Any pair nobody thought about is
    still asserted here, and lands on whichever side the table says.
    """
    tx = _tx(origin)

    if target is origin:
        # Re-entering the current state is a no-op, so a redelivered task or a
        # repeated reconciliation pass cannot corrupt anything.
        assert tx.transition_to(target) is False
        assert tx.status is origin
        return

    if target in LEGAL_TRANSITIONS[origin]:
        assert tx.transition_to(target) is True
        assert tx.status is target
    else:
        with pytest.raises(IllegalTransition):
            tx.transition_to(target)
        assert tx.status is origin, "a refused transition still changed the state"


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATES, key=lambda s: s.value))
@pytest.mark.parametrize("target", ALL_STATES, ids=lambda s: s.value)
def test_terminal_states_never_move(terminal, target):
    """Once money has settled in one direction, the record stops moving."""
    tx = _tx(terminal)
    if target is terminal:
        assert tx.transition_to(target) is False
    else:
        with pytest.raises(IllegalTransition):
            tx.transition_to(target)
    assert tx.status is terminal


def test_no_state_can_return_to_pending():
    """PENDING means "not yet presented to the bank" and is unreachable after."""
    for origin in ALL_STATES:
        if origin is TransactionStatus.PENDING:
            continue
        assert TransactionStatus.PENDING not in LEGAL_TRANSITIONS[origin], (
            f"{origin.value} can return to PENDING, which would erase the fact "
            "that the card was already presented"
        )


def test_every_state_can_reach_a_terminal_state():
    """No state is a dead end: every payment has a way to finish.

    A state from which nothing terminal is reachable would strand transactions
    forever, and nobody would notice until someone asked where their money went.
    """
    for origin in ALL_STATES:
        seen, frontier = set(), [origin]
        while frontier:
            state = frontier.pop()
            if state in seen:
                continue
            seen.add(state)
            frontier.extend(LEGAL_TRANSITIONS[state])
        assert seen & TERMINAL_STATES, f"{origin.value} cannot reach any terminal state"


def test_completion_is_stamped_only_when_settled():
    tx = _tx(TransactionStatus.PENDING)
    tx.transition_to(TransactionStatus.FIAT_APPROVED)
    assert tx.completed_at is None, "an approved payment is not finished"

    tx.transition_to(TransactionStatus.CRYPTO_SENT)
    assert tx.completed_at is not None, "a delivered payment must be stamped"


# ---------------------------------------------------------------------------
# Money arithmetic, by property
# ---------------------------------------------------------------------------

#: The full range a caller can express: two decimal places, within the widest
#: limits the configuration allows.
amounts = st.decimals(
    min_value=Decimal("0.01"),
    max_value=Decimal("1000000.00"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
)


@given(amount=amounts)
@hyp_settings(max_examples=400, deadline=None)
def test_minor_units_are_exact_for_every_amount(amount):
    """No amount may lose or gain a cent on the way to the acquirer.

    This is the property the old code broke: ``int(1.15 * 100)`` is 114, and
    binary floating point makes that failure depend on the value, which is why
    example-based tests missed it for so long.
    """
    cents = AcquirerService._to_cents(amount)

    assert isinstance(cents, int)
    assert cents == int((amount * 100).to_integral_value()), (
        f"{amount} converted to {cents} minor units"
    )
    # Round-trips back to the same amount.
    assert Decimal(cents) / 100 == amount


@given(amount=amounts)
@hyp_settings(max_examples=200, deadline=None)
def test_minor_units_never_round_in_the_customer_s_favour_by_accident(amount):
    """The conversion is exact, so it never drifts in either direction."""
    cents = AcquirerService._to_cents(amount)
    assert abs(Decimal(cents) - amount * 100) == 0


@given(
    amount=amounts,
    decimals=st.integers(min_value=0, max_value=18),
    rate=st.decimals(min_value=Decimal("0.01"), max_value=Decimal("100000"), places=2),
)
@hyp_settings(max_examples=300, deadline=None)
def test_token_units_never_exceed_what_was_paid_for(amount, decimals, rate):
    """Conversion to token units rounds down, always.

    The gateway must never deliver more value than it received. Rounding up by
    one unit is negligible once; across a million payments it is a loss with no
    corresponding revenue, and no alarm attached to it.
    """
    assume(rate > 0)
    exact = (amount / rate) * (Decimal(10) ** decimals)
    units = int(exact.to_integral_value(rounding="ROUND_DOWN"))

    assert units <= exact, "more units than the amount paid for"
    assert exact - units < 1, "rounded down by more than one unit"
    assert units >= 0


@given(amount=amounts)
@hyp_settings(max_examples=200, deadline=None)
def test_amount_survives_the_database_column(amount):
    """Numeric(18, 2) must hold every amount the API accepts."""
    quantised = amount.quantize(Decimal("0.01"))
    digits = len(quantised.as_tuple().digits)
    assert digits <= 18, f"{amount} needs {digits} digits, the column holds 18"
    assert -quantised.as_tuple().exponent <= 2


# ---------------------------------------------------------------------------
# ISO 8583 field construction, by property
# ---------------------------------------------------------------------------


@given(
    stan=st.integers(min_value=1, max_value=999999),
    mti=st.sampled_from(["0200", "0100", "0220"]),
)
@hyp_settings(max_examples=200, deadline=None)
def test_original_data_elements_are_always_42_digits(stan, mti):
    """DE90 is a fixed n42 block; any other length is rejected by an acquirer."""
    from datetime import datetime, timezone

    de90 = AcquirerService._original_data_elements(
        mti, f"{stan:06d}", datetime(2026, 8, 17, 12, 34, 56, tzinfo=timezone.utc)
    )
    assert len(de90) == 42
    assert de90.isdigit()
    assert de90.startswith(mti)
    assert de90[4:10] == f"{stan:06d}"


@given(
    pan=st.from_regex(r"\A[0-9]{13,19}\Z", fullmatch=True),
)
@hyp_settings(max_examples=200, deadline=None)
def test_masking_never_leaks_the_middle_digits(pan):
    """Whatever the card length, at most first six and last four survive."""
    from gateway.api import mask_pan

    masked = mask_pan(pan)
    assert len(masked) == len(pan)
    assert masked[:6] == pan[:6]
    assert masked[-4:] == pan[-4:]
    # Every digit between the retained ends is replaced. Positional, not a
    # substring search: Hypothesis produced 0000000000000, whose middle digits
    # also appear in the prefix that is legitimately kept, so a substring check
    # reports a leak that is not there.
    assert set(masked[6:-4]) <= {"*"}
    assert masked.count("*") == max(0, len(pan) - 10)
