"""Email channel — sends via Mailgun (wraps existing mailgun.py)."""
from app.services.channels.base_channel import (
    NotificationChannel,
    NotificationRecipient,
    ChannelResult,
)
from app.services.mailgun import send_mailgun


class EmailChannel(NotificationChannel):
    """Send email notifications via Mailgun."""

    channel_type = "email"
    provider_name = "mailgun"

    def __init__(self, api_key: str = "", domain: str = "", sender_name: str = "Nexpo", reply_to: str = ""):
        # EmailChannel currently uses global Mailgun config from env vars.
        # Per-tenant credentials stored in notification_channel_configs for future use.
        self.sender_name = sender_name
        self.reply_to = reply_to

    async def send(self, recipient: NotificationRecipient, content: dict) -> ChannelResult:
        """Send email. content = {"subject": str, "html": str, "inline_files"?: list, "attachments"?: list}"""
        if not recipient.email:
            return ChannelResult(success=False, error="No email address", channel="email", provider="mailgun")

        subject = content.get("subject", "")
        html = content.get("html", "")
        if not subject or not html:
            return ChannelResult(success=False, error="Missing subject or html", channel="email", provider="mailgun")

        try:
            ok = await send_mailgun(
                to=recipient.email,
                subject=subject,
                html=html,
                sender_name=self.sender_name,
                inline_files=content.get("inline_files"),
                attachments=content.get("attachments"),
            )
            if ok:
                return ChannelResult(success=True, channel="email", provider="mailgun")
            return ChannelResult(success=False, error="Mailgun returned failure", channel="email", provider="mailgun")
        except Exception as e:
            return ChannelResult(success=False, error=str(e)[:200], channel="email", provider="mailgun")

    def validate_config(self, config: dict) -> bool:
        # Email uses global env vars for now; always valid if Mailgun is configured
        return True
