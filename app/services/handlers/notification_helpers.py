"""
Shared helpers for notification handlers — logging, template lookup, variable substitution.
"""
import re
from datetime import datetime, timezone, timedelta
from app.config import MAILGUN_DOMAIN
from app.services.directus import directus_get, directus_patch


async def append_meeting_notification_log(meeting_id: str, entries: list[dict]) -> None:
    """
    Append notification log entries to the meeting record.
    Each entry: { timestamp, trigger, channel, recipient_type, recipient, status, subject? }
    """
    if not entries:
        return
    try:
        resp = await directus_get(f"/items/meetings/{meeting_id}?fields[]=notification_log")
        existing = (resp.get("data") or {}).get("notification_log") or []
        if not isinstance(existing, list):
            existing = []
        updated_log = existing + entries
        await directus_patch(f"/items/meetings/{meeting_id}", {"notification_log": updated_log})
    except Exception:
        pass  # Never crash over logging


async def _resolve_language_chain(event_id: str) -> list[str | None]:
    """Language fallback chain for template lookup.

    Resolved decision (phase 9): event default → vi → en → no-filter.
    `None` at the end means "any language" — catches templates with null language_code.
    """
    default_lang: str | None = None
    try:
        resp = await directus_get(f"/items/events/{event_id}?fields[]=locale_override")
        default_lang = (resp.get("data") or {}).get("locale_override")
    except Exception:
        pass

    chain: list[str | None] = []
    if default_lang and default_lang not in ("vi", "en"):
        # Non-vi/en event default (rare) — try first
        chain.append(default_lang)
    chain.extend(["vi", "en", None])
    # dedupe preserving order
    seen: set = set()
    out: list[str | None] = []
    for item in chain:
        key = item or "__any__"
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


async def get_meeting_template(event_id: str, trigger_recipient: str, matching_type: str = "talent_matching") -> dict | None:
    """
    Fetch organizer-configured email template for (event_id, trigger_recipient, matching_type).

    Lookup order (post-phase-6):
      1. If USE_UNIFIED_EMAIL_TEMPLATES env flag is truthy → unified `email_templates` (module=meeting)
      2. Fallback to legacy `meeting_email_templates` (null matching_type = talent_matching)

    Returns dict with 'subject' and 'html_template' keys, or None.
    Shape normalized across both sources: `html_template` field always populated.
    """
    import os

    unified_on = os.getenv("USE_UNIFIED_EMAIL_TEMPLATES", "true").lower() in ("1", "true", "yes", "on")

    # ── Primary: unified `email_templates` collection with language fallback ──
    if unified_on:
        # Try each candidate language in order (event-default if known → vi → en → any)
        # Phase 9 resolved decision: event default → vi → en fallback chain
        candidate_langs = await _resolve_language_chain(event_id)
        for lang in candidate_langs:
            try:
                query = (
                    f"/items/email_templates"
                    f"?filter[event_id][_eq]={event_id}"
                    f"&filter[module][_eq]=meeting"
                    f"&filter[trigger_key][_eq]={trigger_recipient}"
                    f"&filter[matching_type][_eq]={matching_type}"
                    f"&filter[is_active][_eq]=true"
                    f"&fields[]=subject,html_compiled,mjml_source,language_code"
                    f"&sort[]=-date_updated&limit=1"
                )
                if lang:
                    query += f"&filter[language_code][_eq]={lang}"
                resp = await directus_get(query)
                items = resp.get("data") or []
                if items:
                    row = items[0]
                    html = row.get("html_compiled") or row.get("mjml_source") or ""
                    if html:
                        return {"subject": row.get("subject") or "", "html_template": html}
            except Exception:
                pass

    # ── Fallback: legacy `meeting_email_templates` ──
    try:
        resp = await directus_get(
            f"/items/meeting_email_templates"
            f"?filter[event_id][_eq]={event_id}"
            f"&filter[trigger_recipient][_eq]={trigger_recipient}"
            f"&filter[matching_type][_eq]={matching_type}"
            f"&fields[]=subject,html_template&limit=1"
        )
        items = resp.get("data") or []
        if items and items[0].get("html_template"):
            return items[0]
        # Legacy null matching_type (only for talent_matching)
        if matching_type == "talent_matching":
            resp2 = await directus_get(
                f"/items/meeting_email_templates"
                f"?filter[event_id][_eq]={event_id}"
                f"&filter[trigger_recipient][_eq]={trigger_recipient}"
                f"&filter[matching_type][_null]=true"
                f"&fields[]=subject,html_template&limit=1"
            )
            items2 = resp2.get("data") or []
            if items2 and items2[0].get("html_template"):
                return items2[0]
    except Exception:
        pass
    return None


