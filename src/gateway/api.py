import asyncio
import ipaddress
import logging
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, field_validator, model_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.acquirer import AcquirerService, AuthorizationTimeout
from gateway.action_codes import describe, is_approved
from gateway.circuit_breaker import web3_circuit_breaker
from gateway.config import settings
from gateway.currency import UnsupportedCurrency, get as get_currency
from gateway.database import async_session, engine, get_session, init_db
from gateway.exchange_rate import RateUnavailable, apply_spread, build_rate_source
from gateway.models import PaymentLink, Transaction, TransactionStatus, utcnow
from gateway.outbox import enqueue, try_publish_now
from gateway.observability import (
    configure_logging,
    correlation_id,
    new_correlation_id,
    transaction_id as transaction_id_var,
)
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
    configure_logging(settings.log_level, structured=settings.log_json)
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
async def attach_correlation_id(request: Request, call_next):
    """Give every request an identifier and hand it back in the response.

    Registered last, which in Starlette means it runs first — an unauthenticated
    or rate-limited request is exactly the kind worth tracing, so it must be
    stamped before any other middleware can reject it.
    """
    incoming = request.headers.get("X-Correlation-Id", "")
    cid = incoming if re.fullmatch(r"[A-Za-z0-9_-]{8,64}", incoming or "") else new_correlation_id()

    token = correlation_id.set(cid)
    tx_token = transaction_id_var.set(None)
    try:
        request.state.correlation_id = cid
        response = await call_next(request)
        response.headers["X-Correlation-Id"] = cid
        return response
    finally:
        correlation_id.reset(token)
        transaction_id_var.reset(tx_token)


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
    """Card data plus an order — either stated outright, or named by a link.

    The two forms exist for two callers. A merchant driving the API directly
    states the order. A payer following a 2D link sends only ``link``: the
    amount and the destination wallet are read from the row that link names,
    so nothing the payer's browser sends can alter either. That is the whole
    point of the token — the order is not in the payer's hands to change.
    """

    pan: str = Field(..., pattern=r"^\d{13,19}$", description="Primary Account Number")
    expiry: str = Field(..., pattern=r"^\d{4}$", description="Expiry date, YYMM")
    cvv: str = Field(..., pattern=r"^\d{3,4}$", description="Card Verification Value")

    #: A payment-link token. When present the order fields must be absent.
    link: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9_-]{16,64}$")

    #: Optional only because a link supplies them instead. The validator below
    #: refuses a request that carries neither, so the direct path is as strict
    #: as it was.
    amount: Optional[Decimal] = Field(default=None, gt=0, decimal_places=2)
    #: ISO 4217 alphabetic. Explicit rather than assumed: an amount without a
    #: currency is ambiguous, and the ambiguity is only ever resolved wrongly.
    currency: str = Field(
        default="USD",
        pattern=r"^[A-Za-z]{3}$",
        description="ISO 4217 currency of the amount, e.g. USD, EUR, GBP",
    )
    target_wallet: Optional[str] = Field(default=None, pattern=r"^0x[a-fA-F0-9]{40}$")

    @model_validator(mode="after")
    def one_form_or_the_other(self):
        if self.link:
            # Accepting both would leave it ambiguous which one governs, and
            # every such ambiguity is eventually resolved in the attacker's
            # favour. Refuse rather than silently prefer one.
            stated = [n for n in ("amount", "target_wallet") if getattr(self, n) is not None]
            if stated:
                raise ValueError(
                    "Send either 'link' or the order fields, not both "
                    f"(got 'link' with {', '.join(sorted(stated))})."
                )
        else:
            missing = [n for n in ("amount", "target_wallet") if getattr(self, n) is None]
            if missing:
                raise ValueError(
                    f"Missing {', '.join(sorted(missing))}; send them, or a 'link' instead."
                )
        return self

    @field_validator("currency")
    @classmethod
    def supported(cls, value: str) -> str:
        try:
            return get_currency(value).alpha
        except UnsupportedCurrency as exc:
            raise ValueError(str(exc))

    @field_validator("amount")
    @classmethod
    def within_limits(cls, value: Optional[Decimal]) -> Optional[Decimal]:
        # None is legitimate here: a link-borne request has no amount of its
        # own, and the amount it resolves to was checked when the link was made.
        if value is None:
            return value
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


#: Idempotency keys are echoed into logs and stored in a VARCHAR(64). An
#: unvalidated header therefore turns a client mistake into a 500 from the
#: database layer, and puts arbitrary caller-supplied text in the log stream.
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{8,64}$")


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
        await get_pan_vault().ping()
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


#: Built once per process. Chainlink needs a Web3 connection; the fixed source
#: needs nothing. Cached because it holds a per-pair movement reference that
#: only means something across successive quotes.
_rate_source = None
_rate_source_lock = asyncio.Lock()


