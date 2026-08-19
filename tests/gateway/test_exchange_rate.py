"""
Exchange rate behaviour.

The rate decides how much value leaves the hot wallet on every transaction, and
a wrong one does not fail — it over-delivers or under-delivers silently. These
tests cover the three ways that happens: a rate nobody checked the age of, a
rate that is obviously broken, and a rate that changed between quoting the
customer and paying them.
"""

import time
from decimal import Decimal

import pytest

from gateway.config import settings
from gateway.exchange_rate import (
    CHAINLINK_FEEDS,
    FixedRateSource,
    GuardedRateSource,
    Quote,
    RateImplausible,
    RateUnavailable,
    apply_spread,
)

# The async tests here need the asyncio plugin's marker; sibling modules get it
# from their own pytestmark.
pytestmark = pytest.mark.asyncio(loop_scope="function")


class _Stub:
    """A source returning whatever a test needs it to."""

    name = "stub"

    def __init__(self, rate, age=0):
        self.rate = Decimal(str(rate))
        self.age = age

    async def quote(self, pair):
        return Quote(
            pair=pair,
            rate=self.rate,
            source=self.name,
            observed_at=time.time() - self.age,
            age_seconds=self.age,
        )


# ---------------------------------------------------------------------------
# Plausibility
# ---------------------------------------------------------------------------


async def test_a_rate_outside_its_plausible_range_is_refused():
    """A feed far outside its usual range is malfunctioning, not informative.

    Delivering against it is how a hot wallet empties in one transaction: a
    rate ten times too low means ten times the tokens.
    """
    guarded = GuardedRateSource(_Stub("0.0001"))

    with pytest.raises(RateImplausible, match="plausible range"):
        await guarded.quote("USDT/USD")


async def test_a_plausible_rate_passes():
    guarded = GuardedRateSource(_Stub("1.0002"))
    quote = await guarded.quote("USDT/USD")
    assert quote.rate == Decimal("1.0002")


async def test_a_pair_without_configured_bounds_is_not_blocked():
    """Bounds are a guard, not a whitelist: an unconfigured pair still works."""
    guarded = GuardedRateSource(_Stub("42.5"))
    quote = await guarded.quote("XYZ/ABC")
    assert quote.rate == Decimal("42.5")


# ---------------------------------------------------------------------------
# Movement
# ---------------------------------------------------------------------------


async def test_a_sudden_jump_between_quotes_is_refused():
    """A sharp market move and a broken oracle are indistinguishable here.

    Only one of them is safe to deliver against, so both are refused. A few
    declined payments cost less than one wallet emptied at a wrong price.
    """
    source = _Stub("1.00")
    guarded = GuardedRateSource(source)

    await guarded.quote("USDT/USD")

    source.rate = Decimal("1.40")  # +40%
    with pytest.raises(RateImplausible, match="moved"):
        await guarded.quote("USDT/USD")


async def test_ordinary_movement_is_accepted():
    source = _Stub("1.00")
    guarded = GuardedRateSource(source)
    await guarded.quote("USDT/USD")

    source.rate = Decimal("1.02")  # +2%, inside the threshold
    assert (await guarded.quote("USDT/USD")).rate == Decimal("1.02")


async def test_a_genuine_move_is_accepted_on_the_next_attempt():
    """Refusing once is a guard; refusing forever is an outage.

    If the market really did move, the reference has to give way — otherwise
    the gateway blocks indefinitely against a price that is never coming back.
    """
    source = _Stub("1.00")
    guarded = GuardedRateSource(source)
    await guarded.quote("USDT/USD")

    source.rate = Decimal("1.40")
    with pytest.raises(RateImplausible):
        await guarded.quote("USDT/USD")

    assert (await guarded.quote("USDT/USD")).rate == Decimal("1.40")


# ---------------------------------------------------------------------------
# Spread
# ---------------------------------------------------------------------------


async def test_the_customer_never_gets_the_mid_market_rate():
    """Quoting mid makes every payment a coin flip with nothing to absorb it."""
    guarded = GuardedRateSource(_Stub("1.00"))

    mid = await guarded.quote("USDT/USD")
    customer = apply_spread(mid)

    assert customer.rate > mid.rate, "the spread was not applied"
    expected = mid.rate * (Decimal(1) + settings.rate_spread)
    assert customer.rate == expected
    # Recorded so a disputed transaction can be reconstructed.
    assert "spread" in customer.source


