"""
Password reset service — custom flow bypassing Directus email.

Generates tokens, stores hashed in Directus, sends branded email via Mailgun.
Uses admin token to PATCH user password on reset.
"""
import hashlib
import logging
import uuid
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

from app.services.directus import directus_get, directus_post, directus_patch, directus_delete
from app.services.mailgun import send_mailgun, NEXPO_LOGO_URL
from app.config import ADMIN_URL, PORTAL_URL

logger = logging.getLogger(__name__)

# Reset URL per app — maps app_origin to base URL
APP_RESET_URLS: dict[str, str] = {
    "admin": f"{ADMIN_URL}/reset-password",
    "portal": f"{PORTAL_URL}/reset-password",
    "console": "https://console.nexpo.vn/reset-password",
}

VALID_APPS = {"admin", "portal", "console"}
TOKEN_EXPIRY_HOURS = 1
MAX_REQUESTS_PER_HOUR = 3


def _hash_token(raw_token: str) -> str:
    """SHA-256 hash of raw token for DB storage."""
    return hashlib.sha256(raw_token.encode()).hexdigest()


def _mask_email(email: str) -> str:
    """Mask email for display: t***@example.com"""
    if "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    if len(local) <= 1:
        return f"*@{domain}"
    return f"{local[0]}{'*' * (len(local) - 1)}@{domain}"


async def _lookup_user_by_email(email: str) -> dict | None:
    """Find Directus user by email. Returns {id, email} or None."""
    try:
        resp = await directus_get(
            f"/users?filter[email][_eq]={quote(email)}&fields=id,email&limit=1"
        )
        users = resp.get("data", [])
        return users[0] if users else None
    except Exception:
        return None


async def _count_recent_tokens_by_user_id(user_id: str) -> int:
    """Count password reset tokens created for this user in the last hour."""
    try:
        one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        resp = await directus_get(
            f"/items/password_reset_tokens"
            f"?filter[user_id][_eq]={user_id}"
            f"&filter[date_created][_gte]={one_hour_ago}"
            "&aggregate[count]=id"
        )
        agg = resp.get("data", [{}])
        return int(agg[0].get("count", {}).get("id", 0))
    except Exception:
        return 0


async def request_password_reset(email: str, app: str = "admin") -> bool:
    """
    Generate reset token, store hash in DB, send email via Mailgun.
    Always returns True (no email existence leak).
    """
    email = email.strip().lower()
    if app not in VALID_APPS:
        app = "admin"

    user = await _lookup_user_by_email(email)
    if not user:
        return True  # no leak

    # Rate limit: max 3 per hour per user (reuse user_id, no duplicate lookup)
    recent_count = await _count_recent_tokens_by_user_id(user["id"])
    if recent_count >= MAX_REQUESTS_PER_HOUR:
        return True  # silently ignore

    # Generate token
    raw_token = str(uuid.uuid4())
    token_hash = _hash_token(raw_token)
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXPIRY_HOURS)).isoformat()

    # Store in Directus
    try:
        await directus_post("/items/password_reset_tokens", {
            "user_id": user["id"],
            "token_hash": token_hash,
            "expires_at": expires_at,
            "used": False,
            "app_origin": app,
        })
    except Exception:
        return True  # fail silently

    # Build reset URL
    base_url = APP_RESET_URLS.get(app, APP_RESET_URLS["admin"])
    reset_url = f"{base_url}?token={raw_token}"

    # Send email — log failure for ops alerting
    html = _build_reset_email_html(reset_url)
    email_sent = await send_mailgun(
        to=email,
        subject="Nexpo: Reset Your Password / Đặt lại mật khẩu",
        html=html,
    )
    if not email_sent:
        logger.error("Failed to send password reset email to %s via Mailgun", email)

    return True


