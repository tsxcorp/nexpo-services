"""
Meeting notification handler — scheduled, confirmed, cancelled triggers.
Sends email (with ICS) + in-app notifications to exhibitor, visitor, organizer.
"""
from datetime import datetime, timezone, timedelta
from app.config import MAILGUN_DOMAIN, ADMIN_URL, PORTAL_URL
from app.services.directus import (
    directus_get, create_notification,
    resolve_visitor_email, resolve_exhibitor_email,
)
from app.services.mailgun import send_mailgun, meeting_notification_html, wrap_email_body
from app.services.ics_service import generate_meeting_ics
from app.services.handlers.notification_helpers import (
    append_meeting_notification_log, get_meeting_template, substitute,
    get_meeting_email_template_v2_or_legacy,
)
from app.services.handlers.template_render import safe_substitute, build_context

VN_TZ = timezone(timedelta(hours=7))

# ── V2/Legacy trigger key mapping ─────────────────────────────────────────────
# Legacy handler uses composite keys (trigger_recipient).
# V2 templates use semantic trigger_key matching the new MEETING_TRIGGERS list.
# FIX 3: Extended to cover all 4 V2 triggers (reschedule + reminder_24h added).
_LEGACY_TO_V2_TRIGGER: dict[str, str] = {
    "scheduled_exhibitor":   "meeting_confirm",
    "scheduled_visitor":     "meeting_confirm",
    "confirmed_exhibitor":   "meeting_confirm",
    "confirmed_visitor":     "meeting_confirm",
    "rescheduled_exhibitor": "meeting_reschedule",
    "rescheduled_visitor":   "meeting_reschedule",
    "cancelled_exhibitor":   "meeting_cancel",
    "cancelled_visitor":     "meeting_cancel",
    "reminder_exhibitor_24h": "meeting_reminder_24h",
    "reminder_visitor_24h":   "meeting_reminder_24h",
}


async def _get_html_for_meeting_email(
    legacy_trigger_recipient: str,
    event_id: str,
    meeting_id: str,
    exhibitor_id: str | None,
    registration_id: str | None,
    matching_type: str,
    language_code: str,
    legacy_tmpl_vars: dict,
    email_style: dict,
) -> tuple[str | None, str | None]:
    """Return (subject, html) using V2 template when available, else legacy.

    V2 path: fetches `email_templates` row → safe_substitute with scoped context.
    Legacy path: calls get_meeting_template → flat substitute.
    Returns (None, None) when no template found at all (caller falls through to default).
    """
    v2_trigger = _LEGACY_TO_V2_TRIGGER.get(legacy_trigger_recipient)
    if v2_trigger:
        result = await get_meeting_email_template_v2_or_legacy(
            event_id, v2_trigger, matching_type, language_code
        )
        source = result.get("source")
        tpl = result.get("template")

        if source == "v2" and tpl:
            html_src = tpl.get("html_compiled") or tpl.get("mjml_source") or ""
            if html_src:
                ctx = await build_context(
                    "meeting",
                    event_id=event_id,
                    meeting_id=meeting_id,
                    exhibitor_id=exhibitor_id,
                    registration_id=registration_id,
                )
                rendered_html = safe_substitute(html_src, ctx, "meeting")
                subject_raw = tpl.get("subject") or ""
                subject = safe_substitute(subject_raw, ctx, "meeting") if subject_raw else None
                return subject, rendered_html

        if source == "legacy" and tpl:
            # legacy tpl shape: {"subject": str, "html_template": str}
            # Step 1: resolve {{scope.field}} V2-syntax tokens via safe_substitute
            # Step 2: resolve legacy flat-key {{var}} / ${var} via substitute
            # Order matters: safe_substitute must run first (it won't touch unknown keys)
            v2_ctx = await build_context(
                "meeting",
                event_id=event_id,
                meeting_id=meeting_id,
                exhibitor_id=exhibitor_id,
                registration_id=registration_id,
            )
            raw_subj = tpl.get("subject") or ""
            raw_html_tpl = tpl.get("html_template") or ""
            # Apply V2 token resolution first ({{event.name}}, {{visitor.full_name}}, etc.)
            raw_subj = safe_substitute(raw_subj, v2_ctx, "meeting")
            raw_html_tpl = safe_substitute(raw_html_tpl, v2_ctx, "meeting")
            # Then apply legacy flat-key substituter ({{visitor_name}}, ${visitor_name}, etc.)
            subject = substitute(raw_subj, legacy_tmpl_vars) or None
            raw_html = substitute(raw_html_tpl, legacy_tmpl_vars)
            html = raw_html if "<html" in raw_html.lower() else wrap_email_body(raw_html, email_style)
            return subject, html

    # No V2 mapping or no template found — direct legacy lookup
    legacy_tpl = await get_meeting_template(event_id, legacy_trigger_recipient, matching_type)
    if legacy_tpl:
        # Same dual-pass substitution: V2 tokens first, legacy flat-keys second
        v2_ctx = await build_context(
            "meeting",
            event_id=event_id,
            meeting_id=meeting_id,
            exhibitor_id=exhibitor_id,
            registration_id=registration_id,
        )
        raw_subj = safe_substitute(legacy_tpl.get("subject") or "", v2_ctx, "meeting")
        raw_html_tpl = safe_substitute(legacy_tpl.get("html_template") or "", v2_ctx, "meeting")
        subject = substitute(raw_subj, legacy_tmpl_vars) or None
        raw_html = substitute(raw_html_tpl, legacy_tmpl_vars)
        html = raw_html if "<html" in raw_html.lower() else wrap_email_body(raw_html, email_style)
        return subject, html

    return None, None


