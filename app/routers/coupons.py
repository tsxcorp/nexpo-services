"""
Coupon validation endpoint — validates coupon codes for subscription checkout.
"""
import logging
from datetime import datetime, timezone
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException

from app.services.directus import directus_get

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/coupons", tags=["coupons"])


class ValidateCouponRequest(BaseModel):
    code: str
    tier_slug: str


@router.post("/validate")
async def validate_coupon(req: ValidateCouponRequest):
    """Validate a coupon code for a given tier. Returns discount info or error."""
    result = await directus_get(
        f"/items/coupon_codes"
        f"?filter[code][_eq]={req.code}"
        f"&filter[is_active][_eq]=true"
        f"&limit=1"
    )
    coupons = result.get("data", [])
    if not coupons:
        raise HTTPException(status_code=404, detail="Invalid coupon code")

    coupon = coupons[0]
    now = datetime.now(timezone.utc)

    # Check validity period
    valid_from = coupon.get("valid_from")
    if valid_from:
        from_dt = datetime.fromisoformat(valid_from.replace("Z", "+00:00"))
        if now < from_dt:
            raise HTTPException(status_code=400, detail="Coupon not yet valid")

    valid_until = coupon.get("valid_until")
    if valid_until:
        until_dt = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
        if now > until_dt:
            raise HTTPException(status_code=400, detail="Coupon has expired")

    # Check max uses
    max_uses = coupon.get("max_uses")
    current_uses = coupon.get("current_uses", 0)
    if max_uses is not None and current_uses >= max_uses:
        raise HTTPException(status_code=400, detail="Coupon usage limit reached")

    # Check applicable tiers
    applicable = coupon.get("applicable_tiers")
    if applicable and req.tier_slug not in applicable:
        raise HTTPException(status_code=400, detail="Coupon not valid for this plan")

    return {
        "valid": True,
        "code": coupon["code"],
        "discount_type": coupon["discount_type"],
        "discount_value": coupon["discount_value"],
        "applicable_tiers": coupon.get("applicable_tiers"),
    }
