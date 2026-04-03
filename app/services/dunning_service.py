"""
Dunning service — automated email escalation for overdue subscription payments.
Runs daily via APScheduler. Stages: 0=initial, 1=reminder, 2=warning, 3=suspend, 4=expire.
"""
import logging
from datetime import datetime, timezone

from app.services.directus import directus_get, directus_patch
from app.services import subscription_service
from app.services.mailgun import send_mailgun
from app.config import ADMIN_URL

logger = logging.getLogger(__name__)

# Dunning timeline (days after period_end)
STAGE_DAYS = {0: 0, 1: 3, 2: 7, 3: 10, 4: 40}


async def process_dunning():
    """Daily cron job — process all overdue subscriptions through dunning stages."""
    now = datetime.now(timezone.utc)

    # Find active/past_due/suspended subscriptions with expired periods
    subs = await directus_get(
        "/items/tenant_subscriptions"
        "?filter[status][_in]=active,past_due,suspended"
        "&filter[current_period_end][_lt]=" + now.isoformat()
        + "&fields=id,tenant_id,status,current_period_end,dunning_stage,dunning_started_at"
        "&limit=100"
    )

    for sub in subs.get("data", []):
        period_end = sub.get("current_period_end")
        if not period_end:
            continue

        period_end_dt = datetime.fromisoformat(period_end.replace("Z", "+00:00"))
        days_overdue = (now - period_end_dt).days
        current_stage = sub.get("dunning_stage")
        tenant_id = sub["tenant_id"]

        try:
            if days_overdue >= STAGE_DAYS[4] and (current_stage is None or current_stage < 4):
                await _expire(tenant_id, sub["id"])
            elif days_overdue >= STAGE_DAYS[3] and (current_stage is None or current_stage < 3):
                await _suspend(tenant_id, sub["id"])
            elif days_overdue >= STAGE_DAYS[2] and (current_stage is None or current_stage < 2):
                await _final_warning(tenant_id, sub["id"])
            elif days_overdue >= STAGE_DAYS[1] and (current_stage is None or current_stage < 1):
                await _reminder(tenant_id, sub["id"])
            elif days_overdue >= STAGE_DAYS[0] and current_stage is None:
                await _initial_notice(tenant_id, sub["id"])
        except Exception as e:
            logger.error(f"Dunning error for tenant {tenant_id}: {e}")


async def _initial_notice(tenant_id: int, sub_id: str):
    """Day 0: First payment failure notice."""
    await directus_patch(f"/items/tenant_subscriptions/{sub_id}", {
        "status": "past_due",
        "dunning_stage": 0,
        "dunning_started_at": datetime.now(timezone.utc).isoformat(),
    })
    email = await _get_tenant_email(tenant_id)
    if email:
        await send_mailgun(
            to=email,
            subject="Nexpo: Payment Failed / Thanh toán chưa thành công",
            html=_dunning_email("initial", tenant_id),
        )
    logger.info(f"Dunning stage 0 (initial): tenant={tenant_id}")


async def _reminder(tenant_id: int, sub_id: str):
    """Day 3: Payment reminder."""
    await directus_patch(f"/items/tenant_subscriptions/{sub_id}", {"dunning_stage": 1})
    email = await _get_tenant_email(tenant_id)
    if email:
        await send_mailgun(
            to=email,
            subject="Nexpo: Payment Reminder / Nhắc nhở thanh toán",
            html=_dunning_email("reminder", tenant_id),
        )
    logger.info(f"Dunning stage 1 (reminder): tenant={tenant_id}")


async def _final_warning(tenant_id: int, sub_id: str):
    """Day 7: Final warning before suspension."""
    await directus_patch(f"/items/tenant_subscriptions/{sub_id}", {"dunning_stage": 2})
    email = await _get_tenant_email(tenant_id)
    if email:
        await send_mailgun(
            to=email,
            subject="Nexpo: Warning — Account Will Be Suspended / Cảnh báo — Tài khoản sẽ bị tạm ngưng",
            html=_dunning_email("warning", tenant_id),
        )
    logger.info(f"Dunning stage 2 (warning): tenant={tenant_id}")


async def _suspend(tenant_id: int, sub_id: str):
    """Day 10: Suspend account."""
    await subscription_service.suspend_subscription(tenant_id)
    email = await _get_tenant_email(tenant_id)
    if email:
        await send_mailgun(
            to=email,
            subject="Nexpo: Account Suspended / Tài khoản đã bị tạm ngưng",
            html=_dunning_email("suspended", tenant_id),
        )
    logger.info(f"Dunning stage 3 (suspended): tenant={tenant_id}")


