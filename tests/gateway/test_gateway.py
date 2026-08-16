import asyncio
import pytest
import pytest_asyncio
import httpx
from unittest.mock import patch, MagicMock
from gateway.api import app
from gateway.database import init_db

pytestmark = pytest.mark.asyncio(loop_scope="session")

@pytest_asyncio.fixture
async def mock_bank_server():
    process = await asyncio.create_subprocess_exec(
        "python", "tests/simulator/bank_server.py",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.sleep(1)
    yield process
    process.terminate()
    await process.wait()

# ---------------------------------------------------------------------------
# Test 1: Successful Payment (202 Accepted + Celery dispatch)
# ---------------------------------------------------------------------------

async def test_successful_payment_returns_202(mock_bank_server):
    """API must approve fiat and return 202 immediately (crypto is async)."""
    with patch("gateway.worker.process_crypto_transfer") as mock_celery_task:
        mock_celery_task.delay = MagicMock()

        from gateway.api import acquirer_service
        await init_db()
        await acquirer_service.start()

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            payload = {
                "pan": "4111111111111111",
                "expiry": "2512",
                "cvv": "123",
                "amount": 50.0,
                "target_wallet": "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
            }

            response = await client.post("/pay", json=payload, headers={"Idempotency-Key": "unique-key-001"})

            assert response.status_code == 202
            data = response.json()
            assert data["status"] == "FIAT_APPROVED"
            assert "transaction_id" in data
            assert "stan" in data

            # Celery task must have been dispatched
            mock_celery_task.delay.assert_called_once()

        await acquirer_service.stop()

# ---------------------------------------------------------------------------
# Test 2: Idempotency - Duplicate request returns existing transaction
# ---------------------------------------------------------------------------

async def test_idempotency_prevents_double_charge(mock_bank_server):
    """The same Idempotency-Key must NOT trigger a second bank authorization."""
    with patch("gateway.worker.process_crypto_transfer") as mock_celery_task:
        mock_celery_task.delay = MagicMock()

        from gateway.api import acquirer_service
        await init_db()
        await acquirer_service.start()

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            payload = {
                "pan": "4111111111111111",
                "expiry": "2512",
                "cvv": "123",
                "amount": 100.0,
                "target_wallet": "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
            }
            headers = {"Idempotency-Key": "idempotent-test-002"}

            # First request
            response1 = await client.post("/pay", json=payload, headers=headers)
            assert response1.status_code == 202
            tx_id_1 = response1.json()["transaction_id"]

            # Second request (same key) - should NOT call bank again
            response2 = await client.post("/pay", json=payload, headers=headers)
            assert response2.status_code == 202
            data2 = response2.json()
            assert data2["transaction_id"] == tx_id_1
            assert data2["message"] == "Duplicate request. Returning existing transaction."

            # Celery must have been called only ONCE
            assert mock_celery_task.delay.call_count == 1

        await acquirer_service.stop()

# ---------------------------------------------------------------------------
# Test 3: Circuit Breaker blocks requests when Web3 is down
# ---------------------------------------------------------------------------

async def test_circuit_breaker_rejects_when_open():
    """When the circuit breaker is OPEN, requests must be rejected with 503."""
    from gateway.circuit_breaker import web3_circuit_breaker, CircuitState

    # Manually open the circuit breaker
    original_state = web3_circuit_breaker.state
    web3_circuit_breaker.state = CircuitState.OPEN
    web3_circuit_breaker.last_failure_time = __import__("time").time()

    try:
        await init_db()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            payload = {
                "pan": "4111111111111111",
                "expiry": "2512",
                "cvv": "123",
                "amount": 25.0,
                "target_wallet": "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
            }

            response = await client.post("/pay", json=payload)
            assert response.status_code == 503
            assert "blockchain network unreachable" in response.json()["detail"]
    finally:
        # Restore
        web3_circuit_breaker.state = original_state
        web3_circuit_breaker.failure_count = 0

# ---------------------------------------------------------------------------
# Test 4: Luhn validation rejects invalid card numbers
# ---------------------------------------------------------------------------

async def test_luhn_rejects_invalid_pan(mock_bank_server):
    """An invalid card number must be rejected immediately without calling the bank."""
    await init_db()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        payload = {
            "pan": "1234567890123456",  # Invalid Luhn
            "expiry": "2512",
            "cvv": "123",
            "amount": 10.0,
            "target_wallet": "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
        }

        response = await client.post("/pay", json=payload)
        assert response.status_code == 400
        assert "Invalid card number" in response.json()["detail"]

# ---------------------------------------------------------------------------
# Test 5: Transaction status endpoint
# ---------------------------------------------------------------------------

async def test_get_transaction_status(mock_bank_server):
    """GET /transaction/{id} must return the current status of a transaction."""
    with patch("gateway.worker.process_crypto_transfer") as mock_celery_task:
        mock_celery_task.delay = MagicMock()

        from gateway.api import acquirer_service
        await init_db()
        await acquirer_service.start()

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            payload = {
                "pan": "4111111111111111",
                "expiry": "2512",
                "cvv": "123",
                "amount": 200.0,
                "target_wallet": "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
            }

            # Create transaction
            resp = await client.post("/pay", json=payload, headers={"Idempotency-Key": "status-test-003"})
            tx_id = resp.json()["transaction_id"]

            # Poll status
            status_resp = await client.get(f"/transaction/{tx_id}")
            assert status_resp.status_code == 200
            data = status_resp.json()
            assert data["id"] == tx_id
            assert data["status"] == "FIAT_APPROVED"

        await acquirer_service.stop()
