"""
ISO 8583 acquirer link.

Correlates 0200/0210 authorisations and 0400/0410 reversals by STAN, keeps the
link alive with 0800 echoes, and allocates STANs from Redis so several gateway
instances never collide.

The reversal path deserves a note, because it is the one that costs real money
when it is wrong. A reversal must carry the *real* PAN and enough of the
original message for the acquirer to match it (DE90), so:

  * DE90 is built from the original MTI, STAN and transmission time — the field
    is a fixed 42-digit block and an acquirer rejects a malformed one;
  * a 0200 that times out is treated as *unknown*, not as declined, and gets a
    reversal of its own. The cardholder may well have been debited by an
    authorisation whose response we simply never saw.
"""

import asyncio
import logging
import ssl
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, Optional

import redis.asyncio as redis

from gateway.action_codes import describe, indicates_misconfiguration, is_reversal_approved
from gateway.config import settings
from gateway.currency import numeric_code, to_minor_units
from gateway.iso_dialect import (
    card_acceptor_name_location,
    get_dialect,
    message_type,
    missing_mandatory,
)
from gateway.redis_client import async_client
from libfin import iso8583
from libfin.network.client import Iso8583Client

LOGGER = logging.getLogger(__name__)

_STAN_KEY = "acquirer:stan"
_STAN_MAX = 999999
_RRN_KEY = "acquirer:rrn"


class AuthorizationTimeout(TimeoutError):
    """The acquirer never answered. The outcome of the authorisation is unknown."""

    def __init__(self, stan: str, rrn: str, sent_at: datetime):
        super().__init__(f"No response from the acquirer for STAN {stan}.")
        self.stan = stan
        self.rrn = rrn
        self.sent_at = sent_at


