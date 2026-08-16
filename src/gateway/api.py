import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, constr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from gateway.acquirer import AcquirerService
from gateway.crypto_service import CryptoService
from gateway.database import init_db, get_session
from gateway.models import Transaction, TransactionStatus
from gateway.circuit_breaker import web3_circuit_breaker

LOGGER = logging.getLogger(__name__)

# Constants
ERC20_TOKEN_ADDRESS = os.environ.get("ERC20_TOKEN_ADDRESS", "0xdAC17F958D2ee523a2206206994597C13D831ec7")
GATEWAY_API_KEY = os.environ.get("GATEWAY_API_KEY", "")
ENVIRONMENT = os.environ.get("ENVIRONMENT", "development")

# In-memory instances for the gateway
BANK_HOST = os.environ.get("BANK_HOST", "127.0.0.1")
BANK_PORT = int(os.environ.get("BANK_PORT", "9000"))
acquirer_service = AcquirerService(host=BANK_HOST, port=BANK_PORT)
crypto_service = CryptoService()

# Rate Limiter
limiter = Limiter(key_func=get_remote_address)

# ---------------------------------------------------------------------------
# Security Utilities
# ---------------------------------------------------------------------------

def luhn_checksum(card_number: str) -> bool:
    """Validate a card number using the Luhn algorithm."""
    try:
        def digits_of(n):
            return [int(d) for d in str(n)]
        digits = digits_of(card_number)
        odd_digits = digits[-1::-2]
        even_digits = [sum(digits_of(d * 2)) for d in digits[-2::-2]]
        return (sum(odd_digits) + sum(even_digits)) % 10 == 0
    except ValueError:
        return False

def mask_pan(pan: str) -> str:
    """Mask a PAN for safe logging and database storage."""
    return f"{pan[:6]}******{pan[-4:]}" if len(pan) >= 13 else "INVALID_PAN"

# ---------------------------------------------------------------------------
# FastAPI Application
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    LOGGER.info("Initializing database...")
    await init_db()
    LOGGER.info("Starting Acquirer Service...")
    await acquirer_service.start()
    yield
    LOGGER.info("Shutting down Acquirer Service...")
    await acquirer_service.stop()

# Disable docs in production
docs_kwargs = {}
if ENVIRONMENT == "production":
    docs_kwargs = {"docs_url": None, "redoc_url": None, "openapi_url": None}

app = FastAPI(title="2D Link Fiat-to-Crypto Gateway", lifespan=lifespan, **docs_kwargs)

# Rate limiter setup
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS setup
CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class PaymentRequest(BaseModel):
    pan: str = Field(..., pattern=r"^\d{13,19}$", description="Primary Account Number")
    expiry: str = Field(..., pattern=r"^\d{4}$", description="Expiry date in YYMM format")
    cvv: str = Field(..., pattern=r"^\d{3,4}$", description="Card Verification Value")
    amount: float = Field(..., gt=0, description="Amount in fiat (e.g. USD)")
    target_wallet: str = Field(..., pattern=r"^0x[a-fA-F0-9]{40}$", description="Destination crypto wallet address")

class PaymentResponse(BaseModel):
    status: str
    message: str
    transaction_id: int
    fiat_amount: float
    tx_hash: Optional[str] = None
    stan: Optional[str] = None

# ---------------------------------------------------------------------------
# API Key Authentication Middleware
# ---------------------------------------------------------------------------

@app.middleware("http")
async def authenticate_api_key(request: Request, call_next):
    """Reject requests without a valid API key using timing-safe comparison."""
    if GATEWAY_API_KEY:
        if request.url.path not in ["/docs", "/openapi.json", "/health", "/health/ready"]:
            api_key = request.headers.get("X-API-Key", "")
            if not secrets.compare_digest(api_key, GATEWAY_API_KEY):
                return JSONResponse(status_code=401, content={"detail": "Invalid or missing API key."})
    return await call_next(request)

# ---------------------------------------------------------------------------
# Health Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "environment": ENVIRONMENT}

@app.get("/health/ready")
async def health_ready(db: AsyncSession = Depends(get_session)):
    """Deep health check for infrastructure dependencies."""
    status = {"db": "ok", "redis": "ok", "bank": "ok", "overall": "ok"}
    try:
        await db.execute(select(1))
    except Exception as e:
        status["db"] = f"error: {str(e)}"
        status["overall"] = "error"
    
    # In a full implementation, we'd ping Redis and Vault here as well.
    if not acquirer_service.client._connected:
        status["bank"] = "disconnected"
        status["overall"] = "error"
        
    return JSONResponse(
        status_code=200 if status["overall"] == "ok" else 503,
        content=status
    )

# ---------------------------------------------------------------------------
# POST /pay Endpoint (Enterprise-Grade)
# ---------------------------------------------------------------------------