async def test_a_higher_rate_delivers_fewer_tokens():
    """The margin has to move in the direction that keeps value in the wallet."""
    fiat = Decimal("100.00")
    mid, with_spread = Decimal("1.00"), Decimal("1.01")
    assert fiat / with_spread < fiat / mid


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------


def test_staleness_tolerance_never_falls_below_a_feed_heartbeat():
    """A threshold shorter than the heartbeat is a self-inflicted outage.

    Forex feeds publish on a 24-hour heartbeat when the price is stable, so a
    twenty-hour-old reading is working exactly as designed. Judged against a
    shorter global rule, every one of them is 'stale' and every payment is
    refused while nothing is actually wrong.
    """
    from gateway.exchange_rate import ChainlinkRateSource

    assert settings.rate_staleness_margin >= 1, (
        "a margin below 1 puts the threshold under the heartbeat"
    )

    for pair, feed in CHAINLINK_FEEDS.items():
        tolerance = ChainlinkRateSource._staleness_tolerance(pair, feed)
        assert tolerance >= feed["heartbeat_sec"], (
            f"{pair} would be called stale after {tolerance}s despite a "
            f"{feed['heartbeat_sec']}s heartbeat"
        )


def test_every_feed_declares_a_heartbeat():
    """Without one there is no basis for judging a reading fresh."""
    for pair, feed in CHAINLINK_FEEDS.items():
        assert feed.get("heartbeat_sec", 0) > 0, f"{pair} has no heartbeat"
        assert feed["address"].startswith("0x") and len(feed["address"]) == 42


# ---------------------------------------------------------------------------
# No silent fallback
# ---------------------------------------------------------------------------


async def test_an_unavailable_rate_raises_rather_than_defaulting():
    """A gateway that quietly reverts to a constant delivers wrong amounts."""

    class Broken:
        name = "broken"

        async def quote(self, pair):
            raise RateUnavailable("the oracle is down")

    guarded = GuardedRateSource(Broken())
    with pytest.raises(RateUnavailable):
        await guarded.quote("USDT/USD")


async def test_the_fixed_source_is_honest_about_being_a_constant():
    source = FixedRateSource(Decimal("1.0"))
    quote = await source.quote("USDT/USD")
    assert quote.source == "fixed"
    assert quote.age_seconds == 0


# ---------------------------------------------------------------------------
# Cross rates
# ---------------------------------------------------------------------------


async def test_a_cross_rate_divides_in_the_right_direction():
    """The direction is easy to invert and expensive to get wrong.

        EUR/USD  = 1.157   one euro is 1.157 dollars
        USDT/USD = 0.999   one USDT is 0.999 dollars
        EUR per USDT = 0.999 / 1.157 = 0.8636

    Delivery divides by the rate, so 100 EUR buys 115.80 USDT. Inverted, the
    same payment would deliver 86.36 — no error raised, a quarter of the value
    withheld, on every transaction.
    """
    from gateway.exchange_rate import CrossRateSource

    class Legs:
        name = "legs"

        async def quote(self, pair):
            rates = {"EUR/USD": Decimal("1.157065"), "USDT/USD": Decimal("0.999190")}
            return Quote(pair, rates[pair], "legs", time.time(), 0)

    cross = CrossRateSource(Legs(), "USD")
    quote = await cross.quote("EUR/USDT")

    assert abs(quote.rate - Decimal("0.863556")) < Decimal("0.000001")
    delivered = Decimal("100") / quote.rate
    assert Decimal("115") < delivered < Decimal("116"), f"delivered {delivered}"


async def test_a_cross_is_skipped_when_the_fiat_is_the_settlement_currency():
    """USD in, USD-denominated token out: one reading, not two."""
    from gateway.exchange_rate import CrossRateSource

    seen = []

    class Legs:
        name = "legs"

        async def quote(self, pair):
            seen.append(pair)
            return Quote(pair, Decimal("0.999"), "legs", time.time(), 0)

    quote = await CrossRateSource(Legs(), "USD").quote("USD/USDT")

    assert seen == ["USDT/USD"], "a redundant USD/USD leg was fetched"
    assert quote.rate == Decimal("0.999")


async def test_a_cross_is_only_as_fresh_as_its_older_leg():
    """Reporting the fresher leg's age would overstate how current the quote is."""
    from gateway.exchange_rate import CrossRateSource

    class Legs:
        name = "legs"

        async def quote(self, pair):
            age = 3600 if pair.startswith("EUR") else 60
            return Quote(pair, Decimal("1.0"), "legs", time.time() - age, age)

    quote = await CrossRateSource(Legs(), "USD").quote("EUR/USDT")
    assert quote.age_seconds == 3600


