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


async def outbox_messages(task_name: str = None) -> list:
    """Rows in the outbox, optionally filtered by task.

    Assertions moved here from `apply_async` call counts when the transactional
    outbox landed. It is the better check: the outbox row is the guarantee, and
    the publish is a retryable consequence of it. A test that watched the call
    would pass on a system that recorded nothing.
    """
    from sqlalchemy import select

    from gateway.database import async_session
    from gateway.models import OutboxMessage

    async with async_session() as session:
        statement = select(OutboxMessage)
        if task_name:
            statement = statement.where(OutboxMessage.task_name == task_name)
        return list((await session.execute(statement)).scalars().all())


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
    """The card is authorised and the crypto leg is handed to a worker.

    In the default auth_capture mode the card is only *held* at this point:
    the debit happens after the transfer is confirmed, so the customer is
    served before they are charged.
    """
    from gateway.config import settings

    with patch("gateway.worker.process_crypto_transfer") as task:
        task.apply_async = MagicMock()

        response = await gateway_client.post(
            "/pay", json=payload(expiry), headers={"Idempotency-Key": "happy-001"}
        )

        assert response.status_code == 202, response.text
        body = response.json()
        expected = "FIAT_AUTHORIZED" if settings.uses_auth_capture else "FIAT_APPROVED"
        assert body["status"] == expected
        assert body["stan"]

    queued = await outbox_messages("process_crypto_transfer")
    assert len(queued) == 1, "the crypto transfer was not recorded in the outbox"


async def test_payment_does_not_send_the_pan_to_the_worker(gateway_client, expiry):
    """Card data must never enter a broker payload (PCI-DSS)."""
    with patch("gateway.worker.process_crypto_transfer") as task:
        task.apply_async = MagicMock()

        await gateway_client.post(
            "/pay", json=payload(expiry), headers={"Idempotency-Key": "no-pan-001"}
        )

    import json

    messages = await outbox_messages("process_crypto_transfer")
    assert messages, "nothing was recorded"
    body = json.loads(messages[-1].payload)

    assert APPROVED_PAN not in messages[-1].payload
    assert "123" not in messages[-1].payload  # nor the CVV
    # An allow-list, not a length check: anything new reaching a broker payload
    # has to be a deliberate decision, since broker messages sit in Redis in
    # clear text and appear in task logs.
    assert set(body) <= {"tx_id", "correlation_id"}


async def test_masked_pan_only_is_persisted(gateway_client, expiry):
    with patch("gateway.worker.process_crypto_transfer") as task:
        task.apply_async = MagicMock()
        response = await gateway_client.post(
            "/pay", json=payload(expiry), headers={"Idempotency-Key": "masking-001"}
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

    # The bank must have been asked exactly once, and one transfer queued.
    queued = await outbox_messages("process_crypto_transfer")
    assert len(queued) == 1, f"a duplicate request queued {len(queued)} transfers"


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
            "/pay", json=payload(expiry, pan=DECLINED_PAN), headers={"Idempotency-Key": "declined-001"}
        )

        assert response.status_code == 400

    assert not await outbox_messages("process_crypto_transfer"), (
        "a declined card queued a crypto transfer"
    )


