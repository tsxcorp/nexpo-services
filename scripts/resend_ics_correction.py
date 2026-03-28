#!/usr/bin/env python3
"""
resend_ics_correction.py — Gửi lại ICS update với múi giờ đúng (UTC+7).

Bug: ICS đã gửi trước đó dùng naive datetime như UTC → giờ hiển thị sai 7 tiếng.
Fix: Gửi lại METHOD:REQUEST với SEQUENCE tăng lên, DTSTART đúng UTC (naive +07:00 → UTC).

Script này chỉ gửi lại cho các meetings:
  - status IN (confirmed, scheduled)
  - notification_log có entry channel=email trigger=confirmed (không phải backfilled)
  - scheduled_at không null

Gửi đúng 1 email tới visitor với ICS đính kèm, subject rõ ràng là cập nhật.

Usage:
    cd nexpo-services
    python scripts/resend_ics_correction.py [--dry-run]

Options:
    --dry-run   In ra danh sách meetings và emails sẽ được gửi, không gửi thực sự.

Environment:
    DIRECTUS_URL          - Directus API URL (default: https://app.nexpo.vn)
    DIRECTUS_ADMIN_TOKEN  - Admin token
    MAILGUN_API_KEY       - Mailgun API key
    MAILGUN_DOMAIN        - Mailgun domain (vd: mail.nexpo.vn)
    MAILGUN_API_URL       - Mailgun API base URL (default: https://api.mailgun.net)
"""

import asyncio
import os
import sys
import httpx
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

DIRECTUS_URL = os.getenv("DIRECTUS_URL", "https://app.nexpo.vn")
DIRECTUS_ADMIN_TOKEN = os.getenv("DIRECTUS_ADMIN_TOKEN", "")
MAILGUN_API_KEY = os.getenv("MAILGUN_API_KEY", "")
MAILGUN_DOMAIN = os.getenv("MAILGUN_DOMAIN", "")
MAILGUN_API_URL = os.getenv("MAILGUN_API_URL", "https://api.mailgun.net")

DRY_RUN = "--dry-run" in sys.argv

VN_TZ = timezone(timedelta(hours=7))

# ── ICS helpers (copy từ ics_service.py, self-contained) ─────────────────────

def _ics_escape(text: str) -> str:
    text = text.replace("\\", "\\\\")
    text = text.replace(";", "\\;")
    text = text.replace(",", "\\,")
    text = text.replace("\n", "\\n")
    return text


def _fold(line: str) -> str:
    if len(line.encode("utf-8")) <= 75:
        return line
    result = []
    while len(line.encode("utf-8")) > 75:
        split = 75
        while len(line[:split].encode("utf-8")) > 75:
            split -= 1
        result.append(line[:split])
        line = " " + line[split:]
    result.append(line)
    return "\r\n".join(result)


def generate_ics(
    meeting_id: str,
    summary: str,
    description: str,
    dtstart: datetime,          # timezone-aware (VN_TZ)
    duration_minutes: int = 30,
    location: str = "",
    organizer_email: str = "noreply@nexpo.vn",
    organizer_name: str = "Nexpo",
    attendee_emails: list[str] | None = None,
    sequence: int = 10,         # Số cao hơn sequence cũ (1) để override
) -> bytes:
    if attendee_emails is None:
        attendee_emails = []

    dtend = dtstart + timedelta(minutes=duration_minutes)

    def fmt_dt(dt: datetime) -> str:
        utc = dt.astimezone(timezone.utc)
        return utc.strftime("%Y%m%dT%H%M%SZ")

    uid = f"{meeting_id}@nexpo.vn"
    now_str = fmt_dt(datetime.now(timezone.utc))

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Nexpo//Meeting Scheduler//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{now_str}",
        f"DTSTART:{fmt_dt(dtstart)}",
        f"DTEND:{fmt_dt(dtend)}",
        f"SUMMARY:{_ics_escape(summary)}",
        f"DESCRIPTION:{_ics_escape(description)}",
        f"LOCATION:{_ics_escape(location)}",
        f"ORGANIZER;CN={_ics_escape(organizer_name)}:mailto:{organizer_email}",
        "STATUS:CONFIRMED",
        f"SEQUENCE:{sequence}",
    ]
    for email in attendee_emails:
        lines.append(
            f"ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;"
            f"PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{email}"
        )
    lines += ["END:VEVENT", "END:VCALENDAR"]

    folded = "\r\n".join(_fold(line) for line in lines) + "\r\n"
    return folded.encode("utf-8")


