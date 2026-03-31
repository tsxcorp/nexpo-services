"""Multi-channel notification dispatch — email, SMS, ZNS."""
from app.services.channels.base_channel import (
    NotificationChannel,
    NotificationRecipient,
    ChannelResult,
)
from app.services.channels.email_channel import EmailChannel
from app.services.channels.sms_channel import SMSChannel
from app.services.channels.zns_channel import ZNSChannel
from app.services.channels.channel_factory import build_channel

__all__ = [
    "NotificationChannel",
    "NotificationRecipient",
    "ChannelResult",
    "EmailChannel",
    "SMSChannel",
    "ZNSChannel",
    "build_channel",
]