async def get_rate_source():
    global _rate_source
    if _rate_source is None:
        async with _rate_source_lock:
            if _rate_source is None:
                if settings.rate_source == "fixed":
                    _rate_source = await asyncio.to_thread(build_rate_source)
                else:
                    from gateway.crypto_service import CryptoService

                    if settings.rate_rpc_url:
                        # A separate, read-only connection for prices. Kept
                        # apart from the signing connection on purpose: the
                        # chain money settles on and the chain prices are read
                        # from need not be the same, and a rate lookup must
                        # never be able to touch a key.
                        from web3 import Web3

                        rate_w3 = Web3(
                            Web3.HTTPProvider(
                                settings.rate_rpc_url, request_kwargs={"timeout": 20}
                            )
                        )
                        _rate_source = build_rate_source(rate_w3)
                    else:
                        service = await asyncio.to_thread(CryptoService)
                        _rate_source = build_rate_source(service.w3)
    return _rate_source


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
            tx.transition_to(status)
            tx.error_message = reason[:512]
            await session.commit()


class LinkRequest(BaseModel):
    """An order a merchant wants someone else to pay."""

    amount: Decimal = Field(..., gt=0, decimal_places=2)
    currency: str = Field(default="USD", pattern=r"^[A-Za-z]{3}$")
    target_wallet: str = Field(..., pattern=r"^0x[a-fA-F0-9]{40}$")

    @field_validator("currency")
    @classmethod
    def supported(cls, value: str) -> str:
        try:
            return get_currency(value).alpha
        except UnsupportedCurrency as exc:
            raise ValueError(str(exc))

    @field_validator("amount")
    @classmethod
    def within_limits(cls, value: Decimal) -> Decimal:
        # Checked here, at issue time, so a merchant learns immediately rather
        # than after sending an unpayable link to a customer.
        if value < settings.amount_min or value > settings.amount_max:
            raise ValueError(
                f"Amount must be between {settings.amount_min} and {settings.amount_max}."
            )
        return value


class LinkResponse(BaseModel):
    token: str
    #: Absent when the link has no deadline, which is the default.
    expires_at: Optional[datetime] = None


class LinkSummary(BaseModel):
    """A link as its merchant sees it — the whole row, wallet included."""

    id: int
    token: str
    amount: Decimal
    currency: str
    target_wallet: str
    active: bool
    created_at: datetime
    expires_at: Optional[datetime] = None
    used_at: Optional[datetime] = None
    payment_count: int
    transaction_id: Optional[int] = None


class LinkUpdate(BaseModel):
    active: bool


class LinkView(BaseModel):
    """What a payer may know about a link: the amount, and nothing else.

    Deliberately not the destination wallet. A cardholder must be told what
    they are about to be charged — card scheme rules and plain fairness both
    require it — but where the crypto goes is the merchant's business, and
    disclosing it to whoever holds the link serves no one.
    """

    amount: Decimal
    currency: str


@app.post("/link", response_model=LinkResponse, status_code=201)
@limiter.limit(lambda: f"{settings.rate_limit_per_minute}/minute")
async def create_link(request: Request, link_req: LinkRequest):
    """Record an order and return the token that names it."""
    token = secrets.token_urlsafe(24)
    # No deadline by default. A link is retired by its merchant, from the
    # console, not by a clock they never set — an expiry nobody chose is a
    # payment that silently stops working.
    expires = None
    if settings.payment_link_ttl_sec > 0:
        expires = utcnow() + timedelta(seconds=settings.payment_link_ttl_sec)

    async with async_session() as session:
        session.add(
            PaymentLink(
                token=token,
                amount=link_req.amount,
                currency=link_req.currency,
                target_wallet=link_req.target_wallet,
                expires_at=expires,
            )
        )
        await session.commit()

    LOGGER.info(
        f"Payment link issued for {link_req.amount} {link_req.currency}"
        + (f", expiring {expires.isoformat()}." if expires else ", no expiry.")
    )
    return LinkResponse(token=token, expires_at=expires)


@app.get("/links", response_model=List[LinkSummary])
@limiter.limit(lambda: f"{settings.rate_limit_per_minute}/minute")
async def list_links(request: Request):
    """Every link this merchant has issued, newest first.

    Reachable from the public origin, but deliberately not given the relay's
    injected API key: whoever calls this must hold the key themselves. It
    discloses destination wallets and leads to the routes below, which switch
    links off and delete them — so the credential is the protection, and it is
    the one thing the relay refuses to supply on a caller's behalf.
    """
    async with async_session() as session:
        rows = (
            await session.execute(select(PaymentLink).order_by(PaymentLink.id.desc()))
        ).scalars().all()
    return [LinkSummary.model_validate(r, from_attributes=True) for r in rows]


