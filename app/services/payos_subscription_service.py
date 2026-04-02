"""
PayOS subscription service — generates payment links for subscription billing.
PayOS has no native recurring — we generate a new payment link per billing cycle.
"""
import hashlib
import hmac
import json
import logging
import time
from typing import Any

import httpx

from app.config import ADMIN_URL

logger = logging.getLogger(__name__)

PAYOS_API_BASE = "https://api-merchant.payos.vn"


class PayOSSubscriptionService:
    """PayOS wrapper for subscription payment link creation."""

    def __init__(self, client_id: str, api_key: str, checksum_key: str):
        self.client_id = client_id
        self.api_key = api_key
        self.checksum_key = checksum_key
        self.headers = {
            "x-client-id": client_id,
            "x-api-key": api_key,
            "Content-Type": "application/json",
        }

    async def create_payment_link(
        self,
        tenant_id: int,
        tier_slug: str,
        amount: int,
        billing_cycle: str,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Create a PayOS payment link for subscription payment."""
        order_code = int(time.time() * 1000) % 2_000_000_000  # unique int < 2B
        desc = (description or f"Nexpo {tier_slug} - {billing_cycle}")[:25]

        return_url = f"{ADMIN_URL}/checkout/success?provider=payos&order={order_code}"
        cancel_url = f"{ADMIN_URL}/checkout/subscription?cancelled=true"

        payload = {
            "orderCode": order_code,
            "amount": amount,
            "description": desc,
            "returnUrl": return_url,
            "cancelUrl": cancel_url,
            "items": [{"name": f"Nexpo {tier_slug}", "quantity": 1, "price": amount}],
        }

        # Generate checksum for PayOS verification
        checksum_data = f"amount={amount}&cancelUrl={cancel_url}&description={desc}&orderCode={order_code}&returnUrl={return_url}"
        checksum = hmac.new(
            self.checksum_key.encode(), checksum_data.encode(), hashlib.sha256
        ).hexdigest()
        payload["signature"] = checksum

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{PAYOS_API_BASE}/v2/payment-requests",
                headers=self.headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        checkout_url = data.get("data", {}).get("checkoutUrl", "")
        logger.info(
            f"PayOS subscription link created: tenant={tenant_id}, "
            f"order={order_code}, amount={amount} VND"
        )
        return {
            "checkout_url": checkout_url,
            "order_code": order_code,
            "amount": amount,
        }

    def verify_webhook(self, payload: dict) -> bool:
        """Verify PayOS webhook checksum signature."""
        try:
            data = payload.get("data", {})
            # PayOS webhook checksum: sorted keys of data object
            sorted_keys = sorted(data.keys())
            checksum_str = "&".join(f"{k}={data[k]}" for k in sorted_keys)
            expected = hmac.new(
                self.checksum_key.encode(),
                checksum_str.encode(),
                hashlib.sha256,
            ).hexdigest()
            return hmac.compare_digest(expected, payload.get("signature", ""))
        except Exception as e:
            logger.error(f"PayOS webhook verification failed: {e}")
            return False


async def get_payos_subscription_service() -> PayOSSubscriptionService | None:
    """Load PayOS credentials from platform_payment_configs."""
    from app.services.directus import directus_get

    try:
        result = await directus_get(
            "/items/platform_payment_configs?filter[provider][_eq]=payos"
            "&filter[is_active][_eq]=true&limit=1"
        )
        configs = result.get("data", [])
        if not configs:
            logger.warning("PayOS subscription config not found or inactive")
            return None

        creds = configs[0].get("credentials", {})
        if isinstance(creds, str):
            creds = json.loads(creds)

        client_id = creds.get("client_id", "")
        api_key = creds.get("api_key", "")
        checksum_key = creds.get("checksum_key", "")
        if not all([client_id, api_key, checksum_key]):
            logger.error("PayOS credentials incomplete")
            return None

        return PayOSSubscriptionService(client_id, api_key, checksum_key)
    except Exception as e:
        logger.error(f"Failed to load PayOS subscription service: {e}")
        return None
