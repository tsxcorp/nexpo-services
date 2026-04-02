"""
Subscription lifecycle service — shared logic for both Polar and PayOS providers.
Handles activation, deactivation, renewal, status transitions, and payment logging.
"""
import logging
from datetime import datetime, timedelta, timezone
from app.services.directus import directus_get, directus_post, directus_patch

logger = logging.getLogger(__name__)


async def activate_subscription(
    tenant_id: int,
    provider: str,
    tier_slug: str,
    external_subscription_id: str | None = None,
    external_customer_id: str | None = None,
    period_start: str | None = None,
    period_end: str | None = None,
) -> dict:
    """Activate or renew a tenant subscription. Idempotent — safe to call multiple times."""

    # Find existing subscription for this tenant
    existing = await _get_tenant_subscription(tenant_id)

    if existing:
        # Update existing subscription
        await directus_patch(f"/items/tenant_subscriptions/{existing['id']}", {
            "status": "active",
            "provider": provider,
            "external_subscription_id": external_subscription_id,
            "external_customer_id": external_customer_id,
            "current_period_start": period_start,
            "current_period_end": period_end,
            "cancel_at_period_end": False,
            "dunning_stage": None,
            "dunning_started_at": None,
        })
    else:
        # Create new subscription record
        await directus_post("/items/tenant_subscriptions", {
            "tenant_id": tenant_id,
            "provider": provider,
            "status": "active",
            "external_subscription_id": external_subscription_id,
            "external_customer_id": external_customer_id,
            "current_period_start": period_start,
            "current_period_end": period_end,
        })

    # Update tenant tier + features
    tier_features = await _get_tier_features(tier_slug)
    await directus_patch(f"/items/tenants/{tenant_id}", {
        "subscription_tier": tier_slug,
        "features": tier_features,
    })

    logger.info(f"Subscription activated: tenant={tenant_id}, tier={tier_slug}, provider={provider}")
    return {"status": "activated", "tenant_id": tenant_id}


async def start_trial(tenant_id: int, tier_slug: str) -> dict:
    """Start a trial period for a tenant. One trial per tenant per tier."""
    # Check if tenant already had a trial for this tier
    existing_trials = await directus_get(
        f"/items/tenant_subscriptions?filter[tenant_id][_eq]={tenant_id}"
        f"&filter[trial_end][_nnull]=true&limit=10"
    )
    # Simple check: if any trial exists, don't allow another
    if existing_trials.get("data"):
        logger.info(f"Trial already used: tenant={tenant_id}")
        return {"status": "trial_already_used", "tenant_id": tenant_id}

    # Get trial days from tier
    tier = await directus_get(
        f"/items/subscription_tiers?filter[slug][_eq]={tier_slug}&fields=trial_days,features&limit=1"
    )
    tier_data = tier.get("data", [])
    if not tier_data or not tier_data[0].get("trial_days"):
        return {"status": "no_trial_available", "tenant_id": tenant_id}

    trial_days = tier_data[0]["trial_days"]
    features = tier_data[0].get("features")
    now = datetime.now(timezone.utc)
    trial_end = now + timedelta(days=trial_days)

    # Create subscription with trialing status
    sub = await _get_tenant_subscription(tenant_id)
    if sub:
        await directus_patch(f"/items/tenant_subscriptions/{sub['id']}", {
            "status": "trialing",
            "trial_end": trial_end.isoformat(),
            "current_period_start": now.isoformat(),
            "current_period_end": trial_end.isoformat(),
        })
    else:
        await directus_post("/items/tenant_subscriptions", {
            "tenant_id": tenant_id,
            "provider": "manual",
            "status": "trialing",
            "trial_end": trial_end.isoformat(),
            "current_period_start": now.isoformat(),
            "current_period_end": trial_end.isoformat(),
        })

    # Set tenant tier + features + trial end
    await directus_patch(f"/items/tenants/{tenant_id}", {
        "subscription_tier": tier_slug,
        "features": features,
        "trial_ends_at": trial_end.isoformat(),
    })

    logger.info(f"Trial started: tenant={tenant_id}, tier={tier_slug}, days={trial_days}")
    return {"status": "trial_started", "tenant_id": tenant_id, "trial_end": trial_end.isoformat()}


