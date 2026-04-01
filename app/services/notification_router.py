"""
Notification Router — Flow-aware dispatch logic.

Maps flow_type + state transitions to notification triggers and recipients.
Uses template_renderer for email content.
"""

from app.services.directus import (
    directus_get,
    resolve_visitor_email,
    resolve_exhibitor_email,
    create_notification,
)
from app.services.mailgun import send_mailgun, meeting_notification_html
from app.services import template_renderer
from app.config import ADMIN_URL, PORTAL_URL

# Complete mapping: flow_type → { "from→to": { trigger, recipients[] } }
FLOW_NOTIFICATION_MAP: dict[str, dict[str, dict]] = {
    "direct_visitor_exhibitor": {
        "pending→exhibitor_agreed": {"trigger": "exhibitor_response", "recipients": ["organizer", "visitor"]},
        "pending→exhibitor_declined": {"trigger": "match_rejected", "recipients": ["visitor"]},
    },
    "organizer_only": {
        "pending→organizer_approved": {"trigger": "match_approved", "recipients": ["visitor"]},
        "pending→organizer_rejected": {"trigger": "match_rejected", "recipients": ["visitor"]},
    },
    "visitor_organizer_exhibitor": {
        "pending→organizer_approved": {"trigger": "match_approved", "recipients": ["exhibitor"]},
        "pending→organizer_rejected": {"trigger": "match_rejected", "recipients": ["visitor"]},
        "organizer_approved→exhibitor_agreed": {"trigger": "exhibitor_response", "recipients": ["organizer", "visitor"]},
        "organizer_approved→exhibitor_declined": {"trigger": "match_rejected", "recipients": ["visitor"]},
    },
    "ai_organizer_exhibitor": {
        "pending→organizer_approved": {"trigger": "match_approved", "recipients": ["exhibitor"]},
        "pending→organizer_rejected": {"trigger": "match_rejected", "recipients": ["visitor"]},
        "organizer_approved→exhibitor_agreed": {"trigger": "exhibitor_response", "recipients": ["organizer", "visitor"]},
        "organizer_approved→exhibitor_declined": {"trigger": "match_rejected", "recipients": ["visitor"]},
    },
    "direct_exhibitor_visitor": {
        "pending→visitor_approved": {"trigger": "exhibitor_response", "recipients": ["organizer", "exhibitor"]},
        "pending→visitor_declined": {"trigger": "match_rejected", "recipients": ["exhibitor"]},
    },
    "exhibitor_organizer_visitor": {
        "pending→organizer_approved": {"trigger": "match_approved", "recipients": ["visitor"]},
        "pending→organizer_rejected": {"trigger": "match_rejected", "recipients": ["exhibitor"]},
        "organizer_approved→visitor_approved": {"trigger": "exhibitor_response", "recipients": ["organizer", "exhibitor"]},
        "organizer_approved→visitor_declined": {"trigger": "match_rejected", "recipients": ["exhibitor"]},
    },
}


async def dispatch_notification(
    flow_type: str,
    from_state: str,
    to_state: str,
    match_request: dict,
) -> dict:
    """
    Dispatch notifications for a match request state transition.
    Returns summary of sent notifications.
    """
    key = f"{from_state}→{to_state}"
    flow_map = FLOW_NOTIFICATION_MAP.get(flow_type, {})
    mapping = flow_map.get(key)

    if not mapping:
        return {"sent": 0, "skipped": True, "reason": f"No mapping for {flow_type}/{key}"}

    trigger = mapping["trigger"]
    recipients = mapping["recipients"]
    event_id = str(match_request.get("event_id", ""))
    registration_id = str(match_request.get("registration_id", ""))
    exhibitor_id = str(match_request.get("exhibitor_id", ""))
    request_type = match_request.get("request_type") or "business"
    matching_type = "business_matching" if request_type == "business" else "talent_matching"

    # Fetch related data
    visitor_name, visitor_email = await _get_visitor_info(registration_id)
    exhibitor_name, exhibitor_email = await _get_exhibitor_info(exhibitor_id)
    event_name = await _get_event_name(event_id)

    variables = {
        "visitor_name": visitor_name,
        "exhibitor_name": exhibitor_name,
        "company_name": exhibitor_name,
        "event_name": event_name,
        "matching_type": matching_type.replace("_", " ").title(),
        "requester_name": visitor_name,
        "portal_url": f"{PORTAL_URL}/matching",
        "action_url": f"{ADMIN_URL}/events/{event_id}/matching",
    }

    results = []
    for recipient_role in recipients:
        try:
            to_email = _resolve_email(recipient_role, visitor_email, exhibitor_email)
            if not to_email:
                results.append({"recipient": recipient_role, "status": "skipped", "reason": "no email"})
                continue

            # Fetch and render template
            tmpl = await template_renderer.get_template(event_id, trigger, recipient_role, matching_type)
            if tmpl and tmpl.get("html_template"):
                rendered_html = template_renderer.render(tmpl["html_template"], variables)
                subject = template_renderer.sanitize_subject(
                    template_renderer.render(tmpl.get("subject") or f"[Nexpo] {event_name}", variables)
                )
            else:
                # Default fallback
                subject = template_renderer.sanitize_subject(f"[Nexpo] {event_name} — Match Update")
                rendered_html = _default_html(trigger, variables)

            email_html = meeting_notification_html(rendered_html)
            await send_mailgun(to_email, subject, email_html)
            results.append({"recipient": recipient_role, "status": "sent", "email": to_email})
        except Exception as e:
            results.append({"recipient": recipient_role, "status": "failed", "error": str(e)})

    return {"sent": sum(1 for r in results if r["status"] == "sent"), "results": results}


def _resolve_email(role: str, visitor_email: str, exhibitor_email: str) -> str | None:
    """Resolve email address by recipient role."""
    if role == "visitor":
        return visitor_email or None
    if role == "exhibitor":
        return exhibitor_email or None
    return None  # organizer notifications are in-app only for now


async def _get_visitor_info(registration_id: str) -> tuple[str, str]:
    """Fetch visitor name and email."""
    try:
        resp = await directus_get(f"/items/registrations/{registration_id}?fields[]=full_name,email")
        data = resp.get("data", {})
        return data.get("full_name", "Visitor"), data.get("email", "")
    except Exception:
        return "Visitor", ""


async def _get_exhibitor_info(exhibitor_id: str) -> tuple[str, str]:
    """Fetch exhibitor company name and email."""
    try:
        resp = await directus_get(
            f"/items/exhibitors/{exhibitor_id}?fields[]=translations.company_name,translations.languages_code"
        )
        data = resp.get("data", {})
        translations = data.get("translations") or []
        name = next((t["company_name"] for t in translations if t.get("company_name")), "Exhibitor")
        email = await resolve_exhibitor_email(exhibitor_id)
        return name, email
    except Exception:
        return "Exhibitor", ""


async def _get_event_name(event_id: str) -> str:
    """Fetch event name."""
    try:
        resp = await directus_get(f"/items/events/{event_id}?fields[]=name")
        return (resp.get("data") or {}).get("name", "Event")
    except Exception:
        return "Event"


def _default_html(trigger: str, variables: dict) -> str:
    """Generate minimal fallback HTML when no custom template exists."""
    name = variables.get("visitor_name", "")
    event = variables.get("event_name", "")
    return f"""
    <h2>Match Update — {event}</h2>
    <p>Dear {name},</p>
    <p>There has been an update to your matching request for <strong>{event}</strong>.</p>
    <p>Please check the platform for details.</p>
    <p>Best regards,<br>Nexpo Team</p>
    """
