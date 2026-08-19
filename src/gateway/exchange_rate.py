"""
Exchange rates.

The gateway takes fiat and sends crypto, so the rate decides how much value
leaves the hot wallet. A wrong rate does not fail — it quietly over-delivers or
under-delivers on every transaction, and neither shows up as an error.

Until now the rate was the constant 1.0, which is correct only for a stablecoin
at parity and silently wrong for everything else. Worse, it was read at
*delivery* time rather than at authorisation, so a live rate would have handed
customers something other than what they were quoted.

Three properties this module exists to guarantee:

  * **The rate is locked when the customer is quoted.** Delivery uses the
    recorded rate. A retry three days later still delivers what was agreed.
  * **A rate is never used without knowing how old it is.** An oracle publishes
    on a heartbeat, not continuously, so "recent" has to be judged per feed —
    a global freshness rule shorter than a feed's heartbeat rejects every
    reading it makes and takes the gateway down by itself.
  * **An implausible rate is refused, not applied.** A feed returning zero, a
    negative number, or a value far outside its usual range is a malfunction,
    and delivering against it empties the wallet.

Chainlink's on-chain feeds are the default source: no API key, no separate
availability to manage, and read over the Web3 connection the gateway already
holds. Addresses below were each verified by calling ``description()`` on
mainnet rather than copied from a list.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Protocol

from gateway.config import settings

LOGGER = logging.getLogger(__name__)

#: Chainlink aggregator addresses on Ethereum mainnet, each confirmed by
#: reading description() from the deployed contract.
#:
#: The heartbeat is how long a feed may go without publishing when the price is
#: stable. Forex feeds run on a 24-hour heartbeat with a small deviation
#: threshold, so a reading twenty hours old is normal rather than stale.
#: Treating it as stale would refuse every payment.
CHAINLINK_FEEDS = {
    "EUR/USD": {
        "address": "0xb49f677943BC038e9857d61E7d053CaA2C1734C1",
        "heartbeat_sec": 86400,
    },
    "GBP/USD": {
        "address": "0x5c0Ab2d9b5a7ed9f470386e82BB36A3613cDd4b5",
        "heartbeat_sec": 86400,
    },
    "USDT/USD": {
        "address": "0x3E7d1eAB13ad0104d2750B8863b489D65364e32D",
        "heartbeat_sec": 86400,
    },
    "ETH/USD": {
        "address": "0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419",
        "heartbeat_sec": 3600,
    },
}

#: latestRoundData() and decimals(), the only two calls needed.
AGGREGATOR_ABI = [
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"name": "roundId", "type": "uint80"},
            {"name": "answer", "type": "int256"},
            {"name": "startedAt", "type": "uint256"},
            {"name": "updatedAt", "type": "uint256"},
            {"name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]


class RateUnavailable(RuntimeError):
    """No rate could be obtained that is fit to quote against.

    Deliberately not a fallback. Quoting on a rate the gateway does not trust
    means delivering an amount nobody can defend, and the customer keeps it.
    """


class RateImplausible(RateUnavailable):
    """The source answered, and the answer cannot be right."""


@dataclass(frozen=True)
class Quote:
    """A rate, and everything needed to justify it later."""

    pair: str
    rate: Decimal
    source: str
    observed_at: float
    age_seconds: int

    def __str__(self) -> str:
        return f"{self.pair}={self.rate} from {self.source}, {self.age_seconds}s old"


class RateSource(Protocol):
    async def quote(self, pair: str) -> Quote: ...


class FixedRateSource:
    """A configured constant. Correct for a stablecoin at parity, and honest
    about being nothing more than that."""

    name = "fixed"

    def __init__(self, rate: Optional[Decimal] = None):
        self._rate = Decimal(str(rate if rate is not None else settings.exchange_rate))

    async def quote(self, pair: str) -> Quote:
        return Quote(
            pair=pair,
            rate=self._rate,
            source=self.name,
            observed_at=time.time(),
            age_seconds=0,
        )


class ChainlinkRateSource:
    """Reads an on-chain Chainlink aggregator.

    Checks the reading is fresh *for that feed* and that the round actually
    completed, both of which Chainlink's own guidance calls for and neither of
    which a naive ``latestAnswer()`` call would give.
    """

    name = "chainlink"

    def __init__(self, web3, feeds: Optional[dict] = None):
        self.w3 = web3
        self.feeds = feeds or CHAINLINK_FEEDS
        self._decimals: dict = {}

    async def quote(self, pair: str) -> Quote:
        feed = self.feeds.get(pair)
        if feed is None:
            raise RateUnavailable(
                f"No Chainlink feed configured for {pair}. Known pairs: "
                f"{', '.join(sorted(self.feeds))}."
            )

        address = self.w3.to_checksum_address(feed["address"])
        contract = self.w3.eth.contract(address=address, abi=AGGREGATOR_ABI)

        try:
            round_data = await asyncio.to_thread(contract.functions.latestRoundData().call)
            if address not in self._decimals:
                self._decimals[address] = await asyncio.to_thread(
                    contract.functions.decimals().call
                )
        except Exception as exc:
            raise RateUnavailable(f"Could not read the {pair} feed at {address}: {exc}") from exc

        round_id, answer, _started_at, updated_at, answered_in_round = round_data
        decimals = self._decimals[address]

        if answer <= 0:
            raise RateImplausible(f"{pair} feed returned {answer}, which cannot be a price.")

        # A round that never completed carries a stale answer under a new id.
        if answered_in_round < round_id:
            raise RateImplausible(
                f"{pair} feed round {round_id} was answered in an earlier round "
                f"({answered_in_round}); the reading is carried over, not fresh."
            )

        age = int(time.time() - updated_at)
        tolerance = self._staleness_tolerance(pair, feed)
        if age > tolerance:
            raise RateUnavailable(
                f"{pair} was last published {age}s ago, beyond the {tolerance}s this "
                f"feed is allowed (heartbeat {feed['heartbeat_sec']}s). The oracle "
                "has stopped updating; refusing to quote against it."
            )

        return Quote(
            pair=pair,
            rate=Decimal(answer) / (Decimal(10) ** decimals),
            source=f"{self.name}:{address[:10]}",
            observed_at=float(updated_at),
            age_seconds=age,
        )

    @staticmethod
    def _staleness_tolerance(pair: str, feed: dict) -> int:
        """How old a reading may be before it is refused.

        The heartbeat plus a margin, per feed. A single global threshold is a
        mistake with a specific failure mode: set below a feed's heartbeat, it
        rejects readings that are working exactly as designed, and the gateway
        refuses every payment while nothing is actually wrong.
        """
        override = settings.rate_staleness_overrides.get(pair)
        if override:
            return override
        return int(feed["heartbeat_sec"] * settings.rate_staleness_margin)


class GuardedRateSource:
    """Wraps a source with the checks that make a rate safe to deliver against.

    A source answering is not the same as a source being right. Three guards,
    each corresponding to a way a bad rate empties the hot wallet without
    raising anything:

      * **Plausible bounds.** A feed returning a number far outside its usual
        range is malfunctioning. Bounds are per pair and deliberately wide —
        wide enough that ordinary volatility passes and a decimal-place error
        does not.
      * **Movement since the last quote.** A rate that jumps beyond a threshold
        between two readings is either a genuine market event or a broken
        oracle, and the two are indistinguishable from here. Refusing costs a
        few declined payments; accepting can cost the wallet.
      * **A spread.** The gateway never quotes the mid-market rate: every
        transaction would then be a coin flip on which side of the spread the
        market moved, with no margin to absorb the losing half.
    """

    def __init__(self, source: RateSource):
        self._source = source
        self._last: dict = {}

    @property
    def name(self) -> str:
        return getattr(self._source, "name", "unknown")

    async def quote(self, pair: str) -> Quote:
        quote = await self._source.quote(pair)

        low, high = settings.rate_bounds_for(pair)
        if low is not None and not (low <= quote.rate <= high):
            raise RateImplausible(
                f"{pair} quoted at {quote.rate}, outside the plausible range "
                f"{low}–{high}. A feed this far off is malfunctioning, and "
                "delivering against it would drain the wallet."
            )

        previous = self._last.get(pair)
        if previous is not None and previous > 0:
            movement = abs(quote.rate - previous) / previous
            if movement > settings.rate_max_movement:
                # Clear the reference so a genuine market move is accepted on
                # the next attempt rather than blocking forever against a
                # price that is never coming back.
                self._last.pop(pair, None)
                raise RateImplausible(
                    f"{pair} moved {movement:.1%} since the last quote "
                    f"({previous} → {quote.rate}), beyond the "
                    f"{settings.rate_max_movement:.1%} threshold. Either the market "
                    "moved sharply or the oracle is wrong; both look identical from "
                    "here, and only one is safe to deliver against."
                )

        self._last[pair] = quote.rate
        return quote


class CrossRateSource:
    """Prices a token in a fiat currency that has no direct feed.

    Oracles publish against USD, so taking euros and settling in a dollar
    stablecoin means two readings rather than one:

        EUR/USD  = 1.157   one euro is 1.157 dollars
        USDT/USD = 0.999   one USDT is 0.999 dollars

    and the rate the gateway needs — euros per USDT — is their quotient:

        EUR per USDT = (USDT/USD) / (EUR/USD) = 0.8636

    The direction matters and is easy to invert. The rate is expressed in *fiat
    per token*, because delivery divides by it: ten euros at 0.8636 buys 11.58
    USDT. Getting this the wrong way round would deliver 8.64 instead — no
    error anywhere, just a third of the value, every time.

    Both legs are read from the same guarded source, so each carries its own
    staleness and plausibility checks. The quote is only as fresh as its older
    leg, which is what gets reported.
    """

    name = "cross"

    def __init__(self, source: RateSource, settlement_currency: str = "USD"):
        self._source = source
        self._settlement = settlement_currency

    async def quote(self, pair: str) -> Quote:
        """``pair`` is written FIAT/TOKEN, for example EUR/USDT."""
        try:
            fiat, token = pair.split("/")
        except ValueError:
            raise RateUnavailable(f"Cross rate pair must be FIAT/TOKEN, got {pair!r}.")

        token_leg = await self._source.quote(f"{token}/{self._settlement}")

        if fiat == self._settlement:
            # Nothing to cross: the fiat is already the settlement currency.
            return Quote(
                pair=pair,
                rate=token_leg.rate,
                source=f"{self.name}:{token_leg.source}",
                observed_at=token_leg.observed_at,
                age_seconds=token_leg.age_seconds,
            )

        fiat_leg = await self._source.quote(f"{fiat}/{self._settlement}")
        if fiat_leg.rate <= 0:
            raise RateImplausible(f"{fiat}/{self._settlement} quoted at {fiat_leg.rate}.")

        return Quote(
            pair=pair,
            rate=token_leg.rate / fiat_leg.rate,
            source=f"{self.name}:{fiat}/{token} via {self._settlement}",
            # A cross is only as fresh as its older leg.
            observed_at=min(token_leg.observed_at, fiat_leg.observed_at),
            age_seconds=max(token_leg.age_seconds, fiat_leg.age_seconds),
        )


def apply_spread(quote: Quote, spread: Optional[Decimal] = None) -> Quote:
    """The rate offered to the customer: mid-market, plus the margin.

    A function rather than a method on one of the wrappers. As a method it had
    to live on whichever layer happened to be outermost, and moving the cache
    above it silently removed it from the assembled pipeline — every payment
    then failed on an attribute that no longer existed. Composition should not
    decide whether the margin applies.

    Applied to the rate itself rather than to the delivered amount, so the
    margin is visible in the recorded rate and a disputed transaction can be
    reconstructed exactly.
    """
    margin = settings.rate_spread if spread is None else spread
    if margin <= 0:
        return quote

    return Quote(
        pair=quote.pair,
        # A higher rate means fewer tokens per unit of fiat, which is the
        # direction the margin has to go.
        rate=quote.rate * (Decimal(1) + margin),
        source=f"{quote.source}+spread{margin}",
        observed_at=quote.observed_at,
        age_seconds=quote.age_seconds,
    )


def build_rate_source(web3=None):
    """The configured source, wrapped in its guards.

    Falls back to the fixed rate only when explicitly configured that way.
    There is no silent fallback: a gateway that quietly reverts to a constant
    when its oracle fails is one that delivers wrong amounts and says nothing.
    """
    if settings.rate_source == "fixed":
        return GuardedRateSource(FixedRateSource())

    if web3 is None:
        raise RateUnavailable(
            "The chainlink rate source needs a Web3 connection. Pass one, or set "
            "RATE_SOURCE=fixed if a constant is genuinely what you want."
        )

    # Guards go on the individual legs, not only the cross: each reading needs
    # its own staleness and plausibility check, and a cross of two implausible
    # numbers can look perfectly reasonable.
    legs = GuardedRateSource(ChainlinkRateSource(web3))
    cross = CrossRateSource(legs, settings.rate_settlement_currency)

    secondary = None
    if settings.rate_cross_check_source == "coingecko":
        secondary = CoinGeckoRateSource()
    elif settings.rate_cross_check_source not in ("", "none"):
        raise RateUnavailable(
            f"Unknown cross-check source {settings.rate_cross_check_source!r}. "
            "Use 'coingecko' or 'none'."
        )

    # Cached outermost: the whole verified answer is what gets reused, so a
    # cache hit does not skip the cross-check for some callers and not others.
    return CachedRateSource(GuardedRateSource(CrossCheckedRateSource(cross, secondary)))


class CoinGeckoRateSource:
    """A second opinion, from off-chain.

    Not a replacement for the oracle — a check on it. A single price source is
    a single point of failure and, worse, a single point of manipulation: an
    oracle that reports a rate ten times too low sends ten times the tokens,
    and nothing in the system would object.

    Deliberately a different kind of source rather than a second oracle: two
    on-chain feeds can share an upstream and fail together, where an aggregator
    of exchange prices fails independently.
    """

    name = "coingecko"

    #: Token symbol to the identifier the API uses.
    TOKEN_IDS = {
        "USDT": "tether",
        "USDC": "usd-coin",
        "DAI": "dai",
        "ETH": "ethereum",
    }

    def __init__(self, base_url: str = "https://api.coingecko.com/api/v3", timeout: float = 8.0):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def quote(self, pair: str) -> Quote:
        """``pair`` is FIAT/TOKEN, matching the cross source."""
        try:
            fiat, token = pair.split("/")
        except ValueError:
            raise RateUnavailable(f"Pair must be FIAT/TOKEN, got {pair!r}.")

        token_id = self.TOKEN_IDS.get(token.upper())
        if token_id is None:
            raise RateUnavailable(
                f"No CoinGecko identifier for {token}. Known: "
                f"{', '.join(sorted(self.TOKEN_IDS))}."
            )

        import httpx

        url = f"{self._base_url}/simple/price"
        params = {"ids": token_id, "vs_currencies": fiat.lower()}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            raise RateUnavailable(f"CoinGecko did not answer for {pair}: {exc}") from exc

        try:
            value = payload[token_id][fiat.lower()]
        except (KeyError, TypeError):
            raise RateUnavailable(f"CoinGecko returned no {fiat} price for {token}: {payload}")

        rate = Decimal(str(value))
        if rate <= 0:
            raise RateImplausible(f"CoinGecko quoted {pair} at {rate}.")

        # The API reports how much fiat one token costs, which is already the
        # direction used everywhere here: delivery divides by it.
        return Quote(
            pair=pair,
            rate=rate,
            source=self.name,
            observed_at=time.time(),
            age_seconds=0,
        )


class CrossCheckedRateSource:
    """A primary source, verified against a second one.

    The second source is advisory, and that asymmetry is deliberate. If it
    disagrees materially the quote is refused, because two independent sources
    disagreeing means at least one is wrong and there is no way to tell which.
    But if it is simply unavailable, the payment proceeds on the primary: an
    aggregator's downtime should not stop a gateway whose oracle is healthy and
    already carries its own staleness, bounds and movement checks.
    """

    def __init__(self, primary: RateSource, secondary: Optional[RateSource] = None):
        self._primary = primary
        self._secondary = secondary

    @property
    def name(self) -> str:
        return getattr(self._primary, "name", "primary")

    async def quote(self, pair: str) -> Quote:
        primary = await self._primary.quote(pair)

        if self._secondary is None or settings.rate_cross_check_tolerance <= 0:
            return primary

        try:
            secondary = await self._secondary.quote(pair)
        except RateUnavailable as exc:
            # Advisory: log it, do not refuse a payment the primary can price.
            LOGGER.warning(
                f"Could not cross-check {pair} ({exc}). Proceeding on "
                f"{primary.source} alone."
            )
            return primary

        if secondary.rate <= 0:
            LOGGER.warning(f"Cross-check for {pair} returned {secondary.rate}; ignoring it.")
            return primary

        divergence = abs(primary.rate - secondary.rate) / secondary.rate
        if divergence > settings.rate_cross_check_tolerance:
            raise RateImplausible(
                f"{pair}: {primary.source} says {primary.rate}, {secondary.source} says "
                f"{secondary.rate} — {divergence:.2%} apart, beyond the "
                f"{settings.rate_cross_check_tolerance:.2%} tolerance. At least one is "
                "wrong and there is no way to tell which, so neither is used."
            )

        LOGGER.debug(f"{pair} cross-checked within {divergence:.3%}.")
        return primary


class CachedRateSource:
    """Holds a quote briefly so the payment path does not re-read per request.

    An oracle on a 24-hour heartbeat does not need reading a hundred times a
    second, and the off-chain second opinion has a rate limit that a busy
    gateway would exhaust in a minute. The window is short enough that a real
    move is still caught by the movement guard on the next refresh.
    """

    def __init__(self, source: RateSource, ttl_seconds: Optional[int] = None):
        self._source = source
        self._ttl = ttl_seconds if ttl_seconds is not None else settings.rate_cache_seconds
        self._cache: dict = {}

    @property
    def name(self) -> str:
        return getattr(self._source, "name", "cached")

    async def quote(self, pair: str) -> Quote:
        cached = self._cache.get(pair)
        if cached is not None:
            quote, fetched_at = cached
            if time.monotonic() - fetched_at < self._ttl:
                return quote

        quote = await self._source.quote(pair)
        self._cache[pair] = (quote, time.monotonic())
        return quote