async def deactivate_subscription(tenant_id: int, reason: str = "cancelled") -> dict:
    """Cancel or expire subscription — downgrade to free tier features."""
    existing = await _get_tenant_subscription(tenant_id)
    if existing:
        await directus_patch(f"/items/tenant_subscriptions/{existing['id']}", {
            "status": reason,
        })

    # Downgrade to free tier
    free_features = await _get_tier_features("free")
    await directus_patch(f"/items/tenants/{tenant_id}", {
        "subscription_tier": "free",
        "features": free_features,
    })

    logger.info(f"Subscription deactivated: tenant={tenant_id}, reason={reason}")
    return {"status": reason, "tenant_id": tenant_id}


async def mark_past_due(tenant_id: int) -> dict:
    """Mark subscription as past due (payment failed/overdue)."""
    existing = await _get_tenant_subscription(tenant_id)
    if existing and existing.get("status") == "active":
        now_iso = datetime.now(timezone.utc).isoformat()
        await directus_patch(f"/items/tenant_subscriptions/{existing['id']}", {
            "status": "past_due",
            "dunning_stage": 0,
            "dunning_started_at": now_iso,
        })
        logger.info(f"Subscription past_due: tenant={tenant_id}")
    return {"status": "past_due", "tenant_id": tenant_id}


async def suspend_subscription(tenant_id: int) -> dict:
    """Suspend subscription — restrict to read-only mode."""
    existing = await _get_tenant_subscription(tenant_id)
    if existing:
        await directus_patch(f"/items/tenant_subscriptions/{existing['id']}", {
            "status": "suspended",
            "dunning_stage": 3,
        })
    logger.info(f"Subscription suspended: tenant={tenant_id}")
    return {"status": "suspended", "tenant_id": tenant_id}


async def log_payment(
    tenant_id: int,
    provider: str,
    external_payment_id: str | None,
    amount: int,
    currency: str = "VND",
    status: str = "succeeded",
    description: str | None = None,
    invoice_url: str | None = None,
) -> dict | None:
    """Log a payment to subscription_payments. Idempotent on external_payment_id."""
    # Idempotency: skip if payment already logged
    if external_payment_id:
        check = await directus_get(
            f"/items/subscription_payments?filter[external_payment_id][_eq]={external_payment_id}&limit=1"
        )
        if check.get("data"):
            logger.info(f"Payment already logged: {external_payment_id}")
            return check["data"][0]

    # Find subscription for linking
    sub = await _get_tenant_subscription(tenant_id)

    result = await directus_post("/items/subscription_payments", {
        "tenant_id": tenant_id,
        "subscription_id": sub["id"] if sub else None,
        "provider": provider,
        "external_payment_id": external_payment_id,
        "amount": amount,
        "currency": currency,
        "status": status,
        "description": description,
        "invoice_url": invoice_url,
    })
    logger.info(f"Payment logged: tenant={tenant_id}, amount={amount} {currency}, status={status}")
    return result.get("data")


async def reset_dunning(tenant_id: int) -> None:
    """Reset dunning state after successful payment."""
    existing = await _get_tenant_subscription(tenant_id)
    if existing and existing.get("dunning_stage") is not None:
        await directus_patch(f"/items/tenant_subscriptions/{existing['id']}", {
            "dunning_stage": None,
            "dunning_started_at": None,
            "status": "active",
        })
        logger.info(f"Dunning reset: tenant={tenant_id}")


# ── Internal helpers ─────────────────────────────────────────────────────────

async def _get_tenant_subscription(tenant_id: int) -> dict | None:
    """Get the most recent subscription for a tenant."""
    result = await directus_get(
        f"/items/tenant_subscriptions?filter[tenant_id][_eq]={tenant_id}"
        f"&sort=-date_created&limit=1"
    )
    data = result.get("data", [])
    return data[0] if data else None


async def _get_tier_features(tier_slug: str) -> list[str] | None:
    """Fetch features array for a subscription tier."""
    result = await directus_get(
        f"/items/subscription_tiers?filter[slug][_eq]={tier_slug}&fields=features&limit=1"
    )
    data = result.get("data", [])
    return data[0].get("features") if data else None
