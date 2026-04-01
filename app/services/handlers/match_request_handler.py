"""
Match Request Handler — Notifications for match request state transitions.

Handles all 6 flow types. Dispatches emails and in-app notifications
based on the flow_type + transition via notification_router.
"""

from app.services.directus import directus_get
from app.services.notification_router import dispatch_notification


async def handle_match_request(
    match_request_id: str,
    new_status: str,
    actor: str = "organizer",
) -> dict:
    """
    Handle notification dispatch for a match request status change.

    Args:
        match_request_id: UUID of the match request
        new_status: The status the request transitioned TO
        actor: Who performed the transition (organizer, exhibitor, visitor, system)

    Returns:
        Summary dict with sent count and results.
    """
    try:
        # Fetch match request with context
        resp = await directus_get(
            f"/items/visitor_match_requests/{match_request_id}"
            "?fields[]=id,status,flow_type,event_id,exhibitor_id,"
            "registration_id,request_type,message,preferred_meeting_time,"
            "organizer_note"
        )
        match_request = resp.get("data")
        if not match_request:
            return {"error": f"Match request {match_request_id} not found", "sent": 0}

        # Determine flow type (null = legacy visitor_organizer_exhibitor)
        flow_type = match_request.get("flow_type") or "visitor_organizer_exhibitor"

        # The current status in DB is already new_status (caller updated before notifying).
        # We need the previous status to determine the transition.
        # Convention: caller passes the transition info. We derive from_state:
        from_state = _infer_from_state(new_status, flow_type)

        result = await dispatch_notification(
            flow_type=flow_type,
            from_state=from_state,
            to_state=new_status,
            match_request=match_request,
        )

        return {
            "match_request_id": match_request_id,
            "flow_type": flow_type,
            "transition": f"{from_state}→{new_status}",
            **result,
        }

    except Exception as e:
        return {"error": str(e), "sent": 0}


def _infer_from_state(new_status: str, flow_type: str) -> str:
    """
    Infer the previous state from the new status and flow type.
    Simple heuristic based on flow structure.
    """
    # States that always come from 'pending'
    if new_status in ("organizer_approved", "organizer_rejected"):
        return "pending"

    # States that come from 'organizer_approved' in multi-step flows
    if new_status in ("exhibitor_agreed", "exhibitor_declined"):
        if flow_type in ("direct_visitor_exhibitor",):
            return "pending"
        return "organizer_approved"

    if new_status in ("visitor_approved", "visitor_declined"):
        if flow_type in ("direct_exhibitor_visitor",):
            return "pending"
        return "organizer_approved"

    # converted_to_meeting comes from the agreed/approved state
    if new_status == "converted_to_meeting":
        return "exhibitor_agreed"

    return "pending"
