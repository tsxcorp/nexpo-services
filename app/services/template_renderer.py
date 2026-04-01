"""
Template Renderer — Fetch, render, and substitute email templates.

Supports the new trigger + recipient_role schema with fallback to
legacy trigger_recipient column and hardcoded defaults.
"""

import re
import html
from app.services.directus import directus_get


async def get_template(
    event_id: str,
    trigger: str,
    recipient_role: str,
    matching_type: str = "talent_matching",
) -> dict | None:
    """
    Fetch email template from Directus with fallback chain:
    1. New schema: trigger + recipient_role + matching_type
    2. Legacy: trigger_recipient column
    3. Legacy: null matching_type (for talent_matching)
    Returns dict with 'subject' and 'html_template', or None.
    """
    try:
        # 1. Try new schema
        resp = await directus_get(
            f"/items/meeting_email_templates"
            f"?filter[event_id][_eq]={event_id}"
            f"&filter[trigger][_eq]={trigger}"
            f"&filter[recipient_role][_eq]={recipient_role}"
            f"&filter[matching_type][_eq]={matching_type}"
            f"&filter[is_active][_eq]=true"
            f"&fields[]=subject,html_template&limit=1"
        )
        items = resp.get("data") or []
        if items and items[0].get("html_template"):
            return items[0]

        # 2. Fallback: legacy trigger_recipient
        legacy_key = _get_legacy_key(trigger, recipient_role)
        if legacy_key:
            resp2 = await directus_get(
                f"/items/meeting_email_templates"
                f"?filter[event_id][_eq]={event_id}"
                f"&filter[trigger_recipient][_eq]={legacy_key}"
                f"&filter[matching_type][_eq]={matching_type}"
                f"&fields[]=subject,html_template&limit=1"
            )
            items2 = resp2.get("data") or []
            if items2 and items2[0].get("html_template"):
                return items2[0]

        # 3. Fallback: null matching_type (legacy talent_matching records)
        if matching_type == "talent_matching" and legacy_key:
            resp3 = await directus_get(
                f"/items/meeting_email_templates"
                f"?filter[event_id][_eq]={event_id}"
                f"&filter[trigger_recipient][_eq]={legacy_key}"
                f"&filter[matching_type][_null]=true"
                f"&fields[]=subject,html_template&limit=1"
            )
            items3 = resp3.get("data") or []
            if items3 and items3[0].get("html_template"):
                return items3[0]
    except Exception:
        pass
    return None


def render(template_html: str, variables: dict) -> str:
    """
    Replace {{variable}} and ${variable} placeholders with HTML-escaped values.
    Supports both syntaxes for backward compatibility.
    """
    def replacer(match: re.Match) -> str:
        key = match.group(1).strip()
        value = variables.get(key)
        if value is None:
            return match.group(0)
        return html.escape(str(value))

    result = re.sub(r"\{\{([^}]+)\}\}", replacer, template_html)
    result = re.sub(r"\$\{([^}]+)\}", replacer, result)
    return result


def sanitize_subject(subject: str) -> str:
    """Remove CRLF characters to prevent email header injection."""
    return subject.replace("\r", "").replace("\n", "")


def _get_legacy_key(trigger: str, recipient_role: str) -> str | None:
    """Map new trigger + recipient_role to legacy trigger_recipient column value."""
    mapping = {
        ("meeting_scheduled", "exhibitor"): "scheduled_exhibitor",
        ("meeting_confirmed", "visitor"): "confirmed_visitor",
        ("meeting_cancelled", "visitor"): "cancelled_visitor",
        ("meeting_cancelled", "exhibitor"): "cancelled_exhibitor",
    }
    return mapping.get((trigger, recipient_role))