@app.patch("/links/{link_id}", response_model=LinkSummary)
@limiter.limit(lambda: f"{settings.rate_limit_per_minute}/minute")
async def update_link(request: Request, link_id: int, change: LinkUpdate):
    """Switch a link on or off. The row and its history are untouched."""
    async with async_session() as session:
        link = await session.get(PaymentLink, link_id)
        if link is None:
            raise HTTPException(status_code=404, detail="No such link.")
        link.active = change.active
        await session.commit()
        await session.refresh(link)
        LOGGER.info(
            f"Payment link {link_id} turned {'on' if change.active else 'off'}."
        )
        return LinkSummary.model_validate(link, from_attributes=True)


@app.delete("/links/{link_id}", status_code=204)
@limiter.limit(lambda: f"{settings.rate_limit_per_minute}/minute")
async def delete_link(request: Request, link_id: int):
    """Remove a link for good.

    The payments it produced are not touched: they live in `transactions`,
    which is the record that matters. What disappears is the ability to make
    more of them.
    """
    async with async_session() as session:
        link = await session.get(PaymentLink, link_id)
        if link is None:
            raise HTTPException(status_code=404, detail="No such link.")
        await session.delete(link)
        await session.commit()
    LOGGER.info(f"Payment link {link_id} deleted.")
    return Response(status_code=204)