# ── Directus helpers ──────────────────────────────────────────────────────────

async def dx_get(client: httpx.AsyncClient, path: str) -> dict:
    resp = await client.get(
        f"{DIRECTUS_URL}{path}",
        headers={"Authorization": f"Bearer {DIRECTUS_ADMIN_TOKEN}"},
    )
    resp.raise_for_status()
    return resp.json()


async def dx_patch(client: httpx.AsyncClient, path: str, data: dict) -> dict:
    resp = await client.patch(
        f"{DIRECTUS_URL}{path}",
        headers={
            "Authorization": f"Bearer {DIRECTUS_ADMIN_TOKEN}",
            "Content-Type": "application/json",
        },
        json=data,
    )
    resp.raise_for_status()
    return resp.json()


async def resolve_visitor_email(client: httpx.AsyncClient, registration_id: str) -> tuple[str, str]:
    """Returns (email, full_name). Mirrors directus.py logic."""
    try:
        reg_resp = await dx_get(
            client,
            f"/items/registrations/{registration_id}"
            "?fields[]=id,full_name,email"
        )
        reg = reg_resp.get("data") or {}
        fallback_email = reg.get("email") or ""
        full_name = reg.get("full_name") or ""

        subs_resp = await dx_get(
            client,
            f"/items/form_submissions"
            f"?filter[registration_id][_eq]={registration_id}"
            "&fields[]=answers.value,answers.field.is_email_contact"
            "&limit=10"
        )
        for sub in subs_resp.get("data") or []:
            for ans in sub.get("answers") or []:
                field = ans.get("field") or {}
                if field.get("is_email_contact") and (ans.get("value") or "").strip():
                    return ans["value"].strip(), full_name
        return fallback_email, full_name
    except Exception:
        return "", ""


async def resolve_exhibitor_company(client: httpx.AsyncClient, exhibitor_id: str, event_id: str) -> str:
    """Returns company name. Mirrors directus.py resolve_exhibitor_email logic."""
    try:
        ee_resp = await dx_get(
            client,
            f"/items/exhibitor_events"
            f"?filter[exhibitor_id][_eq]={exhibitor_id}"
            f"&filter[event_id][_eq]={event_id}"
            "&fields[]=nameboard,exhibitor_id.translations.company_name,"
            "exhibitor_id.translations.languages_code"
            "&limit=1"
        )
        items = ee_resp.get("data") or []
        if items:
            ee = items[0]
            ex = ee.get("exhibitor_id") or {}
            translations = ex.get("translations") or []
            t = next((t for t in translations if t.get("languages_code") == "vi-VN"), None) \
                or (translations[0] if translations else {})
            company_name = t.get("company_name") or ee.get("nameboard") or ""
            if company_name:
                return company_name

        # Fallback: query exhibitors directly
        ex_resp = await dx_get(
            client,
            f"/items/exhibitors/{exhibitor_id}"
            "?fields[]=translations.company_name,translations.languages_code"
        )
        ex = ex_resp.get("data") or {}
        translations = ex.get("translations") or []
        t = next((t for t in translations if t.get("languages_code") == "vi-VN"), None) \
            or (translations[0] if translations else {})
        return t.get("company_name") or ""
    except Exception:
        return ""



# ── Mailgun helper ────────────────────────────────────────────────────────────

