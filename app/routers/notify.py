"""
Unified notification endpoints.

POST /notify
  { "type": "<notification_type>", "context": { ...type-specific fields } }

POST /notify/bulk
  { "type": "<notification_type>", "ids": ["id1", "id2", ...], "context": {...} }
  Processes up to 50 items of the same type, returns per-item results.

POST /notify/test
  { "channel": "email"|"sms"|"zns", "event_id": int, "tenant_id": int,
    "recipient": {"email": "...", "phone": "..."} }
  Sends a test notification via the specified channel.

All notification logic lives in app/services/notification_handlers.py.
Multi-channel dispatch: app/services/notification_dispatcher.py (new).

Supported types:
  meeting.scheduled       context: { meeting_id }
  meeting.confirmed       context: { meeting_id }
  meeting.cancelled       context: { meeting_id }
  registration.qr_email   ids: [registration_id, ...]   (bulk)
                           context: { registration_id }  (single)
  order.facility.created  context: { order_id, event_id }
  ticket.support.created  context: { ticket_id, event_id }
  lead.captured           context: { user_id, attendee_name, attendee_email, attendee_company, event_id }
  candidate.interview_schedule
                          ids: [registration_id, ...]   (bulk)
                          context: { event_id, triggered_by? }
                          Sends consolidated interview schedule email to candidates
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.models.schemas import NotifyRequest, BulkNotifyRequest, BulkNotifyResponse, BulkNotifyItemResult
from app.services.notification_handlers import (
    handle_meeting,
    handle_registration_qr,
    handle_order_facility_created,
    handle_ticket_support_created,
    handle_lead_captured,
    handle_candidate_interview_schedule,
)
from app.services.handlers.match_request_handler import handle_match_request
from app.services.notification_dispatcher import dispatch_multi_channel
from app.services.notification_config import get_trigger_channels, get_channel_config
from app.services.channels.base_channel import NotificationRecipient
from app.services.channels.channel_factory import build_channel
from app.services.directus import directus_get

router = APIRouter()

MEETING_TRIGGERS = {"meeting.scheduled", "meeting.confirmed", "meeting.cancelled"}
BULK_SIZE_LIMIT = 50  # hard cap per request


# ── Helper: resolve tenant_id from event_id ──────────────────────────────────

async def _resolve_tenant_id(event_id: str | None) -> str | None:
    """Get tenant_id from event record. Returns None on failure."""
    if not event_id:
        return None
    try:
        resp = await directus_get(f"/items/events/{event_id}?fields[]=tenant_id")
        return str((resp.get("data") or {}).get("tenant_id", "")) or None
    except Exception:
        return None


# ── Multi-channel follow-up: send SMS/ZNS after legacy email handler ─────────

async def _dispatch_extra_channels(
    notify_type: str,
    event_id: str | None,
    tenant_id: str | None,
    recipient_email: str = "",
    recipient_phone: str = "",
    recipient_name: str = "",
    registration_id: str | None = None,
    variables: dict | None = None,
) -> dict | None:
    """After legacy email handler succeeds, send via any extra configured channels (SMS, ZNS).

    Only sends non-email channels. If admin configured custom email via
    notification_channel_configs, that's handled separately in dispatch_multi_channel.
    Returns None if no extra channels configured.
    """
    if not event_id:
        return None
    if not tenant_id:
        tenant_id = await _resolve_tenant_id(event_id)
    if not tenant_id:
        return None

    try:
        channels = await get_trigger_channels(notify_type, event_id, tenant_id)
        # Only dispatch non-email channels — email already sent by legacy handler
        extra_channels = [ch for ch in channels if ch != "email"]
        if not extra_channels:
            return None

        recipient = NotificationRecipient(
            email=recipient_email,
            phone=recipient_phone,
            name=recipient_name,
        )
        return await dispatch_multi_channel(
            trigger_type=notify_type,
            recipient=recipient,
            variables=variables or {},
            event_id=event_id,
            tenant_id=tenant_id,
            registration_id=registration_id,
        )
    except Exception:
        return None  # never crash the legacy flow over extra channels


# ── Dispatcher — shared by single + bulk ─────────────────────────────────────

async def _dispatch(notify_type: str, item_id: str | None, context: dict) -> dict:
    """
    Route a notification type to its handler.
    1. Legacy handler sends email (always)
    2. Then dispatch extra channels (SMS/ZNS) if admin configured them
    Returns handler result dict (always includes at least status info).
    Raises ValueError for unknown types, Exception for handler errors.
    """
    if notify_type in MEETING_TRIGGERS:
        meeting_id = item_id or context.get("meeting_id")
        if not meeting_id:
            raise ValueError("meeting_id required (via ids[] or context.meeting_id)")
        trigger = notify_type.split(".", 1)[1]  # "scheduled" | "confirmed" | "cancelled"
        result = await handle_meeting(meeting_id=str(meeting_id), trigger=trigger)
        # Send extra channels (SMS/ZNS) if configured
        await _dispatch_extra_channels(
            notify_type, event_id=context.get("event_id"),
            tenant_id=context.get("tenant_id"),
            recipient_phone=result.get("recipient_phone", ""),
            recipient_name=result.get("visitor_name", ""),
            variables=result.get("variables", {}),
        )
        return result

    if notify_type == "registration.qr_email":
        registration_id = item_id or context.get("registration_id")
        if not registration_id:
            raise ValueError("registration_id required (via ids[] or context.registration_id)")
        triggered_by = context.get("triggered_by", "admin")
        result = await handle_registration_qr(
            registration_id=str(registration_id),
            triggered_by=str(triggered_by),
        )
        # Send extra channels (SMS/ZNS) if configured
        await _dispatch_extra_channels(
            notify_type, event_id=context.get("event_id"),
            tenant_id=context.get("tenant_id"),
            recipient_email=result.get("email", ""),
            recipient_phone=result.get("recipient_phone", ""),
            recipient_name=result.get("visitor_name", ""),
            registration_id=str(registration_id),
            variables=result.get("variables", {}),
        )
        return result

    if notify_type == "order.facility.created":
        order_id = context.get("order_id")
        event_id = context.get("event_id")
        if not order_id or not event_id:
            raise ValueError("context.order_id and context.event_id required")
        result = await handle_order_facility_created(order_id=str(order_id), event_id=str(event_id))
        await _dispatch_extra_channels(notify_type, event_id=str(event_id), tenant_id=context.get("tenant_id"), variables=result.get("variables", {}))
        return result

    if notify_type == "ticket.support.created":
        ticket_id = context.get("ticket_id")
        event_id = context.get("event_id")
        if not ticket_id or not event_id:
            raise ValueError("context.ticket_id and context.event_id required")
        result = await handle_ticket_support_created(ticket_id=str(ticket_id), event_id=str(event_id))
        await _dispatch_extra_channels(notify_type, event_id=str(event_id), tenant_id=context.get("tenant_id"), variables=result.get("variables", {}))
        return result

    if notify_type == "lead.captured":
        user_id = context.get("user_id", "")
        if not user_id:
            raise ValueError("context.user_id required")
        return await handle_lead_captured(
            user_id=str(user_id),
            attendee_name=str(context.get("attendee_name", "Khách tham quan")),
            attendee_email=str(context.get("attendee_email", "")),
            attendee_company=str(context.get("attendee_company", "")),
            event_id=str(context.get("event_id", "")),
        )

    if notify_type == "match.status_changed":
        match_request_id = context.get("match_request_id")
        new_status = context.get("new_status")
        if not match_request_id or not new_status:
            raise ValueError("context.match_request_id and context.new_status required")
        return await handle_match_request(
            match_request_id=str(match_request_id),
            new_status=str(new_status),
            actor=str(context.get("actor", "organizer")),
        )

    if notify_type == "candidate.interview_schedule":
        registration_id = item_id or context.get("registration_id")
        event_id = context.get("event_id")
        if not registration_id:
            raise ValueError("registration_id required (via ids[] or context.registration_id)")
        if not event_id:
            raise ValueError("context.event_id required")
        result = await handle_candidate_interview_schedule(
            registration_id=str(registration_id),
            event_id=str(event_id),
            triggered_by=str(context.get("triggered_by", "admin")),
        )
        await _dispatch_extra_channels(
            notify_type, event_id=str(event_id), tenant_id=context.get("tenant_id"),
            registration_id=str(registration_id), variables=result.get("variables", {}),
        )
        return result

    raise ValueError(f"Unknown notification type: {notify_type!r}")


# ── POST /notify — single item ────────────────────────────────────────────────

@router.post("/notify")
async def notify(request: NotifyRequest):
    """Process a single notification. Fire-and-forget friendly."""
    try:
        result = await _dispatch(request.type, item_id=None, context=request.context)
        return {"ok": True, "type": request.type, **result}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Notification error: {str(e)}")


# ── POST /notify/bulk — batch processing ─────────────────────────────────────

@router.post("/notify/bulk", response_model=BulkNotifyResponse)
async def notify_bulk(request: BulkNotifyRequest):
    """
    Process up to 50 notifications of the same type.

    Returns per-item results for SSE streaming by the admin layer.
    Admin layer should chunk large arrays and call this endpoint per-chunk.

    Example request:
      { "type": "registration.qr_email", "ids": ["uuid1", "uuid2", ...], "context": {} }
      { "type": "meeting.confirmed", "ids": ["mtg1", "mtg2"], "context": {} }
    """
    if not request.ids:
        raise HTTPException(status_code=422, detail="ids[] must not be empty")

    ids = request.ids[:BULK_SIZE_LIMIT]  # hard cap — admin should chunk

    results: list[BulkNotifyItemResult] = []

    for item_id in ids:
        try:
            handler_result = await _dispatch(request.type, item_id=item_id, context=request.context)
            # Extract email from handler result if available
            email = (
                handler_result.get("email")
                or (handler_result.get("emails_sent") or [""])[0].split(":", 1)[-1]
                if handler_result.get("emails_sent") else None
            )
            results.append(BulkNotifyItemResult(
                id=item_id,
                status="ok",
                email=email or None,
            ))
        except Exception as e:
            results.append(BulkNotifyItemResult(
                id=item_id,
                status="error",
                error=str(e)[:200],
            ))

    sent = sum(1 for r in results if r.status == "ok")
    failed = len(results) - sent

    return BulkNotifyResponse(results=results, sent=sent, failed=failed)


# ── POST /notify/test — test channel config ──────────────────────────────────


class TestNotifyRequest(BaseModel):
    channel: str  # "email", "sms", "zns"
    event_id: int | None = None
    tenant_id: int
    recipient: dict  # {"email": "...", "phone": "..."}


@router.post("/notify/test")
async def notify_test(request: TestNotifyRequest):
    """Send a test notification via a specific channel. Used by admin UI to verify config."""
    try:
        config = await get_channel_config(
            request.channel, str(request.event_id) if request.event_id else None, str(request.tenant_id)
        )
        if not config:
            raise HTTPException(status_code=404, detail=f"No {request.channel} provider configured")

        provider = config.get("provider", "")
        credentials = config.get("credentials") or {}
        channel_config = config.get("config") or {}
        ch = build_channel(request.channel, provider, credentials, channel_config)

        recipient = NotificationRecipient(
            email=request.recipient.get("email"),
            phone=request.recipient.get("phone"),
            name="Test User",
        )

        # Build test content per channel
        content: dict
        if request.channel == "email":
            content = {
                "subject": "[Nexpo] Test Notification",
                "html": "<h2>Test Email</h2><p>This is a test notification from Nexpo.</p>",
            }
        elif request.channel == "sms":
            content = {"body": "[Nexpo] Day la tin nhan thu nghiem / This is a test SMS."}
        elif request.channel == "zns":
            content = {"template_id": "test", "params": {"customer_name": "Test User"}}
        else:
            raise HTTPException(status_code=422, detail=f"Unknown channel: {request.channel}")

        result = await ch.send(recipient, content)
        return {"ok": result.success, "channel": request.channel, "provider": provider, "error": result.error}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