async def _expire(tenant_id: int, sub_id: str):
    """Day 40: Expire subscription."""
    await directus_patch(f"/items/tenant_subscriptions/{sub_id}", {
        "status": "expired",
        "dunning_stage": 4,
    })
    # Downgrade to free
    await subscription_service.deactivate_subscription(tenant_id, reason="expired")
    email = await _get_tenant_email(tenant_id)
    if email:
        await send_mailgun(
            to=email,
            subject="Nexpo: Subscription Expired / Gói đăng ký đã hết hạn",
            html=_dunning_email("expired", tenant_id),
        )
    logger.info(f"Dunning stage 4 (expired): tenant={tenant_id}")


async def _get_tenant_email(tenant_id: int) -> str | None:
    """Get contact email for a tenant."""
    result = await directus_get(f"/items/tenants/{tenant_id}?fields=email")
    return result.get("data", {}).get("email")


def _dunning_email(stage: str, tenant_id: int) -> str:
    """Generate dunning email HTML for a given stage."""
    payment_url = f"{ADMIN_URL}/settings/subscription"
    # (en_title, en_desc, vi_title, vi_desc, cta_en, cta_vi)
    messages = {
        "initial": (
            "Your Nexpo subscription payment failed.",
            "Please update your payment method to continue using the service.",
            "Thanh toán gói đăng ký Nexpo chưa thành công.",
            "Vui lòng cập nhật phương thức thanh toán để tiếp tục sử dụng dịch vụ.",
            "Update Payment", "Cập nhật thanh toán",
        ),
        "reminder": (
            "Reminder: Your subscription payment is still outstanding.",
            "Please complete payment as soon as possible to avoid service interruption.",
            "Nhắc nhở: Thanh toán gói đăng ký vẫn chưa hoàn tất.",
            "Vui lòng thanh toán trong thời gian sớm nhất để tránh gián đoạn dịch vụ.",
            "Pay Now", "Thanh toán ngay",
        ),
        "warning": (
            "Warning: Your account will be suspended in 3 days.",
            "If payment is not received before the deadline, your account will become read-only.",
            "Cảnh báo: Tài khoản sẽ bị tạm ngưng trong 3 ngày.",
            "Nếu không thanh toán trước thời hạn, tài khoản sẽ chuyển sang chế độ chỉ đọc.",
            "Pay Now", "Thanh toán ngay",
        ),
        "suspended": (
            "Your account has been suspended due to unpaid subscription.",
            "You can still view your data but cannot create or edit. Pay to reactivate.",
            "Tài khoản đã bị tạm ngưng do chưa thanh toán.",
            "Bạn vẫn có thể xem dữ liệu nhưng không thể tạo hoặc chỉnh sửa. Thanh toán để kích hoạt lại.",
            "Reactivate", "Kích hoạt lại",
        ),
        "expired": (
            "Your subscription has expired.",
            "Your account has been downgraded to the free plan. Your data is preserved.",
            "Gói đăng ký đã hết hạn.",
            "Tài khoản đã được chuyển về gói miễn phí. Dữ liệu của bạn vẫn được giữ nguyên.",
            "Upgrade", "Nâng cấp lại",
        ),
    }
    en_title, en_desc, vi_title, vi_desc, cta_en, cta_vi = messages.get(stage, messages["initial"])

    return f"""
    <div style="font-family:'Segoe UI',Arial,sans-serif;max-width:600px;margin:0 auto;padding:40px 20px;">
      <div style="text-align:center;margin-bottom:30px;">
        <h1 style="color:#4F80FF;font-size:24px;margin:0;">NEXPO</h1>
      </div>
      <div style="background:#f8fafc;border-radius:12px;padding:30px;border:1px solid #e2e8f0;">
        <h2 style="color:#1a1a1a;font-size:18px;margin:0 0 8px;">{en_title}</h2>
        <p style="color:#404040;font-size:15px;line-height:1.6;margin:0 0 16px;">{en_desc}</p>
        <h2 style="color:#64748b;font-size:16px;margin:0 0 8px;">{vi_title}</h2>
        <p style="color:#64748b;font-size:14px;line-height:1.6;margin:0 0 20px;">{vi_desc}</p>
        <div style="text-align:center;margin:20px 0;">
          <a href="{payment_url}" style="display:inline-block;background:#4F80FF;color:#fff;text-decoration:none;padding:12px 32px;border-radius:8px;font-weight:600;font-size:15px;">{cta_en} / {cta_vi}</a>
        </div>
      </div>
    </div>"""
