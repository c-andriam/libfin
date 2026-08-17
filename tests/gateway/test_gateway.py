"""
Gateway behaviour tests.

The emphasis is on the paths where money can go missing: a declined card, an
acquirer that never answers, a crypto transfer that fails after the fiat was
captured. The happy path is the easy one.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from gateway.models import TransactionStatus

pytestmark = pytest.mark.asyncio(loop_scope="function")

APPROVED_PAN = "4111111111111111"
DECLINED_PAN = "4000000000000002"
SILENT_PAN = "4000000000000028"
WALLET = "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"


def payload(expiry: str, pan: str = APPROVED_PAN, amount: str = "50.00") -> dict:
    return {
        "pan": pan,
        "expiry": expiry,
        "cvv": "123",
        "amount": amount,
        "target_wallet": WALLET,
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_successful_payment_returns_202(gateway_client, expiry):
    """Fiat is approved and the crypto leg is handed to a worker."""
    with patch("gateway.worker.process_crypto_transfer") as task:
        task.apply_async = MagicMock()

        response = await gateway_client.post(
            "/pay", json=payload(expiry), headers={"Idempotency-Key": "happy-001"}
        )

        assert response.status_code == 202, response.text
        body = response.json()
        assert body["status"] == "FIAT_APPROVED"
        assert body["stan"]
        task.apply_async.assert_called_once()


async def test_payment_does_not_send_the_pan_to_the_worker(gateway_client, expiry):
    """Card data must never enter a broker payload (PCI-DSS)."""
    with patch("gateway.worker.process_crypto_transfer") as task:
        task.apply_async = MagicMock()

        await gateway_client.post(
            "/pay", json=payload(expiry), headers={"Idempotency-Key": "no-pan-001"}
        )

        _, kwargs = task.apply_async.call_args
        queued = str(kwargs)
        assert APPROVED_PAN not in queued
        assert set(kwargs["kwargs"]) == {"tx_id"}


async def test_masked_pan_only_is_persisted(gateway_client, expiry):
    with patch("gateway.worker.process_crypto_transfer") as task:
        task.apply_async = MagicMock()
        response = await gateway_client.post(
            "/pay", json=payload(expiry), headers={"Idempotency-Key": "mask-001"}
        )
        tx_id = response.json()["transaction_id"]

    status = (await gateway_client.get(f"/transaction/{tx_id}")).json()
    assert status["masked_pan"] == "411111******1111"
    assert APPROVED_PAN not in str(status)


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_idempotency_prevents_double_charge(gateway_client, expiry):
    with patch("gateway.worker.process_crypto_transfer") as task:
        task.apply_async = MagicMock()
        headers = {"Idempotency-Key": "idem-002"}

        first = await gateway_client.post("/pay", json=payload(expiry), headers=headers)
        second = await gateway_client.post("/pay", json=payload(expiry), headers=headers)

        assert first.status_code == 202
        assert second.status_code == 202
        assert second.json()["transaction_id"] == first.json()["transaction_id"]
        # The bank must have been asked exactly once.
        assert task.apply_async.call_count == 1


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


async def test_luhn_rejects_invalid_pan(gateway_client, expiry):
    response = await gateway_client.post(
        "/pay", json=payload(expiry, pan="4111111111111112")
    )
    assert response.status_code == 400
    assert "Invalid card number" in response.json()["detail"]


async def test_expired_card_is_rejected(gateway_client):
    response = await gateway_client.post("/pay", json=payload("2001"))
    assert response.status_code == 422


async def test_amount_above_the_ceiling_is_rejected(gateway_client, expiry):
    response = await gateway_client.post(
        "/pay", json=payload(expiry, amount="999999.00")
    )
    assert response.status_code == 422


async def test_malformed_wallet_is_rejected(gateway_client, expiry):
    body = payload(expiry)
    body["target_wallet"] = "not-an-address"
    assert (await gateway_client.post("/pay", json=body)).status_code == 422


# ---------------------------------------------------------------------------
# Bank refusals and silence
# ---------------------------------------------------------------------------


async def test_declined_card_returns_400_and_does_not_queue_crypto(gateway_client, expiry):
    with patch("gateway.worker.process_crypto_transfer") as task:
        task.apply_async = MagicMock()

        response = await gateway_client.post(
            "/pay", json=payload(expiry, pan=DECLINED_PAN), headers={"Idempotency-Key": "dec-1"}
        )

        assert response.status_code == 400
        task.apply_async.assert_not_called()


async def test_acquirer_silence_is_treated_as_unknown_not_declined(gateway_client, expiry):
    """A missing 0210 means we may have debited the cardholder: reverse, don't shrug."""
    with patch("gateway.worker.retry_reversal") as reversal:
        reversal.apply_async = MagicMock()

        response = await gateway_client.post(
            "/pay", json=payload(expiry, pan=SILENT_PAN), headers={"Idempotency-Key": "silent-1"}
        )

        assert response.status_code == 504
        reversal.apply_async.assert_called_once()

    from sqlalchemy import select

    from gateway.database import async_session
    from gateway.models import Transaction

    async with async_session() as session:
        tx = (
            await session.execute(
                select(Transaction).where(Transaction.idempotency_key == "silent-1")
            )
        ).scalar_one()
        assert tx.status is TransactionStatus.FIAT_UNKNOWN


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


