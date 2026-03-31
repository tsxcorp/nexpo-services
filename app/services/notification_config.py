"""Notification config lookups — trigger channels + provider configs from Directus."""
from app.services.directus import directus_get


# Default trigger → channel mapping (fallback when no Directus config exists)
DEFAULT_TRIGGER_CHANNELS: dict[str, list[str]] = {
    "registration.qr_email": ["email"],
    "meeting.scheduled": ["email"],
    "meeting.confirmed": ["email"],
    "meeting.cancelled": ["email"],
    "order.facility.created": ["email"],
    "ticket.support.created": ["email"],
    "lead.captured": [],  # in-app only
    "candidate.interview_schedule": ["email"],
    "match.status_changed": ["email"],
}


async def get_trigger_channels(
    trigger_type: str,
    event_id: str | None,
    tenant_id: str | None,
) -> list[str]:
    """Resolve which channels to use for a trigger.

    Priority: event-level config → tenant-level config → hardcoded default.
    Returns list of channel names, e.g. ["email", "sms"].
    """
    # Try event-specific config first
    if event_id:
        config = await _fetch_trigger_config(trigger_type, event_id=event_id)
        if config:
            return config.get("channels") or []

    # Fallback: tenant-level (event_id is null)
    if tenant_id:
        config = await _fetch_trigger_config(trigger_type, tenant_id=tenant_id)
        if config:
            return config.get("channels") or []

    # Hardcoded default
    return DEFAULT_TRIGGER_CHANNELS.get(trigger_type, ["email"])


async def get_channel_config(
    channel: str,
    event_id: str | None,
    tenant_id: str | None,
) -> dict | None:
    """Resolve provider config for a channel.

    Priority: event-specific → tenant-wide.
    Returns full config dict or None if no config found.
    """
    # Try event-level override
    if event_id and tenant_id:
        config = await _fetch_channel_config(channel, tenant_id, event_id)
        if config:
            return config

    # Fallback: tenant-wide (event_id is null)
    if tenant_id:
        config = await _fetch_channel_config(channel, tenant_id, event_id=None)
        if config:
            return config

    return None


async def _fetch_trigger_config(
    trigger_type: str,
    event_id: str | None = None,
    tenant_id: str | None = None,
) -> dict | None:
    """Fetch a single trigger config from Directus."""
    try:
        filters = f"filter[trigger_type][_eq]={trigger_type}&filter[is_active][_eq]=true"
        if event_id:
            filters += f"&filter[event_id][_eq]={event_id}"
        else:
            filters += "&filter[event_id][_null]=true"
            if tenant_id:
                filters += f"&filter[tenant_id][_eq]={tenant_id}"

        resp = await directus_get(
            f"/items/notification_trigger_configs?{filters}"
            "&fields[]=channels,is_active&limit=1"
        )
        items = resp.get("data") or []
        return items[0] if items else None
    except Exception:
        return None


async def _fetch_channel_config(
    channel: str,
    tenant_id: str,
    event_id: str | None,
) -> dict | None:
    """Fetch a single channel provider config from Directus."""
    try:
        filters = (
            f"filter[channel][_eq]={channel}"
            f"&filter[tenant_id][_eq]={tenant_id}"
            "&filter[is_active][_eq]=true"
        )
        if event_id:
            filters += f"&filter[event_id][_eq]={event_id}"
        else:
            filters += "&filter[event_id][_null]=true"

        resp = await directus_get(
            f"/items/notification_channel_configs?{filters}"
            "&fields[]=id,channel,provider,credentials,config,rate_limit_per_hour"
            "&limit=1"
        )
        items = resp.get("data") or []
        return items[0] if items else None
    except Exception:
        return None
