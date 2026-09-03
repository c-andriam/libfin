"""
PayMeGate acquirer driver.

PayMeGate (https://paymegate.com) is a hosted-checkout orchestrator: you create
an order server-to-server, share the returned ``checkoutUrl`` with the customer,
and PayMeGate collects the payment (card, wallet, or crypto) on its own page.
After a verified settlement it pays the crypto out to the wallet you configured
over ``PUT /v1/wallet``, and notifies you with a signed ``order.paid`` webhook.

This driver is a deliberate counterpart to :mod:`gateway.acquirer`: it talks to
PayMeGate's REST API instead of an ISO 8583 host, and it owns **no** crypto —
PayMeGate settles straight to your configured wallet, so there is no hot wallet,
no nonce bookkeeping, and no on-chain capture to reconcile here. The payment's
outcome is reported by the webhook rather than by a synchronous authorisation
response.

Key properties
--------------
* Server-to-server only. The merchant API key never leaves the backend.
* The order UUID is the correlation handle; like a STAN it must be persisted
  before the order is created, so a crash between creating the order and
  recording it is recoverable.
* The webhook carries a signature (unpadded-base64url HMAC-SHA256 over
  ``"{timestamp}.{raw_body}"``, in the ``X-Paymegate-Signature`` header) which
  must be verified before any state is changed — otherwise anyone could mark an
  order paid, or leak state. The timestamp prefixes the signed message, so a
  freshly-signed delivery cannot be replayed once the window closes.
"""

from __future__ import annotations

import base64
import hmac
import logging
import time
from decimal import Decimal

import httpx

from gateway.config import settings

LOGGER = logging.getLogger(__name__)


class PayMeGateError(RuntimeError):
    """A request to PayMeGate could not be completed."""


class PayMeGateOrderError(PayMeGateError):
    """PayMeGate returned a non-2xx status for the order request."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"PayMeGate error {status}: {detail}")
        self.status = status
        self.detail = detail


class PayMeGateClient:
    """Thin HTTP client for the PayMeGate merchant API."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.base_url = (base_url or settings.paymegate_base_url).rstrip("/")
        self.api_key = api_key or settings.paymegate_api_key
        self._timeout = timeout

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "X-API-Key": self.api_key,
                "Content-Type": "application/json",
            },
            timeout=self._timeout,
        )

    async def _request(self, method: str, path: str, json: dict | None = None) -> dict:
        async with self._client() as client:
            try:
                resp = await client.request(method, path, json=json)
            except httpx.HTTPError as exc:
                raise PayMeGateError(f"PayMeGate unreachable: {exc}") from exc

        if resp.status_code >= 400:
            # FastAPI-style error bodies carry a "detail" string.
            detail = ""
            try:
                body = resp.json()
                detail = str(body.get("detail", body))
            except Exception:
                detail = resp.text or str(resp.status_code)
            raise PayMeGateOrderError(resp.status_code, detail)

        return resp.json()

    async def create_order(
        self,
        *,
        amount: Decimal,
        currency: str,
        external_id: str | None = None,
        customer_email: str,
        customer_name: str | None = None,
        payment_methods: str | None = None,
        return_url: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        """Create an UNPAID order and return the checkout URL.

        The returned ``checkoutUrl`` is the only URL to share with the customer.
        """
        methods = payment_methods or settings.paymegate_payment_methods or "*"
        methods_key = methods.strip().strip("[]")
        methods_list = [
            m.strip() for m in methods_key.split(",") if m.strip()
        ] or ["*"]

        body: dict = {
            "amount": f"{amount:.2f}",
            "currency": currency.upper(),
            "paymentMethodsKeys": methods_list,
            "customer": {
                "email": customer_email,
                **({"fullName": customer_name} if customer_name else {}),
            },
        }
        if external_id:
            body["externalId"] = external_id
        if return_url or settings.paymegate_return_url:
            body["returnUrl"] = return_url or settings.paymegate_return_url
        if metadata:
            body["metadata"] = metadata

        data = await self._request("POST", "/v1/orders", json=body)
        # The API wraps results in {"success": true, "data": {...}}.
        payload = data.get("data", data)
        return payload

    async def get_order(self, order_uuid: str) -> dict:
        """Read a single order's current status."""
        data = await self._request("GET", f"/v1/orders/{order_uuid}")
        return data.get("data", data)

    async def update_wallet(
        self,
        *,
        evm: str | None = None,
        trc20: str | None = None,
        include_crypto: bool = True,
    ) -> dict:
        """Configure the crypto payout networks to the receiving wallet.

        ``evm`` accepts an 0x address (Ethereum/Base/BNB/Arbitrum/Avalanche);
        ``trc20`` accepts a T.. address (TRON). PayMeGate settles the collected
        crypto to the addresses you set here.
        """
        wallets: dict = {}
        if evm:
            wallets["evm"] = evm
        if trc20:
            wallets["trc20"] = trc20
        if not wallets:
            raise PayMeGateError("Provide at least one payout wallet address.")

        data = await self._request(
            "PUT",
            "/v1/wallet",
            json={"includeCrypto": include_crypto, "cryptoWallets": wallets},
        )
        return data.get("data", data)

    async def health(self) -> dict:
        """Cheap reachability probe (the API has no public liveness route)."""
        try:
            async with self._client() as client:
                resp = await client.get("/v1/payment-methods")
            ok = resp.status_code < 500
            return {"reachable": ok, "status_code": resp.status_code}
        except Exception as exc:  # noqa: BLE001
            return {"reachable": False, "error": str(exc)}


def verify_webhook_signature(
    body: bytes,
    signature_header: str | None,
    secret: str | None = None,
    timestamp_header: str | None = None,
    *,
    max_age_sec: int = 300,
) -> bool:
    """Verify the PayMeGate ``order.paid`` webhook signature.

    PayMeGate signs ``"{timestamp}.{raw_body}"`` with HMAC-SHA256, then encodes
    the digest as **unpadded Base64url**. The four relevant headers are:

      * ``X-Paymegate-Signature``  unpadded base64url HMAC-SHA256
      * ``X-Paymegate-Timestamp``  unix seconds, the prefix of the signed message
      * ``X-Paymegate-Event``      event type (``order.paid``)
      * ``X-Paymegate-Event-Id``   idempotency key

    The timestamp is part of the signed payload, so a signature computed over
    any other string (for example just the body) will not match — a legitimate
    trade-off: PayMeGate's exact contract is verified, rather than accepting
    whichever format happens to pass. The timestamp also bounds replay age.

    Comparison is constant-time, so a timing attack cannot recover the secret.
    """
    secret_value = secret or settings.paymegate_webhook_secret
    if not secret_value or not signature_header:
        return False

    # The signature is a fixed-length unpadded base64url digest.
    signature = signature_header.strip()
    if not signature:
        return False
    try:
        expected = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    except (ValueError, TypeError):
        return False

    # A fresh signature is required: the timestamp is in the signed message, so
    # an old event cannot be replayed once max_age_sec has passed.
    if not timestamp_header or not timestamp_header.strip().isdigit():
        return False
    if abs(int(timestamp_header) - int(time.time())) > max_age_sec:
        return False

    message = f"{timestamp_header.strip()}.{body.decode('utf-8', 'surrogateescape')}".encode()
    actual = hmac.new(secret_value.encode(), message, "sha256").digest()
    return hmac.compare_digest(actual, expected)
