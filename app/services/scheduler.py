"""
APScheduler setup and scheduled jobs.
Import `scheduler` and call scheduler.start() / scheduler.shutdown() from lifespan.
"""
from datetime import datetime, timezone, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.config import DIRECTUS_ADMIN_TOKEN, PORTAL_URL
from app.services.directus import directus_get, directus_patch, directus_delete
from app.services.directus import resolve_visitor_email, resolve_exhibitor_email
from app.services.mailgun import send_mailgun, meeting_notification_html
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
            f"?filter[status][_eq]=confirmed"
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
