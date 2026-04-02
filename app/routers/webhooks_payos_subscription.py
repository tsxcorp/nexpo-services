"""
PayOS subscription webhook handler — processes payment confirmations for subscription billing.
Endpoint: POST /webhooks/payos-subscription
"""
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request, HTTPException

from app.services.payos_subscription_service import get_payos_subscription_service
from app.services import subscription_service
from app.services.invoice_service import generate_invoice_for_payment
from app.services.directus import directus_get

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/payos-subscription")
async def handle_payos_subscription_webhook(request: Request):
    """Process PayOS payment webhook for subscription billing."""
    payload = await request.json()

    # Verify signature
    payos = await get_payos_subscription_service()
    if not payos:
        raise HTTPException(status_code=503, detail="PayOS subscription service not configured")

    if not payos.verify_webhook(payload):
        logger.warning("PayOS subscription webhook signature verification failed")
        raise HTTPException(status_code=401, detail="Invalid signature")

    data = payload.get("data", {})
    order_code = data.get("orderCode")
    status = data.get("code", "")  # "00" = success

    logger.info(f"PayOS subscription webhook: order={order_code}, status={status}")

    if status != "00":
        logger.info(f"PayOS payment not successful: code={status}")
        return {"status": "ignored", "reason": "not_success"}

    try:
        await _handle_payment_success(data, order_code)
    except Exception as e:
        logger.error(f"PayOS subscription webhook error: {e}", exc_info=True)
        return {"status": "error_logged"}

    return {"status": "ok", "order_code": order_code}


async def _handle_payment_success(data: dict, order_code: int):
    """Payment succeeded — find pending subscription and activate."""
    amount = data.get("amount", 0)

    # Find the tenant with a pending subscription that matches this payment
    # We store order_code as external_payment_id during checkout creation
    pending = await _find_pending_by_order_code(order_code)
    if not pending:
        logger.warning(f"No pending subscription found for PayOS order {order_code}")
        return

    tenant_id = pending["tenant_id"]
    tier_slug = await _get_tenant_tier(tenant_id)

    # Determine billing period from amount
    billing_cycle = await _detect_billing_cycle(tier_slug, amount)
    now = datetime.now(timezone.utc)
    period_days = 365 if billing_cycle == "yearly" else 30
    period_end = now + timedelta(days=period_days)

    # Activate subscription
    await subscription_service.activate_subscription(
        tenant_id=tenant_id,
        provider="payos",
        tier_slug=tier_slug,
        external_subscription_id=str(order_code),
        period_start=now.isoformat(),
        period_end=period_end.isoformat(),
    )

    # Log payment
    await subscription_service.log_payment(
        tenant_id=tenant_id,
        provider="payos",
        external_payment_id=str(order_code),
        amount=amount,
        currency="VND",
        status="succeeded",
        description=f"{tier_slug} plan - {billing_cycle}",
    )

    await subscription_service.reset_dunning(tenant_id)

    # Generate invoice for VN payments (non-blocking — best effort)
    try:
        # Find the payment record we just logged
        payments = await directus_get(
            f"/items/subscription_payments?filter[external_payment_id][_eq]={order_code}"
            f"&filter[status][_eq]=succeeded&limit=1"
        )
        payment_records = payments.get("data", [])
        if payment_records:
            await generate_invoice_for_payment(payment_records[0]["id"])
    except Exception as e:
        logger.warning(f"Invoice generation failed (non-blocking): {e}")


async def _find_pending_by_order_code(order_code: int) -> dict | None:
    """Find a pending subscription payment matching this order code."""
    result = await directus_get(
        f"/items/subscription_payments"
        f"?filter[external_payment_id][_eq]={order_code}"
        f"&filter[status][_eq]=pending&limit=1"
    )
    data = result.get("data", [])
    return data[0] if data else None


async def _get_tenant_tier(tenant_id: int) -> str:
    """Get the current subscription tier slug for a tenant."""
    result = await directus_get(f"/items/tenants/{tenant_id}?fields=subscription_tier")
    return result.get("data", {}).get("subscription_tier", "starter")


async def _detect_billing_cycle(tier_slug: str, amount: int) -> str:
    """Determine if payment is monthly or yearly based on amount vs tier pricing."""
    result = await directus_get(
        f"/items/subscription_tiers?filter[slug][_eq]={tier_slug}"
        f"&fields=payos_amount_monthly,payos_amount_yearly&limit=1"
    )
    tiers = result.get("data", [])
    if not tiers:
        return "monthly"

    yearly_amount = tiers[0].get("payos_amount_yearly") or 0
    # If amount is closer to yearly price, it's yearly
    if yearly_amount and amount >= yearly_amount * 0.9:
        return "yearly"
    return "monthly"
