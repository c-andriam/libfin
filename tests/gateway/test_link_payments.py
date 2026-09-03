"""The payment-link contract, pinned so a merge cannot quietly undo it.

These assertions exist because a merge already did undo them once. The running
container kept working while the repository carried a version in which link
payments were rejected outright and the anti-tampering guard was gone — a
divergence nothing failed on, because no test asserted either property.

Two rules, and they are a pair:

* ``target_wallet`` and ``amount`` are optional *on the model*, because a link
  supplies them from the database instead. Making either one required again
  breaks every link payment with a 422.
* A request may state an order or name one by link, never both. Accepting both
  leaves it ambiguous which governs, and a payer would resolve that ambiguity
  by pairing a 300 link with a 3 amount.

Losing one without the other is the dangerous case: required fields plus the
guard merely breaks the feature loudly; optional fields without the guard opens
the hole silently.
"""

import pytest
from pydantic import ValidationError

from gateway.api import PaymentRequest

CARD = {"pan": "4111111111111111", "expiry": "3012", "cvv": "123"}
WALLET = "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
TOKEN = "A" * 32


def test_a_link_alone_is_a_complete_request():
    """No amount, no wallet: the row the token names supplies both."""
    req = PaymentRequest(**CARD, link=TOKEN)
    assert req.link == TOKEN
    assert req.amount is None
    assert req.target_wallet is None


def test_an_order_alone_is_a_complete_request():
    """The direct path is unchanged: state the order and it is accepted."""
    req = PaymentRequest(**CARD, amount="25.00", currency="USD", target_wallet=WALLET)
    assert req.link is None
    assert req.target_wallet == WALLET


@pytest.mark.parametrize(
    "extra",
    [
        {"amount": "3.00"},
        {"target_wallet": WALLET},
        {"amount": "3.00", "target_wallet": WALLET},
    ],
    ids=["amount", "wallet", "both"],
)
def test_a_link_may_not_be_accompanied_by_an_order(extra):
    """The tampering case: a link for 300 presented alongside an amount of 3."""
    with pytest.raises(ValidationError) as caught:
        PaymentRequest(**CARD, link=TOKEN, **extra)
    assert "not both" in str(caught.value)


@pytest.mark.parametrize(
    "partial",
    [{}, {"amount": "25.00"}, {"target_wallet": WALLET}],
    ids=["neither", "amount-only", "wallet-only"],
)
def test_an_order_without_a_link_must_be_complete(partial):
    """Optional on the model must not mean optional in practice."""
    with pytest.raises(ValidationError) as caught:
        PaymentRequest(**CARD, **partial)
    assert "Missing" in str(caught.value)


def test_the_fields_are_declared_optional():
    """Asserted on the schema itself, not only through behaviour.

    A future edit could make these required and still satisfy the tests above
    if it also removed the link branch. Checking the declaration catches the
    exact change that broke this before.
    """
    fields = PaymentRequest.model_fields
    assert not fields["target_wallet"].is_required(), (
        "target_wallet became required again; link-only payments will 422"
    )
    assert not fields["amount"].is_required(), (
        "amount became required again; link-only payments will 422"
    )