async def _try_lang_chain(
    fetch_fn,  # callable: language_code -> dict | None (awaitable)
    requested_lang: str,
    event_locale_override: str | None = None,
) -> tuple[dict | None, str | None]:
    """Try template langs in priority order: requested → event override → vi → en.

    Returns (template_dict, matched_lang) or (None, None) if nothing found.
    Logs when fallback fires (i.e. exact match was not the winner).
    """
    import logging as _log_module
    _log = _log_module.getLogger(__name__)

    chain: list[str] = [requested_lang]
    if event_locale_override and event_locale_override not in chain:
        chain.append(event_locale_override)
    for fallback in ("vi", "en"):
        if fallback not in chain:
            chain.append(fallback)

    for lang in chain:
        tpl = await fetch_fn(lang)
        if tpl:
            if lang != requested_lang:
                _log.info(
                    "email template lang fallback: requested=%s picked=%s",
                    requested_lang, lang,
                )
            return tpl, lang
    return None, None


async def get_meeting_email_template_v2_or_legacy(
    event_id: str,
    trigger_key: str,
    matching_type: str | None,
    language_code: str,
) -> dict:
    """Return the best email template for a meeting notification.

    Priority:
      1. V2 row in `email_templates` (module=meeting, is_active=true) — uses
         language fallback chain: requested_lang → event.locale_override → vi → en.
      2. Fallback: existing `get_meeting_template` (legacy `meeting_email_templates`
         + `email_templates` unified path). Returned with source='legacy'.

    Return shape:
      {"source": "v2",     "template": {...email_templates row...}}
      {"source": "legacy", "template": {"subject": str, "html_template": str} | None}
    """
    # Fetch event locale for fallback chain (best-effort)
    event_locale: str | None = None
    try:
        ev_resp = await directus_get(f"/items/events/{event_id}?fields[]=locale_override")
        event_locale = (ev_resp.get("data") or {}).get("locale_override")
    except Exception:
        pass

    # ── Try V2 with language fallback chain ─────────────────────────────────────
    async def _fetch_v2_for_lang(lang: str) -> dict | None:
        try:
            query = (
                "/items/email_templates"
                f"?filter[event_id][_eq]={event_id}"
                "&filter[module][_eq]=meeting"
                f"&filter[trigger_key][_eq]={trigger_key}"
                f"&filter[is_active][_eq]=true"
                f"&filter[language_code][_eq]={lang}"
                "&fields[]=id,subject,html_compiled,mjml_source,language_code"
                "&sort[]=-date_updated&limit=1"
            )
            if matching_type:
                query += f"&filter[matching_type][_eq]={matching_type}"
            resp = await directus_get(query)
            items = resp.get("data") or []
            return items[0] if items else None
        except Exception:
            return None

    template, _picked = await _try_lang_chain(_fetch_v2_for_lang, language_code, event_locale)
    if template:
        return {"source": "v2", "template": template}

    # ── Fallback: legacy path (get_meeting_template handles its own lang chain) ─
    legacy = await get_meeting_template(event_id, trigger_key, matching_type or "talent_matching")
    return {"source": "legacy", "template": legacy}


def substitute(template: str, vars: dict) -> str:
    """Replace {{variable_name}} and ${variable_name} placeholders with values from vars dict.

    LEGACY flat-key substituter — kept for existing meeting_handler.py usage.
    For NEW code, use `template_render.safe_substitute(template, context, module)`
    which enforces per-module whitelist + HTML-escapes values. Migration to
    scoped syntax ({{recipient.full_name}} etc.) happens in plan phase 6.

    Supports both syntaxes:
     - {{company_name}}  → standard (used by new AI-generated and manual templates)
     - ${company_name}   → legacy (old AI-generated templates before the prompt fix)
    """
    def replacer(match: re.Match) -> str:
        key = match.group(1).strip()
        return str(vars.get(key, match.group(0)))

    result = re.sub(r"\{\{([^}]+)\}\}", replacer, template)   # {{var}} — primary
    result = re.sub(r"\$\{([^}]+)\}", replacer, result)        # ${var}  — legacy fallback
    return result
