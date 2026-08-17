import asyncio
import ipaddress
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.acquirer import AcquirerService, AuthorizationTimeout
from gateway.circuit_breaker import web3_circuit_breaker
from gateway.config import settings
from gateway.database import async_session, engine, get_session, init_db
from gateway.models import Transaction, TransactionStatus
from gateway.pan_vault import get_pan_vault

LOGGER = logging.getLogger(__name__)

acquirer_service = AcquirerService()

# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def _peer_is_trusted_proxy(peer: str) -> bool:
    """Whether this peer's ``X-Forwarded-For`` may be believed.

    The header is client-supplied. Believing it from an untrusted peer means
    anyone can present a fresh identity per request and walk straight around the
    rate limit — so the peer address is matched against the configured proxies
    rather than merely checking that the setting is non-empty.
    """
    if "*" in settings.trusted_proxies:
        return True
    if not peer:
        return False
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False

    for entry in settings.trusted_proxies:
        try:
            if "/" in entry:
                if address in ipaddress.ip_network(entry, strict=False):
                    return True
            elif address == ipaddress.ip_address(entry):
                return True
        except ValueError:
            LOGGER.warning(f"TRUSTED_PROXIES contains an unparseable entry: {entry!r}")
    return False


def client_identity(request: Request) -> str:
    """Identify the caller for rate limiting.

    Behind Nginx every request arrives from the proxy, so keying on the socket
    address alone would put the whole internet in one bucket.
    """
    peer = request.client.host if request.client else ""
    if _peer_is_trusted_proxy(peer):
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return peer or "unknown"


limiter = Limiter(key_func=client_identity)

# ---------------------------------------------------------------------------
# Card helpers
# ---------------------------------------------------------------------------


def luhn_checksum(card_number: str) -> bool:
    if not card_number.isdigit():
        return False
    digits = [int(d) for d in card_number]
    odd = digits[-1::-2]
    even = [sum(int(c) for c in str(d * 2)) for d in digits[-2::-2]]
    return (sum(odd) + sum(even)) % 10 == 0


def mask_pan(pan: str) -> str:
    """First six and last four, the most PCI-DSS allows us to retain."""
    if len(pan) < 13:
        return "INVALID_PAN"
    return f"{pan[:6]}{'*' * (len(pan) - 10)}{pan[-4:]}"


def expiry_is_valid(expiry: str) -> bool:
    """``YYMM`` in the future. The card is valid through the end of its month."""
    try:
        year, month = 2000 + int(expiry[:2]), int(expiry[2:])
    except ValueError:
        return False
    if not 1 <= month <= 12:
        return False
    now = datetime.now(timezone.utc)
    return (year, month) >= (now.year, now.month)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.is_production:
        settings.require_valid()
    LOGGER.info(f"Starting gateway: {settings.summary()}")

    await init_db()
    await acquirer_service.start()
    yield
    await acquirer_service.stop()
    await get_pan_vault().close()
    await engine.dispose()


docs_kwargs = {} if settings.docs_enabled else {"docs_url": None, "redoc_url": None, "openapi_url": None}

app = FastAPI(title="2D Link Fiat-to-Crypto Gateway", lifespan=lifespan, **docs_kwargs)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    # Credentials cannot be combined with a wildcard origin, and the gateway
    # authenticates with a header rather than a cookie anyway.
    allow_credentials=not (settings.cors_origins == ["*"]),
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["X-API-Key", "Idempotency-Key", "Content-Type"],
)

#: Endpoints reachable without an API key. Deliberately short.
PUBLIC_PATHS = {"/health"}
if settings.docs_enabled:
    PUBLIC_PATHS |= {"/docs", "/redoc", "/openapi.json"}