async def send_email(
    client: httpx.AsyncClient,
    to: str,
    subject: str,
    html: str,
    ics_bytes: bytes,
) -> bool:
    if not MAILGUN_API_KEY or not MAILGUN_DOMAIN:
        print("  [WARN] MAILGUN_API_KEY hoặc MAILGUN_DOMAIN chưa set!")
        return False
    try:
        resp = await client.post(
            f"{MAILGUN_API_URL}/v3/{MAILGUN_DOMAIN}/messages",
            auth=("api", MAILGUN_API_KEY),
            data={
                "from": f"Nexpo <noreply@{MAILGUN_DOMAIN}>",
                "to": to,
                "subject": subject,
                "html": html,
            },
            files=[("attachment", ("invite.ics", ics_bytes, "text/calendar; method=REQUEST"))],
        )
        return resp.is_success
    except Exception as e:
        print(f"  [ERROR] Mailgun: {e}")
        return False


# ── Email HTML template ───────────────────────────────────────────────────────

def make_html(visitor_name: str, company_name: str, job_title: str, time_str: str, location_str: str) -> str:
    location_block = (
        f'<p style="color:#374151;font-size:14px;"><strong>Địa điểm / Venue:</strong> {location_str}</p>'
        if location_str else ""
    )
    return f"""
<div style="font-family:Inter,sans-serif;max-width:560px;margin:0 auto;padding:32px 24px;background:#fff;">
  <h2 style="font-size:20px;font-weight:700;color:#111827;margin:0 0 16px;">
    📅 Xác nhận lịch phỏng vấn / Interview Schedule Confirmation
  </h2>
  <p style="color:#374151;font-size:14px;">Kính gửi <strong>{visitor_name}</strong>,</p>

  <p style="color:#374151;font-size:14px;">
    Trước tiên, Nexpo xin lỗi vì sự cố kỹ thuật xảy ra trong email trước đó — file đính kèm .ics
    (lịch calendar) có thể hiển thị sai giờ do lỗi múi giờ của hệ thống, gây ra sự nhầm lẫn không
    đáng có. Chúng tôi rất tiếc vì điều này.
  </p>
  <p style="color:#374151;font-size:14px;">
    We sincerely apologize for the confusion caused by a technical issue in our previous email — the
    .ics calendar attachment may have displayed an incorrect time due to a system timezone bug.
  </p>

  <div style="background:#F0FDF4;border:1px solid #BBF7D0;border-radius:8px;padding:16px 20px;margin:20px 0;">
    <p style="color:#166534;font-size:14px;font-weight:600;margin:0 0 4px;">
      ✅ Đây là email xác nhận cuối cùng — thông tin lịch hoàn toàn chính xác.
    </p>
    <p style="color:#166534;font-size:13px;margin:0;">
      This is your final confirmation. The schedule below is 100% correct and has not changed.
    </p>
  </div>

  <hr style="margin:16px 0;border:none;border-top:1px solid #E5E7EB;"/>
  <p style="color:#374151;font-size:14px;"><strong>Công ty / Company:</strong> {company_name}</p>
  <p style="color:#374151;font-size:14px;"><strong>Vị trí / Position:</strong> {job_title}</p>
  <p style="color:#374151;font-size:15px;font-weight:700;color:#111827;">
    🕐 Thời gian / When: {time_str} (GMT+7, giờ Việt Nam)
  </p>
  {location_block}

  <p style="color:#374151;font-size:14px;margin-top:16px;">
    Vui lòng <strong>chấp nhận file .ics đính kèm</strong> để cập nhật đúng lịch Google Calendar / Apple Calendar.
    File này sẽ <strong>thay thế</strong> lịch cũ bị sai giờ trước đó.
  </p>
  <p style="color:#374151;font-size:14px;">
    Please <strong>accept the attached .ics file</strong> to update your calendar with the correct time.
    This will <strong>replace</strong> the previous incorrect calendar event.
  </p>

  <hr style="margin:32px 0;border:none;border-top:1px solid #E5E7EB;"/>
  <p style="font-size:12px;color:#9CA3AF;">
    Đây là email tự động từ hệ thống Nexpo. Nếu có thắc mắc, vui lòng liên hệ ban tổ chức.<br/>
    This is an automated message from Nexpo Platform. Please contact the event organizer if you have any questions.
  </p>
</div>"""


