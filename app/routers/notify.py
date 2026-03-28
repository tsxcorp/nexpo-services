"""
Unified notification endpoints.

POST /notify
  { "type": "<notification_type>", "context": { ...type-specific fields } }

POST /notify/bulk
  { "type": "<notification_type>", "ids": ["id1", "id2", ...], "context": {...} }
  Processes up to 50 items of the same type, returns per-item results.

All notification logic lives in app/services/notification_handlers.py.

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
from app.models.schemas import NotifyRequest, BulkNotifyRequest, BulkNotifyResponse, BulkNotifyItemResult
from app.services.notification_handlers import (
    handle_meeting,
    handle_registration_qr,
    handle_order_facility_created,
    handle_ticket_support_created,
    handle_lead_captured,
    handle_candidate_interview_schedule,
)

router = APIRouter()

MEETING_TRIGGERS = {"meeting.scheduled", "meeting.confirmed", "meeting.cancelled"}
BULK_SIZE_LIMIT = 50  # hard cap per request


# ── Dispatcher — shared by single + bulk ─────────────────────────────────────

async def _dispatch(notify_type: str, item_id: str | None, context: dict) -> dict:
    """
    Route a notification type to its handler.
    Returns handler result dict (always includes at least status info).
    Raises ValueError for unknown types, Exception for handler errors.
    """
    if notify_type in MEETING_TRIGGERS:
        meeting_id = item_id or context.get("meeting_id")
        if not meeting_id:
            raise ValueError("meeting_id required (via ids[] or context.meeting_id)")
        trigger = notify_type.split(".", 1)[1]  # "scheduled" | "confirmed" | "cancelled"
        return await handle_meeting(meeting_id=str(meeting_id), trigger=trigger)

    if notify_type == "registration.qr_email":
        registration_id = item_id or context.get("registration_id")
        if not registration_id:
            raise ValueError("registration_id required (via ids[] or context.registration_id)")
        triggered_by = context.get("triggered_by", "admin")
        return await handle_registration_qr(
            registration_id=str(registration_id),
            triggered_by=str(triggered_by),
        )

    if notify_type == "order.facility.created":
        order_id = context.get("order_id")
        event_id = context.get("event_id")
        if not order_id or not event_id:
            raise ValueError("context.order_id and context.event_id required")
        return await handle_order_facility_created(order_id=str(order_id), event_id=str(event_id))

    if notify_type == "ticket.support.created":
        ticket_id = context.get("ticket_id")
        event_id = context.get("event_id")
        if not ticket_id or not event_id:
            raise ValueError("context.ticket_id and context.event_id required")
        return await handle_ticket_support_created(ticket_id=str(ticket_id), event_id=str(event_id))

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

    if notify_type == "candidate.interview_schedule":
        registration_id = item_id or context.get("registration_id")
        event_id = context.get("event_id")
        if not registration_id:
            raise ValueError("registration_id required (via ids[] or context.registration_id)")
        if not event_id:
            raise ValueError("context.event_id required")
        return await handle_candidate_interview_schedule(
            registration_id=str(registration_id),
            event_id=str(event_id),
            triggered_by=str(context.get("triggered_by", "admin")),
        )

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
