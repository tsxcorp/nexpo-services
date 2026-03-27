"""
Notification handler functions — one per notification type.
Called by POST /notify (unified) and legacy POST /meeting-notification.
All handlers are fire-tolerant: they never crash the caller over partial failures.
"""
import re
from datetime import datetime, timezone, timedelta
from app.config import MAILGUN_DOMAIN, ADMIN_URL, PORTAL_URL
from app.services.directus import (
    directus_get,
    directus_patch,
    create_notification,
    resolve_visitor_email,
    resolve_exhibitor_email,
)
from app.services.mailgun import send_mailgun, meeting_notification_html
from app.services.ics_service import generate_meeting_ics


# ── Notification log helper ───────────────────────────────────────────────────

async def append_meeting_notification_log(meeting_id: str, entries: list[dict]) -> None:
    """
    Append notification log entries to the meeting record.
    Each entry: { timestamp, trigger, channel, recipient_type, recipient, status, subject? }
    """
    if not entries:
        return
    try:
        # Fetch existing log
        resp = await directus_get(f"/items/meetings/{meeting_id}?fields[]=notification_log")
        existing = (resp.get("data") or {}).get("notification_log") or []
        if not isinstance(existing, list):
            existing = []

        # Append new entries
        updated_log = existing + entries

        # Save back
        await directus_patch(f"/items/meetings/{meeting_id}", {"notification_log": updated_log})
    except Exception:
        pass  # Never crash over logging


# ── Meeting email template helpers ────────────────────────────────────────────

async def _get_meeting_template(event_id: str, trigger_recipient: str, matching_type: str = "talent_matching") -> dict | None:
    """
    Fetch organizer-configured email template for (event_id, trigger_recipient, matching_type).
    Falls back to null matching_type (legacy) if no typed template found.
    Returns dict with 'subject' and 'html_template' keys, or None if not found.
    """
    try:
        # Try explicit matching_type first
        resp = await directus_get(
            f"/items/meeting_email_templates"
            f"?filter[event_id][_eq]={event_id}"
            f"&filter[trigger_recipient][_eq]={trigger_recipient}"
            f"&filter[matching_type][_eq]={matching_type}"
            f"&fields[]=subject,html_template&limit=1"
        )
        items = resp.get("data") or []
        if items and items[0].get("html_template"):
            return items[0]
        # Fallback: legacy null matching_type (only for talent_matching)
        if matching_type == "talent_matching":
            resp2 = await directus_get(
                f"/items/meeting_email_templates"
                f"?filter[event_id][_eq]={event_id}"
                f"&filter[trigger_recipient][_eq]={trigger_recipient}"
                f"&filter[matching_type][_null]=true"
                f"&fields[]=subject,html_template&limit=1"
            )
            items2 = resp2.get("data") or []
            if items2 and items2[0].get("html_template"):
                return items2[0]
    except Exception:
        pass
    return None


def _substitute(template: str, vars: dict) -> str:
    """Replace {{variable_name}} and ${variable_name} placeholders with values from vars dict.

    Supports both syntaxes:
     - {{company_name}}  → standard (used by new AI-generated and manual templates)
     - ${company_name}   → legacy (old AI-generated templates before the prompt fix)
    """
    def replacer(match: re.Match) -> str:
        key = match.group(1).strip()
        return str(vars.get(key, match.group(0)))

    result = re.sub(r"\{\{([^}]+)\}\}", replacer, template)   # {{var}} — primary
    result = re.sub(r"\$\{([^}]+)\}", replacer, result)        # ${var}  — legacy fallback
    return result


# ── Meetings ──────────────────────────────────────────────────────────────────

