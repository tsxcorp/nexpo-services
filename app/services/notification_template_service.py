"""Unified template lookup + per-channel rendering for notification system."""
import re
import html as html_mod
from app.services.directus import directus_get


async def get_and_render_template(
    trigger_type: str,
    channel: str,
    language: str,
    event_id: str | None,
    tenant_id: str | None,
    variables: dict,
) -> dict | None:
    """Fetch template from Directus, render for channel.

    Returns channel-ready content dict:
      - email: {"subject": str, "html": str}
      - sms: {"body": str}
      - zns: {"template_id": str, "params": dict}
    Returns None if no template found.
    """
    template = await _lookup_template(trigger_type, channel, language, event_id, tenant_id)
    if not template:
        return None

    match channel:
        case "email":
            return _render_email(template, variables)
        case "sms":
            return _render_sms(template, variables)
        case "zns":
            return _render_zns(template, variables)
    return None


async def _lookup_template(
    trigger_type: str,
    channel: str,
    language: str,
    event_id: str | None,
    tenant_id: str | None,
) -> dict | None:
    """Fetch template: event-specific → tenant-wide fallback."""
    # Try event-level first
    if event_id and tenant_id:
        tmpl = await _fetch_template(trigger_type, channel, language, tenant_id, event_id)
        if tmpl:
            return tmpl

    # Fallback: tenant-wide (event_id is null)
    if tenant_id:
        tmpl = await _fetch_template(trigger_type, channel, language, tenant_id, event_id=None)
        if tmpl:
            return tmpl

    return None


async def _fetch_template(
    trigger_type: str,
    channel: str,
    language: str,
    tenant_id: str,
    event_id: str | None,
) -> dict | None:
    """Fetch a single template from Directus."""
    try:
        filters = (
            f"filter[trigger_type][_eq]={trigger_type}"
            f"&filter[channel][_eq]={channel}"
            f"&filter[tenant_id][_eq]={tenant_id}"
            "&filter[is_active][_eq]=true"
        )
        if event_id:
            filters += f"&filter[event_id][_eq]={event_id}"
        else:
            filters += "&filter[event_id][_null]=true"

        # Try exact language match first
        resp = await directus_get(
            f"/items/notification_templates?{filters}"
            f"&filter[language][_eq]={language}"
            "&fields[]=subject,body_template,zns_template_id,zns_param_mapping,variables"
            "&limit=1"
        )
        items = resp.get("data") or []
        if items:
            return items[0]

        # Fallback: any language (vi default)
        resp = await directus_get(
            f"/items/notification_templates?{filters}"
            "&fields[]=subject,body_template,zns_template_id,zns_param_mapping,variables"
            "&limit=1"
        )
        items = resp.get("data") or []
        return items[0] if items else None
    except Exception:
        return None


def _render_email(template: dict, variables: dict) -> dict:
    """Render email template: substitute variables in subject + body."""
    subject = substitute_variables(template.get("subject") or "", variables)
    html_body = substitute_variables(template.get("body_template") or "", variables)
    return {"subject": subject, "html": html_body}


def _render_sms(template: dict, variables: dict) -> dict:
    """Render SMS template: substitute variables, plain text only."""
    body = substitute_variables(template.get("body_template") or "", variables)
    # Strip HTML tags for SMS
    body = re.sub(r"<[^>]+>", "", body)
    return {"body": body}


def _render_zns(template: dict, variables: dict) -> dict:
    """Render ZNS: map system variables to Zalo template params."""
    mapping = template.get("zns_param_mapping") or {}
    params = {}
    for zns_key, var_pattern in mapping.items():
        # Extract variable name from {{variable_name}} pattern
        var_name = var_pattern.strip("{}")
        params[zns_key] = str(variables.get(var_name, ""))
    return {
        "template_id": template.get("zns_template_id") or "",
        "params": params,
    }


def substitute_variables(template_str: str, variables: dict) -> str:
    """Replace {{variable}} placeholders with HTML-escaped values."""
    def replacer(m: re.Match) -> str:
        key = m.group(1).strip()
        val = variables.get(key)
        if val is None:
            return m.group(0)  # keep placeholder if no value
        return html_mod.escape(str(val))

    return re.sub(r"\{\{([^}]+)\}\}", replacer, template_str)
