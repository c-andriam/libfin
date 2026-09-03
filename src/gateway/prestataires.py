"""
Prestataire API — merchants and liquidity providers.

Two counterparties the gateway deals with that are neither the customer nor
the bank:

  * **Merchants** (commerçants) collect fiat through the gateway and receive
    the crypto in their own wallet. Each carries its own API key and fee rate
    so a compromise is contained and per-merchant economics can be tracked.
  * **Liquidity providers** (fournisseurs de liquidité) are the counterparties
    that fund the settlement. Each names a source wallet and a settlement
    wallet, so who funds the gateway and where the proceeds go is explicit.

Both are management endpoints, authenticated by the same gateway API key as
the payment endpoints: they mutate configuration and answer no cardholder.
"""

import re
import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.database import get_session
from gateway.models import (
    LiquidityProvider,
    LiquidityProviderStatus,
    Merchant,
    MerchantStatus,
)

router = APIRouter(prefix="/prestataires", tags=["prestataires"])


def _wallet(value: str) -> str:
    """A 0x-prefixed 40-hex-char address, lower-cased for uniqueness."""
    value = value.strip()
    if not value[:2].lower() == "0x" or len(value) != 42:
        raise ValueError("must be a 0x address (0x + 40 hex characters)")
    try:
        int(value[2:], 16)
    except ValueError:
        raise ValueError("must be a 0x address (0x + 40 hex characters)")
    return value.lower()


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _email(value: str) -> str:
    """A small, dependency-free email shape check.

    Deliberately lighter than RFC 5322 — the address is stored and echoed, not
    used to route mail, so a thorough parser buys nothing here. It rejects the
    obvious mistakes while avoiding a hard dependency on email-validator.
    """
    value = value.strip()
    if not _EMAIL_RE.match(value):
        raise ValueError("must be a valid email address")
    return value.lower()


def _num(value) -> str | None:
    """A NUMERIC as a clean decimal string, trailing zeros stripped."""
    if value is None:
        return None
    return str(value).rstrip("0").rstrip(".") if "." in str(value) else str(value)


def _merchant_payload(merchant: Merchant) -> dict:
    # The API key is deliberately NOT echoed: it is returned once at creation
    # and then redacted. Echoing it would hand it to anyone with read access.
    return {
        "id": merchant.id,
        "name": merchant.name,
        "legal_name": merchant.legal_name,
        "email": merchant.email,
        "target_wallet": merchant.target_wallet,
        "status": merchant.status.value,
        "fee_rate": _num(merchant.fee_rate),
        "kyc_verified": merchant.kyc_verified,
        "kyc_document_ref": merchant.kyc_document_ref,
        "created_at": merchant.created_at.isoformat() if merchant.created_at else None,
    }


def _provider_payload(provider: LiquidityProvider) -> dict:
    return {
        "id": provider.id,
        "name": provider.name,
        "contact": provider.contact,
        "email": provider.email,
        "source_wallet": provider.source_wallet,
        "settlement_wallet": provider.settlement_wallet,
        "status": provider.status.value,
        "daily_limit_units": _num(provider.daily_limit_units),
        "settlement_token_symbol": provider.settlement_token_symbol,
        "created_at": provider.created_at.isoformat() if provider.created_at else None,
    }


# ---------------------------------------------------------------------------
# Merchants
# ---------------------------------------------------------------------------


class MerchantCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    legal_name: str = Field(..., min_length=1, max_length=128)
    email: str = Field(...)
    target_wallet: str = Field(...)
    fee_rate: str = Field(default="0.00")

    @field_validator("email")
    @classmethod
    def _email_value(cls, value: str) -> str:
        return _email(value)

    @field_validator("target_wallet")
    @classmethod
    def _wallet_value(cls, value: str) -> str:
        return _wallet(value)

    @field_validator("fee_rate")
    @classmethod
    def _fee(cls, value: str) -> str:
        from decimal import Decimal

        rate = Decimal(value)
        if rate < 0 or rate > 1:
            raise ValueError("fee_rate must be between 0 and 1")
        return str(rate)


class MerchantUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    legal_name: str | None = Field(default=None, min_length=1, max_length=128)
    email: str | None = None
    target_wallet: str | None = None
    status: MerchantStatus | None = None
    fee_rate: str | None = None
    kyc_verified: bool | None = None
    kyc_document_ref: str | None = Field(default=None, max_length=128)

    @field_validator("email")
    @classmethod
    def _email_value(cls, value):
        if value is None:
            return value
        return _email(value)

    @field_validator("target_wallet")
    @classmethod
    def _wallet_value(cls, value):
        if value is None:
            return value
        return _wallet(value)

    @field_validator("fee_rate")
    @classmethod
    def _fee(cls, value):
        if value is None:
            return value
        from decimal import Decimal

        rate = Decimal(value)
        if rate < 0 or rate > 1:
            raise ValueError("fee_rate must be between 0 and 1")
        return str(rate)


@router.post("/merchants", status_code=201)
async def create_merchant(body: MerchantCreate, db: AsyncSession = Depends(get_session)):
    merchant = Merchant(
        name=body.name,
        legal_name=body.legal_name,
        email=body.email,
        target_wallet=body.target_wallet,
        # A fresh key per merchant, returned exactly once. 32 random bytes in
        # hex (64 chars), the same shape as a bearer token the frontend/relay
        # can carry in an X-API-Key header.
        api_key=secrets.token_hex(32),
        fee_rate=body.fee_rate,
        status=MerchantStatus.INACTIVE,
        kyc_verified=False,
    )
    db.add(merchant)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="A merchant with this wallet or key already exists.")
    await db.refresh(merchant)
    payload = _merchant_payload(merchant)
    # The plaintext key only ever leaves the server this once.
    payload["api_key"] = merchant.api_key
    return payload


