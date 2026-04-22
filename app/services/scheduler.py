"""
APScheduler setup and scheduled jobs.
Import `scheduler` and call scheduler.start() / scheduler.shutdown() from lifespan.
"""
import logging
from datetime import datetime, timezone, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.config import DIRECTUS_ADMIN_TOKEN, PORTAL_URL, ADMIN_URL
from app.services.directus import directus_get, directus_patch, directus_delete
from app.services.directus import resolve_visitor_email, resolve_exhibitor_email
from app.services.mailgun import send_mailgun, meeting_notification_html

logger = logging.getLogger(__name__)
import logging

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()

# Cache tenant timezone to avoid repeated Directus calls within a scheduler cycle
_tz_cache: dict[str, str] = {}


async def _get_tenant_timezone(event_id: str) -> str:
    """Fetch tenant timezone for an event. Cached per event_id."""
    if event_id in _tz_cache:
        return _tz_cache[event_id]
    try:
        resp = await directus_get(
            f"/items/events/{event_id}?fields[]=tenant_id.timezone"
        )
        tz = (resp.get("data", {}).get("tenant_id") or {}).get("timezone") or "Asia/Ho_Chi_Minh"
    except Exception:
        tz = "Asia/Ho_Chi_Minh"
    _tz_cache[event_id] = tz
    return tz


async def expire_pending_orders() -> None:
    """
    APScheduler job — runs every 5 minutes.
    Finds ticket_orders with status=pending whose expires_at has passed.
    Rolls back quantity_sold, cleans up issued_tickets + stub registrations,
    marks order as expired, and emails buyer.
    """
    if not DIRECTUS_ADMIN_TOKEN:
        return

    now = datetime.now(timezone.utc).isoformat()

    try:
        resp = await directus_get(
            "/items/ticket_orders"
            "?filter[status][_eq]=pending"
            f"&filter[expires_at][_lt]={now}"
            "&fields[]=id,buyer_email,buyer_name"
            "&limit=50"
        )
        orders = resp.get("data", [])
    except Exception as exc:
        logger.warning("[expire_orders] Failed to fetch pending orders: %s", exc)
        return

    for order in orders:
        order_id = order.get("id")
        try:
            await _expire_single_order(order)
        except Exception as exc:
            logger.error("[expire_orders] Failed to expire order %s: %s", order_id, exc)


async def _expire_single_order(order: dict) -> None:
    """Expire one pending order: rollback inventory, cleanup records, notify buyer."""
    order_id = order["id"]

    # 1. Fetch order items for quantity rollback
    items_resp = await directus_get(
        f"/items/ticket_order_items"
        f"?filter[order_id][_eq]={order_id}"
        "&fields[]=ticket_class_id,quantity"
        "&limit=50"
    )
    items = items_resp.get("data", [])

    # 2. Rollback quantity_sold per ticket_class
    for item in items:
        tc_id = item.get("ticket_class_id")
        qty = int(item.get("quantity") or 0)
        if not tc_id or qty <= 0:
            continue
        try:
            tc_resp = await directus_get(f"/items/ticket_classes/{tc_id}?fields[]=quantity_sold")
            current_sold = int((tc_resp.get("data") or {}).get("quantity_sold") or 0)
            await directus_patch(f"/items/ticket_classes/{tc_id}", {"quantity_sold": max(0, current_sold - qty)})
        except Exception as exc:
            logger.error("[expire_orders] Rollback failed for class %s: %s", tc_id, exc)

    # 3. Fetch issued_tickets to cleanup stubs
    tickets_resp = await directus_get(
        f"/items/issued_tickets"
        f"?filter[order_id][_eq]={order_id}"
        "&fields[]=id,registration_id"
        "&limit=200"
    )
    tickets = tickets_resp.get("data", [])

    # 4. Delete stub registrations (only is_stub=true)
    reg_ids = [t["registration_id"] for t in tickets if t.get("registration_id")]
    for reg_id in reg_ids:
        try:
            await directus_delete(f"/items/registrations/{reg_id}")
        except Exception:
            pass  # may already be deleted or not a stub

    # 5. Delete issued_tickets
    for t in tickets:
        try:
            await directus_delete(f"/items/issued_tickets/{t['id']}")
        except Exception:
            pass

    # 6. Mark order expired
    await directus_patch(f"/items/ticket_orders/{order_id}", {"status": "expired"})
    logger.info("[expire_orders] Expired order %s, cleaned %d tickets", order_id, len(tickets))

    # 7. Notify buyer (fire-and-forget)
    buyer_email = order.get("buyer_email")
    if buyer_email:
        try:
            await send_mailgun(
                buyer_email,
                "Đơn đặt vé đã hết hạn / Ticket order expired",
                "<div style='font-family:Inter,sans-serif;max-width:600px;margin:auto;padding:32px'>"
                "<h2 style='color:#06043E'>Đơn đặt vé đã hết hạn</h2>"
                f"<p>Xin chào <strong>{order.get('buyer_name', '')}</strong>,</p>"
                "<p>Đơn đặt vé của bạn đã hết hạn do chưa thanh toán trong thời gian quy định. "
                "Vui lòng thực hiện lại nếu bạn vẫn muốn tham dự sự kiện.</p>"
                "<p style='color:#888;font-size:14px'>Your ticket order has expired due to incomplete payment. "
                "Please try again if you still wish to attend.</p>"
                "</div>",
            )
        except Exception:
            pass




