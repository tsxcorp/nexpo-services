"""
Polar webhook handler — processes subscription lifecycle events.
Endpoint: POST /webhooks/polar
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request, HTTPException

from app.services.polar_service import get_polar_service
from app.services import subscription_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/polar")
async def handle_polar_webhook(request: Request):
    """Process Polar webhook events for subscription billing."""
    body = await request.body()
    signature = request.headers.get("polar-signature", "")

    # Verify signature
    polar = await get_polar_service()
    if not polar:
        raise HTTPException(status_code=503, detail="Polar service not configured")

    if not polar.verify_webhook(body, signature):
        logger.warning("Polar webhook signature verification failed")
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = await request.json()
    event_type = payload.get("type", "")
    data = payload.get("data", {})

    logger.info(f"Polar webhook: type={event_type}")

    try:
        if event_type == "checkout.completed":
            await _handle_checkout_completed(data)
        elif event_type == "subscription.active":
            await _handle_subscription_active(data)
        elif event_type == "subscription.canceled":
            await _handle_subscription_canceled(data)
        elif event_type == "subscription.revoked":
            await _handle_subscription_revoked(data)
        elif event_type == "order.paid":
            await _handle_order_paid(data)
        else:
            logger.info(f"Polar webhook ignored: {event_type}")

    except Exception as e:
        logger.error(f"Polar webhook error ({event_type}): {e}", exc_info=True)
        # Return 200 to prevent Polar from retrying — log error for investigation
        return {"status": "error_logged", "event": event_type}

    return {"status": "ok", "event": event_type}


async def _handle_checkout_completed(data: dict):
    """Checkout completed — activate subscription."""
    metadata = data.get("metadata", {})
    tenant_id = metadata.get("tenant_id")
    tier_slug = metadata.get("tier_slug")
    if not tenant_id or not tier_slug:
        logger.error(f"Polar checkout missing metadata: {metadata}")
        return

    subscription = data.get("subscription", {})
    customer = data.get("customer", {})

    await subscription_service.activate_subscription(
        tenant_id=int(tenant_id),
        provider="polar",
        tier_slug=tier_slug,
        external_subscription_id=subscription.get("id"),
        external_customer_id=customer.get("id"),
        period_start=subscription.get("current_period_start"),
        period_end=subscription.get("current_period_end"),
    )

    # Log the initial payment
    amount = data.get("amount", 0)
    currency = data.get("currency", "usd").upper()
    await subscription_service.log_payment(
        tenant_id=int(tenant_id),
        provider="polar",
        external_payment_id=data.get("id"),
        amount=amount,
        currency=currency,
        status="succeeded",
        description=f"{tier_slug} plan - initial payment",
    )


async def _handle_subscription_active(data: dict):
    """Subscription became active (e.g., after renewal)."""
    metadata = data.get("metadata", {})
    tenant_id = metadata.get("tenant_id")
    tier_slug = metadata.get("tier_slug")
    if not tenant_id:
        return

    await subscription_service.activate_subscription(
        tenant_id=int(tenant_id),
        provider="polar",
        tier_slug=tier_slug or "starter",
        external_subscription_id=data.get("id"),
        period_start=data.get("current_period_start"),
        period_end=data.get("current_period_end"),
    )
    await subscription_service.reset_dunning(int(tenant_id))


async def _handle_subscription_canceled(data: dict):
    """Subscription canceled by user or Polar."""
    metadata = data.get("metadata", {})
    tenant_id = metadata.get("tenant_id")
    if not tenant_id:
        return
    await subscription_service.deactivate_subscription(int(tenant_id), reason="cancelled")


async def _handle_subscription_revoked(data: dict):
    """Subscription revoked (payment failed after retries)."""
    metadata = data.get("metadata", {})
    tenant_id = metadata.get("tenant_id")
    if not tenant_id:
        return
    await subscription_service.deactivate_subscription(int(tenant_id), reason="expired")


async def _handle_order_paid(data: dict):
    """Recurring payment received — log and ensure active."""
    metadata = data.get("metadata", {})
    tenant_id = metadata.get("tenant_id")
    if not tenant_id:
        return

    await subscription_service.log_payment(
        tenant_id=int(tenant_id),
        provider="polar",
        external_payment_id=data.get("id"),
        amount=data.get("amount", 0),
        currency=data.get("currency", "usd").upper(),
        status="succeeded",
        description="Recurring payment",
        invoice_url=data.get("invoice_url"),
    )
    await subscription_service.reset_dunning(int(tenant_id))