def _parse_scheduled_at(raw: str) -> datetime | None:
    """Parse Directus naive dateTime as Vietnam local time (UTC+7)."""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=VN_TZ)
        return dt
    except Exception:
        return None


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
        "event_id,registration_id,exhibitor_id,job_requirement_id.job_title,"
        "business_requirement_id.summary,business_requirement_id.requirement_type,"
        "organizer_note,duration_minutes"
    )
    meeting = m_resp.get("data", {})
    if not meeting:
        raise ValueError(f"Meeting {meeting_id} not found")

    event_id = str(meeting.get("event_id", ""))
    registration_id = str(meeting.get("registration_id", ""))
    exhibitor_id = str(meeting.get("exhibitor_id", ""))

    # Fetch event brand settings for styled default emails
    email_style: dict | None = None
    try:
        ev_resp = await directus_get(f"/items/events/{event_id}?fields[]=email_style,name,logo")
        ev_data = ev_resp.get("data") or {}
        email_style = ev_data.get("email_style") if isinstance(ev_data.get("email_style"), dict) else {}
        if not event_name:
            event_name = ev_data.get("name") or ""
        # Auto-populate logo from event.logo if not set in email_style
        if not email_style.get("logo_url") and ev_data.get("logo"):
            from app.config import DIRECTUS_URL
            email_style["logo_url"] = f"{DIRECTUS_URL}/assets/{ev_data['logo']}"
        # Auto-populate event label from event name if not set
        if not email_style.get("event_label") and event_name:
            email_style["event_label"] = event_name
    except Exception:
        pass
    meeting_category = meeting.get("meeting_category") or "talent"
    matching_type = "business_matching" if meeting_category == "business" else "talent_matching"
    is_business = meeting_category == "business"

    # Context-aware labels for fallback templates
    if is_business:
        biz_req = meeting.get("business_requirement_id")
        if not isinstance(biz_req, dict):
            biz_req = {}
        job_title = biz_req.get("summary") or biz_req.get("requirement_type") or "hợp tác kinh doanh / business partnership"
        visitor_label_vi, visitor_label_en = "đối tác", "partner"
        context_label_vi = "Nhu cầu / Requirement"
        company_fallback_vi = "đối tác"
    else:
        job_title = (meeting.get("job_requirement_id") or {}).get("job_title") or "vị trí này / this position"
        visitor_label_vi, visitor_label_en = "ứng viên", "candidate"
        context_label_vi = "Vị trí / Position"
        company_fallback_vi = "nhà tuyển dụng"

    tab = "hiring" if meeting_category == "talent" else "business"
    portal_url = f"{PORTAL_URL}/meetings?event={event_id}&tab={tab}"
    admin_link = f"{ADMIN_URL}/events/{event_id}/meetings?open={meeting_id}"

    scheduled_at = meeting.get("scheduled_at")
    time_str = ""
    if scheduled_at:
        dt_parsed = _parse_scheduled_at(scheduled_at)
        if dt_parsed:
            time_str = dt_parsed.astimezone(VN_TZ).strftime("%d/%m/%Y %H:%M")
        else:
            time_str = scheduled_at
    location_str = meeting.get("location") or ""

    visitor_email, visitor_name = await resolve_visitor_email(registration_id)
    exhibitor_email, company_name = await resolve_exhibitor_email(exhibitor_id, event_id)

    duration_minutes = int(meeting.get("duration_minutes") or 30)

    emails_sent: list[str] = []
    in_app_created: list[str] = []
    notification_log: list[dict] = []

    def _log_entry(channel: str, recipient_type: str, recipient: str, status: str, subject: str = None) -> dict:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trigger": trigger, "channel": channel,
            "recipient_type": recipient_type, "recipient": recipient, "status": status,
        }
        if subject:
            entry["subject"] = subject
        return entry

    # ── Shared helpers ────────────────────────────────────────────────────────

    def _ics_attachment(method: str, attendee_emails: list[str], sequence: int) -> list | None:
        if not scheduled_at:
            return None
        try:
            dt = _parse_scheduled_at(scheduled_at)
            if dt is None:
                return None
            summary = f"Gặp mặt: {visitor_name or 'Ứng viên'} — {company_name or 'Exhibitor'}"
            description = f"Vị trí: {job_title}\nThời gian: {time_str}\nĐịa điểm: {location_str}"
            ics_bytes = generate_meeting_ics(
                meeting_id=meeting_id, method=method, summary=summary,
                description=description, dtstart=dt, duration_minutes=duration_minutes,
                location=location_str, organizer_email=f"noreply@{MAILGUN_DOMAIN}",
                organizer_name="Nexpo", attendee_emails=attendee_emails, sequence=sequence,
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
                notification_log.append(_log_entry("in_app", "exhibitor", user_id, "sent"))
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
                notification_log.append(_log_entry("in_app", "organizer", organizer_id, "sent"))
        except Exception:
            pass

    # ── Template variables ────────────────────────────────────────────────────
    tmpl_vars = {
        "visitor_name": visitor_name or "", "company_name": company_name or "",
        "job_title": job_title, "scheduled_at": time_str, "location": location_str,
        "portal_url": portal_url, "event_name": event_name or "",
        "matching_type": matching_type, "visitor_label": visitor_label_vi,
        "context_label": context_label_vi,
    }

    # ── SCHEDULED ─────────────────────────────────────────────────────────────
    if trigger == "scheduled":
        if exhibitor_email:
            # FIX 1: Use V2-aware helper instead of direct get_meeting_template call.
            # _get_html_for_meeting_email checks V2 email_templates first, then legacy fallback.
            v2_subject, v2_html = await _get_html_for_meeting_email(
                "scheduled_exhibitor", event_id, meeting_id,
                exhibitor_id, registration_id, matching_type, "vi", tmpl_vars, email_style,
            )
            if v2_html:
                subject = v2_subject or \
                    f"[Nexpo] Yêu cầu gặp mặt mới / New meeting request — {visitor_name or 'Ứng viên / Candidate'}"
                html = v2_html
            else:
                subject = f"[Nexpo] Yêu cầu gặp mặt mới / New meeting request — {visitor_name or visitor_label_vi.capitalize()}"
                body_lines = [
                    f"Bạn có một yêu cầu gặp mặt mới từ <strong>{visitor_name or visitor_label_vi}</strong>.",
                    f"You have a new meeting request from <strong>{visitor_name or ('a ' + visitor_label_en)}</strong>.",
                    f"<strong>{context_label_vi}:</strong> {job_title}",
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
                    title="Yêu cầu gặp mặt mới / New Meeting Request",
                    body_lines=body_lines,
                    cta_label="Xem cuộc họp / View Meeting",
                    cta_url=portal_url,
                    email_style=email_style,
                )
            ics = _ics_attachment("REQUEST", [exhibitor_email], sequence=0)
            email_sent = await send_mailgun(exhibitor_email, subject, html,
                                            from_email=f"Nexpo <noreply@{MAILGUN_DOMAIN}>",
                                            attachments=ics)
            if email_sent:
                emails_sent.append(f"exhibitor:{exhibitor_email}")
            notification_log.append(_log_entry(
                "email", "exhibitor", exhibitor_email,
                "sent" if email_sent else "failed", subject,
            ))

        contact_summary = f"{visitor_name or visitor_label_vi.capitalize()} — {job_title}" + (f" · {time_str}" if time_str else "")
        await _notify_exhibitor_user("Yêu cầu gặp mặt mới", contact_summary, portal_url, "meeting_scheduled")
        await _notify_organizer(
            "Yêu cầu gặp mặt mới",
            f"{visitor_name or visitor_label_vi.capitalize()} — {company_name or 'Exhibitor'}" + (f" · {time_str}" if time_str else ""),
            "meeting_scheduled",
        )

    # ── CONFIRMED ─────────────────────────────────────────────────────────────
    elif trigger == "confirmed":
        if visitor_email:
            # FIX 1: Use V2-aware helper — maps confirmed_visitor → meeting_confirm trigger key.
            v2_subject, v2_html = await _get_html_for_meeting_email(
                "confirmed_visitor", event_id, meeting_id,
                exhibitor_id, registration_id, matching_type, "vi", tmpl_vars, email_style,
            )
            if v2_html:
                subject = v2_subject or \
                    f"[Nexpo] Cuộc họp đã được xác nhận / Meeting confirmed — {company_name or 'Exhibitor'}"
                html = v2_html
            else:
                subject = f"[Nexpo] Cuộc họp đã được xác nhận / Meeting confirmed — {company_name or 'Exhibitor'}"
                body_lines = [
                    f"Cuộc họp của bạn với <strong>{company_name or company_fallback_vi}</strong> đã được xác nhận.",
                    f"Your meeting with <strong>{company_name or 'the exhibitor'}</strong> has been confirmed.",
                    f"<strong>{context_label_vi}:</strong> {job_title}",
                ]
                if time_str:
                    body_lines.append(f"<strong>Thời gian / When:</strong> {time_str}")
                if location_str:
                    body_lines.append(f"<strong>Địa điểm / Where:</strong> {location_str}")
                body_lines.append(
                    "Vui lòng đến đúng giờ. Chúc bạn buổi gặp mặt thành công! "
                    "/ Please be on time. We look forward to seeing you!"
                )
                html = meeting_notification_html(
                    title="Cuộc họp đã được xác nhận! / Meeting Confirmed!",
                    body_lines=body_lines,
                    email_style=email_style,
                )
            ics = _ics_attachment("REQUEST", [visitor_email], sequence=1)
            email_sent = await send_mailgun(visitor_email, subject, html,
                                            from_email=f"Nexpo <noreply@{MAILGUN_DOMAIN}>",
                                            attachments=ics)
            if email_sent:
                emails_sent.append(f"visitor:{visitor_email}")
            notification_log.append(_log_entry(
                "email", "visitor", visitor_email,
                "sent" if email_sent else "failed", subject,
            ))

        await _notify_exhibitor_user(
            "Bạn đã xác nhận cuộc họp",
            f"{visitor_name or visitor_label_vi.capitalize()} — {job_title}" + (f" · {time_str}" if time_str else ""),
            portal_url, "meeting_confirmed",
        )
        await _notify_organizer(
            "Cuộc họp đã được xác nhận",
            f"{company_name or 'Exhibitor'} xác nhận gặp {visitor_name or visitor_label_vi}",
            "meeting_confirmed",
        )

    # ── CANCELLED ─────────────────────────────────────────────────────────────
    elif trigger == "cancelled":
        for recipient_type, email in [("exhibitor", exhibitor_email), ("visitor", visitor_email)]:
            if not email:
                continue
            tr_key = f"cancelled_{recipient_type}"
            # FIX 1: Use V2-aware helper — maps cancelled_* → meeting_cancel trigger key.
            v2_subject, v2_html = await _get_html_for_meeting_email(
                tr_key, event_id, meeting_id,
                exhibitor_id, registration_id, matching_type, "vi", tmpl_vars, email_style,
            )
            if v2_html:
                subject = v2_subject or \
                    f"[Nexpo] Cuộc họp đã bị hủy / Meeting cancelled — {job_title}"
                html = v2_html
            else:
                subject = f"[Nexpo] Cuộc họp đã bị hủy / Meeting cancelled — {job_title}"
                if recipient_type == "visitor":
                    body_lines = [
                        f"Rất tiếc, cuộc họp của bạn với <strong>{company_name or company_fallback_vi}</strong> đã bị hủy.",
                        f"Unfortunately, your meeting with <strong>{company_name or 'the exhibitor'}</strong> has been cancelled.",
                        f"<strong>{context_label_vi}:</strong> {job_title}",
                        "Vui lòng liên hệ ban tổ chức nếu bạn có thắc mắc. / Please contact the organizer if you have any questions.",
                    ]
                else:
                    body_lines = [
                        f"Cuộc họp với <strong>{visitor_name or visitor_label_vi}</strong> đã bị hủy.",
                        f"The meeting with <strong>{visitor_name or ('the ' + visitor_label_en)}</strong> has been cancelled.",
                        f"<strong>{context_label_vi}:</strong> {job_title}",
                    ]
                html = meeting_notification_html(
                    title="Cuộc họp đã bị hủy / Meeting Cancelled",
                    body_lines=body_lines,
                    email_style=email_style,
                )
            ics = _ics_attachment("CANCEL", [email], sequence=2)
            email_sent = await send_mailgun(email, subject, html,
                                            from_email=f"Nexpo <noreply@{MAILGUN_DOMAIN}>",
                                            attachments=ics)
            if email_sent:
                emails_sent.append(f"{recipient_type}:{email}")
            notification_log.append(_log_entry(
                "email", recipient_type, email,
                "sent" if email_sent else "failed", subject,
            ))

        await _notify_exhibitor_user(
            "Cuộc họp đã bị hủy",
            f"{visitor_name or visitor_label_vi.capitalize()} — {job_title}",
            portal_url, "meeting_cancelled",
        )
        await _notify_organizer(
            "Cuộc họp bị hủy",
            f"{company_name or 'Exhibitor'} — {visitor_name or visitor_label_vi.capitalize()}" + (f" · {time_str}" if time_str else ""),
            "meeting_cancelled",
        )

    # ── Persist notification log ──────────────────────────────────────────────
    await append_meeting_notification_log(meeting_id, notification_log)

    return {"emails_sent": emails_sent, "in_app_created": in_app_created}