@app.get("/link/{token}", response_model=LinkView)
@limiter.limit(lambda: f"{settings.rate_limit_per_minute}/minute")
async def read_link(request: Request, token: str):
    """The amount this link will charge. Unauthenticated: the token is the key."""
    async with async_session() as session:
        link = (
            await session.execute(select(PaymentLink).where(PaymentLink.token == token))
        ).scalar_one_or_none()

    # One message for absent, expired and spent alike. Distinguishing them
    # would let anyone holding a wrong token learn whether it ever existed.
    if link is None or not link.is_spendable():
        raise HTTPException(status_code=404, detail="This payment link is not valid.")
    return LinkView(amount=link.amount, currency=link.currency)


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

    # ── A link, if that is how the order arrived ────────────────────────────
    # Resolved before anything is done with the card, and resolved from the
    # database rather than from the request: this is the step that makes the
    # amount and the destination wallet unalterable by the payer.
    link_row_id = None
    if payment_req.link:
        async with async_session() as session:
            link = (
                await session.execute(
                    select(PaymentLink).where(PaymentLink.token == payment_req.link)
                )
            ).scalar_one_or_none()
            if link is None or not link.is_spendable():
                # Same answer for absent, expired and already paid. A payer who
                # follows a dead link learns it is dead, and nothing else.
                raise HTTPException(
                    status_code=404, detail="This payment link is not valid."
                )
            link_row_id = link.id
            payment_req = payment_req.model_copy(
                update={
                    "amount": Decimal(str(link.amount)),
                    "currency": link.currency,
                    "target_wallet": link.target_wallet,
                }
            )

    # ── Idempotency ─────────────────────────────────────────────────────────
    if idempotency_key is not None and not IDEMPOTENCY_KEY_PATTERN.match(idempotency_key):
        raise HTTPException(
            status_code=400,
            detail=(
                "Idempotency-Key must be 8 to 64 characters, using letters, digits, "
                "and any of _ . : -"
            ),
        )

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

    # ── Lock the rate before anything is authorised ─────────────────────────
    # Quoted here and recorded on the transaction, so delivery uses this rate
    # and not a fresh lookup. A rate that moved in between would hand the
    # customer something other than what they agreed to, and a retry days later
    # would use a rate from a different world entirely.
    # The pair follows the currency the customer is paying in, not a single
    # value fixed for the whole deployment.
    rate_pair = f"{payment_req.currency}/{settings.rate_token_symbol}"
    try:
        rate_source = await get_rate_source()
        quote = apply_spread(await rate_source.quote(rate_pair))
    except RateUnavailable as exc:
        # No fallback to a constant. A gateway that quietly reverts to a stale
        # or made-up rate delivers wrong amounts and says nothing.
        LOGGER.error(f"Refusing the payment: no usable rate for {rate_pair} ({exc})")
        raise HTTPException(
            status_code=503,
            detail="Service temporarily unavailable. No funds were debited.",
        )

    masked = mask_pan(payment_req.pan)
    LOGGER.info(
        f"Payment request: {payment_req.amount} {payment_req.currency} "
        f"from {masked} to {payment_req.target_wallet}"
    )

    # ── Persist before authorising, so nothing is debited off the books ─────
    async with async_session() as session:
        transaction = Transaction(
            amount=payment_req.amount,
            currency=get_currency(payment_req.currency).numeric,
            masked_pan=masked,
            target_wallet=payment_req.target_wallet,
            idempotency_key=idempotency_key,
            status=TransactionStatus.PENDING,
            exchange_rate=quote.rate,
            exchange_rate_source=quote.source[:64],
            exchange_rate_at=datetime.fromtimestamp(quote.observed_at, timezone.utc),
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

        # Record the use in the same commit as the payment it belongs to, so
        # the count can never drift from the transactions it describes. Links
        # are reusable, so this counts rather than consumes: what stops a link
        # is the merchant switching it off, not a payment having happened.
        # The increment is done in SQL rather than read-modify-write, so two
        # concurrent payments both land instead of one overwriting the other.
        if link_row_id is not None:
            await session.execute(
                update(PaymentLink)
                .where(PaymentLink.id == link_row_id)
                .values(
                    used_at=utcnow(),
                    transaction_id=tx_id,
                    payment_count=PaymentLink.payment_count + 1,
                )
            )
            await session.commit()
    transaction_id_var.set(tx_id)

    # The worker needs the real PAN to reverse the payment if the crypto leg
    # fails, but it must not travel through the broker. Store it encrypted.
    try:
        await get_pan_vault().store(tx_id, payment_req.pan)
    except Exception as exc:
        LOGGER.error(f"Could not stage the PAN for transaction {tx_id}: {exc}")
        await _fail_transaction(tx_id, TransactionStatus.FIAT_DECLINED, "pan staging failed")
        raise HTTPException(status_code=503, detail="Service temporarily unavailable.")

    # ── Fiat authorisation ──────────────────────────────────────────────────
    # In auth_capture mode this places a hold and moves no money; the debit
    # happens only once the crypto is confirmed on-chain. That ordering is what
    # turns a delivery failure from "refund owed" into "hold released".
    authorize = (
        acquirer_service.authorize_only
        if settings.uses_auth_capture
        else acquirer_service.authorize_payment
    )
    try:
        response_iso = await authorize(
            pan=payment_req.pan,
            amount=payment_req.amount,
            expiry=payment_req.expiry,
            cvv=payment_req.cvv,
            currency=payment_req.currency,
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
            tx.transition_to(TransactionStatus.FIAT_UNKNOWN)
            tx.stan = timeout.stan
            tx.rrn = timeout.rrn
            tx.error_message = "No response from the acquirer."

            # Through the outbox for the same reason as the transfer: this is
            # the branch where the cardholder may already have been debited, so
            # losing the reversal is the worst outcome in the system.
            recovery = enqueue(
                session,
                task_name="retry_reversal",
                payload={"tx_id": tx_id, "correlation_id": correlation_id.get()},
                countdown=5,
            )
            await session.commit()
            recovery_id = recovery.id

        await try_publish_now(recovery_id)
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

        if not is_approved(action_code):
            LOGGER.warning(
                f"Transaction {tx_id} declined by the bank: {describe(action_code)}."
            )
            tx.mark_completed(TransactionStatus.FIAT_DECLINED)
            await session.commit()
            await get_pan_vault().purge(tx_id)
            raise HTTPException(status_code=400, detail="Payment declined by the bank.")

        approved_state = (
            TransactionStatus.FIAT_AUTHORIZED
            if settings.uses_auth_capture
            else TransactionStatus.FIAT_APPROVED
        )
        tx.transition_to(approved_state)

        # The dispatch intent is written here, in the same commit as the state
        # change. Publishing to the broker afterwards is a separate, retryable
        # step reading from a durable row — so a process that dies between the
        # two leaves a record of work to do rather than a charged card with
        # nothing queued against it.
        message = enqueue(
            session,
            task_name="process_crypto_transfer",
            payload={"tx_id": tx_id, "correlation_id": correlation_id.get()},
        )
        await session.commit()
        message_id = message.id

    LOGGER.info(
        f"Transaction {tx_id} {approved_state.value} (STAN {stan})."
        + (" No money has moved yet." if settings.uses_auth_capture else "")
    )

    # ── Hand the crypto leg to the worker ───────────────────────────────────
    # An optimisation, not the guarantee: the work is already durably recorded
    # above. If the broker is unreachable the relay publishes it on its next
    # pass, so a broker outage delays a payment instead of stranding it — and
    # the customer is not told to try again for something already in hand.
    await try_publish_now(message_id)

    return PaymentResponse(
        status=approved_state.value,
        message=(
            "Card authorised. The crypto transfer is being processed; the card is "
            "charged once it is confirmed."
            if settings.uses_auth_capture
            else "Fiat payment approved. The crypto transfer is being processed."
        ),
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
