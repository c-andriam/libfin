"""
Prestataire API tests — merchants and liquidity providers.

These are management endpoints: they record who the gateway transacts with and
on what terms. The tests focus on the shape of a counterparty (wallet format,
email format), the one-time API key contract, and the duplicate-wallet guard,
since a mis-recorded counterparty is where money gets attributed to the wrong
place.
"""

import pytest
from decimal import Decimal

from gateway.models import LiquidityProviderStatus, MerchantStatus

pytestmark = pytest.mark.asyncio(loop_scope="function")

MERCHANT_WALLET = "0x742d35cc6634c0532925a3b844bc454e4438f44e"
PROVIDER_SOURCE = "0x70997970c51812dc3a010c7d01b50e0d17dc79c8"
PROVIDER_SETTLEMENT = "0x3c44cdddb6a900fa2b585dd299e03d12fa4293bc"


def merchant_payload(wallet: str = MERCHANT_WALLET, **overrides) -> dict:
    body = {
        "name": "Cafe du Coin",
        "legal_name": "Cafe du Coin SARL",
        "email": "billing@cafeducoin.example",
        "target_wallet": wallet,
        "fee_rate": "0.02",
    }
    body.update(overrides)
    return body


def provider_payload(**overrides) -> dict:
    body = {
        "name": "MWSS Pools",
        "contact": "Ops Team",
        "email": "ops@mwssexample.example",
        "source_wallet": PROVIDER_SOURCE,
        "settlement_wallet": PROVIDER_SETTLEMENT,
        "settlement_token_symbol": "USDT",
        "daily_limit_units": "1000000",
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Merchants
# ---------------------------------------------------------------------------


async def test_create_merchant_returns_key_once_and_redacts_it_after(gateway_client):
    created = await gateway_client.post(
        "/prestataires/merchants", json=merchant_payload()
    )
    assert created.status_code == 201, created.text
    body = created.json()

    assert body["name"] == "Cafe du Coin"
    assert body["status"] == MerchantStatus.INACTIVE.value
    assert body["fee_rate"] == "0.02"
    assert body["kyc_verified"] is False
    # The plaintext key is part of this response and only this one.
    api_key = body.pop("api_key")
    assert len(api_key) == 64

    merchant_id = body["id"]

    # Every subsequent read redacts it.
    fetched = (await gateway_client.get(f"/prestataires/merchants/{merchant_id}")).json()
    assert "api_key" not in fetched


async def test_merchant_wallet_is_lower_cased_and_validated(gateway_client):
    created = await gateway_client.post(
        "/prestataires/merchants",
        json=merchant_payload(wallet=MERCHANT_WALLET.upper()),
    )
    assert created.status_code == 201, created.text
    assert created.json()["target_wallet"] == MERCHANT_WALLET

    bad = await gateway_client.post(
        "/prestataires/merchants", json=merchant_payload(wallet="not-an-address")
    )
    assert bad.status_code == 422


async def test_merchant_email_is_validated(gateway_client):
    bad = await gateway_client.post(
        "/prestataires/merchants", json=merchant_payload(email="not-an-email")
    )
    assert bad.status_code == 422


async def test_duplicate_merchant_wallet_is_rejected(gateway_client):
    first = await gateway_client.post(
        "/prestataires/merchants", json=merchant_payload()
    )
    assert first.status_code == 201

    second = await gateway_client.post(
        "/prestataires/merchants", json=merchant_payload()
    )
    assert second.status_code == 409


async def test_list_merchants_filter_by_status(gateway_client):
    await gateway_client.post(
        "/prestataires/merchants", json=merchant_payload()
    )

    all_merchants = (await gateway_client.get("/prestataires/merchants")).json()
    assert len(all_merchants) == 1
    inactive = (
        await gateway_client.get("/prestataires/merchants?status=INACTIVE")
    ).json()
    assert len(inactive) == 1
    active = (
        await gateway_client.get("/prestataires/merchants?status=ACTIVE")
    ).json()
    assert len(active) == 0


async def test_update_merchant(gateway_client):
    created = (
        await gateway_client.post(
            "/prestataires/merchants", json=merchant_payload()
        )
    ).json()
    merchant_id = created["id"]

    updated = await gateway_client.patch(
        f"/prestataires/merchants/{merchant_id}",
        json={"status": "ACTIVE", "kyc_verified": True, "fee_rate": "0.015"},
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["status"] == MerchantStatus.ACTIVE.value
    assert body["kyc_verified"] is True
    assert Decimal(body["fee_rate"]) == Decimal("0.015")


async def test_delete_merchant(gateway_client):
    created = (
        await gateway_client.post(
            "/prestataires/merchants", json=merchant_payload()
        )
    ).json()
    merchant_id = created["id"]

    removed = await gateway_client.delete(f"/prestataires/merchants/{merchant_id}")
    assert removed.status_code == 204

    assert (
        await gateway_client.get(f"/prestataires/merchants/{merchant_id}")
    ).status_code == 404


async def test_unknown_merchant_returns_404(gateway_client):
    assert (await gateway_client.get("/prestataires/merchants/999999")).status_code == 404


# ---------------------------------------------------------------------------
# Liquidity providers
# ---------------------------------------------------------------------------


async def test_create_and_get_provider(gateway_client):
    created = await gateway_client.post(
        "/prestataires/liquidity-providers", json=provider_payload()
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "MWSS Pools"
    assert body["status"] == LiquidityProviderStatus.ACTIVE.value
    assert body["settlement_token_symbol"] == "USDT"
    assert body["source_wallet"] == PROVIDER_SOURCE

    fetched = (
        await gateway_client.get(f"/prestataires/liquidity-providers/{body['id']}")
    ).json()
    assert fetched["settlement_wallet"] == PROVIDER_SETTLEMENT


async def test_provider_wallets_are_validated(gateway_client):
    bad = await gateway_client.post(
        "/prestataires/liquidity-providers",
        json=provider_payload(source_wallet="nope"),
    )
    assert bad.status_code == 422


async def test_duplicate_provider_settlement_is_rejected(gateway_client):
    first = await gateway_client.post(
        "/prestataires/liquidity-providers", json=provider_payload()
    )
    assert first.status_code == 201

    second = await gateway_client.post(
        "/prestataires/liquidity-providers",
        json=provider_payload(source_wallet=PROVIDER_SOURCE),
    )
    assert second.status_code == 409


async def test_list_providers_filter_by_status(gateway_client):
    await gateway_client.post(
        "/prestataires/liquidity-providers", json=provider_payload()
    )
    active = (
        await gateway_client.get("/prestataires/liquidity-providers?status=ACTIVE")
    ).json()
    assert len(active) == 1
    inactive = (
        await gateway_client.get("/prestataires/liquidity-providers?status=INACTIVE")
    ).json()
    assert len(inactive) == 0


async def test_update_provider_status(gateway_client):
    created = (
        await gateway_client.post(
            "/prestataires/liquidity-providers", json=provider_payload()
        )
    ).json()
    provider_id = created["id"]

    updated = await gateway_client.patch(
        f"/prestataires/liquidity-providers/{provider_id}",
        json={"status": "INACTIVE", "daily_limit_units": "500000"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["status"] == LiquidityProviderStatus.INACTIVE.value
    assert updated.json()["daily_limit_units"] == "500000"


async def test_delete_provider(gateway_client):
    created = (
        await gateway_client.post(
            "/prestataires/liquidity-providers", json=provider_payload()
        )
    ).json()
    provider_id = created["id"]

    removed = await gateway_client.delete(
        f"/prestataires/liquidity-providers/{provider_id}"
    )
    assert removed.status_code == 204
    assert (
        await gateway_client.get(f"/prestataires/liquidity-providers/{provider_id}")
    ).status_code == 404
