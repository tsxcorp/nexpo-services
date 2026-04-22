"""Mailgun email delivery helpers."""
import httpx
from typing import Optional
from app.config import MAILGUN_API_KEY, MAILGUN_DOMAIN, MAILGUN_API_URL, ADMIN_URL

NEXPO_LOGO_URL = f"{ADMIN_URL}/nexpo-logo-light.png"


def _email_shell(inner_html: str, email_style: Optional[dict] = None) -> str:
    """Shared branded email shell — header (logo | event name), body, footer."""
    s = email_style or {}
    header_start = s.get("header_color_start", "#1a1a2e")
    header_end = s.get("header_color_end", "#0f3460")
    logo_url = s.get("logo_url", "") or NEXPO_LOGO_URL
    event_label = s.get("event_label", "")
    footer_text = s.get("footer_text", "This is an automated notification from Nexpo Platform.")

    # Header: Logo (left) | Event Name (right), vertically centered
    event_name_td = (
        f'<td style="padding-left:16px;vertical-align:middle;">'
        f'<span style="font-size:14px;font-weight:600;color:#ffffff;letter-spacing:0.3px;">{event_label}</span>'
        f'</td>'
        if event_label else ""
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#f4f5f7;font-family:'Be Vietnam Pro',Inter,-apple-system,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f5f7;padding:24px 0;">
<tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.06);">
  <tr><td style="background:linear-gradient(135deg,{header_start},{header_end});padding:24px 32px;">
    <table cellpadding="0" cellspacing="0"><tr>
      <td style="vertical-align:middle;">
        <img src="{logo_url}" alt="" style="height:40px;display:block;" onerror="this.style.display='none'"/>
      </td>
      {event_name_td}
    </tr></table>
  </td></tr>
  {inner_html}
  <tr><td style="padding:20px 32px 24px;border-top:1px solid #f0f0f0;">
    <p style="margin:0;font-size:11px;color:#9CA3AF;line-height:1.5;">{footer_text}</p>
  </td></tr>
</table>
</td></tr>
</table>
</body></html>"""


_BODY_INNER_RE = __import__('re').compile(r'<body\b[^>]*>([\s\S]*?)</body>', __import__('re').I)


def _is_full_html_document(html: str) -> bool:
    """Detect whether the input is already a complete HTML document (MJML output)
    or a fragment (legacy TipTap body HTML)."""
    if not html:
        return False
    head = html.lstrip()[:200].lower()
    return head.startswith("<!doctype") or head.startswith("<html") or "<mjml" in head


def _extract_body_inner(html: str) -> str:
    """Pull <body>…</body> inner HTML so it can be nested inside the shell <td>.
    Falls back to the original string if no <body> tag found."""
    m = _BODY_INNER_RE.search(html)
    return m.group(1) if m else html


def wrap_email_body(body_html: str, email_style: Optional[dict] = None) -> str:
    """Wrap organizer-authored content in branded email layout.

    V1 legacy TipTap templates emit body fragments → wrap in branded shell
    (gradient header + card + footer) so they look polished by default.

    V2 MJML-compiled emails are a complete email document with their own
    header/footer/container styled by the user in the builder — return
    as-is so the builder is true WYSIWYG (Mailchimp pattern). Wrapping V2
    in the shell would produce a doubled header (server gradient + user's
    own header section).
    """
    if _is_full_html_document(body_html):
        return body_html
    inner = f'<tr><td style="padding:28px 32px;">{body_html}</td></tr>'
    return _email_shell(inner, email_style)


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
    email_style: Optional[dict] = None,
) -> str:
    """Render a branded HTML email for meeting notifications (default template)."""
    s = email_style or {}
    primary = s.get("primary_color", "#4F80FF")

    body_html = "".join(
        f"<tr><td style='padding:6px 0;color:#374151;font-size:14px;line-height:1.6;'>{line}</td></tr>"
        for line in body_lines
    )
    cta_html = (
        f"<tr><td style='padding:24px 0 8px;'>"
        f"<a href='{cta_url}' style='display:inline-block;padding:12px 28px;"
        f"background:{primary};color:#fff;border-radius:8px;text-decoration:none;"
        f"font-size:14px;font-weight:600;letter-spacing:0.3px;'>{cta_label}</a>"
        f"</td></tr>"
        if cta_label and cta_url else ""
    )

    inner = (
        f'<tr><td style="padding:28px 32px 8px;">'
        f'<h2 style="margin:0;font-size:20px;font-weight:700;color:#111827;line-height:1.3;">{title}</h2>'
        f'</td></tr>'
        f'<tr><td style="padding:8px 32px 16px;">'
        f'<table width="100%" cellpadding="0" cellspacing="0">{body_html}{cta_html}</table>'
        f'</td></tr>'
    )
    return _email_shell(inner, email_style)
