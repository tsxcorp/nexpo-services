"""
Candidate interview schedule handler — consolidated email with all confirmed meetings + ICS attachment.
"""
from datetime import datetime, timezone as _tz, timedelta
from app.config import MAILGUN_DOMAIN
from app.services.directus import directus_get
from app.services.mailgun import send_mailgun
from app.services.ics_service import generate_combined_ics
from app.services.handlers.notification_helpers import append_meeting_notification_log

VN_TZ = _tz(timedelta(hours=7))


async def handle_candidate_interview_schedule(
    registration_id: str, event_id: str, triggered_by: str = "admin",
) -> dict:
    """
    Send a consolidated interview schedule email to a candidate.
    Fetches all confirmed meetings, groups by date/time, sends ONE email with full schedule + ICS.
    Returns: { "email": str, "status": "sent"|"failed", "meetings_count": int, "error"?: str }
    """
    try:
        # 1. Get candidate info
        reg_resp = await directus_get(f"/items/registrations/{registration_id}?fields[]=id,full_name,email,event_id")
        reg = reg_resp.get("data") or {}
        if not reg:
            return {"email": "", "status": "failed", "meetings_count": 0, "error": f"Registration {registration_id} not found"}

        candidate_name = reg.get("full_name") or "Ứng viên"
        candidate_email = reg.get("email") or ""
        reg_event_id = str(reg.get("event_id") or event_id)

        if not candidate_email:
            return {"email": "", "status": "failed", "meetings_count": 0, "error": "No email address found"}

        # 2. Get all confirmed meetings
        meetings_resp = await directus_get(
            f"/items/meetings?filter[registration_id][_eq]={registration_id}"
            f"&filter[event_id][_eq]={reg_event_id}&filter[status][_eq]=confirmed"
            f"&fields[]=id,scheduled_at,location,duration_minutes,exhibitor_id,job_requirement_id.job_title"
            f"&sort[]=scheduled_at&limit=50"
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
        for ex_id in exhibitor_ids:
            try:
                ex_resp = await directus_get(
                    f"/items/exhibitor_events?filter[exhibitor_id][_eq]={ex_id}"
                    f"&filter[event_id][_eq]={reg_event_id}"
                    f"&fields[]=exhibitor_id.translations.languages_code,exhibitor_id.translations.company_name,booth_number"
                    f"&limit=1"
                )
                ex_data = (ex_resp.get("data") or [{}])[0]
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

        # 5. Build schedule rows + ICS events
        schedule_rows = []
        ics_events = []
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
                "time": time_str, "company": company, "job_title": job_title,
                "location": location, "duration": duration, "meeting_id": m.get("id"),
            })
            if dt_parsed:
                ics_events.append({
                    "meeting_id": str(m.get("id") or ""),
                    "summary": f"Phỏng vấn: {company}" + (f" - {job_title}" if job_title else ""),
                    "description": f"Vị trí: {job_title}\nCông ty: {company}\nĐịa điểm: {location or event_location or 'TBA'}",
                    "dtstart": dt_parsed, "duration_minutes": duration,
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
        <head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
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
                    <tbody>{rows_html}</tbody>
                </table>
                <div style="background: #e8f4fd; border-left: 4px solid #4F80FF; padding: 15px; margin: 20px 0; border-radius: 0 8px 8px 0;">
                    <strong>⏰ Lưu ý:</strong>
                    <p style="margin: 10px 0 0 0; line-height: 1.7;">Bạn vui lòng có mặt trước giờ hẹn ít nhất 15 phút để làm thủ tục check-in và chuẩn bị tâm thế tốt nhất. Đừng quên mang theo bản cứng CV (nhiều hơn 2 bản) để thuận tiện cho việc trao đổi với nhà tuyển dụng.</p>
                    <p style="margin: 15px 0 0 0; line-height: 1.7;">📍 <strong>Địa điểm:</strong> {event_location or 'TBA'}
                    </p>
                    <p style="margin: 15px 0 0 0; font-size: 13px; color: #666;">📅 <strong>File lịch phỏng vấn (.ics)</strong> đã được đính kèm email này — mở file để thêm tất cả lịch hẹn vào ứng dụng lịch của bạn.</p>
                </div>
                <p style="margin-top: 25px; font-size: 15px;">Chúc bạn có một buổi phỏng vấn thành công rực rỡ! 🎉</p>
                <p style="color: #333; font-size: 14px; margin-top: 20px;">Trân trọng,<br><strong>Ban Tổ chức {event_name}</strong></p>
            </div>
            <p style="text-align: center; color: #999; font-size: 12px; margin-top: 20px;">Email được gửi tự động từ hệ thống Nexpo</p>
        </body>
        </html>
        """

        subject = f"[{event_name}] Lịch phỏng vấn của bạn ({len(meetings)} cuộc hẹn)"

        # 7. Generate combined ICS
        ics_attachment = None
        if ics_events:
            ics_bytes = generate_combined_ics(
                events=ics_events,
                organizer_email=f"noreply@{MAILGUN_DOMAIN}",
                organizer_name="Nexpo",
            )
            ics_attachment = [("attachment", ("lich-phong-van.ics", ics_bytes, "text/calendar"))]

        # 8. Send email
        email_sent = await send_mailgun(
            to=candidate_email, subject=subject, html=html,
            from_email=f"Nexpo <noreply@{MAILGUN_DOMAIN}>",
            attachments=ics_attachment,
        )

        # 9. Log activity to each meeting
        log_entries = [{
            "timestamp": datetime.now(_tz.utc).isoformat(),
            "trigger": "schedule_summary", "channel": "email",
            "recipient_type": "visitor", "recipient": candidate_email,
            "status": "sent" if email_sent else "failed", "subject": subject,
        }]
        for row in schedule_rows:
            try:
                await append_meeting_notification_log(row["meeting_id"], log_entries)
            except Exception:
                pass

        return {"email": candidate_email, "status": "sent" if email_sent else "failed", "meetings_count": len(meetings)}

    except Exception as e:
        return {"email": "", "status": "failed", "meetings_count": 0, "error": str(e)[:200]}
