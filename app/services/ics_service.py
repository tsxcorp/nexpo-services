"""
ics_service.py — Generate iCalendar (.ics) attachments for meeting emails.

METHOD:REQUEST  → new invitation or update
METHOD:CANCEL   → meeting cancelled

Supports both single meeting ICS and combined multi-event ICS for interview schedules.
"""

from datetime import datetime, timezone, timedelta
import re


def _ics_escape(text: str) -> str:
    """Escape special characters in ICS text fields."""
    text = text.replace("\\", "\\\\")
    text = text.replace(";", "\\;")
    text = text.replace(",", "\\,")
    text = text.replace("\n", "\\n")
    return text


def _fold(line: str) -> str:
    """Fold long lines per RFC 5545 (max 75 octets per line)."""
    if len(line.encode("utf-8")) <= 75:
        return line
    result = []
    while len(line.encode("utf-8")) > 75:
        # Find safe split point (don't split multi-byte chars)
        split = 75
        while len(line[:split].encode("utf-8")) > 75:
            split -= 1
        result.append(line[:split])
        line = " " + line[split:]
    result.append(line)
    return "\r\n".join(result)


def generate_meeting_ics(
    meeting_id: str,
    method: str,           # "REQUEST" | "CANCEL"
    summary: str,          # Event title shown in calendar
    description: str,      # Plain-text body shown in calendar details
    dtstart: datetime,     # UTC datetime
    duration_minutes: int = 30,
    location: str = "",
    organizer_email: str = "noreply@nexpo.vn",
    organizer_name: str = "Nexpo",
    attendee_emails: list[str] | None = None,
    sequence: int = 0,     # Increment when updating existing event
) -> bytes:
    """
    Generate a standards-compliant iCalendar file.
    Returns UTF-8 bytes ready to attach to an email.
    """
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
        f"METHOD:{method}",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{now_str}",
        f"DTSTART:{fmt_dt(dtstart)}",
        f"DTEND:{fmt_dt(dtend)}",
        f"SUMMARY:{_ics_escape(summary)}",
        f"DESCRIPTION:{_ics_escape(description)}",
        f"LOCATION:{_ics_escape(location)}",
        f"ORGANIZER;CN={_ics_escape(organizer_name)}:mailto:{organizer_email}",
        f"STATUS:{'CONFIRMED' if method == 'REQUEST' else 'CANCELLED'}",
        f"SEQUENCE:{sequence}",
    ]

    for email in attendee_emails:
        lines.append(
            f"ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;"
            f"PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{email}"
        )

    lines += [
        "END:VEVENT",
        "END:VCALENDAR",
    ]

    # Fold & join with CRLF
    folded = "\r\n".join(_fold(line) for line in lines) + "\r\n"
    return folded.encode("utf-8")


# ── Combined ICS for Multiple Events ───────────────────────────────────────────

def generate_combined_ics(
    events: list[dict],
    organizer_email: str = "noreply@nexpo.vn",
    organizer_name: str = "Nexpo",
) -> bytes:
    """
    Generate a single iCalendar file containing multiple events.

    Each event dict should have:
        - meeting_id: str (used as UID)
        - summary: str (event title)
        - description: str (event details)
        - dtstart: datetime (start time, timezone-aware)
        - duration_minutes: int (default 30)
        - location: str (optional)

    Returns UTF-8 bytes ready to attach to an email.
    """
    def fmt_dt(dt: datetime) -> str:
        utc = dt.astimezone(timezone.utc)
        return utc.strftime("%Y%m%dT%H%M%SZ")

    now_str = fmt_dt(datetime.now(timezone.utc))

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Nexpo//Interview Scheduler//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:Lịch Phỏng Vấn",
    ]

    for event in events:
        meeting_id = event.get("meeting_id") or ""
        summary = event.get("summary") or "Phỏng vấn"
        description = event.get("description") or ""
        dtstart = event.get("dtstart")
        duration_minutes = event.get("duration_minutes") or 30
        location = event.get("location") or ""

        if not dtstart:
            continue  # Skip events without start time

        dtend = dtstart + timedelta(minutes=duration_minutes)
        uid = f"{meeting_id}@nexpo.vn" if meeting_id else f"{now_str}-{id(event)}@nexpo.vn"

        lines += [
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
            "SEQUENCE:0",
            "BEGIN:VALARM",
            "TRIGGER:-PT15M",
            "ACTION:DISPLAY",
            f"DESCRIPTION:Reminder: {_ics_escape(summary)}",
            "END:VALARM",
            "END:VEVENT",
        ]

    lines.append("END:VCALENDAR")

    # Fold & join with CRLF
    folded = "\r\n".join(_fold(line) for line in lines) + "\r\n"
    return folded.encode("utf-8")