class AcquirerService:
    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        redis_url: Optional[str] = None,
    ):
        self.host = host or settings.bank_host
        self.port = port or settings.bank_port
        self.ssl_context = self._build_ssl_context()

        self.client = Iso8583Client(
            self.host, self.port, length_header_size=2, ssl_context=self.ssl_context
        )
        self.client.on_message_received = self._handle_incoming_message

        self._pending_requests: Dict[str, asyncio.Future] = {}
        self._redis_url = redis_url or settings.redis_url
        self._redis: Optional[redis.Redis] = None
        self._connect_task: Optional[asyncio.Task] = None
        self._echo_task: Optional[asyncio.Task] = None
        self._running = False
        self._last_echo_ok: Optional[float] = None

    # ── Lifecycle ───────────────────────────────────────────────────────────

    @staticmethod
    def _build_ssl_context() -> Optional[ssl.SSLContext]:
        if not settings.bank_use_tls:
            return None

        context = ssl.create_default_context(
            cafile=settings.bank_tls_ca_file or None
        )
        if settings.bank_tls_client_cert and settings.bank_tls_client_key:
            context.load_cert_chain(
                settings.bank_tls_client_cert, settings.bank_tls_client_key
            )
        if settings.bank_tls_insecure:
            if settings.is_production:
                raise ValueError("BANK_TLS_INSECURE cannot be enabled in production.")
            LOGGER.warning("Bank TLS certificate verification is DISABLED (simulation only).")
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    @property
    def redis(self) -> redis.Redis:
        if self._redis is None:
            self._redis = async_client(self._redis_url)
        return self._redis

    @property
    def is_connected(self) -> bool:
        return bool(self.client._connected)

    async def start(self, wait_timeout: float = 5.0) -> bool:
        """Start connecting. Returns whether the link came up within the timeout."""
        if self._running:
            return self.is_connected

        self._running = True
        self._connect_task = asyncio.create_task(self.client.connect())

        deadline = asyncio.get_running_loop().time() + wait_timeout
        while asyncio.get_running_loop().time() < deadline:
            if self.is_connected:
                break
            await asyncio.sleep(0.05)

        if settings.bank_echo_interval_sec > 0:
            self._echo_task = asyncio.create_task(self._echo_loop())

        if not self.is_connected:
            LOGGER.warning(
                f"Acquirer link to {self.host}:{self.port} not up after {wait_timeout}s; "
                "the client keeps retrying in the background."
            )
        return self.is_connected

    async def stop(self) -> None:
        self._running = False
        for task in (self._echo_task, self._connect_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._echo_task = None
        self._connect_task = None

        for future in self._pending_requests.values():
            if not future.done():
                future.cancel()
        self._pending_requests.clear()

        await self.client.disconnect()
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    # ── Identifiers ─────────────────────────────────────────────────────────

    async def _next_counter(self, key: str, modulo: int) -> int:
        """Monotonic counter shared across instances, wrapping without a race."""
        try:
            value = await self.redis.incr(key)
            return (value - 1) % modulo + 1
        except Exception as exc:
            LOGGER.error(f"Redis counter {key} unavailable ({exc}); falling back to the clock.")
            # Time-derived fallback: still unique enough to correlate a response,
            # unlike a random value it stays monotonic within a process.
            return int(datetime.now(timezone.utc).timestamp() * 1000) % modulo + 1

    async def _next_stan(self) -> str:
        return f"{await self._next_counter(_STAN_KEY, _STAN_MAX):06d}"

    async def _next_rrn(self) -> str:
        """DE37, 12 digits. The acquirer's durable handle on the authorisation."""
        counter = await self._next_counter(_RRN_KEY, 999999)
        return f"{datetime.now(timezone.utc).strftime('%y%j')}{counter:06d}"[:12].zfill(12)

    @staticmethod
    def _to_cents(amount: Decimal) -> int:
        """Exact minor units. ``int(1.15 * 100)`` is 114 — never do that with money."""
        return int(
            (Decimal(str(amount)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )

    @staticmethod
    def _encode(message: Dict[str, Any]) -> bytes:
        """Serialise using the acquirer's dialect.

        Field lengths, character encoding and bitmap form all come from the
        profile: they are one coherent choice, not three independent settings.
        The library defaults were the 1993 lengths, which no acquirer host
        consulted actually expects.
        """
        dialect = get_dialect(settings.acquirer_dialect)
        return iso8583.dumps(
            message,
            encoding=settings.acquirer_encoding or dialect.encoding,
            hex_bitmap=settings.acquirer_hex_bitmap or dialect.hex_bitmap,
            iso_config=dialect.bit_config(),
        )

    @staticmethod
    def _decode(raw: bytes) -> Dict[str, Any]:
        """Parse a response with the same dialect used to send."""
        dialect = get_dialect(settings.acquirer_dialect)
        return iso8583.loads(
            raw,
            encoding=settings.acquirer_encoding or dialect.encoding,
            hex_bitmap=settings.acquirer_hex_bitmap or dialect.hex_bitmap,
            iso_config=dialect.bit_config(),
        )

    @staticmethod
    def _original_data_elements(mti: str, stan: str, sent_at: datetime) -> str:
        """DE90, n42: how the acquirer matches a reversal to what it undoes.

        Four components, in order, per Worldpay's guide and the ISO 8583 field
        table: the original MTI (4), its STAN (6), its transmission date and
        time (10, MMDDhhmmss — the DE7 value), and the acquiring and forwarding
        institution identification codes (11 each).

        The timestamp must be byte-identical to the DE7 that was sent on the
        original message, which is why both are derived from the same
        ``sent_at`` rather than from the clock at reversal time.
        """
        institution = (settings.acquirer_institution_id or "")[:11].rjust(11, "0")
        return (
            f"{mti:0>4}"
            f"{stan:0>6}"
            f"{sent_at.strftime('%m%d%H%M%S')}"
            f"{institution}"          # original acquiring institution
            f"{'0' * 11}"             # original forwarding institution
        )

    # ── Message handling ────────────────────────────────────────────────────

    async def _handle_incoming_message(self, msg_bytes: bytes) -> None:
        try:
            msg = self._decode(msg_bytes)
        except Exception as exc:
            LOGGER.error(f"Undecodable message from the acquirer: {exc}")
            return

        mti = str(msg.get("MTI", ""))
        stan = str(msg.get("DE11", "")).zfill(6)

        # The acquirer polls us too; an unanswered 0800 gets the link torn down.
        if mti == "0800":
            await self._respond_to_echo(msg)
            return

        future = self._pending_requests.pop(stan, None)
        if future is not None and not future.done():
            future.set_result(msg)
            return

        LOGGER.warning(f"Unmatched acquirer message: MTI={mti} STAN={stan}")

    async def _respond_to_echo(self, request: Dict[str, Any]) -> None:
        response = dict(request)
        response["MTI"] = "0810"
        response["DE39"] = "00"
        try:
            # Bounded for the same reason as every other send on this socket:
            # a peer that stops reading must not hold this coroutine forever.
            await asyncio.wait_for(
                self.client.send(self._encode(response)), settings.bank_timeout_sec
            )
            LOGGER.debug("Answered an acquirer echo (0800 -> 0810).")
        except asyncio.TimeoutError:
            LOGGER.error("Timed out answering an acquirer echo; the link may be half-open.")
        except Exception as exc:
            LOGGER.error(f"Could not answer the acquirer echo: {exc}")

    async def _send_and_wait(
        self, request: Dict[str, Any], stan: str, timeout: Optional[float] = None
    ) -> Dict[str, Any]:
        if not self.is_connected:
            raise ConnectionError("Acquirer link is down.")

        future = asyncio.get_running_loop().create_future()
        self._pending_requests[stan] = future
        try:
            await self.client.send(self._encode(request))
            return await asyncio.wait_for(future, timeout or settings.bank_timeout_sec)
        finally:
            self._pending_requests.pop(stan, None)

    # ── Echo heartbeat ──────────────────────────────────────────────────────

    async def _echo_loop(self) -> None:
        """Periodic 0800. Detects a half-open TCP link before a payment does."""
        while self._running:
            try:
                await asyncio.sleep(settings.bank_echo_interval_sec)
                if not self.is_connected:
                    continue
                await self.send_echo()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning(f"Acquirer echo failed: {exc}")

    async def send_echo(self) -> bool:
        stan = await self._next_stan()
        request = {
            "MTI": "0800",
            "DE11": int(stan),
            "DE12": datetime.now(timezone.utc),
            "DE70": 301,  # network management information code: echo test
        }
        # DE70 is not in every dialect; drop it rather than fail the heartbeat.
        try:
            self._encode(request)
        except Exception:
            request.pop("DE70", None)

        try:
            response = await self._send_and_wait(request, stan, timeout=5.0)
            ok = str(response.get("DE39", "00")) == "00"
            self._last_echo_ok = asyncio.get_running_loop().time() if ok else None
            return ok
        except Exception as exc:
            LOGGER.warning(f"No echo response from the acquirer: {exc}")
            return False

    # ── Authorisation ───────────────────────────────────────────────────────

    async def authorize_payment(
        self,
        pan: str,
        amount: Decimal,
        expiry: str,
        cvv: Optional[str] = None,
        currency: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send a 0200 (purchase, captured at authorisation) and return the 0210.

        Used when the acquirer does not support separate authorisation and
        capture. Prefer :meth:`authorize_only`: a 0200 takes the money before
        the crypto exists, and every failure after it needs a reversal.
        """
        return await self._authorization_exchange(
            mti="0200", pan=pan, amount=amount, expiry=expiry, cvv=cvv, currency=currency
        )

    async def _authorization_exchange(
        self,
        mti: str,
        pan: str,
        amount: Decimal,
        expiry: str,
        cvv: Optional[str] = None,
        currency: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build and send an authorisation, and wait for its response.

        Shared by the 0200 purchase and the 0100 authorisation-only flows: the
        message body is identical, only the MTI and what the acquirer does with
        it differ. Keeping one builder means a field added for certification is
        added to both by construction.

        Raises :class:`AuthorizationTimeout` when no response arrives — the
        caller must treat that as *unknown*, never as declined.
        """
        if not self.is_connected:
            raise ConnectionError("Acquirer link is down.")

        stan = await self._next_stan()
        rrn = await self._next_rrn()
        sent_at = datetime.now(timezone.utc)

        dialect = get_dialect(settings.acquirer_dialect)
        alpha = (currency or "").strip().upper() or None

        request: Dict[str, Any] = {
            "MTI": mti,
            "DE2": pan,
            "DE3": settings.acquirer_processing_code,
            # Minor units per the currency's own exponent, not a fixed factor
            # of a hundred: the yen has no minor unit and the dinar has three.
            "DE4": to_minor_units(amount, alpha) if alpha else self._to_cents(amount),
            # Mandatory, and previously absent altogether. DE90 in every
            # reversal quotes the original DE7, so without it no acquirer could
            # have matched a reversal to the authorisation it undoes.
            "DE7": sent_at,
            "DE11": int(stan),
            # DE12 is the local transaction *time* (n6); the date is DE13 (n4).
            # These were being packed together into a twelve-digit DE12, which
            # is the 1993 form and shifts every field after it on a 1987 host.
            "DE12": sent_at,
            "DE13": sent_at,
            "DE14": expiry,
            # Merchant category code, mandatory.
            "DE18": settings.acquirer_merchant_category,
            # Point of service *entry mode*: n3 or n4 depending on the dialect,
            # not the twelve-character point-of-service *data code*.
            "DE22": settings.acquirer_pos_data or dialect.default_pos_entry,
            # Identifies this as electronic commerce. Optional in the field
            # table, but its absence prices and protects the transaction as
            # something other than what it is.
            "DE25": settings.acquirer_pos_condition,
            "DE37": rrn,
            # Numeric ISO 4217, which is a different code from the alphabetic
            # one the rate feeds use. 840, not "USD".
            "DE49": numeric_code(alpha) if alpha else settings.acquirer_currency,
        }
        if settings.acquirer_institution_id:
            request["DE32"] = settings.acquirer_institution_id
        if settings.acquirer_terminal_id:
            request["DE41"] = settings.acquirer_terminal_id
        if settings.acquirer_merchant_id:
            request["DE42"] = settings.acquirer_merchant_id
        name_location = settings.acquirer_name_location or (
            card_acceptor_name_location(
                settings.acquirer_name,
                settings.acquirer_city,
                settings.acquirer_state,
                settings.acquirer_country_alpha,
            )
            if settings.acquirer_name
            else ""
        )
        if name_location:
            request["DE43"] = name_location
        if cvv and settings.acquirer_send_cvv:
            # Card-not-present authorisations are declined or downgraded without
            # CVV2. The subelement number is acquirer-specific — check your spec.
            request["PDS0052"] = cvv

        # Refuse an incomplete message here rather than learn about it from a
        # rejection: the host's answer costs a round trip and says far less.
        absent = missing_mandatory(request)
        if absent:
            raise ValueError(
                f"Authorisation is missing mandatory field(s): {', '.join(absent)}. "
                "Check the ACQUIRER_* settings against your acquirer's specification."
            )

        LOGGER.info(
            f"{mti} authorisation STAN={stan} RRN={rrn} amount={amount} "
            f"dialect={dialect.name}"
        )

        try:
            response = await self._send_and_wait(request, stan)
        except asyncio.TimeoutError:
            LOGGER.error(f"No response to {mti} STAN={stan}; the outcome is unknown.")
            raise AuthorizationTimeout(stan, rrn, sent_at)

        response.setdefault("DE11", int(stan))
        response["_stan"] = stan
        response["_rrn"] = rrn
        response["_sent_at"] = sent_at
        response["_mti"] = mti
        return response

    # ── Authorise, then capture ─────────────────────────────────────────────
    #
    # The pairing that makes a failed delivery cheap. A 0100 places a hold and
    # moves no money; a 0220 turns that hold into a debit once the crypto is
    # on-chain; a 0420 releases it if the crypto never arrives. The cardholder
    # sees a pending amount vanish rather than a refund that has to be chased.

    async def authorize_only(
        self,
        pan: str,
        amount: Decimal,
        expiry: str,
        cvv: Optional[str] = None,
        currency: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send a 0100 authorisation request and return the 0110.

        Places a hold. No money moves until :meth:`capture` is called, and the
        hold expires by itself if it never is — typically after seven days,
        which is the safety net behind this whole design.
        """
        return await self._authorization_exchange(
            mti="0100", pan=pan, amount=amount, expiry=expiry, cvv=cvv, currency=currency
        )

    async def next_stan(self) -> str:
        """Allocate a STAN without sending anything.

        Lets a caller persist the trace number *before* the message leaves, so a
        crash between sending and recording is recoverable: the stored STAN says
        an attempt was made, and the retry reconciles instead of repeating it.
        """
        return await self._next_stan()

    async def capture(
        self,
        original_stan: str,
        pan: str,
        amount: Decimal,
        original_sent_at: Optional[datetime] = None,
        original_rrn: Optional[str] = None,
        auth_code: Optional[str] = None,
        stan: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Convert a hold into a debit with a 0220, and wait for the 0230.

        Sent only after the crypto is confirmed on-chain, so the customer is
        served before they are charged. A capture against a valid authorisation
        is the reliable half of this exchange: the hold already reserved the
        funds.
        """
        result: Dict[str, Any] = {"success": False, "action_code": "", "stan": ""}

        if not self.is_connected:
            LOGGER.error(f"Acquirer link is down; cannot capture STAN={original_stan}.")
            result["action_code"] = "LINK_DOWN"
            return result

        stan = stan or await self._next_stan()
        result["stan"] = stan
        sent_at = original_sent_at or datetime.now(timezone.utc)

        capture_mti = message_type(settings.acquirer_capture_mode, "capture")
        request: Dict[str, Any] = {
            "MTI": capture_mti,
            "DE2": pan,
            "DE3": settings.acquirer_processing_code,
            "DE4": self._to_cents(amount),
            "DE11": int(stan),
            "DE12": datetime.now(timezone.utc),
            "DE49": settings.acquirer_currency,
            "DE90": self._original_data_elements(
                message_type(settings.acquirer_capture_mode, "authorize"),
                original_stan,
                sent_at,
            ),
        }
        if auth_code:
            request["DE38"] = auth_code
        if original_rrn:
            request["DE37"] = original_rrn
        if settings.acquirer_terminal_id:
            request["DE41"] = settings.acquirer_terminal_id
        if settings.acquirer_merchant_id:
            request["DE42"] = settings.acquirer_merchant_id

        LOGGER.info(
            f"{capture_mti} capture STAN={stan} for authorisation STAN={original_stan}"
        )

        try:
            response = await self._send_and_wait(request, stan)
        except asyncio.TimeoutError:
            # The crypto is already delivered. An unconfirmed capture is money
            # we may never collect, so it has to be retried, not abandoned.
            LOGGER.error(f"No response to {capture_mti} STAN={stan}; it must be retried.")
            result["action_code"] = "TIMEOUT"
            return result
        except Exception as exc:
            LOGGER.error(f"Capture STAN={stan} could not be sent: {exc}")
            result["action_code"] = "SEND_ERROR"
            return result

        action_code = str(response.get("DE39", ""))
        result["action_code"] = action_code
        result["success"] = is_reversal_approved(action_code)

        if result["success"]:
            LOGGER.info(f"Capture confirmed for authorisation STAN={original_stan}.")
        else:
            LOGGER.critical(
                f"Acquirer REFUSED the capture for STAN={original_stan} "
                f"(code {action_code}). The crypto is already delivered; this is "
                "revenue that must be collected by hand."
            )
        return result

    async def void_authorization(
        self,
        original_stan: str,
        pan: str,
        amount: Decimal,
        original_sent_at: Optional[datetime] = None,
        original_rrn: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Release a hold with a 0420, and wait for the 0430.

        The cheap failure path: nothing was debited, so nothing is owed. Even
        when this message is refused, the hold expires on its own — which is
        why a void that fails is a warning rather than the emergency a failed
        reversal is.
        """
        result: Dict[str, Any] = {"success": False, "action_code": "", "stan": ""}

        if not pan or "*" in pan:
            LOGGER.error(
                f"Refusing to void authorisation STAN={original_stan} with a masked PAN."
            )
            result["action_code"] = "NO_PAN"
            return result

        if not self.is_connected:
            LOGGER.warning(
                f"Acquirer link is down; cannot void STAN={original_stan}. "
                "The hold will expire on its own."
            )
            result["action_code"] = "LINK_DOWN"
            return result

        stan = await self._next_stan()
        result["stan"] = stan
        sent_at = original_sent_at or datetime.now(timezone.utc)

        reversal_mti = message_type(settings.acquirer_capture_mode, "reversal")
        request: Dict[str, Any] = {
            "MTI": reversal_mti,
            "DE2": pan,
            "DE3": settings.acquirer_processing_code,
            "DE4": self._to_cents(amount),
            "DE11": int(stan),
            "DE12": datetime.now(timezone.utc),
            "DE49": settings.acquirer_currency,
            "DE90": self._original_data_elements(
                message_type(settings.acquirer_capture_mode, "authorize"),
                original_stan,
                sent_at,
            ),
        }
        if original_rrn:
            request["DE37"] = original_rrn
        if settings.acquirer_terminal_id:
            request["DE41"] = settings.acquirer_terminal_id
        if settings.acquirer_merchant_id:
            request["DE42"] = settings.acquirer_merchant_id

        LOGGER.info(f"{reversal_mti} void STAN={stan} for authorisation STAN={original_stan}")

        try:
            response = await self._send_and_wait(request, stan)
        except asyncio.TimeoutError:
            LOGGER.warning(
                f"No response to {reversal_mti} STAN={stan}. The hold expires by itself, so no "
                "money is at risk; it simply stays pending on the card for longer."
            )
            result["action_code"] = "TIMEOUT"
            return result
        except Exception as exc:
            LOGGER.warning(f"Void STAN={stan} could not be sent: {exc}")
            result["action_code"] = "SEND_ERROR"
            return result

        action_code = str(response.get("DE39", ""))
        result["action_code"] = action_code
        result["success"] = is_reversal_approved(action_code)

        if result["success"]:
            LOGGER.info(f"Authorisation STAN={original_stan} voided; no debit occurred.")
        else:
            LOGGER.warning(
                f"Acquirer refused the void for STAN={original_stan}: "
                f"{describe(action_code)}. The hold expires on its own, so no money "
                "is at risk — it simply stays pending on the card for longer."
            )
        return result

    # ── Reversal ────────────────────────────────────────────────────────────

    async def reverse_payment(
        self,
        original_stan: str,
        pan: str,
        amount: Decimal,
        original_sent_at: Optional[datetime] = None,
        original_rrn: Optional[str] = None,
        original_mti: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send a 0400 and wait for the 0410.

        Returns ``{"success": bool, "action_code": str, "stan": str}``. The PAN
        must be the real card number: an acquirer cannot match a masked one.
        """
        result: Dict[str, Any] = {"success": False, "action_code": "", "stan": ""}

        if not pan or "*" in pan:
            LOGGER.critical(
                f"Refusing to send a reversal for STAN={original_stan} with a masked PAN. "
                "The cardholder must be refunded by hand."
            )
            result["action_code"] = "NO_PAN"
            return result

        if not self.is_connected:
            LOGGER.error(f"Acquirer link is down; cannot reverse STAN={original_stan}.")
            result["action_code"] = "LINK_DOWN"
            return result

        stan = await self._next_stan()
        result["stan"] = stan
        sent_at = original_sent_at or datetime.now(timezone.utc)

        reversal_mti = message_type(settings.acquirer_capture_mode, "reversal")
        request: Dict[str, Any] = {
            "MTI": reversal_mti,
            "DE2": pan,
            "DE3": settings.acquirer_processing_code,
            "DE4": self._to_cents(amount),
            "DE11": int(stan),
            "DE12": datetime.now(timezone.utc),
            "DE49": settings.acquirer_currency,
            "DE90": self._original_data_elements(
                original_mti or message_type(settings.acquirer_capture_mode, "authorize"),
                original_stan,
                sent_at,
            ),
        }
        if original_rrn:
            request["DE37"] = original_rrn
        if settings.acquirer_terminal_id:
            request["DE41"] = settings.acquirer_terminal_id
        if settings.acquirer_merchant_id:
            request["DE42"] = settings.acquirer_merchant_id

        LOGGER.warning(
            f"{reversal_mti} reversal STAN={stan} for original STAN={original_stan}"
        )

        try:
            response = await self._send_and_wait(request, stan)
        except asyncio.TimeoutError:
            LOGGER.error(f"No response to {reversal_mti} STAN={stan}; it must be retried.")
            result["action_code"] = "TIMEOUT"
            return result
        except Exception as exc:
            LOGGER.error(f"Reversal STAN={stan} could not be sent: {exc}")
            result["action_code"] = "SEND_ERROR"
            return result

        action_code = str(response.get("DE39", ""))
        result["action_code"] = action_code
        result["success"] = is_reversal_approved(action_code)

        if result["success"]:
            LOGGER.info(f"Reversal confirmed by the acquirer ({describe(action_code)}).")
        else:
            LOGGER.critical(
                f"Acquirer REFUSED the reversal for STAN={original_stan}: "
                f"{describe(action_code)}. The cardholder has NOT been refunded; "
                "this needs a manual refund."
            )
            if indicates_misconfiguration(action_code):
                LOGGER.critical(
                    "That code points at our own configuration, not the issuer — "
                    "every reversal will fail the same way until it is fixed."
                )
        return result

    # ── Health ──────────────────────────────────────────────────────────────

    async def health(self) -> dict:
        return {
            "connected": self.is_connected,
            "host": f"{self.host}:{self.port}",
            "tls": bool(self.ssl_context),
            "pending_requests": len(self._pending_requests),
        }