# ── Main ──────────────────────────────────────────────────────────────────────

async def fetch_affected_meetings(client: httpx.AsyncClient) -> list[dict]:
    """
    Lấy tất cả meetings có email đã gửi (trigger=confirmed, channel=email, không backfilled).
    """
    all_meetings = []
    offset = 0
    limit = 100

    while True:
        resp = await dx_get(
            client,
            f"/items/meetings"
            f"?filter[status][_in]=confirmed,scheduled"
            f"&filter[scheduled_at][_nnull]=true"
            f"&fields[]=id,status,scheduled_at,event_id,registration_id,exhibitor_id,"
            f"job_requirement_id.job_title,location,duration_minutes,meeting_category,notification_log"
            f"&limit={limit}&offset={offset}"
        )
        items = resp.get("data") or []
        if not items:
            break
        all_meetings.extend(items)
        offset += limit

    # Lọc chỉ những meeting có email đã gửi (confirmed trigger, channel=email, không phải backfilled)
    affected = []
    for m in all_meetings:
        log = m.get("notification_log") or []
        has_email_sent = any(
            entry.get("channel") == "email"
            and entry.get("trigger") == "confirmed"
            and not entry.get("backfilled")
            for entry in log
            if isinstance(entry, dict)
        )
        if has_email_sent:
            # Lấy email visitor từ log
            visitor_emails = [
                entry.get("recipient")
                for entry in log
                if isinstance(entry, dict)
                and entry.get("channel") == "email"
                and entry.get("recipient_type") == "visitor"
                and entry.get("trigger") == "confirmed"
                and not entry.get("backfilled")
                and entry.get("recipient")
            ]
            m["_visitor_emails_from_log"] = list(set(visitor_emails))
            affected.append(m)

    return affected