async def test_acquirer_silence_is_treated_as_unknown_not_declined(gateway_client, expiry):
    """A missing 0210 means we may have debited the cardholder: reverse, don't shrug."""
    response = await gateway_client.post(
        "/pay", json=payload(expiry, pan=SILENT_PAN), headers={"Idempotency-Key": "silent-auth-001"}
    )

    assert response.status_code == 504
    # The recovery must be durably recorded, not merely attempted: this is the
    # branch where the cardholder may already have been debited.
    assert await outbox_messages("retry_reversal"), "no reversal was recorded"

    from sqlalchemy import select

    from gateway.database import async_session
    from gateway.models import Transaction

    async with async_session() as session:
        tx = (
            await session.execute(
                select(Transaction).where(Transaction.idempotency_key == "silent-auth-001")
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

    from gateway.config import settings

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == tx_id
    expected = "FIAT_AUTHORIZED" if settings.uses_auth_capture else "FIAT_APPROVED"
    assert body["status"] == expected
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
                "/pay", json=payload(expiry), headers={"Idempotency-Key": "nosign-key-001"}
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


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


async def _make_transaction(status, age_days: int, key: str):
    from datetime import timedelta

    from gateway.database import async_session, init_db
    from gateway.models import Transaction, utcnow

    await init_db()
    async with async_session() as session:
        tx = Transaction(
            amount=Decimal("25.00"),
            currency="840",
            masked_pan="411111******1111",
            target_wallet=WALLET,
            idempotency_key=key,
            status=status,
            created_at=utcnow() - timedelta(days=age_days),
            updated_at=utcnow() - timedelta(days=age_days),
        )
        session.add(tx)
        await session.commit()
        await session.refresh(tx)
        return tx.id


async def test_retention_redacts_old_settled_transactions():
    from gateway.database import async_session
    from gateway.models import Transaction
    from gateway.retention import REDACTED_PAN, apply_retention

    tx_id = await _make_transaction(TransactionStatus.CRYPTO_SENT, 200, "ret-old-1")
    await apply_retention()

    async with async_session() as session:
        tx = await session.get(Transaction, tx_id)
        assert tx.masked_pan == REDACTED_PAN
        assert tx.target_wallet != WALLET
        # The financial record survives: retention removes identity, not history.
        assert tx.amount == Decimal("25.00")
        assert tx.status is TransactionStatus.CRYPTO_SENT


async def test_retention_never_touches_a_transaction_that_owes_money():
    """A refund still owed must outlive any retention horizon."""
    from gateway.database import async_session
    from gateway.models import Transaction
    from gateway.retention import apply_retention

    tx_id = await _make_transaction(TransactionStatus.REVERSAL_FAILED, 900, "ret-owed-1")
    await apply_retention()

    async with async_session() as session:
        tx = await session.get(Transaction, tx_id)
        assert tx is not None, "a transaction owing a refund was deleted"
        assert tx.masked_pan == "411111******1111", "the card reference was stripped"


async def test_retention_leaves_recent_transactions_alone():
    from gateway.database import async_session
    from gateway.models import Transaction
    from gateway.retention import apply_retention

    tx_id = await _make_transaction(TransactionStatus.CRYPTO_SENT, 1, "ret-new-1")
    await apply_retention()

    async with async_session() as session:
        tx = await session.get(Transaction, tx_id)
        assert tx.masked_pan == "411111******1111"


async def test_erasure_request_is_refused_while_unsettled():
    from gateway.retention import redact_transaction

    tx_id = await _make_transaction(TransactionStatus.FIAT_APPROVED, 1, "ret-inflight-1")
    assert await redact_transaction(tx_id) is False

    settled = await _make_transaction(TransactionStatus.CRYPTO_SENT, 1, "ret-settled-1")
    assert await redact_transaction(settled) is True


# ---------------------------------------------------------------------------
# Authorise, deliver, then capture
# ---------------------------------------------------------------------------


async def test_a_failed_delivery_owes_the_cardholder_nothing(gateway_client, expiry):
    """The property this whole ordering exists for.

    Under auth_capture the card is held, not charged. When the crypto cannot be
    delivered the hold is released and the transaction ends AUTH_VOIDED — a
    terminal state that owes nobody anything. Under the old ordering the same
    failure produced a captured payment plus a reversal that could be refused,
    which is how a cardholder ended up out of pocket.
    """
    from gateway.config import settings

    if not settings.uses_auth_capture:
        pytest.skip("purchase mode captures up front; this property does not apply")

    from gateway.database import async_session
    from gateway.models import MANUAL_INTERVENTION_STATES, Transaction
    from gateway.worker import _release_funds

    with patch("gateway.worker.process_crypto_transfer") as task:
        task.apply_async = MagicMock()
        response = await gateway_client.post(
            "/pay", json=payload(expiry), headers={"Idempotency-Key": "void-hold-001"}
        )
        assert response.status_code == 202, response.text
        tx_id = response.json()["transaction_id"]

    async with async_session() as session:
        tx = await session.get(Transaction, tx_id)
        assert tx.status is TransactionStatus.FIAT_AUTHORIZED
        assert tx.completed_at is None

    await _release_funds(tx_id, reason="crypto transfer failed in a test")

    async with async_session() as session:
        tx = await session.get(Transaction, tx_id)
        assert tx.status is TransactionStatus.AUTH_VOIDED
        assert tx.status not in MANUAL_INTERVENTION_STATES, "a released hold owes nothing"
        assert tx.completed_at is not None


async def test_delivery_captures_the_held_funds(gateway_client, expiry):
    """Money is taken only once the transfer is confirmed on-chain."""
    from gateway.config import settings

    if not settings.uses_auth_capture:
        pytest.skip("purchase mode captures up front")

    from gateway.database import async_session
    from gateway.models import Transaction
    from gateway.worker import _mark_sent

    with patch("gateway.worker.process_crypto_transfer") as task:
        task.apply_async = MagicMock()
        response = await gateway_client.post(
            "/pay", json=payload(expiry), headers={"Idempotency-Key": "capture-hold-001"}
        )
        tx_id = response.json()["transaction_id"]

    await _mark_sent(tx_id)

    async with async_session() as session:
        tx = await session.get(Transaction, tx_id)
        assert tx.status is TransactionStatus.CRYPTO_SENT
        assert tx.completed_at is not None


async def test_releasing_a_hold_can_never_owe_a_refund():
    """Failing to deliver against a hold costs the cardholder nothing.

    Stated precisely, because the transitive version is not true and should not
    be: once funds are actually captured, a chain reorganisation can undo a
    delivered transfer, and refunding is then the correct response. What must
    hold is narrower — the *direct* failure path from a hold never reaches a
    state that owes money.
    """
    from gateway.models import (
        LEGAL_TRANSITIONS,
        MANUAL_INTERVENTION_STATES,
        TERMINAL_STATES,
        TransactionStatus,
    )

    successors = LEGAL_TRANSITIONS[TransactionStatus.FIAT_AUTHORIZED]

    # Nothing one step from a hold owes anyone a refund.
    owing = successors & set(MANUAL_INTERVENTION_STATES)
    assert not owing, f"a hold can move straight into {owing}"

    # CRYPTO_FAILED exists only to trigger a reversal. A hold is released with
    # a void instead, so it must not be reachable in one step.
    assert TransactionStatus.CRYPTO_FAILED not in successors

    # And the release itself is terminal: no further obligation follows.
    assert TransactionStatus.AUTH_VOIDED in successors
    assert TransactionStatus.AUTH_VOIDED in TERMINAL_STATES


async def test_only_a_capture_can_lead_to_owing_a_refund():
    """Every route to REVERSAL_FAILED passes through money actually taken.

    The guarantee behind the ordering: a cardholder can only be left out of
    pocket by a transaction that was genuinely debited, never by one that was
    merely authorised and then abandoned.
    """
    from gateway.models import LEGAL_TRANSITIONS, TransactionStatus

    captured_states = {
        TransactionStatus.FIAT_CAPTURED,
        TransactionStatus.FIAT_APPROVED,   # purchase mode debits immediately
        TransactionStatus.FIAT_UNKNOWN,    # may have debited; we cannot tell
    }

    # Walk from the hold, refusing to pass through any state where money moved.
    reachable, frontier = set(), [TransactionStatus.FIAT_AUTHORIZED]
    while frontier:
        state = frontier.pop()
        if state in reachable or state in captured_states:
            continue
        reachable.add(state)
        frontier.extend(LEGAL_TRANSITIONS[state] - captured_states)

    assert TransactionStatus.REVERSAL_FAILED not in reachable, (
        "a refund can be owed without any money having been captured"
    )


async def test_a_refused_capture_keeps_the_card_number_for_the_retry():
    """The stored PAN survives a failed capture, because the retry needs it.

    Observed in the simulation stack: a transfer delivered and confirmed, the
    capture unable to run, and the card number already discarded — so the money
    could never be collected and there was nothing left to retry with. The purge
    now belongs to whoever knows the transaction is finished.
    """
    from gateway.config import settings

    if not settings.uses_auth_capture:
        pytest.skip("purchase mode has no capture step")

    from gateway.database import async_session
    from gateway.models import Transaction
    from gateway.pan_vault import get_pan_vault
    from gateway.worker import _mark_sent

    tx_id = await _make_transaction(
        TransactionStatus.FIAT_AUTHORIZED, 0, "capture-retry-001"
    )
    vault = get_pan_vault()
    await vault.store(tx_id, APPROVED_PAN)

    with patch("gateway.worker._capture_funds", return_value=False):
        settled = await _mark_sent(tx_id)

    assert settled is False, "a refused capture must not report the payment as settled"
    assert await vault.retrieve(tx_id) == APPROVED_PAN, (
        "the card number was discarded, so the capture can never be retried"
    )

    async with async_session() as session:
        tx = await session.get(Transaction, tx_id)
        # Still held, which is the truth: delivered, but not yet collected.
        assert tx.status is TransactionStatus.FIAT_AUTHORIZED


async def test_a_successful_capture_discards_the_card_number():
    """Once settled, nothing needs the PAN and it must not linger."""
    from gateway.config import settings

    if not settings.uses_auth_capture:
        pytest.skip("purchase mode has no capture step")

    from gateway.database import async_session
    from gateway.models import Transaction
    from gateway.pan_vault import get_pan_vault
    from gateway.worker import _mark_sent

    tx_id = await _make_transaction(
        TransactionStatus.FIAT_AUTHORIZED, 0, "capture-purge-001"
    )
    vault = get_pan_vault()
    await vault.store(tx_id, APPROVED_PAN)

    async def capture_succeeds(target_id):
        # What the real capture does: move the transaction to FIAT_CAPTURED.
        # A mock that only returns True would let the test pass against a state
        # machine that forbids the shortcut — the machine is right to.
        async with async_session() as session:
            tx = await session.get(Transaction, target_id)
            tx.transition_to(TransactionStatus.FIAT_CAPTURED)
            await session.commit()
        return True

    with patch("gateway.worker._capture_funds", side_effect=capture_succeeds):
        settled = await _mark_sent(tx_id)

    assert settled is True
    assert await vault.retrieve(tx_id) is None, "the card number outlived its purpose"

    async with async_session() as session:
        tx = await session.get(Transaction, tx_id)
        assert tx.status is TransactionStatus.CRYPTO_SENT


# ---------------------------------------------------------------------------
# Transactional outbox
# ---------------------------------------------------------------------------


async def test_a_broker_outage_delays_a_payment_instead_of_stranding_it(
    gateway_client, expiry
):
    """The property the outbox exists for.

    With the broker unreachable the payment is still accepted and still
    recorded, because the intent lives in the same commit as the authorisation.
    Previously the publish happened after the commit as a separate act: a broker
    outage there left a card authorised with nothing queued against it, and only
    the reconciliation sweep noticed, minutes later.
    """
    with patch("gateway.outbox.publish_one", side_effect=ConnectionError("broker down")):
        response = await gateway_client.post(
            "/pay", json=payload(expiry), headers={"Idempotency-Key": "broker-down-001"}
        )

    assert response.status_code == 202, "the payment was refused for a recoverable outage"

    pending = await outbox_messages("process_crypto_transfer")
    assert len(pending) == 1
    assert pending[0].published_at is None, "recorded as published while the broker was down"


async def test_the_relay_publishes_what_the_outage_left_behind(gateway_client, expiry):
    """Once the broker returns, the relay delivers without anyone intervening."""
    from gateway.outbox import publish_pending

    with patch("gateway.outbox.publish_one", side_effect=ConnectionError("broker down")):
        await gateway_client.post(
            "/pay", json=payload(expiry), headers={"Idempotency-Key": "relay-recovers-001"}
        )

    assert (await outbox_messages())[0].published_at is None

    with patch("gateway.outbox.publish_one", return_value=True):
        counts = await publish_pending()

    assert counts["published"] == 1
    assert (await outbox_messages())[0].published_at is not None


async def test_a_message_is_only_marked_published_once_the_broker_accepts_it():
    """At-least-once, never at-most-once.

    Marking before the broker accepts would turn a crash into a lost payment.
    Marking after can publish twice, and both gateway tasks refuse to act on the
    same transaction twice — which makes duplication the cheaper failure.
    """
    from gateway.database import async_session
    from gateway.outbox import enqueue, publish_pending

    async with async_session() as session:
        enqueue(session, "process_crypto_transfer", {"tx_id": 999})
        await session.commit()

    with patch("gateway.outbox.publish_one", side_effect=RuntimeError("broker refused")):
        counts = await publish_pending()

    assert counts["failed"] == 1
    async with async_session() as session:
        message = (await session.execute(select_outbox())).scalars().first()
        assert message.published_at is None, "marked published despite a refusal"
        assert message.attempts == 1
        assert "refused" in (message.last_error or "")


def select_outbox():
    from sqlalchemy import select

    from gateway.models import OutboxMessage

    return select(OutboxMessage)


async def test_a_message_is_abandoned_loudly_rather_than_retried_forever():
    """After enough failures a human is named; the row is kept, not dropped."""
    from gateway.database import async_session
    from gateway.outbox import MAX_ATTEMPTS, enqueue, publish_pending

    async with async_session() as session:
        enqueue(session, "process_crypto_transfer", {"tx_id": 998})
        await session.commit()

    with patch("gateway.outbox.publish_one", side_effect=RuntimeError("broker gone")):
        for _ in range(MAX_ATTEMPTS):
            await publish_pending()

    async with async_session() as session:
        message = (await session.execute(select_outbox())).scalars().first()
        assert message.abandoned is True
        assert message.published_at is None
        # Kept: the payment it describes still has not been acted on.
        assert message is not None


async def test_a_hold_is_never_released_while_the_transfer_may_still_land():
    """Releasing a hold against a live transfer gives the crypto away.

    The reversal path always checked the chain before refunding. The void path
    did not, and it showed: a hold released against a transfer sitting in the
    mempool, which would have delivered the crypto and returned the money for
    it. Both outcomes — confirmed, and still pending — must stop the release.
    """
    from gateway.database import async_session
    from gateway.models import Transaction
    from gateway.worker import _void_authorization

    for onchain_status in ("success", "pending"):
        tx_id = await _make_transaction(
            TransactionStatus.FIAT_AUTHORIZED, 0, f"void-guard-{onchain_status}"
        )
        async with async_session() as session:
            tx = await session.get(Transaction, tx_id)
            tx.crypto_tx_hash = "0x" + "ab" * 32
            await session.commit()

        with patch(
            "gateway.crypto_service.CryptoService.get_onchain_status",
            return_value=onchain_status,
        ):
            released = await _void_authorization(tx_id, reason="test")

        assert released is False, f"released a hold against a {onchain_status} transfer"

        async with async_session() as session:
            tx = await session.get(Transaction, tx_id)
            assert tx.status is TransactionStatus.FIAT_AUTHORIZED, (
                f"the hold was given up while the transfer was {onchain_status}"
            )


async def test_a_hold_is_released_when_the_transfer_truly_failed():
    """The guard must not block the ordinary case it exists alongside."""
    from gateway.database import async_session
    from gateway.models import Transaction
    from gateway.pan_vault import get_pan_vault
    from gateway.worker import _void_authorization

    tx_id = await _make_transaction(TransactionStatus.FIAT_AUTHORIZED, 0, "void-ok-001")
    await get_pan_vault().store(tx_id, APPROVED_PAN)

    async with async_session() as session:
        tx = await session.get(Transaction, tx_id)
        tx.crypto_tx_hash = "0x" + "cd" * 32
        await session.commit()

    with patch(
        "gateway.crypto_service.CryptoService.get_onchain_status", return_value="failed"
    ):
        with patch(
            "gateway.acquirer.AcquirerService.void_authorization",
            return_value={"success": True, "action_code": "00", "stan": "000001"},
        ):
            released = await _void_authorization(tx_id, reason="test")

    assert released is True
    async with async_session() as session:
        tx = await session.get(Transaction, tx_id)
        assert tx.status is TransactionStatus.AUTH_VOIDED


async def test_a_stalled_confirmation_raises_a_timeout_the_caller_can_act_on():
    """web3 signals a stalled wait with TimeExhausted, which is not a TimeoutError.

    The worker decides a transfer is stuck — and needs replacing at the same
    nonce — by catching TimeoutError. With web3's own type leaking through, that
    branch was unreachable and the replacement logic never ran once: a transfer
    sat pending at a fixed nonce while a hold stayed open against it.
    """
    from unittest.mock import MagicMock

    from web3.exceptions import TimeExhausted

    from gateway.crypto_service import CryptoService

    # The premise, stated so a future web3 upgrade cannot quietly invalidate it.
    assert not issubclass(TimeExhausted, TimeoutError)

    service = CryptoService.__new__(CryptoService)
    service.w3 = MagicMock()
    service.w3.eth.wait_for_transaction_receipt.side_effect = TimeExhausted("not mined")

    with pytest.raises(TimeoutError):
        await service.await_confirmation("0x" + "ef" * 32)


# ---------------------------------------------------------------------------
# Nonce gaps and stuck transfers
# ---------------------------------------------------------------------------


async def test_each_replacement_bids_strictly_higher_than_the_last():
    """Repricing must escalate, or the node rejects the retry as a duplicate.

    Observed in the simulation stack: a stuck transfer replaced once, then every
    further attempt repriced against the same market, produced a byte-identical
    transaction, and came back "transaction already imported". The transfer
    stayed exactly as stuck while the retry counter climbed.
    """
    from unittest.mock import MagicMock

    from gateway.config import settings
    from gateway.crypto_service import CryptoService

    service = CryptoService.__new__(CryptoService)
    service.w3 = MagicMock()
    service.w3.eth.get_block.return_value = {"baseFeePerGas": 10 * 10**9}
    service.w3.eth.max_priority_fee = 2 * 10**9
    service.w3.to_wei = lambda value, unit: value * 10**9

    original = settings.web3_max_fee_gwei
    settings.web3_max_fee_gwei = Decimal("100000")
    try:
        fees = [
            service._fee_parameters(
                multiplier=settings.web3_replacement_multiplier ** attempt
            )["maxFeePerGas"]
            for attempt in (1, 2, 3)
        ]
    finally:
        settings.web3_max_fee_gwei = original

    assert fees[0] < fees[1] < fees[2], f"replacement fees did not escalate: {fees}"


async def test_a_nonce_gap_is_measured_against_the_chain():
    """A transfer stuck behind a missing nonce is not an underpricing problem.

    A transaction signed at nonce N cannot mine until every nonce below it has.
    Repricing does nothing about that, and the hot wallet stays frozen with
    every later payment queued behind the hole.
    """
    from unittest.mock import AsyncMock, MagicMock

    from gateway.crypto_service import CryptoService

    service = CryptoService.__new__(CryptoService)
    service.w3 = MagicMock()
    service.account = MagicMock()
    service.account.address = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
    service.w3.eth.get_transaction_count.return_value = 508
    service._redis = AsyncMock()

    # Our bookkeeping says 510; the chain will only accept 508.
    service._redis.get.return_value = b"510"
    assert await service.nonce_gap() == 2

    # Contiguous: nothing to fill.
    service._redis.get.return_value = b"508"
    assert await service.nonce_gap() == 0

    # Behind the chain is not a gap either.
    service._redis.get.return_value = b"500"
    assert await service.nonce_gap() == 0


async def test_a_capture_in_flight_is_never_sent_twice():
    """The double-charge this guard exists to prevent.

    A capture that reached the acquirer while the process died before recording
    it leaves the row still FIAT_AUTHORIZED. Without evidence that an attempt
    was made, the retry sends a second capture and the cardholder pays twice for
    one purchase. Persisting the trace number before the message leaves is the
    same discipline the chain side already follows with the transaction hash.
    """
    from unittest.mock import AsyncMock, patch

    from gateway.config import settings
    from gateway.database import async_session
    from gateway.models import Transaction
    from gateway.pan_vault import get_pan_vault
    from gateway.worker import _capture_funds

    if not settings.uses_auth_capture:
        pytest.skip("purchase mode has no capture step")

    tx_id = await _make_transaction(
        TransactionStatus.FIAT_AUTHORIZED, 0, "capture-once-001"
    )
    await get_pan_vault().store(tx_id, APPROVED_PAN)

    # A previous attempt got as far as the acquirer and no further.
    async with async_session() as session:
        tx = await session.get(Transaction, tx_id)
        tx.capture_stan = "000999"
        tx.capture_attempts = 1
        await session.commit()

    acquirer = AsyncMock()
    with patch("gateway.worker.AcquirerService", return_value=acquirer):
        captured = await _capture_funds(tx_id)

    assert captured is False, "a capture with an unknown outcome was reported as done"
    acquirer.capture.assert_not_called(), "a second capture was sent"

    async with async_session() as session:
        tx = await session.get(Transaction, tx_id)
        # Still held, and still carrying the evidence for a human to reconcile.
        assert tx.status is TransactionStatus.FIAT_AUTHORIZED
        assert tx.capture_stan == "000999"


async def test_the_visibility_timeout_exceeds_every_retry_countdown():
    """A countdown longer than the visibility timeout multiplies messages.

    The Redis broker redelivers a message once the visibility timeout elapses.
    A task scheduled further into the future than that is therefore redelivered
    while still waiting for its own ETA — and each redelivery is redelivered in
    turn. Observed in the simulation stack: a 3600s reversal backoff against a
    900s visibility timeout turned a handful of stuck reversals into 65,000
    queued messages, which starved every real payment behind them.

    Asserted as a relationship rather than a number, so tuning either side
    cannot silently reintroduce it.
    """
    from gateway.worker import (
        _MAX_RETRY_COUNTDOWN,
        _MAX_TASK_SECONDS,
        _VISIBILITY_TIMEOUT,
        celery_app,
    )

    assert _VISIBILITY_TIMEOUT > _MAX_RETRY_COUNTDOWN, (
        "a task scheduled beyond the visibility timeout is redelivered before it runs"
    )
    assert _VISIBILITY_TIMEOUT > _MAX_TASK_SECONDS, (
        "a task running longer than the visibility timeout is redelivered while working"
    )
    # And the configured value is the computed one, not a stale literal.
    configured = celery_app.conf.broker_transport_options["visibility_timeout"]
    assert configured == _VISIBILITY_TIMEOUT


async def test_no_task_schedules_itself_beyond_the_declared_maximum():
    """_MAX_RETRY_COUNTDOWN has to bound what the tasks actually use."""
    import pathlib
    import re

    source = pathlib.Path("src/gateway/worker.py").read_text()
    from gateway.worker import _MAX_RETRY_COUNTDOWN

    # Read the largest integer on each line that schedules a countdown. The
    # expressions nest parentheses, so this looks at the whole line rather than
    # trying to match the call shape.
    countdown_lines = [
        line for line in source.splitlines() if "countdown=" in line and "min(" in line
    ]
    assert countdown_lines, "no bounded countdowns found — check this test still matches"

    for line in countdown_lines:
        ceiling = max(int(n) for n in re.findall(r"\b(\d{2,})\b", line))
        assert ceiling <= _MAX_RETRY_COUNTDOWN, (
            f"a task can schedule itself {ceiling}s ahead, beyond the declared "
            f"maximum of {_MAX_RETRY_COUNTDOWN}s that sizes the visibility timeout:\n"
            f"    {line.strip()}"
        )


async def test_a_send_that_never_completes_is_bounded():
    """An acquirer that stops reading must not hang the request forever.

    Underneath the send is a writer.drain(), which blocks while the peer's
    receive window stays full. Only the *response* was bounded, so a peer that
    accepted the connection and then went quiet would hold the coroutine — and
    the response timeout never started counting. One held Gunicorn worker per
    payment, until the process was killed.
    """
    import asyncio
    from unittest.mock import AsyncMock, patch

    from gateway.acquirer import AcquirerService

    service = AcquirerService()
    service.client = AsyncMock()
    service.client._connected = True

    async def never_returns(*_args, **_kwargs):
        await asyncio.sleep(3600)

    service.client.send = never_returns

    with patch.object(type(service), "is_connected", property(lambda self: True)):
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                service._send_and_wait({"MTI": "0800", "DE11": 1}, "000001", timeout=0.2),
                timeout=5,
            )
