"""
Polar payment service — creates checkout sessions, manages subscriptions,
generates customer portal URLs. Uses Polar REST API directly (no SDK dependency).
"""
import hashlib
import hmac
import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

POLAR_API_BASE = "https://api.polar.sh/v1"


class PolarService:
    """Polar API wrapper for subscription billing."""

    def __init__(self, access_token: str, webhook_secret: str):
        self.access_token = access_token
        self.webhook_secret = webhook_secret
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    async def create_checkout(
        self,
        product_price_id: str,
        customer_email: str,
        success_url: str,
        metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Create a Polar checkout session → returns checkout URL."""
        payload: dict[str, Any] = {
            "product_price_id": product_price_id,
            "success_url": success_url,
            "customer_email": customer_email,
        }
        if metadata:
            payload["metadata"] = metadata

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{POLAR_API_BASE}/checkouts/custom",
                headers=self.headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            logger.info(f"Polar checkout created: {data.get('id')}")
            return data

    async def get_subscription(self, subscription_id: str) -> dict[str, Any]:
        """Fetch subscription details by ID."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{POLAR_API_BASE}/subscriptions/{subscription_id}",
                headers=self.headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def get_customer_portal_url(self, customer_id: str) -> str:
        """Create a customer portal session → returns portal URL."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{POLAR_API_BASE}/customer-portal/sessions",
                headers=self.headers,
                json={"customer_id": customer_id},
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("customer_portal_url", "")

    def verify_webhook(self, payload: bytes, signature: str) -> bool:
        """Verify Polar webhook signature (HMAC-SHA256)."""
        expected = hmac.new(
            self.webhook_secret.encode(),
            payload,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)


async def get_polar_service() -> PolarService | None:
    """Load Polar credentials from platform_payment_configs and return service instance."""
    from app.services.directus import directus_get

    try:
        result = await directus_get(
            "/items/platform_payment_configs?filter[provider][_eq]=polar"
            "&filter[is_active][_eq]=true&limit=1"
        )
        configs = result.get("data", [])
        if not configs:
            logger.warning("Polar payment config not found or inactive")
            return None

        creds = configs[0].get("credentials", {})
        if isinstance(creds, str):
            creds = json.loads(creds)

        access_token = creds.get("access_token", "")
        webhook_secret = creds.get("webhook_secret", "")
        if not access_token:
            logger.error("Polar access_token missing in credentials")
            return None

        return PolarService(access_token, webhook_secret)
    except Exception as e:
        logger.error(f"Failed to load Polar service: {e}")
        return None