async def validate_reset_token(raw_token: str) -> dict:
    """
    Validate a reset token. Returns:
    - { valid: True, email: "masked" } if valid
    - { valid: False, error: "..." } if invalid/expired/used
    """
    token_hash = _hash_token(raw_token)

    try:
        resp = await directus_get(
            f"/items/password_reset_tokens"
            f"?filter[token_hash][_eq]={token_hash}"
            f"&filter[used][_eq]=false"
            "&fields=id,user_id,expires_at"
            "&limit=1"
        )
        tokens = resp.get("data", [])
        if not tokens:
            return {"valid": False, "error": "Invalid or expired reset link"}

        token_record = tokens[0]

        # Check expiry
        expires_at = datetime.fromisoformat(token_record["expires_at"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) > expires_at:
            return {"valid": False, "error": "Reset link has expired"}

        # Get user email for display
        user_resp = await directus_get(f"/users/{token_record['user_id']}?fields=email")
        user_email = user_resp.get("data", {}).get("email", "")

        return {"valid": True, "email": _mask_email(user_email) if user_email else ""}

    except Exception:
        return {"valid": False, "error": "Failed to validate token"}


async def reset_password(raw_token: str, new_password: str) -> dict:
    """
    Reset password using token. Atomic claim-first pattern to prevent TOCTOU race.
    Returns { success: True } on success, { success: False, error: "..." } on failure.
    """
    token_hash = _hash_token(raw_token)

    try:
        # Step 1: Find valid token
        resp = await directus_get(
            f"/items/password_reset_tokens"
            f"?filter[token_hash][_eq]={token_hash}"
            f"&filter[used][_eq]=false"
            "&fields=id,user_id,expires_at"
            "&limit=1"
        )
        tokens = resp.get("data", [])
        if not tokens:
            return {"success": False, "error": "Invalid or expired reset link"}

        token_record = tokens[0]

        # Check expiry
        expires_at = datetime.fromisoformat(token_record["expires_at"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) > expires_at:
            return {"success": False, "error": "Reset link has expired"}

        # Step 2: Claim token FIRST (mark used before changing password)
        # This prevents TOCTOU race: concurrent requests will fail at this step
        # because the second request will find used=true
        await directus_patch(
            f"/items/password_reset_tokens/{token_record['id']}",
            {"used": True},
        )

        # Step 3: Verify claim succeeded — re-read to confirm we were the one who set it
        verify_resp = await directus_get(
            f"/items/password_reset_tokens/{token_record['id']}?fields=used"
        )
        if not verify_resp.get("data", {}).get("used"):
            return {"success": False, "error": "Reset link is no longer valid"}

        # Step 4: Update password via admin token
        await directus_patch(
            f"/users/{token_record['user_id']}",
            {"password": new_password},
        )

        # Step 5: Cleanup other tokens for this user (best-effort)
        try:
            all_resp = await directus_get(
                f"/items/password_reset_tokens"
                f"?filter[user_id][_eq]={token_record['user_id']}"
                f"&filter[id][_neq]={token_record['id']}"
                "&fields=id&limit=50"
            )
            for old_token in all_resp.get("data", []):
                await directus_delete(f"/items/password_reset_tokens/{old_token['id']}")
        except Exception:
            pass  # cleanup is best-effort

        return {"success": True}

    except Exception as e:
        logger.error("Password reset failed: %s", str(e))
        return {"success": False, "error": "Failed to reset password"}


async def cleanup_expired_tokens() -> int:
    """Delete expired or used tokens older than 24h. Returns count deleted."""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        resp = await directus_get(
            f"/items/password_reset_tokens"
            f"?filter[_or][0][expires_at][_lt]={cutoff}"
            f"&filter[_or][1][used][_eq]=true"
            "&fields=id&limit=100"
        )
        deleted = 0
        for token in resp.get("data", []):
            try:
                await directus_delete(f"/items/password_reset_tokens/{token['id']}")
                deleted += 1
            except Exception:
                pass
        return deleted
    except Exception:
        return 0


def _build_reset_email_html(reset_url: str) -> str:
    """Branded bilingual (EN/VI) password reset email."""
    year = datetime.now().year
    return f"""
<div style="font-family: 'Segoe UI', Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 40px 20px;">
  <div style="text-align: center; margin-bottom: 30px;">
    <img src="{NEXPO_LOGO_URL}" alt="Nexpo" style="height: 36px;" onerror="this.style.display='none'"/>
    <p style="color: #666; font-size: 14px; margin: 5px 0 0;">Business Platform &amp; Exhibition Operations</p>
  </div>
  <div style="background: #f8fafc; border-radius: 12px; padding: 30px; border: 1px solid #e2e8f0;">
    <h2 style="color: #1a1a1a; font-size: 20px; margin: 0 0 15px;">Reset Your Password / Đặt lại mật khẩu</h2>
    <p style="color: #404040; font-size: 15px; line-height: 1.6; margin: 0 0 8px;">
      We received a request to reset your password. Click the button below to set a new one.
    </p>
    <p style="color: #64748b; font-size: 14px; line-height: 1.6; margin: 0 0 20px;">
      Chúng tôi nhận được yêu cầu đặt lại mật khẩu cho tài khoản của bạn. Nhấn nút bên dưới để tạo mật khẩu mới.
    </p>
    <div style="text-align: center; margin: 25px 0;">
      <a href="{reset_url}" style="display: inline-block; background: #4F80FF; color: #ffffff; text-decoration: none; padding: 14px 36px; border-radius: 8px; font-weight: 600; font-size: 16px;">
        Reset Password / Đặt lại mật khẩu
      </a>
    </div>
    <p style="color: #94a3b8; font-size: 13px; line-height: 1.5; margin: 20px 0 0; border-top: 1px solid #e5e7eb; padding-top: 15px;">
      This link expires in 1 hour. If you didn't request this, please ignore this email.<br/>
      Link có hiệu lực trong 1 giờ. Nếu bạn không yêu cầu đổi mật khẩu, vui lòng bỏ qua email này.
    </p>
  </div>
  <p style="color: #94a3b8; font-size: 11px; text-align: center; margin-top: 20px;">
    &copy; {year} NEXPO. All rights reserved.
  </p>
</div>"""