async def test_circuit_breaker_rejects_when_open(gateway_client, expiry):
    from gateway.circuit_breaker import CircuitState, web3_circuit_breaker

    for _ in range(web3_circuit_breaker.failure_threshold):
        web3_circuit_breaker.record_failure()
    assert web3_circuit_breaker.state is CircuitState.OPEN

    response = await gateway_client.post("/pay", json=payload(expiry))

    assert response.status_code == 503
    # The customer must be told nothing was taken.
    assert "No funds were debited" in response.json()["detail"]


async def test_circuit_breaker_fails_closed_when_redis_is_unreachable():
    """During an infrastructure incident the breaker must refuse, not wave through."""
    from gateway.circuit_breaker import Web3CircuitBreaker

    breaker = Web3CircuitBreaker(fail_closed=True)
    with patch.object(breaker.redis, "get", side_effect=ConnectionError("redis is down")):
        assert breaker.can_execute() is False

    lenient = Web3CircuitBreaker(fail_closed=False)
    with patch.object(lenient.redis, "get", side_effect=ConnectionError("redis is down")):
        assert lenient.can_execute() is True


async def test_only_one_probe_passes_in_half_open_state():
    from gateway.circuit_breaker import Web3CircuitBreaker

    breaker = Web3CircuitBreaker(failure_threshold=1, recovery_timeout_sec=0)
    breaker.reset()
    breaker.record_failure()

    assert breaker.can_execute() is True   # the probe
    assert breaker.can_execute() is False  # everyone else waits


# ---------------------------------------------------------------------------
# Status endpoint
# ---------------------------------------------------------------------------


