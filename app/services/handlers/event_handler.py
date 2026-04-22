"""
Event-scoped notification handlers — facility orders, support tickets, lead capture.
Small handlers grouped together since each is <40 lines.
"""
from app.config import ADMIN_URL
from app.services.directus import directus_get, create_notification


async def handle_order_facility_created(order_id: str, event_id: str) -> dict:
    """In-app → Organizer when exhibitor submits a facility order."""
    in_app_created: list[str] = []
    try:
        order_resp = await directus_get(f"/items/facility_orders/{order_id}?fields[]=ref_number,total_amount")
        order = order_resp.get("data", {})

        items_resp = await directus_get(
            f"/items/facility_order_items?filter[order_id][_eq]={order_id}&aggregate[count][]=id"
        )
        items_data = items_resp.get("data") or [{}]
        item_count = (items_data[0].get("count") or {}).get("id", 0)

        event_resp = await directus_get(f"/items/events/{event_id}?fields[]=user_created")
        organizer_id = (event_resp.get("data") or {}).get("user_created")
        if organizer_id:
            ref = order.get("ref_number", order_id)
            total = float(order.get("total_amount") or 0)
            await create_notification(
                user_id=organizer_id,
                title="Đơn hàng thiết bị mới / New facility order",
                body=f"Ref: {ref} · {item_count} item(s) · {total:,.0f} VND",
                link=f"{ADMIN_URL}/events/{event_id}/orders?open={order_id}",
                notif_type="order_facility_created",
                entity_type="facility_orders",
                entity_id=order_id,
            )
            in_app_created.append(f"organizer:{organizer_id}")
    except Exception:
        pass
    return {"in_app_created": in_app_created}


async def handle_ticket_support_created(ticket_id: str, event_id: str) -> dict:
    """In-app → Organizer when exhibitor opens a support ticket."""
    in_app_created: list[str] = []
    try:
        ticket_resp = await directus_get(f"/items/support_tickets/{ticket_id}?fields[]=subject,priority")
        ticket = ticket_resp.get("data", {})

        event_resp = await directus_get(f"/items/events/{event_id}?fields[]=user_created")
        organizer_id = (event_resp.get("data") or {}).get("user_created")
        if organizer_id:
            priority = (ticket.get("priority") or "medium").upper()
            subject = ticket.get("subject", "")
            await create_notification(
                user_id=organizer_id,
                title="Ticket hỗ trợ mới / New support ticket",
                body=f"[{priority}] {subject}",
                link=f"{ADMIN_URL}/events/{event_id}/tickets?open={ticket_id}",
                notif_type="ticket_support_created",
                entity_type="support_tickets",
                entity_id=ticket_id,
            )
            in_app_created.append(f"organizer:{organizer_id}")
    except Exception:
        pass
    return {"in_app_created": in_app_created}


async def handle_lead_captured(
    user_id: str, attendee_name: str, attendee_email: str,
    attendee_company: str, event_id: str,
) -> dict:
    """In-app → Exhibitor user (self-notify / activity log) when lead is captured."""
    in_app_created: list[str] = []
    try:
        if not user_id:
            return {"in_app_created": in_app_created}
        body = f"{attendee_company} · {attendee_email}" if attendee_company else attendee_email
        await create_notification(
            user_id=user_id,
            title=f"Lead mới: {attendee_name or 'Khách tham quan'}",
            body=body,
            link=f"/leads?event={event_id}" if event_id else None,
            notif_type="lead_captured",
            entity_type="leads",
        )
        in_app_created.append(f"exhibitor:{user_id}")
    except Exception:
        pass
    return {"in_app_created": in_app_created}
