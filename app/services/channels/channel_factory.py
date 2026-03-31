"""Factory that builds channel instances from Directus provider config."""
from app.services.channels.base_channel import NotificationChannel
from app.services.channels.email_channel import EmailChannel
from app.services.channels.sms_channel import SMSChannel
from app.services.channels.zns_channel import ZNSChannel, ZNSDirectZaloChannel


def build_channel(channel: str, provider: str, credentials: dict, config: dict = None) -> NotificationChannel:
    """Build a NotificationChannel instance from provider config.

    Args:
        channel: "email", "sms", or "zns"
        provider: "mailgun", "esms", "zalo", "twilio"
        credentials: Provider-specific credentials (api_key, secret_key, etc.)
        config: Optional channel settings (sender_name, brandname, etc.)
    """
    config = config or {}

    if channel == "email" and provider == "mailgun":
        return EmailChannel(
            api_key=credentials.get("api_key", ""),
            domain=credentials.get("domain", ""),
            sender_name=config.get("sender_name", "Nexpo"),
            reply_to=config.get("reply_to", ""),
        )

    if channel == "sms" and provider == "esms":
        return SMSChannel(
            api_key=credentials.get("api_key", ""),
            secret_key=credentials.get("secret_key", ""),
            brandname=config.get("brandname", "NEXPO"),
        )

    if channel == "zns" and provider == "esms":
        return ZNSChannel(
            api_key=credentials.get("api_key", ""),
            secret_key=credentials.get("secret_key", ""),
            oa_id=credentials.get("oa_id", ""),
        )

    if channel == "zns" and provider == "zalo":
        return ZNSDirectZaloChannel(
            access_token=credentials.get("access_token", ""),
        )

    raise ValueError(f"Unsupported channel/provider combo: {channel}/{provider}")