@router.get("/merchants")
async def list_merchants(
    status: MerchantStatus | None = None, db: AsyncSession = Depends(get_session)
):
    statement = select(Merchant).order_by(Merchant.id)
    if status is not None:
        statement = statement.where(Merchant.status == status)
    result = await db.execute(statement)
    return [_merchant_payload(m) for m in result.scalars().all()]


@router.get("/merchants/{merchant_id}")
async def get_merchant(merchant_id: int, db: AsyncSession = Depends(get_session)):
    merchant = await db.get(Merchant, merchant_id)
    if merchant is None:
        raise HTTPException(status_code=404, detail="Merchant not found.")
    return _merchant_payload(merchant)


@router.patch("/merchants/{merchant_id}")
async def update_merchant(
    merchant_id: int, body: MerchantUpdate, db: AsyncSession = Depends(get_session)
):
    merchant = await db.get(Merchant, merchant_id)
    if merchant is None:
        raise HTTPException(status_code=404, detail="Merchant not found.")

    updates = body.model_dump(exclude_unset=True)
    for field_name, value in updates.items():
        setattr(merchant, field_name, value)

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="A merchant with this wallet already exists.")
    await db.refresh(merchant)
    return _merchant_payload(merchant)


@router.delete("/merchants/{merchant_id}", status_code=204)
async def delete_merchant(merchant_id: int, db: AsyncSession = Depends(get_session)):
    merchant = await db.get(Merchant, merchant_id)
    if merchant is None:
        raise HTTPException(status_code=404, detail="Merchant not found.")
    await db.delete(merchant)
    await db.commit()


# ---------------------------------------------------------------------------
# Liquidity providers
# ---------------------------------------------------------------------------


class LiquidityProviderCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    contact: str | None = Field(default=None, max_length=256)
    email: str = Field(...)
    source_wallet: str = Field(...)
    settlement_wallet: str = Field(...)
    settlement_token_symbol: str = Field(default="USDT", min_length=2, max_length=12)
    daily_limit_units: str | None = None

    @field_validator("email")
    @classmethod
    def _email_value(cls, value: str) -> str:
        return _email(value)

    @field_validator("source_wallet", "settlement_wallet")
    @classmethod
    def _wallets(cls, value: str) -> str:
        return _wallet(value)

    @field_validator("settlement_token_symbol")
    @classmethod
    def _symbol(cls, value: str) -> str:
        return value.upper()

    @field_validator("daily_limit_units")
    @classmethod
    def _limit(cls, value):
        if value is None:
            return value
        from decimal import Decimal

        limit = Decimal(value)
        if limit < 0:
            raise ValueError("daily_limit_units must be non-negative")
        return str(limit)


class LiquidityProviderUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    contact: str | None = Field(default=None, max_length=256)
    email: str | None = None
    source_wallet: str | None = None
    settlement_wallet: str | None = None
    status: LiquidityProviderStatus | None = None
    settlement_token_symbol: str | None = Field(default=None, min_length=2, max_length=12)
    daily_limit_units: str | None = None

    @field_validator("email")
    @classmethod
    def _email_value(cls, value):
        if value is None:
            return value
        return _email(value)

    @field_validator("source_wallet", "settlement_wallet")
    @classmethod
    def _wallets(cls, value):
        if value is None:
            return value
        return _wallet(value)

    @field_validator("settlement_token_symbol")
    @classmethod
    def _symbol(cls, value):
        if value is None:
            return value
        return value.upper()


@router.post("/liquidity-providers", status_code=201)
async def create_provider(
    body: LiquidityProviderCreate, db: AsyncSession = Depends(get_session)
):
    provider = LiquidityProvider(
        name=body.name,
        contact=body.contact,
        email=body.email,
        source_wallet=body.source_wallet,
        settlement_wallet=body.settlement_wallet,
        settlement_token_symbol=body.settlement_token_symbol,
        daily_limit_units=body.daily_limit_units,
        status=LiquidityProviderStatus.ACTIVE,
    )
    db.add(provider)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="A provider with this settlement wallet already exists.")
    await db.refresh(provider)
    return _provider_payload(provider)


@router.get("/liquidity-providers")
async def list_providers(
    status: LiquidityProviderStatus | None = None, db: AsyncSession = Depends(get_session)
):
    statement = select(LiquidityProvider).order_by(LiquidityProvider.id)
    if status is not None:
        statement = statement.where(LiquidityProvider.status == status)
    result = await db.execute(statement)
    return [_provider_payload(p) for p in result.scalars().all()]


@router.get("/liquidity-providers/{provider_id}")
async def get_provider(provider_id: int, db: AsyncSession = Depends(get_session)):
    provider = await db.get(LiquidityProvider, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Liquidity provider not found.")
    return _provider_payload(provider)


@router.patch("/liquidity-providers/{provider_id}")
async def update_provider(
    provider_id: int, body: LiquidityProviderUpdate, db: AsyncSession = Depends(get_session)
):
    provider = await db.get(LiquidityProvider, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Liquidity provider not found.")

    updates = body.model_dump(exclude_unset=True)
    for field_name, value in updates.items():
        setattr(provider, field_name, value)

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="A provider with this settlement wallet already exists."
        )
    await db.refresh(provider)
    return _provider_payload(provider)


@router.delete("/liquidity-providers/{provider_id}", status_code=204)
async def delete_provider(provider_id: int, db: AsyncSession = Depends(get_session)):
    provider = await db.get(LiquidityProvider, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Liquidity provider not found.")
    await db.delete(provider)
    await db.commit()
