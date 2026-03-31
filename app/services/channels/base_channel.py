"""Base classes for the notification channel abstraction."""
from abc import ABC, abstractmethod
from typing import Optional
from pydantic import BaseModel


class NotificationRecipient(BaseModel):
    """Recipient info — channels use whichever field they need."""
    email: Optional[str] = None
    phone: Optional[str] = None  # format: 84xxxxxxxxx (no leading 0 or +)
    name: str = ""
    language: str = "vi"


class ChannelResult(BaseModel):
    """Result of a channel send attempt."""
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None
    channel: str = ""
    provider: str = ""


class NotificationChannel(ABC):
    """Abstract base for all notification channels (email, SMS, ZNS)."""

    channel_type: str = ""  # "email", "sms", "zns"
    provider_name: str = ""  # "mailgun", "esms", "zalo", "twilio"

    @abstractmethod
    async def send(self, recipient: NotificationRecipient, content: dict) -> ChannelResult:
        """Send a notification. content format varies per channel:
        - email: {"subject": str, "html": str}
        - sms: {"body": str}
        - zns: {"template_id": str, "params": dict}
        """

    @abstractmethod
    def validate_config(self, config: dict) -> bool:
        """Check if provider credentials are complete."""
