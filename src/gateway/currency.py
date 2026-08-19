"""
Currencies.

Two codes exist for every currency and they are not interchangeable. ISO 8583
carries the **numeric** code in DE49 — 840 for dollars — while price feeds and
humans use the **alphabetic** one, USD. Sending "USD" where 840 belongs is a
three-character field carrying the wrong three characters, which shifts nothing
and simply prices the transaction in a currency the acquirer does not recognise.

Amounts also differ in how finely they divide. Most currencies have two decimal
places, the yen has none, and the dinar has three. A gateway that assumes two
everywhere charges a yen customer a hundred times too much, and a dinar customer
a tenth of what they agreed — neither raises anything.

The table below is deliberately short. A currency belongs here only once its
rate feed has been verified, because accepting a currency the gateway cannot
price is a promise it cannot keep.
"""

from decimal import Decimal
from typing import NamedTuple, Optional


class Currency(NamedTuple):
    alpha: str          # ISO 4217 alphabetic, for rate feeds and people
    numeric: str        # ISO 4217 numeric, for DE49
    exponent: int       # decimal places the currency actually has
    name: str

    @property
    def minor_units_per_unit(self) -> int:
        return 10 ** self.exponent


#: Currencies the gateway can price, each with a verified rate feed.
SUPPORTED = {
    "USD": Currency("USD", "840", 2, "United States dollar"),
    "EUR": Currency("EUR", "978", 2, "Euro"),
    "GBP": Currency("GBP", "826", 2, "Pound sterling"),
}

#: Currencies whose minor unit is not a hundredth. Listed so the assumption is
#: visible rather than buried: adding one of these without handling its
#: exponent charges the customer by a factor of a hundred.
KNOWN_NON_TWO_DECIMAL = {
    "JPY": 0,   # yen, no minor unit
    "KRW": 0,   # won
    "BHD": 3,   # Bahraini dinar
    "KWD": 3,   # Kuwaiti dinar
    "TND": 3,   # Tunisian dinar
}


class UnsupportedCurrency(ValueError):
    """A currency the gateway cannot price, and so must not accept."""


def get(alpha: str) -> Currency:
    code = (alpha or "").strip().upper()
    currency = SUPPORTED.get(code)
    if currency is None:
        raise UnsupportedCurrency(
            f"{code or '(blank)'} is not supported. Available: "
            f"{', '.join(sorted(SUPPORTED))}. A currency is added here only once its "
            "rate feed is verified — accepting one the gateway cannot price is a "
            "promise it cannot keep."
        )
    return currency


def numeric_code(alpha: str) -> str:
    """The DE49 value for a currency, from its alphabetic code."""
    return get(alpha).numeric


def to_minor_units(amount: Decimal, alpha: str) -> int:
    """Exact minor units for an amount, using the currency's own exponent.

    Not a fixed multiplication by a hundred: the yen has no minor unit and the
    dinar has three, so a fixed factor charges by a hundred either way.
    """
    from decimal import ROUND_HALF_UP

    currency = get(alpha)
    scaled = Decimal(str(amount)) * currency.minor_units_per_unit
    return int(scaled.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def find_by_numeric(numeric: str) -> Optional[Currency]:
    """Reverse lookup, for reading a stored DE49 back."""
    for currency in SUPPORTED.values():
        if currency.numeric == numeric:
            return currency
    return None