@app.post("/pay", response_model=PaymentResponse, status_code=202)
@limiter.limit("5/minute")
async def process_payment(
    request: Request,
    payment_req: PaymentRequest,
    db: AsyncSession = Depends(get_session),
    idempotency_key: str = Header(None, alias="Idempotency-Key"),
):
    """
    Process a 2D Link payment and dispatch crypto transfer to Celery worker.
    Returns 202 Accepted immediately after fiat approval.
    """

    # ── Step 0: Circuit Breaker Check ──────────────────────────────────
    if not web3_circuit_breaker.can_execute():
        LOGGER.warning("Circuit breaker is OPEN. Rejecting request to protect fiat funds.")
        raise HTTPException(
            status_code=503,
            detail="Service temporarily unavailable: blockchain network unreachable. No funds were debited."
        )

    # ── Step 1: Idempotency Check ──────────────────────────────────────
    if idempotency_key:
        stmt = select(Transaction).where(Transaction.idempotency_key == idempotency_key)
        result = await db.execute(stmt)
        existing_tx = result.scalar_one_or_none()
        if existing_tx:
            LOGGER.info(f"Idempotency hit for key={idempotency_key}. Returning existing Tx ID={existing_tx.id}")
            return PaymentResponse(
                status=existing_tx.status.value,
                message="Duplicate request. Returning existing transaction.",
                transaction_id=existing_tx.id,
                fiat_amount=existing_tx.amount,
                tx_hash=existing_tx.crypto_tx_hash,
                stan=existing_tx.stan
            )

    # ── Step 2: Luhn Validation ────────────────────────────────────────
    if not luhn_checksum(payment_req.pan):
        LOGGER.warning("Invalid PAN attempt blocked by Luhn check.")
        raise HTTPException(status_code=400, detail="Invalid card number.")

    masked = mask_pan(payment_req.pan)
    LOGGER.info(f"Payment request: {payment_req.amount} USD from card {masked} -> wallet {payment_req.target_wallet}")

    # ── Step 3: Create Transaction in DB ───────────────────────────────
    transaction = Transaction(
        amount=payment_req.amount,
        masked_pan=masked,
        target_wallet=payment_req.target_wallet,
        idempotency_key=idempotency_key,
        status=TransactionStatus.PENDING
    )
    db.add(transaction)
    await db.commit()
    await db.refresh(transaction)

    # ── Step 4: Authorize Fiat via ISO8583 (synchronous) ───────────────
    try:
        response_iso = await acquirer_service.authorize_payment(
            pan=payment_req.pan,
            amount=payment_req.amount,
            expiry=payment_req.expiry
        )
    except Exception as e:
        LOGGER.error(f"Acquirer authorization failed: {e}")
        transaction.status = TransactionStatus.FIAT_DECLINED
        transaction.error_message = str(e)
        await db.commit()
        raise HTTPException(status_code=502, detail="Bank network error.")

    action_code = str(response_iso.get("DE39", ""))
    stan = str(response_iso.get("DE11", ""))

    transaction.stan = stan
    transaction.action_code = action_code

    if action_code != "00":
        LOGGER.warning(f"Payment declined. Action code: {action_code}")
        transaction.status = TransactionStatus.FIAT_DECLINED
        await db.commit()
        raise HTTPException(status_code=400, detail="Payment declined by bank.")

    LOGGER.info(f"Fiat payment approved! STAN: {stan}, Tx ID: {transaction.id}")
    transaction.status = TransactionStatus.FIAT_APPROVED
    await db.commit()

    # ── Step 5: Dispatch crypto transfer to Celery Worker ──────────────
    from gateway.worker import process_crypto_transfer
    process_crypto_transfer.delay(
        tx_id=transaction.id,
        target_wallet=payment_req.target_wallet,
        amount_fiat=payment_req.amount,
        pan=payment_req.pan,
        stan=stan
    )

    LOGGER.info(f"Crypto transfer dispatched to worker for Tx ID: {transaction.id}")

    return PaymentResponse(
        status="FIAT_APPROVED",
        message="Fiat payment approved. Crypto transfer is being processed asynchronously.",
        transaction_id=transaction.id,
        fiat_amount=payment_req.amount,
        stan=stan
    )

# ---------------------------------------------------------------------------
# GET /transaction/{id} - Check transaction status
# ---------------------------------------------------------------------------

@app.get("/transaction/{tx_id}")
async def get_transaction_status(tx_id: int, db: AsyncSession = Depends(get_session)):
    """Poll the status of a transaction (used by the client to check if crypto was sent)."""
    tx = await db.get(Transaction, tx_id)
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found.")
    return {
        "id": tx.id,
        "status": tx.status.value,
        "amount": tx.amount,
        "masked_pan": tx.masked_pan,
        "target_wallet": tx.target_wallet,
        "stan": tx.stan,
        "crypto_tx_hash": tx.crypto_tx_hash,
        "error_message": tx.error_message,
        "created_at": str(tx.created_at)
    }
