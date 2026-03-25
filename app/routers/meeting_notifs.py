from datetime import datetime
from fastapi import APIRouter, HTTPException
from app.models.schemas import MeetingNotificationRequest
from app.config import DIRECTUS_ADMIN_TOKEN, MAILGUN_API_KEY, MAILGUN_DOMAIN, MAILGUN_API_URL, PORTAL_URL, ADMIN_URL
from app.services.directus import directus_get, create_notification, resolve_visitor_email, resolve_exhibitor_email
from app.services.mailgun import send_mailgun, meeting_notification_html
import httpx

router = APIRouter()


@router.post("/meeting-notification")
async def send_meeting_notification(request: MeetingNotificationRequest):
    """
    Resolve meeting details from Directus and send notification emails + in-app notifications.
    trigger = "scheduled"  → email exhibitor
    trigger = "confirmed"  → email visitor
    trigger = "cancelled"  → email both parties
    """
    if not DIRECTUS_ADMIN_TOKEN:
        raise HTTPException(status_code=500, detail="DIRECTUS_ADMIN_TOKEN not configured")

    try:
        m_resp = await directus_get(
            f"/items/meetings/{request.meeting_id}"
            "?fields[]=id,status,scheduled_at,location,meeting_type,meeting_category,"
            "event_id,registration_id,exhibitor_id,job_requirement_id.job_title,organizer_note"
        )
        meeting = m_resp.get("data", {})
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")

        event_id = str(meeting.get("event_id", ""))
        registration_id = str(meeting.get("registration_id", ""))
        exhibitor_id = str(meeting.get("exhibitor_id", ""))
        meeting_category = meeting.get("meeting_category") or "talent"
        job_title = (meeting.get("job_requirement_id") or {}).get("job_title") or "vị trí này / this position"
        event_name = request.event_name or "sự kiện / the event"

        tab = "hiring" if meeting_category == "talent" else "business"
        portal_url = f"{PORTAL_URL}/meetings?event={event_id}&tab={tab}"

        scheduled_at = meeting.get("scheduled_at")
        time_str = ""
        if scheduled_at:
            try:
                dt = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
                time_str = dt.strftime("%d/%m/%Y %H:%M")
            except Exception:
                time_str = scheduled_at
        location_str = meeting.get("location") or ""

        visitor_email, visitor_name = await resolve_visitor_email(registration_id)
        exhibitor_email, company_name = await resolve_exhibitor_email(exhibitor_id, event_id)

        emails_sent = []
        in_app_created = []

        # ── SCHEDULED ────────────────────────────────────────────────────────
        if request.trigger == "scheduled" and exhibitor_email:
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
            body_lines.append("Vui lòng đăng nhập vào portal để xác nhận hoặc đổi lịch. / Please log in to your exhibitor portal to confirm or reschedule.")
            html = meeting_notification_html(
                "Yêu cầu gặp mặt mới / New Meeting Request", body_lines,
                cta_label="Xem cuộc họp / View Meeting", cta_url=portal_url,
            )
            if await send_mailgun(exhibitor_email, subject, html, from_email=f"Nexpo <noreply@{MAILGUN_DOMAIN}>"):
                emails_sent.append(f"exhibitor:{exhibitor_email}")

            try:
                ex_resp = await directus_get(f"/items/exhibitors/{exhibitor_id}?fields[]=user_id")
                user_id = (ex_resp.get("data") or {}).get("user_id")
                if user_id:
                    await create_notification(
                        user_id=user_id,
                        title="Yêu cầu gặp mặt mới",
                        body=f"{visitor_name or 'Ứng viên'} — {job_title}" + (f" · {time_str}" if time_str else ""),
                        link=portal_url,
                        notif_type="meeting_scheduled",
                        entity_type="meeting",
                        entity_id=request.meeting_id,
                    )
                    in_app_created.append(f"exhibitor:{user_id}")
            except Exception:
                pass

            # Also notify the event organizer with an admin link
            try:
                admin_matching_url = f"{ADMIN_URL}/events/{event_id}/talent-matching/requests" if tab == "hiring" else f"{ADMIN_URL}/events/{event_id}/business-matching/requests"
                event_resp = await directus_get(f"/items/events/{event_id}?fields[]=user_created")
                organizer_user_id = (event_resp.get("data") or {}).get("user_created")
                if organizer_user_id:
                    await create_notification(
                        user_id=organizer_user_id,
                        title="Yêu cầu gặp mặt mới",
                        body=f"{visitor_name or 'Ứng viên'} — {company_name or 'Exhibitor'}" + (f" · {time_str}" if time_str else ""),
                        link=admin_matching_url,
                        notif_type="meeting_scheduled",
                        entity_type="meeting",
                        entity_id=request.meeting_id,
                    )
                    in_app_created.append(f"organizer:{organizer_user_id}")
            except Exception:
                pass

        # ── CONFIRMED ─────────────────────────────────────────────────────────
        elif request.trigger == "confirmed" and visitor_email:
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
            body_lines.append("Vui lòng đến đúng giờ. Chúc bạn buổi gặp mặt thành công! / Please be on time. We look forward to seeing you!")
            html = meeting_notification_html("Cuộc họp đã được xác nhận! / Meeting Confirmed!", body_lines)
            if await send_mailgun(visitor_email, subject, html, from_email=f"Nexpo <noreply@{MAILGUN_DOMAIN}>"):
                emails_sent.append(f"visitor:{visitor_email}")

        # ── CANCELLED ─────────────────────────────────────────────────────────
        elif request.trigger == "cancelled":
            for recipient_type, email, name in [
                ("exhibitor", exhibitor_email, company_name),
                ("visitor", visitor_email, visitor_name),
            ]:
                if not email:
                    continue
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
                subject = f"[Nexpo] Cuộc họp đã bị hủy / Meeting cancelled — {job_title}"
                if await send_mailgun(email, subject, html, from_email=f"Nexpo <noreply@{MAILGUN_DOMAIN}>"):
                    emails_sent.append(f"{recipient_type}:{email}")

            try:
                ex_resp = await directus_get(f"/items/exhibitors/{exhibitor_id}?fields[]=user_id")
                user_id = (ex_resp.get("data") or {}).get("user_id")
                if user_id:
                    await create_notification(
                        user_id=user_id,
                        title="Cuộc họp đã bị hủy",
                        body=f"{visitor_name or 'Ứng viên'} — {job_title}",
                        link=portal_url,
                        notif_type="meeting_cancelled",
                        entity_type="meeting",
                        entity_id=request.meeting_id,
                    )
                    in_app_created.append(f"exhibitor_cancelled:{user_id}")
            except Exception:
                pass

        return {
            "success": True,
            "trigger": request.trigger,
            "emails_sent": emails_sent,
            "in_app_created": in_app_created,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Meeting notification error: {str(e)}")
