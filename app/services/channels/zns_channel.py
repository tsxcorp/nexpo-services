"""ZNS channel — sends Zalo Notification Service messages via eSMS or direct Zalo API."""
import httpx
from app.services.channels.base_channel import (
    NotificationChannel,
    NotificationRecipient,
    ChannelResult,
)
from app.services.channels.sms_channel import normalize_phone


class ZNSChannel(NotificationChannel):
    """Send ZNS messages via eSMS.vn API (recommended) or direct Zalo API."""

    channel_type = "zns"
    provider_name = "esms"  # default, overridden by ZNSDirectZaloChannel

    ESMS_ZNS_URL = "https://rest.esms.vn/MainService.svc/json/SendZaloMessage_V5_post/"

    def __init__(self, api_key: str, secret_key: str, oa_id: str):
        self.api_key = api_key
        self.secret_key = secret_key
        self.oa_id = oa_id

    async def send(self, recipient: NotificationRecipient, content: dict) -> ChannelResult:
        """Send ZNS. content = {"template_id": str, "params": dict}"""
        if not recipient.phone:
            return ChannelResult(success=False, error="No phone number", channel="zns", provider="esms")

        template_id = content.get("template_id")
        params = content.get("params", {})
        if not template_id:
            return ChannelResult(success=False, error="No ZNS template_id", channel="zns", provider="esms")

        phone = normalize_phone(recipient.phone)

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(self.ESMS_ZNS_URL, json={
                    "ApiKey": self.api_key,
                    "SecretKey": self.secret_key,
                    "Phone": phone,
                    "TempID": template_id,
                    "TempData": params,
                    "OAID": self.oa_id,
                    "Sandbox": "0",
                })
                data = resp.json()

            code = data.get("CodeResult", "")
            if code == "100":
                return ChannelResult(
                    success=True,
                    message_id=data.get("SMSID"),
                    channel="zns",
                    provider="esms",
                )
            return ChannelResult(
                success=False,
                error=f"eSMS ZNS code {code}",
                channel="zns",
                provider="esms",
            )
        except Exception as e:
            return ChannelResult(success=False, error=str(e)[:200], channel="zns", provider="esms")

    def validate_config(self, config: dict) -> bool:
        return bool(config.get("api_key") and config.get("secret_key") and config.get("oa_id"))


class ZNSDirectZaloChannel(NotificationChannel):
    """Send ZNS messages directly via Zalo Business API (requires OAuth token management)."""

    channel_type = "zns"
    provider_name = "zalo"

    ZALO_ZNS_URL = "https://business.openapi.zalo.me/message/template"

    def __init__(self, access_token: str):
        self.access_token = access_token

    async def send(self, recipient: NotificationRecipient, content: dict) -> ChannelResult:
        """Send ZNS via direct Zalo API. content = {"template_id": str, "params": dict}"""
        if not recipient.phone:
            return ChannelResult(success=False, error="No phone number", channel="zns", provider="zalo")

        template_id = content.get("template_id")
        params = content.get("params", {})
        if not template_id:
            return ChannelResult(success=False, error="No ZNS template_id", channel="zns", provider="zalo")

        phone = normalize_phone(recipient.phone)

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    self.ZALO_ZNS_URL,
                    headers={"access_token": self.access_token},
                    json={
                        "phone": phone,
                        "template_id": template_id,
                        "template_data": params,
                    },
                )
                data = resp.json()

            if data.get("error") == 0:
                return ChannelResult(
                    success=True,
                    message_id=data.get("data", {}).get("msg_id"),
                    channel="zns",
                    provider="zalo",
                )
            return ChannelResult(
                success=False,
                error=f"Zalo error {data.get('error')}: {data.get('message', '')}",
                channel="zns",
                provider="zalo",
            )
        except Exception as e:
            return ChannelResult(success=False, error=str(e)[:200], channel="zns", provider="zalo")

    def validate_config(self, config: dict) -> bool:
        return bool(config.get("access_token"))
