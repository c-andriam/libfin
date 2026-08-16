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