async def test_get_transaction_status(gateway_client, expiry):
    with patch("gateway.worker.process_crypto_transfer") as task:
        task.apply_async = MagicMock()
        created = await gateway_client.post(
            "/pay", json=payload(expiry), headers={"Idempotency-Key": "status-003"}
        )
        tx_id = created.json()["transaction_id"]

    response = await gateway_client.get(f"/transaction/{tx_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == tx_id
    assert body["status"] == "FIAT_APPROVED"
    assert Decimal(body["amount"]) == Decimal("50.00")


async def test_unknown_transaction_returns_404(gateway_client):
    assert (await gateway_client.get("/transaction/999999")).status_code == 404


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


async def test_health_is_public_and_shallow(gateway_client):
    response = await gateway_client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Rate-limit identity
# ---------------------------------------------------------------------------


def _request_from(peer: str, forwarded: str = None):
    """Minimal stand-in for a Starlette request."""
    from types import SimpleNamespace

    return SimpleNamespace(
        client=SimpleNamespace(host=peer),
        headers={"X-Forwarded-For": forwarded} if forwarded else {},
    )


async def test_forwarded_header_ignored_from_an_untrusted_peer():
    """An untrusted peer must not be able to pick its own rate-limit bucket."""
    from gateway import api
    from gateway.config import settings

    original = settings.trusted_proxies
    settings.trusted_proxies = ["10.0.0.1"]
    try:
        # The proxy we trust: believe what it forwards.
        assert api.client_identity(_request_from("10.0.0.1", "203.0.113.7")) == "203.0.113.7"
        # Anyone else: their forged header is ignored, they are keyed on the socket.
        assert api.client_identity(_request_from("198.51.100.9", "203.0.113.7")) == "198.51.100.9"
    finally:
        settings.trusted_proxies = original


async def test_trusted_proxy_accepts_cidr_ranges():
    from gateway import api
    from gateway.config import settings

    original = settings.trusted_proxies
    settings.trusted_proxies = ["10.89.0.0/16"]
    try:
        assert api.client_identity(_request_from("10.89.4.3", "203.0.113.7")) == "203.0.113.7"
        assert api.client_identity(_request_from("10.90.4.3", "203.0.113.7")) == "10.90.4.3"
    finally:
        settings.trusted_proxies = original


# ---------------------------------------------------------------------------
# Gas ceiling
# ---------------------------------------------------------------------------


async def test_fee_ceiling_refuses_a_spike_and_is_retryable():
    """A fee spike must not be paid in full, and must not be a hard failure."""
    from unittest.mock import MagicMock

    from gateway.config import settings
    from gateway.crypto_service import CryptoService, GasPriceTooHigh
    from gateway.worker import _is_transient

    service = CryptoService.__new__(CryptoService)
    service.w3 = MagicMock()
    # 5000 gwei base fee — a serious spike.
    service.w3.eth.get_block.return_value = {"baseFeePerGas": 5000 * 10**9}
    service.w3.eth.max_priority_fee = 2 * 10**9
    service.w3.to_wei = lambda value, unit: value * 10**9

    original = settings.web3_max_fee_gwei
    settings.web3_max_fee_gwei = Decimal("200")
    try:
        with pytest.raises(GasPriceTooHigh):
            service._fee_parameters()

        # Calm market: fees are built normally.
        service.w3.eth.get_block.return_value = {"baseFeePerGas": 10 * 10**9}
        fees = service._fee_parameters()
        assert fees["maxFeePerGas"] <= 200 * 10**9
        assert fees["maxPriorityFeePerGas"] <= fees["maxFeePerGas"]
    finally:
        settings.web3_max_fee_gwei = original

    # Waiting for cheaper gas is the right response; giving up is not.
    assert _is_transient(GasPriceTooHigh("too expensive")) is True


async def test_insufficient_funds_is_never_retried():
    from gateway.crypto_service import InsufficientFunds
    from gateway.worker import _is_transient

    assert _is_transient(InsufficientFunds("empty wallet")) is False


# ---------------------------------------------------------------------------
# Refusing what cannot be delivered
# ---------------------------------------------------------------------------


async def test_payment_refused_when_the_gateway_cannot_sign(gateway_client, expiry):
    """A sealed Vault must stop payments, not produce charge-then-refund cycles."""
    from gateway import api

    api._signing_check.update({"ok": False, "checked_at": 0.0, "reason": "test"})

    with patch("gateway.api.can_sign", return_value=(False, "no hot wallet key")):
        with patch("gateway.worker.process_crypto_transfer") as task:
            task.apply_async = MagicMock()

            response = await gateway_client.post(
                "/pay", json=payload(expiry), headers={"Idempotency-Key": "nosign-1"}
            )

            assert response.status_code == 503
            assert "No funds were debited" in response.json()["detail"]
            # The bank must not have been asked at all.
            task.apply_async.assert_not_called()

    api._signing_check.update({"ok": False, "checked_at": 0.0, "reason": "reset"})


async def test_signing_check_reports_a_missing_key():
    from gateway import api
    from gateway.crypto_service import CryptoService

    api._signing_check.update({"ok": False, "checked_at": 0.0, "reason": "reset"})

    def keyless(*args, **kwargs):
        service = CryptoService.__new__(CryptoService)
        service.account = None
        return service

    with patch("gateway.crypto_service.CryptoService", side_effect=keyless):
        ok, reason = await api.can_sign()
        assert ok is False
        assert "Vault" in reason or "key" in reason

    api._signing_check.update({"ok": False, "checked_at": 0.0, "reason": "reset"})


# ---------------------------------------------------------------------------
# ISO 8583 dialect
# ---------------------------------------------------------------------------


async def test_both_bitmap_formats_round_trip():
    """Bitmap encoding is per-acquirer, so both forms must work.

    Cross-checking libfin against an independent ISO 8583 implementation is what
    turned this from an unexamined library default into a setting: a host that
    expects hex and receives binary rejects every message.
    """
    from datetime import datetime, timezone

    from libfin import iso8583

    message = {
        "MTI": "0200",
        "DE2": "4111111111111111",
        "DE3": "000000",
        "DE4": 2500,
        "DE11": 123456,
        "DE12": datetime(2026, 8, 17, 12, 34, 56, tzinfo=timezone.utc),
        "DE14": "3012",
        "DE22": "810101Y00000",
        "DE37": "026229000001",
        "DE41": "TERM0001",
        "DE42": "MERCHANT0000001",
        "DE49": "840",
        "PDS0052": "123",
    }

    for hex_bitmap in (False, True):
        raw = iso8583.dumps(message, hex_bitmap=hex_bitmap)
        back = iso8583.loads(raw, hex_bitmap=hex_bitmap)
        assert back["DE2"] == "4111111111111111"
        assert back["DE4"] == 2500
        assert back["DE22"] == "810101Y00000"
        assert back["DE37"] == "026229000001"
        assert back["DE41"] == "TERM0001"
        assert back["DE42"] == "MERCHANT0000001"
        assert back["PDS0052"] == "123", f"CVV2 lost with hex_bitmap={hex_bitmap}"

    # The two encodings must not produce the same bytes, or the setting is inert.
    assert iso8583.dumps(message) != iso8583.dumps(message, hex_bitmap=True)


async def test_acquirer_encodes_with_the_configured_dialect():
    from gateway.acquirer import AcquirerService
    from gateway.config import settings

    message = {"MTI": "0800", "DE11": 1}

    original = settings.acquirer_hex_bitmap
    try:
        settings.acquirer_hex_bitmap = False
        binary = AcquirerService._encode(message)
        settings.acquirer_hex_bitmap = True
        hexed = AcquirerService._encode(message)
    finally:
        settings.acquirer_hex_bitmap = original

    assert binary != hexed
    # A hex bitmap is 32 printable characters; a binary one is 16 raw bytes.
    assert len(hexed) > len(binary)


async def test_reversal_original_data_elements_are_42_digits():
    """DE90 is a fixed n42 block; an acquirer rejects any other length."""
    from datetime import datetime, timezone

    from gateway.acquirer import AcquirerService

    de90 = AcquirerService._original_data_elements(
        "0200", "000123", datetime(2026, 8, 17, 12, 34, 56, tzinfo=timezone.utc)
    )
    assert len(de90) == 42
    assert de90.isdigit()
    assert de90.startswith("0200000123")
