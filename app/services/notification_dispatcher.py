"""Multi-channel notification dispatcher — orchestrates config → template → channel → log."""
from app.services.channels.base_channel import NotificationRecipient, ChannelResult
from app.services.channels.channel_factory import build_channel
from app.services.notification_config import get_trigger_channels, get_channel_config
from app.services.notification_template_service import get_and_render_template
from app.services.directus import directus_post


async def dispatch_multi_channel(
    trigger_type: str,
    recipient: NotificationRecipient,
    variables: dict,
    event_id: str | None,
    tenant_id: str | None,
    registration_id: str | None = None,
    extra_content: dict | None = None,
) -> dict:
    """Send notifications via all configured channels for a trigger.

    Args:
        trigger_type: e.g. "registration.qr_email"
        recipient: NotificationRecipient with email/phone/name
        variables: Template variables dict
        event_id: Event context
        tenant_id: Tenant context
        registration_id: For activity logging
        extra_content: Override/extend rendered content (e.g. inline QR files for email)

    Returns:
        Dict with per-channel results: {"email": "sent", "sms": "skipped: no config", ...}
    """
    channels = await get_trigger_channels(trigger_type, event_id, tenant_id)
    if not channels:
        return {"_skipped": True, "reason": "No channels configured for trigger"}

    results = {}
    extra_content = extra_content or {}

    for channel_name in channels:
        try:
            # 1. Get provider config
            config = await get_channel_config(channel_name, event_id, tenant_id)
            if not config:
                results[channel_name] = "skipped: no provider config"
                continue

            provider = config.get("provider", "")
            credentials = config.get("credentials") or {}
            channel_config = config.get("config") or {}

            # 2. Get and render template
            content = await get_and_render_template(
                trigger_type=trigger_type,
                channel=channel_name,
                language=recipient.language,
                event_id=event_id,
                tenant_id=tenant_id,
                variables=variables,
            )
            if not content:
                results[channel_name] = "skipped: no template"
                continue

            # 3. Merge extra content (e.g. QR inline files for email)
            if channel_name in extra_content:
                content.update(extra_content[channel_name])

            # 4. Build channel instance and send
            ch = build_channel(channel_name, provider, credentials, channel_config)
            result: ChannelResult = await ch.send(recipient, content)

            # 5. Log activity
            await _log_activity(
                registration_id=registration_id,
                channel=channel_name,
                provider=provider,
                trigger_type=trigger_type,
                recipient=recipient.email if channel_name == "email" else recipient.phone,
                status="success" if result.success else "failed",
                error=result.error,
                subject=content.get("subject", ""),
            )

            results[channel_name] = "sent" if result.success else f"failed: {result.error}"

        except Exception as e:
            results[channel_name] = f"error: {str(e)[:200]}"

    return results


async def _log_activity(
    registration_id: str | None,
    channel: str,
    provider: str,
    trigger_type: str,
    recipient: str | None,
    status: str,
    error: str | None = None,
    subject: str = "",
) -> None:
    """Log notification activity to registration_activities. Silent — never raises."""
    if not registration_id:
        return
    try:
        payload = {
            "registration_id": registration_id,
            "channel": channel,
            "action": trigger_type.replace(".", "_"),
            "status": status,
            "recipient": recipient or "",
            "subject": subject,
            "triggered_by": "system",
            "provider": provider,
            "trigger_type": trigger_type,
        }
        if error:
            payload["error_message"] = error[:500]
        await directus_post("/items/registration_activities", payload)
    except Exception:
        pass  # never crash over logging