async def handle_meeting(meeting_id: str, trigger: str, event_name: str | None = None) -> dict:
    """
    trigger: "scheduled" | "confirmed" | "cancelled"

    scheduled  → email Exhibitor   + in-app Exhibitor + Organizer
    confirmed  → email Visitor     + in-app Exhibitor + Organizer
    cancelled  → email Visitor + Exhibitor + in-app Exhibitor + Organizer
    """
    m_resp = await directus_get(
        f"/items/meetings/{meeting_id}"
        "?fields[]=id,status,scheduled_at,location,meeting_type,meeting_category,"
        "event_id,registration_id,exhibitor_id,job_requirement_id.job_title,organizer_note,"
        "duration_minutes"
    )
    meeting = m_resp.get("data", {})
    if not meeting:
        raise ValueError(f"Meeting {meeting_id} not found")

    event_id = str(meeting.get("event_id", ""))
    registration_id = str(meeting.get("registration_id", ""))
    exhibitor_id = str(meeting.get("exhibitor_id", ""))
    meeting_category = meeting.get("meeting_category") or "talent"
    matching_type = "business_matching" if meeting_category == "business" else "talent_matching"
    job_title = (meeting.get("job_requirement_id") or {}).get("job_title") or "vị trí này / this position"

    tab = "hiring" if meeting_category == "talent" else "business"
    portal_url = f"{PORTAL_URL}/meetings?event={event_id}&tab={tab}"
    admin_link = f"{ADMIN_URL}/events/{event_id}/meetings?open={meeting_id}"

    scheduled_at = meeting.get("scheduled_at")
    # Directus `dateTime` fields are stored as naive local time (Vietnam UTC+7).
    # We must attach the correct timezone before converting to UTC for ICS.
    VN_TZ = timezone(timedelta(hours=7))

    def _parse_scheduled_at(raw: str) -> datetime | None:
        """Parse Directus naive dateTime as Vietnam local time (UTC+7)."""
        try:
            dt = datetime.fromisoformat(raw.replace("Z", ""))  # strip Z if present
            if dt.tzinfo is None:  # naive → treat as UTC+7
                dt = dt.replace(tzinfo=VN_TZ)
            return dt
        except Exception:
            return None

    time_str = ""
    if scheduled_at:
        dt_parsed = _parse_scheduled_at(scheduled_at)
        if dt_parsed:
            # Display in Vietnam local time
            time_str = dt_parsed.astimezone(VN_TZ).strftime("%d/%m/%Y %H:%M")
        else:
            time_str = scheduled_at
    location_str = meeting.get("location") or ""

    visitor_email, visitor_name = await resolve_visitor_email(registration_id)
    exhibitor_email, company_name = await resolve_exhibitor_email(exhibitor_id, event_id)

    duration_minutes = int(meeting.get("duration_minutes") or 30)

    emails_sent: list[str] = []
    in_app_created: list[str] = []
    notification_log: list[dict] = []  # Will be saved to meeting record

    def _log_entry(
        channel: str,
        recipient_type: str,
        recipient: str,
        status: str,
        subject: str = None,
    ) -> dict:
        """Build a notification log entry."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trigger": trigger,
            "channel": channel,
            "recipient_type": recipient_type,
            "recipient": recipient,
            "status": status,
        }
        if subject:
            entry["subject"] = subject
        return entry

    # ── Shared helpers ────────────────────────────────────────────────────────

    def _ics_attachment(method: str, attendee_emails: list[str], sequence: int) -> list | None:
        """Build a Mailgun-compatible attachment tuple for an .ics file, or None if no datetime."""
        if not scheduled_at:
            return None
        try:
            dt = _parse_scheduled_at(scheduled_at)  # timezone-aware UTC+7
            if dt is None:
                return None
            summary = f"Gặp mặt: {visitor_name or 'Ứng viên'} — {company_name or 'Exhibitor'}"
            description = f"Vị trí: {job_title}\nThời gian: {time_str}\nĐịa điểm: {location_str}"
            ics_bytes = generate_meeting_ics(
                meeting_id=meeting_id,
                method=method,
                summary=summary,
                description=description,
                dtstart=dt,
                duration_minutes=duration_minutes,
                location=location_str,
                organizer_email=f"noreply@{MAILGUN_DOMAIN}",
                organizer_name="Nexpo",
                attendee_emails=attendee_emails,
                sequence=sequence,
            )
            return [("attachment", ("invite.ics", ics_bytes, "text/calendar; method=" + method))]
        except Exception:
            return None

    async def _notify_exhibitor_user(title: str, body: str, link: str, notif_type: str) -> None:
        try:
            ex_resp = await directus_get(f"/items/exhibitors/{exhibitor_id}?fields[]=user_id")
            user_id = (ex_resp.get("data") or {}).get("user_id")
            if user_id:
                await create_notification(
                    user_id=user_id, title=title, body=body, link=link,
                    notif_type=notif_type, entity_type="meeting", entity_id=meeting_id,
                )
                in_app_created.append(f"exhibitor:{user_id}")
                notification_log.append(_log_entry(
                    channel="in_app",
                    recipient_type="exhibitor",
                    recipient=user_id,
                    status="sent",
                ))
        except Exception:
            pass

    async def _notify_organizer(title: str, body: str, notif_type: str) -> None:
        try:
            event_resp = await directus_get(f"/items/events/{event_id}?fields[]=user_created")
            organizer_id = (event_resp.get("data") or {}).get("user_created")
            if organizer_id:
                await create_notification(
                    user_id=organizer_id, title=title, body=body, link=admin_link,
                    notif_type=notif_type, entity_type="meeting", entity_id=meeting_id,
                )
                in_app_created.append(f"organizer:{organizer_id}")
                notification_log.append(_log_entry(
                    channel="in_app",
                    recipient_type="organizer",
                    recipient=organizer_id,
                    status="sent",
                ))
        except Exception:
            pass

    # ── Shared template variables ──────────────────────────────────────────────
    tmpl_vars = {
        "visitor_name": visitor_name or "",
        "company_name": company_name or "",
        "job_title": job_title,
        "scheduled_at": time_str,
        "location": location_str,
        "portal_url": portal_url,
        "event_name": event_name or "",
    }

    # ── SCHEDULED ─────────────────────────────────────────────────────────────
    if trigger == "scheduled":
        if exhibitor_email:
            tmpl = await _get_meeting_template(event_id, "scheduled_exhibitor", matching_type)
            if tmpl:
                subject = _substitute(tmpl.get("subject") or "", tmpl_vars) or \
                    f"[Nexpo] Yêu cầu gặp mặt mới / New meeting request — {visitor_name or 'Ứng viên / Candidate'}"
                html = _substitute(tmpl["html_template"], tmpl_vars)
            else:
                subject = f"[Nexpo] Yêu cầu gặp mặt mới / New meeting request — {visitor_name or 'Ứng viên / Candidate'}"
                body_lines = [
                    f"Bạn có một yêu cầu gặp mặt mới từ <strong>{visitor_name or 'ứng viên'}</strong>.",
                    f"You have a new meeting request from <strong>{visitor_name or 'a candidate'}</strong>.",
                    f"<strong>Vị trí / Position:</strong> {job_title}",
                ]
                if time_str:
                    body_lines.append(f"<strong>Thời gian / Scheduled:</strong> {time_str}")
                if location_str:
                    body_lines.append(f"<strong>Địa điểm / Location:</strong> {location_str}")
                body_lines.append(
                    "Vui lòng đăng nhập vào portal để xác nhận hoặc đổi lịch. "
                    "/ Please log in to your exhibitor portal to confirm or reschedule."
                )
                html = meeting_notification_html(
                    "Yêu cầu gặp mặt mới / New Meeting Request", body_lines,
                    cta_label="Xem cuộc họp / View Meeting", cta_url=portal_url,
                )
            ics = _ics_attachment("REQUEST", [exhibitor_email], sequence=0)
            email_sent = await send_mailgun(exhibitor_email, subject, html,
                                            from_email=f"Nexpo <noreply@{MAILGUN_DOMAIN}>",
                                            attachments=ics)
            if email_sent:
                emails_sent.append(f"exhibitor:{exhibitor_email}")
            notification_log.append(_log_entry(
                channel="email",
                recipient_type="exhibitor",
                recipient=exhibitor_email,
                status="sent" if email_sent else "failed",
                subject=subject,
            ))

        candidate_summary = f"{visitor_name or 'Ứng viên'} — {job_title}" + (f" · {time_str}" if time_str else "")
        await _notify_exhibitor_user(
            title="Yêu cầu gặp mặt mới",
            body=candidate_summary,
            link=portal_url,
            notif_type="meeting_scheduled",
        )
        await _notify_organizer(
            title="Yêu cầu gặp mặt mới",
            body=f"{visitor_name or 'Ứng viên'} — {company_name or 'Exhibitor'}" + (f" · {time_str}" if time_str else ""),
            notif_type="meeting_scheduled",
        )

    # ── CONFIRMED ─────────────────────────────────────────────────────────────
    elif trigger == "confirmed":
        if visitor_email:
            tmpl = await _get_meeting_template(event_id, "confirmed_visitor", matching_type)
            if tmpl:
                subject = _substitute(tmpl.get("subject") or "", tmpl_vars) or \
                    f"[Nexpo] Cuộc họp đã được xác nhận / Meeting confirmed — {company_name or 'Exhibitor'}"
                html = _substitute(tmpl["html_template"], tmpl_vars)
            else:
                subject = f"[Nexpo] Cuộc họp đã được xác nhận / Meeting confirmed — {company_name or 'Exhibitor'}"
                body_lines = [
                    f"Cuộc họp của bạn với <strong>{company_name or 'nhà tuyển dụng'}</strong> đã được xác nhận.",
                    f"Your meeting with <strong>{company_name or 'the exhibitor'}</strong> has been confirmed.",
                    f"<strong>Vị trí / Position:</strong> {job_title}",
                ]
                if time_str:
                    body_lines.append(f"<strong>Thời gian / When:</strong> {time_str}")
                if location_str:
                    body_lines.append(f"<strong>Địa điểm / Where:</strong> {location_str}")
                body_lines.append(
                    "Vui lòng đến đúng giờ. Chúc bạn buổi gặp mặt thành công! "
                    "/ Please be on time. We look forward to seeing you!"
                )
                html = meeting_notification_html("Cuộc họp đã được xác nhận! / Meeting Confirmed!", body_lines)
            ics = _ics_attachment("REQUEST", [visitor_email], sequence=1)
            email_sent = await send_mailgun(visitor_email, subject, html,
                                            from_email=f"Nexpo <noreply@{MAILGUN_DOMAIN}>",
                                            attachments=ics)
            if email_sent:
                emails_sent.append(f"visitor:{visitor_email}")
            notification_log.append(_log_entry(
                channel="email",
                recipient_type="visitor",
                recipient=visitor_email,
                status="sent" if email_sent else "failed",
                subject=subject,
            ))

        await _notify_exhibitor_user(
            title="Bạn đã xác nhận cuộc họp",
            body=f"{visitor_name or 'Ứng viên'} — {job_title}" + (f" · {time_str}" if time_str else ""),
            link=portal_url,  # Exhibitors are portal users — link to portal meetings page
            notif_type="meeting_confirmed",
        )
        await _notify_organizer(
            title="Cuộc họp đã được xác nhận",
            body=f"{company_name or 'Exhibitor'} xác nhận gặp {visitor_name or 'ứng viên'}",
            notif_type="meeting_confirmed",
        )

    # ── CANCELLED ─────────────────────────────────────────────────────────────
    elif trigger == "cancelled":
        for recipient_type, email in [("exhibitor", exhibitor_email), ("visitor", visitor_email)]:
            if not email:
                continue
            tr_key = f"cancelled_{recipient_type}"
            tmpl = await _get_meeting_template(event_id, tr_key, matching_type)
            if tmpl:
                subject = _substitute(tmpl.get("subject") or "", tmpl_vars) or \
                    f"[Nexpo] Cuộc họp đã bị hủy / Meeting cancelled — {job_title}"
                html = _substitute(tmpl["html_template"], tmpl_vars)
            else:
                subject = f"[Nexpo] Cuộc họp đã bị hủy / Meeting cancelled — {job_title}"
                if recipient_type == "visitor":
                    body_lines = [
                        f"Rất tiếc, cuộc họp của bạn với <strong>{company_name or 'nhà tuyển dụng'}</strong> đã bị hủy.",
                        f"Unfortunately, your meeting with <strong>{company_name or 'the exhibitor'}</strong> has been cancelled.",
                        f"<strong>Vị trí / Position:</strong> {job_title}",
                        "Vui lòng liên hệ ban tổ chức nếu bạn có thắc mắc. / Please contact the organizer if you have any questions.",
                    ]
                else:
                    body_lines = [
                        f"Cuộc họp với <strong>{visitor_name or 'ứng viên'}</strong> đã bị hủy.",
                        f"The meeting with <strong>{visitor_name or 'the candidate'}</strong> has been cancelled.",
                        f"<strong>Vị trí / Position:</strong> {job_title}",
                    ]
                html = meeting_notification_html("Cuộc họp đã bị hủy / Meeting Cancelled", body_lines)
            ics = _ics_attachment("CANCEL", [email], sequence=2)
            email_sent = await send_mailgun(email, subject, html,
                                            from_email=f"Nexpo <noreply@{MAILGUN_DOMAIN}>",
                                            attachments=ics)
            if email_sent:
                emails_sent.append(f"{recipient_type}:{email}")
            notification_log.append(_log_entry(
                channel="email",
                recipient_type=recipient_type,
                recipient=email,
                status="sent" if email_sent else "failed",
                subject=subject,
            ))

        await _notify_exhibitor_user(
            title="Cuộc họp đã bị hủy",
            body=f"{visitor_name or 'Ứng viên'} — {job_title}",
            link=portal_url,
            notif_type="meeting_cancelled",
        )
        await _notify_organizer(
            title="Cuộc họp bị hủy",
            body=f"{company_name or 'Exhibitor'} — {visitor_name or 'Ứng viên'}" + (f" · {time_str}" if time_str else ""),
            notif_type="meeting_cancelled",
        )

    # ── Persist notification log to meeting record ────────────────────────────
    await append_meeting_notification_log(meeting_id, notification_log)

    return {"emails_sent": emails_sent, "in_app_created": in_app_created}


# ── Facility Orders ───────────────────────────────────────────────────────────

async def handle_order_facility_created(order_id: str, event_id: str) -> dict:
    """In-app → Organizer when exhibitor submits a facility order."""
    in_app_created: list[str] = []
    try:
        order_resp = await directus_get(f"/items/facility_orders/{order_id}?fields[]=ref_number,total_amount")
        order = order_resp.get("data", {})

        items_resp = await directus_get(
            f"/items/facility_order_items?filter[order_id][_eq]={order_id}&aggregate[count][]=id"
        )
        items_data = items_resp.get("data") or [{}]
        item_count = (items_data[0].get("count") or {}).get("id", 0)

        event_resp = await directus_get(f"/items/events/{event_id}?fields[]=user_created")
        organizer_id = (event_resp.get("data") or {}).get("user_created")
        if organizer_id:
            ref = order.get("ref_number", order_id)
            total = float(order.get("total_amount") or 0)
            await create_notification(
                user_id=organizer_id,
                title="Đơn hàng thiết bị mới / New facility order",
                body=f"Ref: {ref} · {item_count} item(s) · {total:,.0f} VND",
                link=f"{ADMIN_URL}/events/{event_id}/orders?open={order_id}",
                notif_type="order_facility_created",
                entity_type="facility_orders",
                entity_id=order_id,
            )
            in_app_created.append(f"organizer:{organizer_id}")
    except Exception:
        pass
    return {"in_app_created": in_app_created}


# ── Support Tickets ───────────────────────────────────────────────────────────

async def handle_ticket_support_created(ticket_id: str, event_id: str) -> dict:
    """In-app → Organizer when exhibitor opens a support ticket."""
    in_app_created: list[str] = []
    try:
        ticket_resp = await directus_get(f"/items/support_tickets/{ticket_id}?fields[]=subject,priority")
        ticket = ticket_resp.get("data", {})

        event_resp = await directus_get(f"/items/events/{event_id}?fields[]=user_created")
        organizer_id = (event_resp.get("data") or {}).get("user_created")
        if organizer_id:
            priority = (ticket.get("priority") or "medium").upper()
            subject = ticket.get("subject", "")
            await create_notification(
                user_id=organizer_id,
                title="Ticket hỗ trợ mới / New support ticket",
                body=f"[{priority}] {subject}",
                link=f"{ADMIN_URL}/events/{event_id}/tickets?open={ticket_id}",
                notif_type="ticket_support_created",
                entity_type="support_tickets",
                entity_id=ticket_id,
            )
            in_app_created.append(f"organizer:{organizer_id}")
    except Exception:
        pass
    return {"in_app_created": in_app_created}


# ── Lead Capture ──────────────────────────────────────────────────────────────

async def handle_lead_captured(
    user_id: str,
    attendee_name: str,
    attendee_email: str,
    attendee_company: str,
    event_id: str,
) -> dict:
    """In-app → Exhibitor user (self-notify / activity log) when lead is captured."""
    in_app_created: list[str] = []
    try:
        if not user_id:
            return {"in_app_created": in_app_created}
        body = f"{attendee_company} · {attendee_email}" if attendee_company else attendee_email
        await create_notification(
            user_id=user_id,
            title=f"Lead mới: {attendee_name or 'Khách tham quan'}",
            body=body,
            link=f"/leads?event={event_id}" if event_id else None,
            notif_type="lead_captured",
            entity_type="leads",
        )
        in_app_created.append(f"exhibitor:{user_id}")
    except Exception:
        pass
    return {"in_app_created": in_app_created}
