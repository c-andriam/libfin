"""
PayMeGate driver tests.

These cover the two PayMeGate-specific entry points without a live network:

* ``POST /pay`` in PayMeGate mode — creates a remote order and returns its
  ``checkoutUrl`` to redirect the customer, without touching the ISO 8583 link
  or the hot wallet.
* ``POST /webhook/paymegate`` — verifies the HMAC signature and settles the
  transaction to ``CRYPTO_SENT`` on an ``order.paid`` event.

The signature test matters most: PayMeGate signs the raw body, and settling on
an unsigned request would let anyone mark an order paid.
"""

import base64
import hmac
import json
import time
import uuid

import pytest

from gateway.models import TransactionStatus

APPROVED_PAN = "4111111111111111"
WALLET = "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
SECRET = "test-paymegate-webhook-secret"


def sign_payload(
    payload: dict, secret: str = SECRET, *, timestamp: int | None = None
) -> tuple[bytes, str, str, str]:
    """Serialise a webhook payload and sign it exactly as PayMeGate does.

    Returns ``(raw_body, signature, timestamp, event_id)``. The signature is an
    unpadded-base64url HMAC-SHA256 over ``"{timestamp}.{raw_body}"``, per the
    documented ``X-Paymegate-Signature`` contract.
    """
    ts = str(timestamp if timestamp is not None else int(time.time()))
    body = json.dumps(payload).encode()
    message = f"{ts}.".encode() + body
    digest = hmac.new(secret.encode(), message, "sha256").digest()
    signature = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return body, signature, ts, str(uuid.uuid4())


async def make_paymegate_request(gateway_client, monkeypatch, *, expiry) -> tuple[object, str]:
    """Drive POST /pay through the PayMeGate path with a stubbed client.

    Returns ``(response, order_uuid)``. Each call draws a fresh order UUID so
    the shared in-memory database never trips the unique constraint on
    ``paymegate_order_id``.
    """
    order_uuid = str(uuid.uuid4())
    checkout_url = f"https://www.paymegate.com/pay/{order_uuid}"

    async def fake_create_order(**kwargs):
        return {
            "orderUUID": order_uuid,
            "checkoutUrl": checkout_url,
            "status": "UNPAID",
            "amount": kwargs["amount"],
            "currency": kwargs["currency"],
        }

    from gateway import api

    monkeypatch.setattr(api.settings, "acquirer", "paymegate")

    class _FakePayMeGateClient:
        create_order = staticmethod(fake_create_order)

        async def get_order(self, order_uuid):
            return {"orderUUID": order_uuid, "status": "PAID"}

    monkeypatch.setattr(api, "PayMeGateClient", _FakePayMeGateClient)

    body = {
        "pan": APPROVED_PAN,
        "expiry": expiry,
        "cvv": "123",
        "amount": "50.00",
        "currency": "USD",
        "target_wallet": WALLET,
    }
    response = await gateway_client.post("/pay", json=body)
    return response, order_uuid


@pytest.mark.asyncio(loop_scope="function")
async def test_paymegate_pay_returns_checkout_url(gateway_client, monkeypatch, expiry):
    response, order_uuid = await make_paymegate_request(
        gateway_client, monkeypatch, expiry=expiry
    )

    # POST /pay always answers 202 across the gateway, even in PayMeGate mode.
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["checkout_url"] == f"https://www.paymegate.com/pay/{order_uuid}"
    assert body["order_uuid"] == order_uuid
    assert body["status"] == "PENDING"

    from sqlalchemy import select

    from gateway.database import async_session
    from gateway.models import Transaction

    async with async_session() as session:
        tx = (
            await session.execute(select(Transaction).where(Transaction.id == body["transaction_id"]))
        ).scalar_one()
        assert tx.paymegate_order_id == order_uuid
        assert tx.status is TransactionStatus.PENDING


