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
    directus_post,
    directus_patch,
    create_notification,
    resolve_visitor_email,
    resolve_exhibitor_email,
)
from app.services.mailgun import send_mailgun, meeting_notification_html
from app.services.ics_service import generate_meeting_ics, generate_combined_ics


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


# ── Registration QR Email ─────────────────────────────────────────────────────

async def handle_registration_qr(registration_id: str, triggered_by: str = "admin") -> dict:
    """
    Fetch registration → resolve email + render HTML template → send QR email → log activity.
    This is the single source of truth for registration QR email sending.

    Returns: { "email": str, "status": "sent" | "failed", "error"?: str }
    """
    import re as _re
    from datetime import datetime, timezone as _tz

    def _format_field_value(value: str, field_type: str, option_map: dict) -> str:
        if not value:
            return value
        if field_type == "date":
            m = _re.match(r"^(\d{4})-(\d{2})-(\d{2})", value)
            if m:
                return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
        if field_type in ("datetime", "timestamp"):
            try:
                from datetime import datetime as _dt
                d = _dt.fromisoformat(value.replace("Z", "+00:00"))
                return d.strftime("%d/%m/%Y %H:%M")
            except Exception:
                pass
        if field_type in ("select", "radio") and option_map:
            return option_map.get(value, value)
        if field_type in ("multiselect", "checkbox") and option_map:
            parts = [v.strip() for v in value.split(",")]
            return "<br>".join(option_map.get(p, p) for p in parts)
        return value

    try:
        # 1. Get registration + submission id
        reg_resp = await directus_get(
            f"/items/registrations/{registration_id}"
            "?fields[]=id,event_id,submissions"
        )
        reg = reg_resp.get("data") or {}
        if not reg:
            return {"email": "", "status": "failed", "error": f"Registration {registration_id} not found"}

        event_id = str(reg.get("event_id") or "")
        sub_id = reg.get("submissions")
        if isinstance(sub_id, dict):
            sub_id = sub_id.get("id", "")

        # 2. Get form config (template_email, subject, sender_name)
        form_resp = await directus_get(
            f"/items/forms"
            f"?filter[event_id][_eq]={event_id}"
            f"&filter[is_registration][_eq]=true"
            f"&filter[status][_eq]=published"
            f"&fields[]=id,template_email,email_subject,email_sender_name"
            f"&limit=1"
        )
        forms = form_resp.get("data") or []
        if not forms:
            return {"email": "", "status": "failed", "error": "No published registration form found"}
        form = forms[0]
        form_id = form.get("id")
        html_template: str = form.get("template_email") or ""
        email_subject: str = form.get("email_subject") or "Registration Confirmation"
        sender_name: str = form.get("email_sender_name") or "Nexpo"

        # 3. Get event name for subject substitution
        try:
            ev_resp = await directus_get(f"/items/events/{event_id}?fields[]=name")
            event_name = (ev_resp.get("data") or {}).get("name") or ""
            email_subject = email_subject.replace("{event_name}", event_name)
        except Exception:
            event_name = ""

        from_email = f"{sender_name} <no-reply@m.nexpo.vn>"

        # 4. Get form fields (for type + option maps)
        fields_resp = await directus_get(
            f"/items/form_fields"
            f"?filter[form_id][_eq]={form_id}"
            f"&fields[]=id,name,type,is_email_contact,translations.languages_code,translations.options"
            f"&limit=-1"
        )
        form_fields = fields_resp.get("data") or []
        email_field_id: str | None = None
        field_type_map: dict[str, str] = {}
        field_option_map: dict[str, dict] = {}

        for f in form_fields:
            fid = f.get("id", "")
            ftype = f.get("type", "")
            field_type_map[fid] = ftype
            if f.get("is_email_contact"):
                email_field_id = fid
            if ftype in ("select", "multiselect", "radio", "checkbox"):
                translations = f.get("translations") or []
                preferred = (
                    next((t for t in translations if t.get("languages_code") == "vi-VN"), None)
                    or next((t for t in translations if t.get("languages_code") == "en-US"), None)
                    or (translations[0] if translations else {})
                )
                opts = (preferred or {}).get("options") or []
                field_option_map[fid] = {o["value"]: o["label"] for o in opts if "value" in o and "label" in o}

        if not email_field_id:
            return {"email": "", "status": "failed", "error": "No email contact field (is_email_contact=true) on form"}

        # 5. Get form answers
        recipient_email = ""
        answers: list[dict] = []
        if sub_id:
            ans_resp = await directus_get(
                f"/items/form_answers"
                f"?filter[submission][_eq]={sub_id}"
                f"&fields[]=field,value"
                f"&limit=-1"
            )
            answers = ans_resp.get("data") or []
            for ans in answers:
                if ans.get("field") == email_field_id and ans.get("value", "").strip():
                    recipient_email = ans["value"].strip()
                    break

        # Fallback: registration.email
        if not recipient_email:
            reg_email_resp = await directus_get(
                f"/items/registrations/{registration_id}?fields[]=email"
            )
            recipient_email = (reg_email_resp.get("data") or {}).get("email") or ""

        if not recipient_email:
            await _log_reg_activity(registration_id, "failed", "", email_subject, triggered_by, "No email address found")
            return {"email": "", "status": "failed", "error": "No email address found"}

        # 6. Render HTML template (substitute ${field_id} placeholders)
        html = html_template
        for ans in answers:
            fid = ans.get("field", "")
            val = _format_field_value(
                ans.get("value") or "",
                field_type_map.get(fid, ""),
                field_option_map.get(fid, {}),
            )
            html = html.replace(f"${{{fid}}}", val)
        # Clear any remaining unfilled placeholders
        html = _re.sub(r"\$\{[0-9a-f\-]{36}\}", "", html)

        # 7. Send via /send-email-with-qr (internal call to email router)
        from app.services.mailgun import send_mailgun
        from app.services.qr_service import generate_qr_code_bytes, append_qr_cid_to_html, inject_qr_extras

        qr_bytes = generate_qr_code_bytes(registration_id)
        html_with_qr = append_qr_cid_to_html(html)
        html_with_qr = inject_qr_extras(html_with_qr, registration_id)

        import httpx
        from app.config import MAILGUN_API_KEY, MAILGUN_DOMAIN, MAILGUN_API_URL
        email_sent = False
        error_msg: str | None = None
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                mg_resp = await client.post(
                    f"{MAILGUN_API_URL}/v3/{MAILGUN_DOMAIN}/messages",
                    auth=("api", MAILGUN_API_KEY),
                    data={"from": from_email, "to": recipient_email, "subject": email_subject, "html": html_with_qr},
                    files=[("inline", ("qrcode.png", qr_bytes, "image/png"))],
                )
                if mg_resp.is_success:
                    email_sent = True
                else:
                    error_msg = f"Mailgun {mg_resp.status_code}: {mg_resp.text[:200]}"
        except Exception as e:
            error_msg = str(e)[:200]

        # 8. Log activity
        status = "success" if email_sent else "failed"
        await _log_reg_activity(registration_id, status, recipient_email, email_subject, triggered_by, error_msg)

        return {
            "email": recipient_email,
            "status": "sent" if email_sent else "failed",
            **({"error": error_msg} if error_msg else {}),
        }

    except Exception as e:
        try:
            await _log_reg_activity(registration_id, "failed", "", "", triggered_by, str(e)[:200])
        except Exception:
            pass
        return {"email": "", "status": "failed", "error": str(e)[:200]}


