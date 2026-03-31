"""SMS channel — sends via eSMS.vn API."""
import httpx
from app.services.channels.base_channel import (
    NotificationChannel,
    NotificationRecipient,
    ChannelResult,
)


class SMSChannel(NotificationChannel):
    """Send SMS notifications via eSMS.vn."""

    channel_type = "sms"
    provider_name = "esms"

    ESMS_URL = "https://rest.esms.vn/MainService.svc/json/SendMultipleMessage_V4_post_json/"
    SMS_TYPE_BRANDNAME = "2"

    def __init__(self, api_key: str, secret_key: str, brandname: str = "NEXPO"):
        self.api_key = api_key
        self.secret_key = secret_key
        self.brandname = brandname

    async def send(self, recipient: NotificationRecipient, content: dict) -> ChannelResult:
        """Send SMS. content = {"body": str}"""
        if not recipient.phone:
            return ChannelResult(success=False, error="No phone number", channel="sms", provider="esms")

        body = content.get("body", "")
        if not body:
            return ChannelResult(success=False, error="Empty SMS body", channel="sms", provider="esms")

        phone = normalize_phone(recipient.phone)

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(self.ESMS_URL, json={
                    "ApiKey": self.api_key,
                    "SecretKey": self.secret_key,
                    "Phone": phone,
                    "Content": body,
                    "SmsType": self.SMS_TYPE_BRANDNAME,
                    "Brandname": self.brandname,
                    "Sandbox": "0",
                })
                data = resp.json()

            code = data.get("CodeResult", "")
            if code == "100":
                return ChannelResult(
                    success=True,
                    message_id=data.get("SMSID"),
                    channel="sms",
                    provider="esms",
                )
            return ChannelResult(
                success=False,
                error=f"eSMS code {code}: {ESMS_ERROR_CODES.get(code, 'Unknown')}",
                channel="sms",
                provider="esms",
            )
        except Exception as e:
            return ChannelResult(success=False, error=str(e)[:200], channel="sms", provider="esms")

    def validate_config(self, config: dict) -> bool:
        return bool(config.get("api_key") and config.get("secret_key"))


def normalize_phone(phone: str) -> str:
    """Convert +84xxx, 0xxx, or 84xxx to 84xxx format for eSMS."""
    phone = phone.strip().replace(" ", "").replace("-", "").replace("+", "")
    if phone.startswith("0"):
        phone = "84" + phone[1:]
    return phone


# eSMS error code reference
ESMS_ERROR_CODES = {
    "100": "Success",
    "101": "Authentication failure",
    "102": "Account locked",
    "103": "Insufficient balance",
    "104": "Invalid brandname",
    "118": "Invalid SMS type",
    "119": "Brandname not registered for this content",
    "131": "Max message length exceeded",
    "789": "Template not configured",
}