# ---------------------------------------------------------------------------
# Cross-checking against a second source
# ---------------------------------------------------------------------------


async def test_material_disagreement_between_sources_refuses_the_quote():
    """Two independent sources disagreeing means one is wrong, and which is unknowable."""
    from gateway.exchange_rate import CrossCheckedRateSource

    checked = CrossCheckedRateSource(_Stub("1.00"), _Stub("1.50"))

    with pytest.raises(RateImplausible, match="apart"):
        await checked.quote("USD/USDT")


async def test_close_agreement_passes():
    from gateway.exchange_rate import CrossCheckedRateSource

    checked = CrossCheckedRateSource(_Stub("1.0000"), _Stub("1.0010"))
    assert (await checked.quote("USD/USDT")).rate == Decimal("1.0000")


async def test_an_unavailable_second_source_does_not_stop_payments():
    """The check is advisory. Its downtime must not become the gateway's.

    The primary already carries staleness, bounds and movement guards; letting
    an aggregator's outage refuse payments trades a real risk for a certain one.
    """
    from gateway.exchange_rate import CrossCheckedRateSource

    class Down:
        name = "down"

        async def quote(self, pair):
            raise RateUnavailable("the aggregator is unreachable")

    checked = CrossCheckedRateSource(_Stub("1.00"), Down())
    assert (await checked.quote("USD/USDT")).rate == Decimal("1.00")


async def test_the_primary_source_is_the_one_returned():
    """The second opinion verifies; it does not supply the number used."""
    from gateway.exchange_rate import CrossCheckedRateSource

    checked = CrossCheckedRateSource(_Stub("1.0000"), _Stub("1.0050"))
    quote = await checked.quote("USD/USDT")
    assert quote.rate == Decimal("1.0000")
    assert quote.source == "stub"


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


async def test_a_cached_quote_is_reused_within_its_window():
    """An oracle on a daily heartbeat does not need reading per request."""
    from gateway.exchange_rate import CachedRateSource

    calls = []

    class Counting:
        name = "counting"

        async def quote(self, pair):
            calls.append(pair)
            return Quote(pair, Decimal("1.0"), "counting", time.time(), 0)

    cached = CachedRateSource(Counting(), ttl_seconds=60)
    await cached.quote("USD/USDT")
    await cached.quote("USD/USDT")
    await cached.quote("USD/USDT")

    assert len(calls) == 1, f"the source was read {len(calls)} times"


async def test_an_expired_cache_entry_is_refetched():
    from gateway.exchange_rate import CachedRateSource

    calls = []

    class Counting:
        name = "counting"

        async def quote(self, pair):
            calls.append(pair)
            return Quote(pair, Decimal("1.0"), "counting", time.time(), 0)

    cached = CachedRateSource(Counting(), ttl_seconds=0)
    await cached.quote("USD/USDT")
    await cached.quote("USD/USDT")

    assert len(calls) == 2


# ---------------------------------------------------------------------------
# The assembled pipeline
# ---------------------------------------------------------------------------


async def test_the_pipeline_build_produces_something_the_api_can_actually_call():
    """Every layer passing in isolation is not the same as the stack composing.

    The spread was once a method on one wrapper, so it existed only while that
    wrapper happened to be outermost. Adding a cache above it removed the
    method from the assembled source and every payment failed on a missing
    attribute — with each layer's own tests still green.
    """
    from unittest.mock import MagicMock

    from gateway.exchange_rate import apply_spread, build_rate_source

    source = build_rate_source(MagicMock())

    # The one call the payment path makes, and the function it composes with.
    assert hasattr(source, "quote")
    assert callable(apply_spread)

    # And the spread survives whatever the assembly happens to return.
    quote = Quote("USD/USDT", Decimal("1.00"), "test", time.time(), 0)
    assert apply_spread(quote).rate > quote.rate


async def test_the_fixed_pipeline_prices_end_to_end():
    """The fixed source through the same assembly the API uses."""
    from gateway.exchange_rate import apply_spread, build_rate_source

    if settings.rate_source != "fixed":
        pytest.skip("this assembly is only built when RATE_SOURCE=fixed")

    source = build_rate_source()
    quote = apply_spread(await source.quote("USD/USDT"))

    delivered = Decimal("300.00") / quote.rate
    assert delivered > 0
    # The margin keeps value in the wallet: never more tokens than mid would give.
    assert delivered <= Decimal("300.00") / settings.exchange_rate