async def send_meeting_reminders() -> None:
    """
    APScheduler job — runs every hour.
    Finds confirmed meetings scheduled 23-25h from now with reminder_sent IS NULL.
    Sends bilingual reminder emails to exhibitor + visitor, then marks reminder_sent.
    """
    if not DIRECTUS_ADMIN_TOKEN:
        return

    now = datetime.now(timezone.utc)
    window_start = (now + timedelta(hours=23)).isoformat()
    window_end = (now + timedelta(hours=25)).isoformat()

    try:
        resp = await directus_get(
            "/items/meetings"
            f"?filter[status][_in]=confirmed,scheduled"
            f"&filter[scheduled_at][_gte]={window_start}"
            f"&filter[scheduled_at][_lte]={window_end}"
            f"&filter[reminder_sent][_null]=true"
            "&fields[]=id,scheduled_at,location,meeting_category,event_id,"
            "registration_id,exhibitor_id,job_requirement_id.job_title"
            "&limit=100"
        )
        meetings = resp.get("data", [])
    except Exception:
        return

    for meeting in meetings:
        meeting_id = meeting.get("id")
        event_id = str(meeting.get("event_id", ""))
        registration_id = str(meeting.get("registration_id", ""))
        exhibitor_id = str(meeting.get("exhibitor_id", ""))
        meeting_category = meeting.get("meeting_category") or "talent"
        job_title = (meeting.get("job_requirement_id") or {}).get("job_title") or "vị trí này / this position"
        tab = "hiring" if meeting_category == "talent" else "business"
        portal_url = f"{PORTAL_URL}/meetings?event={event_id}&tab={tab}"

        scheduled_at = meeting.get("scheduled_at", "")
        try:
            dt = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
            # Convert to tenant timezone for display in email
            tenant_tz = await _get_tenant_timezone(event_id)
            from zoneinfo import ZoneInfo
            local_dt = dt.astimezone(ZoneInfo(tenant_tz))
            time_str = local_dt.strftime("%d/%m/%Y %H:%M")
        except Exception:
            time_str = scheduled_at

        location_str = meeting.get("location") or ""

        visitor_email, visitor_name = await resolve_visitor_email(registration_id)
        exhibitor_email, company_name = await resolve_exhibitor_email(exhibitor_id, event_id)

        reminder_body = [
            "<strong>Nhắc nhở / Reminder:</strong> Cuộc họp của bạn sẽ diễn ra vào ngày mai.",
            "<strong>Reminder:</strong> Your meeting is scheduled for tomorrow.",
            f"<strong>Vị trí / Position:</strong> {job_title}",
        ]
        if time_str:
            reminder_body.append(f"<strong>Thời gian / When:</strong> {time_str}")
        if location_str:
            reminder_body.append(f"<strong>Địa điểm / Where:</strong> {location_str}")

        sent_count = 0

        if exhibitor_email:
            subject = f"[Nexpo] Nhắc lịch gặp mặt ngày mai / Meeting reminder tomorrow — {visitor_name or 'Ứng viên'}"
            html = meeting_notification_html(
                "Nhắc lịch gặp mặt / Meeting Reminder",
                reminder_body + ["Vui lòng chuẩn bị trước để buổi gặp mặt diễn ra suôn sẻ. / Please prepare in advance for a smooth meeting."],
                cta_label="Xem lịch họp / View Meeting", cta_url=portal_url,
            )
            if await send_mailgun(exhibitor_email, subject, html):
                sent_count += 1

        if visitor_email:
            subject = f"[Nexpo] Nhắc lịch gặp mặt ngày mai / Meeting reminder tomorrow — {company_name or 'Exhibitor'}"
            html = meeting_notification_html(
                "Nhắc lịch gặp mặt / Meeting Reminder",
                reminder_body + ["Chúc bạn buổi gặp mặt thành công! / We look forward to seeing you!"],
            )
            if await send_mailgun(visitor_email, subject, html):
                sent_count += 1

        if sent_count > 0 and meeting_id:
            try:
                await directus_patch(
                    f"/items/meetings/{meeting_id}",
                    {"reminder_sent": datetime.now(timezone.utc).isoformat()},
                )
            except Exception:
                pass


# ── Trial Reminder Emails ────────────────────────────────────────────────────