@pytest.mark.asyncio(loop_scope="function")
async def test_paymegate_webhook_settles_order_paid(gateway_client, monkeypatch, expiry):
    # Create the order first so there is a row to settle.
    response, order_uuid = await make_paymegate_request(
        gateway_client, monkeypatch, expiry=expiry
    )

    from gateway import api

    monkeypatch.setattr(api.settings, "paymegate_webhook_secret", SECRET)

    payload = {
        "id": "evt_123",
        "type": "order.paid",
        "createdAt": "2026-09-03T12:00:00Z",
        "orderUUID": order_uuid,
        "transactionUUID": "txn_123",
        "transactionRef": "ref_123",
        "status": "PAID",
        "amount": "50.00",
        "currency": "USD",
        "customerEmail": "buyer@example.com",
        "externalId": "libfin-1",
        "paidAt": "2026-09-03T12:00:01Z",
    }
    body, signature, timestamp, event_id = sign_payload(payload)
    response = await gateway_client.post(
        "/webhook/paymegate",
        content=body,
        headers={
            "X-Paymegate-Signature": signature,
            "X-Paymegate-Timestamp": timestamp,
            "X-Paymegate-Event": "order.paid",
            "X-Paymegate-Event-Id": event_id,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["action"] == "settled"

    from sqlalchemy import select

    from gateway.database import async_session
    from gateway.models import Transaction

    async with async_session() as session:
        tx = (
            await session.execute(select(Transaction).where(Transaction.paymegate_order_id == order_uuid))
        ).scalar_one()
        assert tx.status is TransactionStatus.CRYPTO_SENT
        assert tx.completed_at is not None, "a settled payment must be stamped"


@pytest.mark.asyncio(loop_scope="function")
async def test_paymegate_webhook_rejects_bad_signature(gateway_client, monkeypatch, expiry):
    response, order_uuid = await make_paymegate_request(
        gateway_client, monkeypatch, expiry=expiry
    )

    from gateway import api

    monkeypatch.setattr(api.settings, "paymegate_webhook_secret", SECRET)

    payload = {
        "id": "evt_123",
        "type": "order.paid",
        "status": "PAID",
        "orderUUID": order_uuid,
        "amount": "50.00",
        "currency": "USD",
    }
    body, _, timestamp, event_id = sign_payload(payload, secret="different-secret")
    response = await gateway_client.post(
        "/webhook/paymegate",
        content=body,
        headers={
            "X-Paymegate-Signature": "wrong",
            "X-Paymegate-Timestamp": timestamp,
            "X-Paymegate-Event": "order.paid",
            "X-Paymegate-Event-Id": event_id,
        },
    )

    assert response.status_code == 401, response.text

    from sqlalchemy import select

    from gateway.database import async_session
    from gateway.models import Transaction

    async with async_session() as session:
        tx = (
            await session.execute(select(Transaction).where(Transaction.paymegate_order_id == order_uuid))
        ).scalar_one()
        assert tx.status is TransactionStatus.PENDING, "a bad signature must not settle"


def _b64url_bare(digest: bytes) -> str:
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _payload_signature(body: bytes, secret: str, timestamp: str) -> str:
    message = f"{timestamp}.".encode() + body
    return _b64url_bare(hmac.new(secret.encode(), message, "sha256").digest())


def test_verify_webhook_signature_constant_time():
    from gateway.paymegate import verify_webhook_signature

    body = b'{"status":"PAID"}'
    timestamp = str(int(time.time()))
    signature = _payload_signature(body, SECRET, timestamp)

    # A valid, freshly-timestamped signature passes.
    assert verify_webhook_signature(
        body, signature, secret=SECRET, timestamp_header=timestamp
    ) is True

    # A signature computed over the bare body (not "{ts}.{body}") must fail —
    # this is PayMeGate's documented contract and the exact format is required.
    bare = _b64url_bare(hmac.new(SECRET.encode(), body, "sha256").digest())
    assert verify_webhook_signature(
        body, bare, secret=SECRET, timestamp_header=timestamp
    ) is False

    # A stale timestamp is rejected (replay protection).
    old = str(int(time.time()) - 3600)
    assert verify_webhook_signature(
        body, _payload_signature(body, SECRET, old), secret=SECRET, timestamp_header=old
    ) is False

    # A signature made with the wrong secret is rejected (constant-time detect).
    wrong = _payload_signature(body, "other-secret", timestamp)
    assert verify_webhook_signature(
        body, wrong, secret=SECRET, timestamp_header=timestamp
    ) is False

    # Hex encoding is not accepted — PayMeGate uses unpadded base64url.
    hex_sig = hmac.new(
        SECRET.encode(), f"{timestamp}.".encode() + body, "sha256"
    ).hexdigest()
    assert verify_webhook_signature(
        body, hex_sig, secret=SECRET, timestamp_header=timestamp
    ) is False

    # Missing signature/timestamp/secret are all rejected.
    assert verify_webhook_signature(body, None, secret=SECRET, timestamp_header=timestamp) is False
    assert verify_webhook_signature(body, signature, secret=SECRET, timestamp_header=None) is False
    assert verify_webhook_signature(body, signature, secret="", timestamp_header=timestamp) is False


# ---------------------------------------------------------------------------
# Public checkout page (GET /checkout + POST /checkout/pay)
# ---------------------------------------------------------------------------


async def _fake_paymegate(monkeypatch):
    """Stub the PayMeGate client and switch the acquirer to PayMeGate mode."""
    order_uuid = str(uuid.uuid4())
    checkout_url = f"https://www.paymegate.com/pay/{order_uuid}"

    async def fake_create_order(**kwargs):
        return {
            "orderUUID": order_uuid,
            "checkoutUrl": checkout_url,
            "status": "UNPAID",
            "amount": kwargs["amount"],
            "currency": kwargs["currency"],
        }

    from gateway import api

    monkeypatch.setattr(api.settings, "acquirer", "paymegate")

    class _FakeClient:
        create_order = staticmethod(fake_create_order)

        async def get_order(self, order_uuid):
            return {"orderUUID": order_uuid, "status": "PAID"}

    monkeypatch.setattr(api, "PayMeGateClient", _FakeClient)
    return order_uuid, checkout_url


@pytest.mark.asyncio(loop_scope="function")
async def test_checkout_page_is_served_publicly(gateway_client):
    # Public (no API key): the page is meant to be opened by a payer's browser.
    response = await gateway_client.get("/checkout")
    assert response.status_code == 200, response.text
    assert "text/html" in response.headers["content-type"]
    assert "Paiement" in response.text


@pytest.mark.asyncio(loop_scope="function")
async def test_checkout_pay_returns_checkout_url(gateway_client, monkeypatch):
    order_uuid, checkout_url = await _fake_paymegate(monkeypatch)

    response = await gateway_client.post(
        "/checkout/pay", json={"amount": "25.00", "currency": "USD"}
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["checkout_url"] == checkout_url
    assert body["order_uuid"] == order_uuid
    assert body["status"] == "PENDING"


@pytest.mark.asyncio(loop_scope="function")
async def test_checkout_pay_rejects_invalid_amount(gateway_client, monkeypatch):
    await _fake_paymegate(monkeypatch)

    # Zero/negative is refused by the model.
    response = await gateway_client.post(
        "/checkout/pay", json={"amount": "0.00", "currency": "USD"}
    )
    assert response.status_code == 422, response.text

    # Above AMOUNT_MAX is refused by the model.
    response = await gateway_client.post(
        "/checkout/pay", json={"amount": "999999.00", "currency": "USD"}
    )
    assert response.status_code == 422, response.text

    # An unsupported currency is refused too.
    response = await gateway_client.post(
        "/checkout/pay", json={"amount": "25.00", "currency": "XXX"}
    )
    assert response.status_code == 422, response.text