@app.middleware("http")
async def authenticate_api_key(request: Request, call_next):
    if not settings.api_key:
        if settings.is_production:
            return JSONResponse(status_code=503, content={"detail": "Gateway misconfigured."})
        return await call_next(request)

    if request.url.path in PUBLIC_PATHS or request.method == "OPTIONS":
        return await call_next(request)

    provided = request.headers.get("X-API-Key", "")
    if not secrets.compare_digest(provided, settings.api_key):
        return JSONResponse(status_code=401, content={"detail": "Invalid or missing API key."})
    return await call_next(request)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Never leak internals. The detail goes to the log, not to the caller."""
    LOGGER.exception(f"Unhandled error on {request.method} {request.url.path}: {exc}")
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class PaymentRequest(BaseModel):
    pan: str = Field(..., pattern=r"^\d{13,19}$", description="Primary Account Number")
    expiry: str = Field(..., pattern=r"^\d{4}$", description="Expiry date, YYMM")
    cvv: str = Field(..., pattern=r"^\d{3,4}$", description="Card Verification Value")
    amount: Decimal = Field(..., gt=0, decimal_places=2, description="Fiat amount")
    target_wallet: str = Field(..., pattern=r"^0x[a-fA-F0-9]{40}$")

    @field_validator("amount")
    @classmethod
    def within_limits(cls, value: Decimal) -> Decimal:
        if value < settings.amount_min or value > settings.amount_max:
            raise ValueError(
                f"Amount must be between {settings.amount_min} and {settings.amount_max}."
            )
        return value

    @field_validator("expiry")
    @classmethod
    def not_expired(cls, value: str) -> str:
        if not expiry_is_valid(value):
            raise ValueError("Card has expired.")
        return value


class PaymentResponse(BaseModel):
    status: str
    message: str
    transaction_id: int
    fiat_amount: Decimal
    tx_hash: Optional[str] = None
    stan: Optional[str] = None


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    """Liveness. Cheap, public, says nothing about the internals."""
    return {"status": "ok", "mode": settings.mode}


@app.get("/health/ready")
async def health_ready(db: AsyncSession = Depends(get_session)):
    """Readiness. Authenticated: the component names alone are a map of the estate."""
    checks = {}

    try:
        await db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        LOGGER.error(f"Readiness: database unreachable: {exc}")
        checks["database"] = "error"

    try:
        pan_vault = get_pan_vault()
        await pan_vault.client.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        LOGGER.error(f"Readiness: Redis unreachable: {exc}")
        checks["redis"] = "error"

    checks["acquirer"] = "ok" if acquirer_service.is_connected else "disconnected"

    signable, sign_reason = await can_sign()
    checks["signing"] = "ok" if signable else "unavailable"
    if not signable:
        LOGGER.error(f"Readiness: {sign_reason}")

    breaker = web3_circuit_breaker.health()
    checks["circuit_breaker"] = breaker["state"]
    if not breaker["reachable"]:
        checks["circuit_breaker"] = "error"

    if settings.vault_addr:
        try:
            import hvac

            client = hvac.Client(url=settings.vault_addr, token=settings.vault_token)
            checks["vault"] = "ok" if client.sys.is_initialized() and not client.sys.is_sealed() else "sealed"
        except Exception as exc:
            LOGGER.error(f"Readiness: Vault unreachable: {exc}")
            checks["vault"] = "error"

    degraded = [k for k, v in checks.items() if v in ("error", "disconnected", "sealed")]
    return JSONResponse(
        status_code=503 if degraded else 200,
        content={"status": "degraded" if degraded else "ok", "checks": checks, "mode": settings.mode},
    )


# ---------------------------------------------------------------------------
# Payment
# ---------------------------------------------------------------------------


#: Cached answer to "can this gateway sign a transfer right now?", with the
#: time it was taken. Checked on every payment, so it must be cheap.
_signing_check: dict = {"ok": False, "checked_at": 0.0, "reason": "not checked yet"}
_SIGNING_CHECK_TTL = 30.0


async def can_sign() -> tuple:
    """Whether the hot wallet key is currently reachable.

    Without it no transfer can be made, and accepting a payment anyway means
    charging a card the gateway cannot serve — the customer is debited, the
    transfer fails, and the reversal machinery runs for a problem that was
    knowable before taking the money. A sealed Vault after a restart is the
    ordinary way this happens.

    The result is cached briefly: this sits on the payment path.
    """
    import time

    now = time.monotonic()
    if now - _signing_check["checked_at"] < _SIGNING_CHECK_TTL:
        return _signing_check["ok"], _signing_check["reason"]

    def probe() -> tuple:
        try:
            from gateway.crypto_service import CryptoService

            service = CryptoService()
            if service.account is None:
                return False, "no hot wallet key available (is Vault sealed?)"
            return True, "ok"
        except Exception as exc:
            return False, f"crypto service unavailable: {exc}"

    ok, reason = await asyncio.to_thread(probe)
    _signing_check.update({"ok": ok, "checked_at": now, "reason": reason})
    if not ok:
        LOGGER.critical(f"Gateway cannot sign transfers: {reason}. Refusing payments.")
    return ok, reason


def _duplicate_response(existing: Transaction) -> "PaymentResponse":
    return PaymentResponse(
        status=existing.status.value,
        message="Duplicate request. Returning the existing transaction.",
        transaction_id=existing.id,
        fiat_amount=existing.amount,
        tx_hash=existing.crypto_tx_hash,
        stan=existing.stan,
    )


async def _fail_transaction(tx_id: int, status: TransactionStatus, reason: str) -> None:
    async with async_session() as session:
        tx = await session.get(Transaction, tx_id)
        if tx is not None:
            tx.status = status
            tx.error_message = reason[:512]
            await session.commit()


@app.post("/pay", response_model=PaymentResponse, status_code=202)
@limiter.limit(lambda: f"{settings.rate_limit_per_minute}/minute")
async def process_payment(
    request: Request,
    payment_req: PaymentRequest,
    idempotency_key: str = Header(None, alias="Idempotency-Key"),
):
    """Authorise the fiat leg synchronously, then hand the crypto leg to a worker.

    Returns 202 once the bank has approved: the crypto transfer is asynchronous
    and its progress is available from ``GET /transaction/{id}``.

    Note the absence of a ``Depends(get_session)`` here. A dependency-injected
    session lives for the whole request, and this request spends most of its
    life waiting on the acquirer — up to ``BANK_TIMEOUT_SEC``. Pinning a
    database connection across that external call exhausts the pool under load:
    concurrent payments then queue on the pool and fail with a 500 after
    ``pool_timeout``, which is how a busy period turns into an outage. Each
    block below opens a session, does its work, and gives the connection back.
    """

    # ── Refuse before touching the card if we cannot deliver the crypto ─────
    signable, reason = await can_sign()
    if not signable:
        LOGGER.error(f"Refusing the payment before any debit: {reason}")
        raise HTTPException(
            status_code=503,
            detail="Service temporarily unavailable. No funds were debited.",
        )

    if not await web3_circuit_breaker.can_execute_async():
        LOGGER.warning("Circuit breaker is open; refusing the payment before any debit.")
        raise HTTPException(
            status_code=503,
            detail="Service temporarily unavailable. No funds were debited.",
        )

    # ── Idempotency ─────────────────────────────────────────────────────────
    if idempotency_key:
        async with async_session() as session:
            existing = (
                await session.execute(
                    select(Transaction).where(Transaction.idempotency_key == idempotency_key)
                )
            ).scalar_one_or_none()
            if existing:
                LOGGER.info(
                    f"Idempotency hit for key={idempotency_key} (transaction {existing.id})."
                )
                return _duplicate_response(existing)

    if not luhn_checksum(payment_req.pan):
        LOGGER.warning("Rejected a card number that fails the Luhn check.")
        raise HTTPException(status_code=400, detail="Invalid card number.")

    masked = mask_pan(payment_req.pan)
    LOGGER.info(
        f"Payment request: {payment_req.amount} {settings.acquirer_currency} "
        f"from {masked} to {payment_req.target_wallet}"
    )

    # ── Persist before authorising, so nothing is debited off the books ─────
    async with async_session() as session:
        transaction = Transaction(
            amount=payment_req.amount,
            currency=settings.acquirer_currency,
            masked_pan=masked,
            target_wallet=payment_req.target_wallet,
            idempotency_key=idempotency_key,
            status=TransactionStatus.PENDING,
        )
        session.add(transaction)
        try:
            await session.commit()
        except IntegrityError:
            # Two identical requests raced past the lookup above; the unique
            # index on the idempotency key is what actually prevents the
            # double charge.
            await session.rollback()
            existing = (
                await session.execute(
                    select(Transaction).where(Transaction.idempotency_key == idempotency_key)
                )
            ).scalar_one_or_none()
            if existing is None:
                raise HTTPException(status_code=409, detail="Conflicting request.")
            return _duplicate_response(existing)
        await session.refresh(transaction)
        tx_id = transaction.id

    # The worker needs the real PAN to reverse the payment if the crypto leg
    # fails, but it must not travel through the broker. Store it encrypted.
    try:
        await get_pan_vault().store(tx_id, payment_req.pan)
    except Exception as exc:
        LOGGER.error(f"Could not stage the PAN for transaction {tx_id}: {exc}")
        await _fail_transaction(tx_id, TransactionStatus.FIAT_DECLINED, "pan staging failed")
        raise HTTPException(status_code=503, detail="Service temporarily unavailable.")

    # ── Fiat authorisation ──────────────────────────────────────────────────
    try:
        response_iso = await acquirer_service.authorize_payment(
            pan=payment_req.pan,
            amount=payment_req.amount,
            expiry=payment_req.expiry,
            cvv=payment_req.cvv,
        )
    except AuthorizationTimeout as timeout:
        # We do not know whether the cardholder was debited. Flag it and let the
        # worker reverse it; treating this as a decline would risk keeping money.
        LOGGER.critical(
            f"Authorisation timed out for transaction {tx_id} (STAN {timeout.stan}). "
            "Outcome unknown; a reversal will be attempted."
        )
        async with async_session() as session:
            tx = await session.get(Transaction, tx_id)
            tx.status = TransactionStatus.FIAT_UNKNOWN
            tx.stan = timeout.stan
            tx.rrn = timeout.rrn
            tx.error_message = "No response from the acquirer."
            await session.commit()

        from gateway.worker import retry_reversal

        try:
            retry_reversal.apply_async(args=[tx_id], countdown=5)
        except Exception as exc:
            LOGGER.critical(f"Could not queue the reversal for transaction {tx_id}: {exc}")
        raise HTTPException(status_code=504, detail="Bank network did not respond.")

    except Exception as exc:
        LOGGER.error(f"Acquirer authorisation failed for transaction {tx_id}: {exc}")
        await _fail_transaction(tx_id, TransactionStatus.FIAT_DECLINED, str(exc))
        await get_pan_vault().purge(tx_id)
        raise HTTPException(status_code=502, detail="Bank network error.")

    action_code = str(response_iso.get("DE39", ""))
    stan = response_iso.get("_stan") or str(response_iso.get("DE11", "")).zfill(6)

    async with async_session() as session:
        tx = await session.get(Transaction, tx_id)
        tx.stan = stan
        tx.rrn = response_iso.get("_rrn")
        tx.action_code = action_code
        tx.auth_code = str(response_iso.get("DE38", "")) or None

        if action_code != "00":
            LOGGER.warning(f"Transaction {tx_id} declined by the bank (code {action_code}).")
            tx.mark_completed(TransactionStatus.FIAT_DECLINED)
            await session.commit()
            await get_pan_vault().purge(tx_id)
            raise HTTPException(status_code=400, detail="Payment declined by the bank.")

        tx.status = TransactionStatus.FIAT_APPROVED
        await session.commit()

    LOGGER.info(f"Fiat approved for transaction {tx_id} (STAN {stan}).")

    # ── Hand the crypto leg to the worker ───────────────────────────────────
    from gateway.worker import process_crypto_transfer

    try:
        process_crypto_transfer.apply_async(kwargs={"tx_id": tx_id})
    except Exception as exc:
        # The broker is down and the card has been charged. Reverse it now
        # rather than leave the customer paying for nothing.
        LOGGER.critical(f"Could not queue the crypto transfer for transaction {tx_id}: {exc}")
        from gateway.worker import _reverse_transaction

        try:
            await _reverse_transaction(tx_id, reason="crypto transfer could not be queued")
        except Exception as reversal_exc:
            LOGGER.critical(
                f"MANUAL ACTION REQUIRED — transaction {tx_id} was charged but neither "
                f"queued nor reversed: {reversal_exc}"
            )
        raise HTTPException(status_code=503, detail="Service temporarily unavailable.")

    return PaymentResponse(
        status=TransactionStatus.FIAT_APPROVED.value,
        message="Fiat payment approved. The crypto transfer is being processed.",
        transaction_id=tx_id,
        fiat_amount=payment_req.amount,
        stan=stan,
    )


@app.get("/transaction/{tx_id}")
async def get_transaction_status(tx_id: int, db: AsyncSession = Depends(get_session)):
    tx = await db.get(Transaction, tx_id)
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found.")
    return {
        "id": tx.id,
        "status": tx.status.value,
        "amount": str(tx.amount),
        "currency": tx.currency,
        "masked_pan": tx.masked_pan,
        "target_wallet": tx.target_wallet,
        "stan": tx.stan,
        "rrn": tx.rrn,
        "crypto_tx_hash": tx.crypto_tx_hash,
        # Internal error text stays internal; the customer sees the state only.
        "error": tx.error_message if not settings.is_production else None,
        "created_at": tx.created_at.isoformat() if tx.created_at else None,
        "completed_at": tx.completed_at.isoformat() if tx.completed_at else None,
    }
