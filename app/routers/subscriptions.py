"""
Subscription management endpoints — create checkout sessions, get status.
Called by nexpo-admin server actions.
"""
import logging
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException

from app.services.polar_service import get_polar_service
from app.services.payos_subscription_service import get_payos_subscription_service
from app.services.directus import directus_get
from app.services import subscription_service
from app.config import ADMIN_URL

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


class CreateCheckoutRequest(BaseModel):
    tenant_id: int
    tier_slug: str
    billing_cycle: str = "monthly"  # monthly | yearly
    payment_region: str = "vietnam"  # vietnam | global
    customer_email: str | None = None


class CreateCheckoutResponse(BaseModel):
    checkout_url: str
    provider: str
    order_code: int | None = None


@router.post("/create-checkout", response_model=CreateCheckoutResponse)
async def create_subscription_checkout(req: CreateCheckoutRequest):
    """Create a checkout session for subscription payment (Polar or PayOS)."""

    # Fetch tier pricing info
    tier = await _get_tier(req.tier_slug)
    if not tier:
        raise HTTPException(status_code=404, detail=f"Tier '{req.tier_slug}' not found")

    if req.payment_region == "global":
        return await _create_polar_checkout(req, tier)
    else:
        return await _create_payos_checkout(req, tier)


@router.get("/status/{tenant_id}")
async def get_subscription_status(tenant_id: int):
    """Get current subscription status for a tenant."""
    result = await directus_get(
        f"/items/tenant_subscriptions?filter[tenant_id][_eq]={tenant_id}"
        f"&sort=-date_created&limit=1"
    )
    data = result.get("data", [])
    if not data:
        return {"status": "none", "tenant_id": tenant_id}
    return data[0]


# ── Provider-specific checkout ───────────────────────────────────────────────

async def _create_polar_checkout(req: CreateCheckoutRequest, tier: dict) -> CreateCheckoutResponse:
    """Create Polar hosted checkout session."""
    polar = await get_polar_service()
    if not polar:
        raise HTTPException(status_code=503, detail="Polar not configured")

    # Get the correct Polar product price ID
    price_field = f"polar_product_id_{'yearly' if req.billing_cycle == 'yearly' else 'monthly'}"
    product_price_id = tier.get(price_field)
    if not product_price_id:
        raise HTTPException(
            status_code=400,
            detail=f"Polar product not configured for {req.tier_slug} {req.billing_cycle}",
        )

    success_url = f"{ADMIN_URL}/checkout/success?provider=polar&session={{CHECKOUT_ID}}"

    checkout = await polar.create_checkout(
        product_price_id=product_price_id,
        customer_email=req.customer_email or "",
        success_url=success_url,
        metadata={
            "tenant_id": str(req.tenant_id),
            "tier_slug": req.tier_slug,
            "billing_cycle": req.billing_cycle,
        },
    )

    return CreateCheckoutResponse(
        checkout_url=checkout.get("url", ""),
        provider="polar",
    )


async def _create_payos_checkout(req: CreateCheckoutRequest, tier: dict) -> CreateCheckoutResponse:
    """Create PayOS payment link for subscription."""
    payos = await get_payos_subscription_service()
    if not payos:
        raise HTTPException(status_code=503, detail="PayOS subscription not configured")

    amount_field = f"payos_amount_{'yearly' if req.billing_cycle == 'yearly' else 'monthly'}"
    amount = tier.get(amount_field)
    if not amount:
        raise HTTPException(
            status_code=400,
            detail=f"PayOS amount not configured for {req.tier_slug} {req.billing_cycle}",
        )

    result = await payos.create_payment_link(
        tenant_id=req.tenant_id,
        tier_slug=req.tier_slug,
        amount=int(amount),
        billing_cycle=req.billing_cycle,
    )

    # Pre-log a pending payment for webhook matching
    await subscription_service.log_payment(
        tenant_id=req.tenant_id,
        provider="payos",
        external_payment_id=str(result["order_code"]),
        amount=int(amount),
        currency="VND",
        status="pending",
        description=f"{req.tier_slug} plan - {req.billing_cycle}",
    )

    return CreateCheckoutResponse(
        checkout_url=result["checkout_url"],
        provider="payos",
        order_code=result["order_code"],
    )


async def _get_tier(slug: str) -> dict | None:
    """Fetch subscription tier by slug."""
    result = await directus_get(
        f"/items/subscription_tiers?filter[slug][_eq]={slug}&limit=1"
    )
    data = result.get("data", [])
    return data[0] if data else None