async def send_trial_reminders():
    """Send email reminders for trials ending in 7 days and 1 day."""
    now = datetime.now(timezone.utc)

    for days_left in [7, 1]:
        target = now + timedelta(days=days_left)
        target_start = target.replace(hour=0, minute=0, second=0)
        target_end = target.replace(hour=23, minute=59, second=59)

        try:
            result = await directus_get(
                f"/items/tenant_subscriptions"
                f"?filter[status][_eq]=trialing"
                f"&filter[trial_end][_gte]={target_start.isoformat()}"
                f"&filter[trial_end][_lte]={target_end.isoformat()}"
                f"&fields=tenant_id,trial_end"
                f"&limit=50"
            )
            subs = result.get("data", [])
            for sub in subs:
                tenant_id = sub["tenant_id"]
                try:
                    tenant = await directus_get(f"/items/tenants/{tenant_id}?fields=email,name,subscription_tier")
                    email = tenant.get("data", {}).get("email")
                    name = tenant.get("data", {}).get("name", "")
                    tier = tenant.get("data", {}).get("subscription_tier", "Pro")
                    if email:
                        subject = f"Nexpo: {days_left} Days Left in Trial / Dùng thử còn {days_left} ngày" if days_left > 1 else "Nexpo: Trial Ends Tomorrow / Dùng thử kết thúc ngày mai"
                        await send_mailgun(
                            to=email,
                            subject=subject,
                            html=_trial_reminder_html(name, tier, days_left),
                        )
                        logger.info(f"Trial reminder sent: tenant={tenant_id}, days_left={days_left}")
                except Exception as e:
                    logger.error(f"Trial reminder error for tenant {tenant_id}: {e}")
        except Exception as e:
            logger.error(f"[trial_reminders] Failed to fetch trials ending in {days_left}d: {e}")


def _trial_reminder_html(tenant_name: str, tier: str, days_left: int) -> str:
    """HTML email for trial reminder."""
    upgrade_url = f"{ADMIN_URL}/settings/subscription"
    urgency = " ⚡" if days_left <= 1 else ""
    return f"""
    <div style="font-family:'Segoe UI',Arial,sans-serif;max-width:600px;margin:0 auto;padding:40px 20px;">
      <div style="text-align:center;margin-bottom:30px;"><h1 style="color:#4F80FF;font-size:24px;margin:0;">NEXPO</h1></div>
      <div style="background:#f8fafc;border-radius:12px;padding:30px;border:1px solid #e2e8f0;">
        <h2 style="color:#1a1a1a;font-size:18px;margin:0 0 8px;">{days_left} Days Left in Your {tier} Trial{urgency}</h2>
        <p style="color:#404040;font-size:15px;line-height:1.6;margin:0 0 16px;">
          Hi {tenant_name}, your trial is ending soon. Upgrade now to keep full access to all features.
        </p>
        <h2 style="color:#64748b;font-size:16px;margin:0 0 8px;">Dùng thử gói {tier} còn {days_left} ngày{urgency}</h2>
        <p style="color:#64748b;font-size:14px;line-height:1.6;margin:0 0 20px;">
          Xin chào {tenant_name}, thời gian dùng thử sắp kết thúc. Nâng cấp ngay để tiếp tục sử dụng đầy đủ tính năng.
        </p>
        <div style="text-align:center;margin:20px 0;">
          <a href="{upgrade_url}" style="display:inline-block;background:#4F80FF;color:#fff;text-decoration:none;padding:12px 32px;border-radius:8px;font-weight:600;font-size:15px;">Upgrade Now / Nâng cấp ngay</a>
        </div>
        <p style="color:#94a3b8;font-size:13px;margin:15px 0 0;">
          Your account will be automatically downgraded to the free plan after the trial ends.<br/>
          Tài khoản sẽ tự động chuyển sang gói miễn phí sau khi hết thời gian dùng thử.
        </p>
      </div>
    </div>"""


async def expire_form_drafts() -> None:
    """
    APScheduler job — runs every hour.
    Deletes form_drafts whose expires_at has passed.
    """
    if not DIRECTUS_ADMIN_TOKEN:
        return

    now = datetime.now(timezone.utc).isoformat()
    try:
        resp = await directus_get(
            "/items/form_drafts"
            "?filter[status][_eq]=active"
            f"&filter[expires_at][_lt]={now}"
            "&fields[]=id"
            "&limit=100"
        )
        drafts = resp.get("data", [])
    except Exception as exc:
        logger.warning("[expire_drafts] Failed to fetch expired drafts: %s", exc)
        return

    for draft in drafts:
        try:
            await directus_patch(f"/items/form_drafts/{draft['id']}", {"status": "expired"})
        except Exception as exc:
            logger.error("[expire_drafts] Failed to expire draft %s: %s", draft["id"], exc)

    if drafts:
        logger.info("[expire_drafts] Expired %d form drafts", len(drafts))