async def _log_reg_activity(
    registration_id: str,
    status: str,
    recipient: str,
    subject: str,
    triggered_by: str,
    error_message: str | None = None,
) -> None:
    """Log a registration email activity to Directus. Silent — never raises."""
    from datetime import datetime, timezone as _tz
    try:
        payload: dict = {
            "registration_id": registration_id,
            "channel": "email",
            "action": "qr_email",
            "status": status,
            "recipient": recipient,
            "subject": subject,
            "triggered_by": triggered_by,
            "date_created": datetime.now(_tz.utc).isoformat(),
        }
        if error_message:
            payload["error_message"] = error_message
        await directus_post("/items/registration_activities", payload)
    except Exception:
        pass


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


# ── Candidate Interview Schedule (Consolidated) ───────────────────────────────

async def handle_candidate_interview_schedule(
    registration_id: str,
    event_id: str,
    triggered_by: str = "admin",
) -> dict:
    """
    Send a consolidated interview schedule email to a candidate.

    - Fetches all confirmed meetings for the registration_id
    - Groups them by date/time
    - Sends ONE email with full schedule
    - Logs activity to each meeting's notification_log

    Returns: { "email": str, "status": "sent"|"failed", "meetings_count": int, "error"?: str }
    """
    from datetime import datetime, timezone as _tz, timedelta

    VN_TZ = _tz(timedelta(hours=7))

    try:
        # 1. Get candidate info from registration
        reg_resp = await directus_get(
            f"/items/registrations/{registration_id}"
            "?fields[]=id,full_name,email,event_id"
        )
        reg = reg_resp.get("data") or {}
        if not reg:
            return {"email": "", "status": "failed", "meetings_count": 0, "error": f"Registration {registration_id} not found"}

        candidate_name = reg.get("full_name") or "Ứng viên"
        candidate_email = reg.get("email") or ""
        reg_event_id = str(reg.get("event_id") or event_id)

        if not candidate_email:
            return {"email": "", "status": "failed", "meetings_count": 0, "error": "No email address found"}

        # 2. Get all confirmed meetings for this candidate
        meetings_resp = await directus_get(
            f"/items/meetings"
            f"?filter[registration_id][_eq]={registration_id}"
            f"&filter[event_id][_eq]={reg_event_id}"
            f"&filter[status][_eq]=confirmed"
            f"&fields[]=id,scheduled_at,location,duration_minutes,exhibitor_id,job_requirement_id.job_title"
            f"&sort[]=scheduled_at"
            f"&limit=50"
        )
        meetings = meetings_resp.get("data") or []

        if not meetings:
            return {"email": candidate_email, "status": "failed", "meetings_count": 0, "error": "No confirmed meetings found"}

        # 3. Get event info
        event_resp = await directus_get(f"/items/events/{reg_event_id}?fields[]=name,start_date,end_date,location")
        event = event_resp.get("data") or {}
        event_name = event.get("name") or "Sự kiện"
        event_location = event.get("location") or ""

        # 4. Enrich meetings with exhibitor info
        exhibitor_ids = list(set(str(m.get("exhibitor_id")) for m in meetings if m.get("exhibitor_id")))
        exhibitor_map: dict[str, str] = {}

        if exhibitor_ids:
            for ex_id in exhibitor_ids:
                try:
                    # Fetch company_name from exhibitor translations (vi-VN preferred)
                    ex_resp = await directus_get(
                        f"/items/exhibitor_events"
                        f"?filter[exhibitor_id][_eq]={ex_id}"
                        f"&filter[event_id][_eq]={reg_event_id}"
                        f"&fields[]=exhibitor_id.translations.languages_code,exhibitor_id.translations.company_name,booth_number"
                        f"&limit=1"
                    )
                    ex_data = (ex_resp.get("data") or [{}])[0]

                    # Extract company_name from translations (prefer vi-VN)
                    translations = (ex_data.get("exhibitor_id") or {}).get("translations") or []
                    company = ""
                    for t in translations:
                        if t.get("languages_code") == "vi-VN":
                            company = t.get("company_name") or ""
                            break
                    if not company and translations:
                        company = translations[0].get("company_name") or ""

                    booth = ex_data.get("booth_number") or ""
                    exhibitor_map[ex_id] = f"{company}" + (f" (Booth {booth})" if booth else "")
                except Exception:
                    pass

        # 5. Build schedule rows + prepare data for combined ICS
        schedule_rows = []
        ics_events = []  # For combined ICS file
        for m in meetings:
            scheduled_at = m.get("scheduled_at")
            time_str = ""
            dt_parsed = None
            if scheduled_at:
                try:
                    dt_parsed = datetime.fromisoformat(scheduled_at.replace("Z", ""))
                    if dt_parsed.tzinfo is None:
                        dt_parsed = dt_parsed.replace(tzinfo=VN_TZ)
                    time_str = dt_parsed.strftime("%d/%m/%Y %H:%M")
                except Exception:
                    time_str = scheduled_at

            ex_id = str(m.get("exhibitor_id") or "")
            company = exhibitor_map.get(ex_id, "Nhà tuyển dụng")
            job_title = (m.get("job_requirement_id") or {}).get("job_title") or ""
            location = m.get("location") or ""
            duration = m.get("duration_minutes") or 30

            schedule_rows.append({
                "time": time_str,
                "company": company,
                "job_title": job_title,
                "location": location,
                "duration": duration,
                "meeting_id": m.get("id"),
            })

            # Collect event data for combined ICS
            if dt_parsed:
                ics_events.append({
                    "meeting_id": str(m.get("id") or ""),
                    "summary": f"Phỏng vấn: {company}" + (f" - {job_title}" if job_title else ""),
                    "description": f"Vị trí: {job_title}\nCông ty: {company}\nĐịa điểm: {location or event_location or 'TBA'}",
                    "dtstart": dt_parsed,
                    "duration_minutes": duration,
                    "location": location or event_location or "",
                })

        # 6. Render email HTML
        rows_html = ""
        for row in schedule_rows:
            rows_html += f"""
            <tr style="border-bottom: 1px solid #eee;">
                <td style="padding: 12px; font-weight: 600; white-space: nowrap;">{row['time']}</td>
                <td style="padding: 12px;">
                    <strong>{row['company']}</strong>
                    {f"<br><span style='color: #666; font-size: 13px;'>{row['job_title']}</span>" if row['job_title'] else ""}
                </td>
                <td style="padding: 12px; color: #666;">{row['location'] or '—'}</td>
                <td style="padding: 12px; text-align: center;">{row['duration']} phút</td>
            </tr>
            """

        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
        </head>
        <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px;">
            <div style="background: linear-gradient(135deg, #4F80FF 0%, #3B5998 100%); padding: 30px; border-radius: 12px 12px 0 0; text-align: center;">
                <h1 style="color: white; margin: 0; font-size: 24px;">📅 Lịch Phỏng Vấn</h1>
                <p style="color: rgba(255,255,255,0.9); margin: 10px 0 0 0;">{event_name}</p>
            </div>

            <div style="background: #fff; padding: 30px; border: 1px solid #e0e0e0; border-top: none; border-radius: 0 0 12px 12px;">
                <p>Xin chào <strong>{candidate_name}</strong>,</p>

                <p>Dưới đây là lịch phỏng vấn của bạn tại <strong>{event_name}</strong>{f" ({event_location})" if event_location else ""}:</p>

                <table style="width: 100%; border-collapse: collapse; margin: 20px 0; font-size: 14px;">
                    <thead>
                        <tr style="background: #f8f9fa; border-bottom: 2px solid #dee2e6;">
                            <th style="padding: 12px; text-align: left;">Thời gian</th>
                            <th style="padding: 12px; text-align: left;">Công ty / Vị trí</th>
                            <th style="padding: 12px; text-align: left;">Địa điểm</th>
                            <th style="padding: 12px; text-align: center;">Thời lượng</th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows_html}
                    </tbody>
                </table>

                <div style="background: #e8f4fd; border-left: 4px solid #4F80FF; padding: 15px; margin: 20px 0; border-radius: 0 8px 8px 0;">
                    <strong>⏰ Lưu ý:</strong>
                    <p style="margin: 10px 0 0 0; line-height: 1.7;">
                        Bạn vui lòng có mặt trước giờ hẹn ít nhất 15 phút để làm thủ tục check-in và chuẩn bị tâm thế tốt nhất. Đừng quên mang theo bản cứng CV (nhiều hơn 2 bản) để thuận tiện cho việc trao đổi với nhà tuyển dụng.
                    </p>
                    <p style="margin: 15px 0 0 0; line-height: 1.7;">
                        📍 <strong>Địa điểm:</strong> Trung tâm Hội nghị & Triển lãm Bình Dương – B11, Đường Hùng Vương, phường Hòa Phú, TP. Thủ Dầu Một, Bình Dương.<br>
                        <a href="https://maps.app.goo.gl/faypC6XX17SH5Pi78?g_st=iz" style="color: #4F80FF;">Xem Google Maps</a>
                    </p>
                    <p style="margin: 15px 0 0 0; line-height: 1.7;">
                        Nếu cần hỗ trợ thêm thông tin, bạn vui lòng liên hệ:<br>
                        📞 <strong>Hotline:</strong> (+84) 938.414.437 (Ms. Thủy Bồ)
                    </p>
                    <p style="margin: 15px 0 0 0;">
                        🔗 Xem thêm các vị trí đang tuyển dụng tại chương trình qua link: <a href="https://drive.google.com/drive/folders/1ApoRcvR1FQQBqqdW-jkU4jT_0I4IDKP3" style="color: #4F80FF; font-weight: 600;">Job Fair 2026</a>
                    </p>
                    <p style="margin: 15px 0 0 0; font-size: 13px; color: #666;">
                        📅 <strong>File lịch phỏng vấn (.ics)</strong> đã được đính kèm email này — mở file để thêm tất cả lịch hẹn vào ứng dụng lịch của bạn.
                    </p>
                </div>

                <p style="margin-top: 25px; font-size: 15px;">
                    Chúc bạn có một buổi phỏng vấn thành công rực rỡ! 🎉
                </p>

                <p style="color: #333; font-size: 14px; margin-top: 20px;">
                    Trân trọng,<br>
                    <strong>Ban Tổ chức {event_name}</strong>
                </p>
            </div>

            <p style="text-align: center; color: #999; font-size: 12px; margin-top: 20px;">
                Email được gửi tự động từ hệ thống Nexpo
            </p>
        </body>
        </html>
        """

        subject = f"[{event_name}] Lịch phỏng vấn của bạn ({len(meetings)} cuộc hẹn)"

        # 7. Generate combined ICS file for all meetings
        ics_attachment = None
        if ics_events:
            ics_bytes = generate_combined_ics(
                events=ics_events,
                organizer_email=f"noreply@{MAILGUN_DOMAIN}",
                organizer_name="Nexpo",
            )
            # Mailgun attachment format: [("attachment", (filename, content, mimetype))]
            ics_attachment = [("attachment", ("lich-phong-van.ics", ics_bytes, "text/calendar"))]

        # 8. Send email with ICS attachment
        from app.services.mailgun import send_mailgun
        email_sent = await send_mailgun(
            to=candidate_email,
            subject=subject,
            html=html,
            from_email=f"Nexpo <noreply@{MAILGUN_DOMAIN}>",
            attachments=ics_attachment,
        )

        # 9. Log activity to each meeting's notification_log
        log_entries = [{
            "timestamp": datetime.now(_tz.utc).isoformat(),
            "trigger": "schedule_summary",
            "channel": "email",
            "recipient_type": "visitor",
            "recipient": candidate_email,
            "status": "sent" if email_sent else "failed",
            "subject": subject,
        }]

        for row in schedule_rows:
            try:
                await append_meeting_notification_log(row["meeting_id"], log_entries)
            except Exception:
                pass  # Never crash over logging

        return {
            "email": candidate_email,
            "status": "sent" if email_sent else "failed",
            "meetings_count": len(meetings),
        }

    except Exception as e:
        return {"email": "", "status": "failed", "meetings_count": 0, "error": str(e)[:200]}
