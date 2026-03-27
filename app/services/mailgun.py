"""Mailgun email delivery helpers."""
import httpx
from typing import Optional
from app.config import MAILGUN_API_KEY, MAILGUN_DOMAIN, MAILGUN_API_URL, ADMIN_URL

NEXPO_LOGO_URL = f"{ADMIN_URL}/nexpo-logo-light.png"


async def send_mailgun(
    to: str,
    subject: str,
    html: str,
    from_email: Optional[str] = None,
    sender_name: str = "Nexpo",
    inline_files: Optional[list] = None,
    attachments: Optional[list] = None,
) -> bool:
    """
    Send an email via Mailgun.
    Returns True on success, False on failure.
    inline_files: list of ("inline", (filename, bytes, content_type)) tuples
    attachments:  list of ("attachment", (filename, bytes, content_type)) tuples
                  e.g. [("attachment", ("invite.ics", ics_bytes, "text/calendar"))]
    """
    if not MAILGUN_API_KEY or not MAILGUN_DOMAIN:
        return False

    from_addr = from_email or f"{sender_name} <noreply@{MAILGUN_DOMAIN}>"
    data = {"from": from_addr, "to": to, "subject": subject, "html": html}

    files = list(inline_files or []) + list(attachments or [])

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{MAILGUN_API_URL}/v3/{MAILGUN_DOMAIN}/messages",
                auth=("api", MAILGUN_API_KEY),
                data=data,
                files=files,
            )
            return resp.is_success
    except Exception:
        return False



def meeting_notification_html(
    title: str,
    body_lines: list[str],
    cta_label: str = "",
    cta_url: str = "",
) -> str:
    """Render a simple branded HTML email for meeting notifications."""
    body_html = "".join(
        f"<p style='margin:8px 0;color:#374151;font-size:14px;'>{line}</p>"
        for line in body_lines
    )
    cta_html = (
        f"<div style='margin-top:24px;'>"
        f"<a href='{cta_url}' style='display:inline-block;padding:10px 20px;background:#4F80FF;color:#fff;"
        f"border-radius:8px;text-decoration:none;font-size:14px;font-weight:600;'>{cta_label}</a></div>"
        if cta_label and cta_url else ""
    )
    return f"""
<div style="font-family:Inter,sans-serif;max-width:560px;margin:0 auto;padding:32px 24px;background:#fff;">
  <div style="margin-bottom:24px;">
    <img src="{NEXPO_LOGO_URL}" alt="Nexpo" style="height:32px;" onerror="this.style.display='none'"/>
  </div>
  <h2 style="font-size:20px;font-weight:700;color:#111827;margin:0 0 16px;">{title}</h2>
  {body_html}
  {cta_html}
  <hr style="margin:32px 0;border:none;border-top:1px solid #E5E7EB;"/>
  <p style="font-size:12px;color:#9CA3AF;">This is an automated notification from Nexpo Platform.</p>
</div>"""