async def main():
    print("=" * 65)
    print("ICS Timezone Correction — Resend ICS Update")
    print("=" * 65)
    print(f"Directus: {DIRECTUS_URL}")
    print(f"Dry-run:  {DRY_RUN}")
    print()

    if not DIRECTUS_ADMIN_TOKEN:
        print("[ERROR] DIRECTUS_ADMIN_TOKEN chưa set!")
        sys.exit(1)

    async with httpx.AsyncClient(timeout=60) as client:
        # 1. Lấy danh sách meetings bị ảnh hưởng
        print("[1/3] Đang tìm meetings đã gửi email có .ics sai giờ...")
        meetings = await fetch_affected_meetings(client)
        print(f"  → Tìm thấy {len(meetings)} meetings cần gửi lại ICS\n")

        if not meetings:
            print("Không có meetings nào cần xử lý. Done!")
            return

        # In danh sách để review
        print(f"{'ID':<40} {'scheduled_at':<20} {'Visitor emails'}")
        print("-" * 90)
        for m in meetings:
            emails = ", ".join(m.get("_visitor_emails_from_log") or [])
            print(f"{m['id']:<40} {m.get('scheduled_at', ''):<20} {emails}")
        print()

        if DRY_RUN:
            print("[DRY-RUN] Không gửi email thực sự. Chạy lại không có --dry-run để gửi.")
            return

        confirm = input(f"Tiếp tục gửi ICS correction cho {len(meetings)} meetings? [y/N] ").strip().lower()
        if confirm != "y":
            print("Đã hủy.")
            return

        # 2. Gửi lại ICS
        print("\n[2/3] Đang gửi ICS correction emails...")
        success_ids = []
        fail_ids = []

        for i, m in enumerate(meetings, 1):
            meeting_id = m["id"]
            scheduled_at_raw = m.get("scheduled_at") or ""
            registration_id = m.get("registration_id") or ""
            exhibitor_id = m.get("exhibitor_id") or ""
            event_id = str(m.get("event_id") or "")
            job_title = (m.get("job_requirement_id") or {}).get("job_title") or "vị trí phỏng vấn"
            location_str = m.get("location") or ""
            duration_minutes = int(m.get("duration_minutes") or 30)

            # Parse scheduled_at như giờ Vietnam (UTC+7)
            try:
                dt_naive = datetime.fromisoformat(scheduled_at_raw.replace("Z", ""))
                if dt_naive.tzinfo is None:
                    dt_vn = dt_naive.replace(tzinfo=VN_TZ)
                else:
                    dt_vn = dt_naive.astimezone(VN_TZ)
            except Exception:
                print(f"  [{i}] {meeting_id}: [SKIP] Không parse được scheduled_at: {scheduled_at_raw}")
                fail_ids.append(meeting_id)
                continue

            time_str = dt_vn.strftime("%d/%m/%Y %H:%M")

            # Resolve visitor info
            visitor_emails_from_log = m.get("_visitor_emails_from_log") or []
            visitor_email_resolved, visitor_name = await resolve_visitor_email(client, registration_id)

            # Ưu tiên email từ log (đã gửi trước đó), fallback sang resolve
            send_to_emails = visitor_emails_from_log if visitor_emails_from_log else (
                [visitor_email_resolved] if visitor_email_resolved else []
            )

            if not send_to_emails:
                print(f"  [{i}] {meeting_id}: [SKIP] Không tìm được email visitor")
                fail_ids.append(meeting_id)
                continue

            company_name = await resolve_exhibitor_company(client, exhibitor_id, event_id)

            # Tạo ICS với SEQUENCE=10 (cao hơn sequence cũ 1) để calendar apps update event
            summary = f"Phỏng vấn: {visitor_name} — {company_name}"
            description = f"Vị trí: {job_title}\nThời gian: {time_str}\nĐịa điểm: {location_str}"
            ics_bytes = generate_ics(
                meeting_id=meeting_id,
                summary=summary,
                description=description,
                dtstart=dt_vn,
                duration_minutes=duration_minutes,
                location=location_str,
                organizer_email=f"noreply@{MAILGUN_DOMAIN}",
                organizer_name="Nexpo",
                attendee_emails=send_to_emails,
                sequence=10,    # Cao hơn sequence=1 cũ → calendar apps sẽ update
            )

            subject = f"[Xác nhận lịch] Phỏng vấn {time_str} — {company_name}"
            html = make_html(visitor_name, company_name, job_title, time_str, location_str)

            # Gửi tới tất cả email visitor đã nhận trước đó
            sent_any = False
            for email in send_to_emails:
                ok = await send_email(client, email, subject, html, ics_bytes)
                if ok:
                    print(f"  [{i}/{len(meetings)}] ✓ {meeting_id} → {email} ({time_str})")
                    sent_any = True
                else:
                    print(f"  [{i}/{len(meetings)}] ✗ {meeting_id} → {email} (FAILED)")

            if sent_any:
                success_ids.append(meeting_id)
                # Ghi log vào notification_log
                try:
                    resp = await dx_get(client, f"/items/meetings/{meeting_id}?fields[]=notification_log")
                    existing_log = (resp.get("data") or {}).get("notification_log") or []
                    if not isinstance(existing_log, list):
                        existing_log = []
                    existing_log.append({
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "trigger": "ics_correction",
                        "channel": "email",
                        "recipient_type": "visitor",
                        "recipient": ", ".join(send_to_emails),
                        "status": "sent",
                        "subject": subject,
                        "note": "ICS timezone correction resend (SEQUENCE=10)",
                    })
                    await dx_patch(client, f"/items/meetings/{meeting_id}", {"notification_log": existing_log})
                except Exception as log_err:
                    print(f"  [WARN] Không ghi log cho {meeting_id}: {log_err}")
            else:
                fail_ids.append(meeting_id)

            # Throttle để tránh rate limit Mailgun
            await asyncio.sleep(0.3)

        # 3. Summary
        print(f"\n[3/3] Kết quả:")
        print("=" * 65)
        print(f"  ✓ Thành công: {len(success_ids)}")
        print(f"  ✗ Thất bại:   {len(fail_ids)}")
        if fail_ids:
            print(f"\n  Meetings thất bại:")
            for mid in fail_ids:
                print(f"    - {mid}")


if __name__ == "__main__":
    asyncio.run(main())
