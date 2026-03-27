from fastapi import APIRouter, HTTPException
from app.models.schemas import (
    EmailRequest, EmailResponse,
    BulkEmailRequest, BulkEmailResponse,
    PlainEmailRequest,
)
from app.services.qr_service import generate_qr_code_bytes, append_qr_cid_to_html, inject_qr_extras
from app.services.mailgun import send_mailgun
from app.services.directus import directus_post
from app.config import MAILGUN_API_KEY, MAILGUN_DOMAIN, MAILGUN_API_URL
import httpx
from datetime import datetime, timezone

router = APIRouter()


async def _log_registration_activity(
    registration_id: str,
    status: str,
    recipient: str,
    subject: str,
    triggered_by: str = "system",
    error_message: str | None = None,
) -> None:
    """Silently log email activity to Directus registration_activities. Never raises."""
    try:
        payload = {
            "registration_id": registration_id,
            "channel": "email",
            "action": "qr_email",
            "status": status,
            "recipient": recipient,
            "subject": subject,
            "triggered_by": triggered_by,
            "date_created": datetime.now(timezone.utc).isoformat(),
        }
        if error_message:
            payload["error_message"] = error_message
        await directus_post("/items/registration_activities", payload)
    except Exception:
        pass  # never crash the email flow over a log write


@router.post("/send-email-with-qr", response_model=EmailResponse)
async def send_email_with_qr(request: EmailRequest):
    """
    Receive email params, generate QR from content_qr,
    send via Mailgun with inline CID attachment — works on Gmail.
    """
    if not request.from_email.strip():
        raise HTTPException(status_code=400, detail="from_email không được để trống")
    if not request.to.strip():
        raise HTTPException(status_code=400, detail="to không được để trống")
    if not request.subject.strip():
        raise HTTPException(status_code=400, detail="subject không được để trống")
    if not request.content_qr.strip():
        raise HTTPException(status_code=400, detail="content_qr không được để trống")
    if not MAILGUN_API_KEY or not MAILGUN_DOMAIN:
        raise HTTPException(status_code=500, detail="Mailgun chưa được cấu hình")

    qr_bytes = generate_qr_code_bytes(request.content_qr)
    html_with_qr = append_qr_cid_to_html(request.html)
    html_with_qr = inject_qr_extras(html_with_qr, request.content_qr)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{MAILGUN_API_URL}/v3/{MAILGUN_DOMAIN}/messages",
                auth=("api", MAILGUN_API_KEY),
                data={
                    "from": request.from_email,
                    "to": request.to,
                    "subject": request.subject,
                    "html": html_with_qr,
                },
                files=[("inline", ("qrcode.png", qr_bytes, "image/png"))],
            )
            response.raise_for_status()
            result = response.json()

        # Log activity if registration_id provided (e.g. self-registration from Directus Flow)
        if request.registration_id:
            await _log_registration_activity(
                registration_id=request.registration_id,
                status="success",
                recipient=request.to,
                subject=request.subject,
                triggered_by=request.triggered_by or "system",
            )

        return EmailResponse(
            success=True,
            message="Email đã được gửi thành công",
            message_id=result.get("id", ""),
        )
    except httpx.HTTPStatusError as e:
        err_msg = f"Mailgun {e.response.status_code} - {e.response.text[:200]}"
        if request.registration_id:
            await _log_registration_activity(
                registration_id=request.registration_id,
                status="failed",
                recipient=request.to,
                subject=request.subject,
                triggered_by=request.triggered_by or "system",
                error_message=err_msg,
            )
        raise HTTPException(status_code=500, detail=f"Lỗi khi gửi email qua Mailgun: {err_msg}")
    except Exception as e:
        if request.registration_id:
            await _log_registration_activity(
                registration_id=request.registration_id,
                status="failed",
                recipient=request.to,
                subject=request.subject,
                triggered_by=request.triggered_by or "system",
                error_message=str(e),
            )
        raise HTTPException(status_code=500, detail=f"Lỗi khi xử lý: {str(e)}")



@router.post("/send-bulk-email-with-qr", response_model=BulkEmailResponse)
async def send_bulk_email_with_qr(request: BulkEmailRequest):
    """Send email with QR code to multiple recipients."""
    if not MAILGUN_API_KEY or not MAILGUN_DOMAIN:
        raise HTTPException(status_code=500, detail="Mailgun chưa được cấu hình")

    sender_name = request.sender_name or "Nexpo"
    from_email = request.from_email or f"{sender_name} <noreply@{MAILGUN_DOMAIN}>"
    sent = 0
    failed = 0
    errors = []

    async with httpx.AsyncClient(timeout=30) as client:
        for recipient in request.recipients:
            if not recipient.email or not recipient.content_qr:
                failed += 1
                errors.append("Invalid recipient: missing email or content_qr")
                continue
            try:
                personalized_html = request.html.replace("{{name}}", recipient.full_name or "")
                personalized_html = personalized_html.replace("{{full_name}}", recipient.full_name or "")
                html_with_qr = append_qr_cid_to_html(personalized_html)
                html_with_qr = inject_qr_extras(html_with_qr, recipient.content_qr)
                qr_bytes = generate_qr_code_bytes(recipient.content_qr)
                mg_response = await client.post(
                    f"{MAILGUN_API_URL}/v3/{MAILGUN_DOMAIN}/messages",
                    auth=("api", MAILGUN_API_KEY),
                    data={"from": from_email, "to": recipient.email, "subject": request.subject, "html": html_with_qr},
                    files=[("inline", ("qrcode.png", qr_bytes, "image/png"))],
                )
                if not mg_response.is_success:
                    failed += 1
                    errors.append(f"{recipient.email}: Mailgun {mg_response.status_code} - {mg_response.text[:200]}")
                else:
                    sent += 1
            except Exception as e:
                failed += 1
                errors.append(f"{recipient.email}: {str(e)[:150]}")

    return BulkEmailResponse(sent=sent, failed=failed, errors=errors)


@router.post("/send-email")
async def send_plain_email(request: PlainEmailRequest):
    """Send a plain HTML email without QR code."""
    if not MAILGUN_API_KEY or not MAILGUN_DOMAIN:
        raise HTTPException(status_code=500, detail="Mailgun not configured")
    if not request.to.strip():
        raise HTTPException(status_code=400, detail="to is required")

    sender_name = request.sender_name or "Nexpo"
    from_email = request.from_email or f"{sender_name} <noreply@{MAILGUN_DOMAIN}>"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{MAILGUN_API_URL}/v3/{MAILGUN_DOMAIN}/messages",
                auth=("api", MAILGUN_API_KEY),
                data={"from": from_email, "to": request.to, "subject": request.subject, "html": request.html},
            )
            resp.raise_for_status()
            result = resp.json()
        return EmailResponse(success=True, message="Email sent", message_id=result.get("id", ""))
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=500, detail=f"Mailgun error: {e.response.status_code} - {e.response.text[:200]}")
