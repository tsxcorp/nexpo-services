"""
Template Render — Safe variable substitution with whitelist enforcement.

Shared by form confirmation, meeting notification, and broadcast handlers.
Implements the contract defined in `packages/ui/src/email-templates/variable-whitelist.ts`.

Contract:
  {{scope.field}} → resolved from TemplateContext
  ${uuid}        → legacy form field syntax (backward compat)
"""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Iterable

from app.services.directus import directus_get

_VN_TZ = timezone(timedelta(hours=7))


def _format_vn_datetime(raw: Any) -> str:
    """Format Directus datetime as `HH:MM dd/mm/YYYY` in Vietnam local time.

    Handles both UTC ISO (`...Z`, `+00:00`) and naive (no tz) inputs:
      - UTC input → converted to VN time (+7h)
      - Naive input → assumed VN local already (legacy meetings.scheduled_at convention)

    Returns empty string on None / parse failure so templates don't print `None`.
    """
    if not raw:
        return ""
    try:
        s = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_VN_TZ)
        else:
            dt = dt.astimezone(_VN_TZ)
        return dt.strftime("%H:%M %d/%m/%Y")
    except Exception:
        return str(raw)

log = logging.getLogger(__name__)

# ── Authoritative whitelist (mirrors TypeScript variable-whitelist.ts) ────────

_EVENT_KEYS = {
    "event.name", "event.start_date", "event.end_date",
    "event.location", "event.venue",  # venue alias for location (common typo)
    "event.logo_url", "event.portal_url", "event.url",
}
_RECIPIENT_KEYS = {
    "recipient.full_name", "recipient.email", "recipient.phone_number",
    "recipient.company", "recipient.badge_id",
}
_MEETING_KEYS = {
    "meeting.scheduled_at", "meeting.location", "meeting.meeting_type",
    "meeting.duration_minutes", "meeting.portal_url",
}
_EXHIBITOR_KEYS = {
    "exhibitor.name", "exhibitor.booth", "exhibitor.booth_code", "exhibitor.email",
}

# Meeting module domain-specific alias for the registration / attendee.
# UI uses `{{visitor.*}}` for readability; context resolver mirrors registration fields.
_VISITOR_KEYS = {
    "visitor.full_name", "visitor.email", "visitor.phone_number",
    "visitor.company", "visitor.badge_id",
}

# Form-specific top-level vars (registration/QR/Insight Hub)
_FORM_REGISTRATION_KEYS = {
    "registration_id",
    "qr_code",
    "insight_hub_url",
    "registration.code",      # alias for registration_id (template-friendly)
    "registration.full_name", # alias for recipient.full_name
}

# Group registration vars — available in form_confirm_group templates.
# MVP: member names as comma-joined string (no full loop support in V2 MJML).
_FORM_GROUP_KEYS = {
    "group.member_names",
    "group.member_count",
}

# Broadcast-specific keys beyond the shared recipient set
_BROADCAST_EXTRA_KEYS = {
    "recipient.type",       # 'exhibitor' | 'visitor'
    "unsubscribe_url",      # injected per-recipient by broadcast router
}

# Static allow-list per module. Form module also accepts dynamic form.<uuid> keys
# which are validated at substitute time. Group keys are included in the form
# allow-list so group templates resolve {{group.member_names}} correctly;
# for non-group sends the context simply won't have a 'group' key and the
# placeholder substitutes to an empty string (safe default).
ALLOWED_KEYS_BY_MODULE: dict[str, set[str]] = {
    "form": _EVENT_KEYS | _RECIPIENT_KEYS | _FORM_REGISTRATION_KEYS | _FORM_GROUP_KEYS,
    "meeting": _EVENT_KEYS | _RECIPIENT_KEYS | _MEETING_KEYS | _EXHIBITOR_KEYS | _VISITOR_KEYS,
    "broadcast": _EVENT_KEYS | _RECIPIENT_KEYS | _BROADCAST_EXTRA_KEYS,
}

# UUID v4 loose regex for legacy ${uuid} form field lookup
_UUID_RE = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$", re.I)
# Matches both dotted keys (event.name) and flat keys (registration_id, qr_code, insight_hub_url)
_VAR_RE = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*(?:\.[a-z0-9_-]+)?)\s*\}\}", re.I)
_LEGACY_RE = re.compile(r"\$\{\s*([^}]+)\s*\}")


# ── Core substitution ────────────────────────────────────────────────────────

def safe_substitute(
    template: str,
    context: dict[str, Any],
    module: str,
    *,
    escape_html: bool = True,
) -> str:
    """Substitute `{{scope.field}}` placeholders with values from context.

    Args:
        template: MJML/HTML source with placeholders
        context: TemplateContext dict (event, recipient, meeting, exhibitor, form_answers)
        module: 'form' | 'meeting' | 'broadcast' — determines whitelist
        escape_html: HTML-escape replacement values (default True)

    Unknown or disallowed keys are left as literal placeholders with a warn log.
    Legacy `${uuid}` syntax is accepted for form module (deprecated, logs warn).
    """
    allowed = ALLOWED_KEYS_BY_MODULE.get(module, set())
    if not allowed:
        log.warning("safe_substitute: unknown module %r — substituting nothing", module)
        return template

    def replace_modern(match: re.Match[str]) -> str:
        key = match.group(1)
        # form.<uuid> is validated against context form_answers dict
        if key.startswith("form."):
            if module != "form":
                log.warning("safe_substitute: form.* key %r used outside form module", key)
                return match.group(0)
            field_id = key[len("form."):]
            value = (context.get("form_answers") or {}).get(field_id)
            return _format(value, escape_html)
        if key not in allowed:
            log.warning("safe_substitute: key %r not in whitelist for module %s", key, module)
            return match.group(0)
        # Flat top-level keys (registration_id, qr_code, insight_hub_url) — no dot traversal
        if "." not in key:
            return _format(context.get(key), escape_html)
        return _format(_resolve(context, key), escape_html)

    result = _VAR_RE.sub(replace_modern, template)

    # Legacy ${uuid} shim — only meaningful for form module
    def replace_legacy(match: re.Match[str]) -> str:
        token = match.group(1).strip()
        if not _UUID_RE.match(token):
            return match.group(0)  # leave unknown ${} literal
        if module != "form":
            log.info("safe_substitute: legacy ${%s} ignored outside form module", token)
            return match.group(0)
        log.info("safe_substitute: legacy ${%s} syntax — migrate to {{form.%s}}", token, token)
        value = (context.get("form_answers") or {}).get(token)
        return _format(value, escape_html)

    result = _LEGACY_RE.sub(replace_legacy, result)
    return result


def _resolve(context: dict[str, Any], dotted_key: str) -> Any:
    """Traverse dotted key path against context dict. Returns None on miss."""
    parts = dotted_key.split(".")
    node: Any = context
    for part in parts:
        if not isinstance(node, dict):
            return None
        node = node.get(part)
        if node is None:
            return None
    return node


def _format(value: Any, escape: bool) -> str:
    """Coerce to str + optionally HTML-escape. None → empty string."""
    if value is None:
        return ""
    s = str(value)
    return html.escape(s, quote=True) if escape else s


# ── Context hydration from Directus ──────────────────────────────────────────

async def build_context(
    module: str,
    *,
    event_id: str | None = None,
    registration_id: str | None = None,
    meeting_id: str | None = None,
    exhibitor_id: str | None = None,
    form_submission_id: str | None = None,
) -> dict[str, Any]:
    """Hydrate a TemplateContext dict from Directus for server-side rendering.

    Only fetches collections relevant to the module to avoid over-fetching.
    Safe defaults: missing records → empty sub-dicts (not None) so substituter
    produces empty strings rather than literal {{…}} placeholders.
    """
    ctx: dict[str, Any] = {"event": {}, "recipient": None}

    # Event — always needed
    if event_id:
        try:
            resp = await directus_get(
                f"/items/events/{event_id}"
                "?fields[]=id,name,start_date,end_date,location,email_style"
            )
            ev = resp.get("data") or {}
            style = ev.get("email_style") if isinstance(ev.get("email_style"), dict) else {}
            location = ev.get("location")
            ctx["event"] = {
                "name": ev.get("name"),
                "start_date": _format_vn_datetime(ev.get("start_date")),
                "end_date": _format_vn_datetime(ev.get("end_date")),
                "location": location,
                "venue": location,  # alias — templates commonly use {{event.venue}}
                "logo_url": style.get("logo_url"),
                "portal_url": f"https://portal.nexpo.vn/events/{event_id}",
                "url": f"https://app.nexpo.vn/events/{event_id}",  # public event page
            }
        except Exception as exc:
            log.warning("build_context: event fetch failed %s", exc)

    # Recipient (registration)
    if registration_id:
        try:
            resp = await directus_get(
                f"/items/registrations/{registration_id}"
                "?fields[]=id,full_name,email,phone_number,badge_id"
            )
            reg = resp.get("data") or {}
            recipient_block = {
                "full_name": reg.get("full_name"),
                "email": reg.get("email"),
                "phone_number": reg.get("phone_number"),
                "company": None,  # resolved from form_answers if present — see below
                "badge_id": reg.get("badge_id"),
            }
            ctx["recipient"] = recipient_block
            # Form module: registration.* alias block (templates use {{registration.code}}, {{registration.full_name}})
            if module == "form":
                ctx["registration"] = {
                    "code": reg.get("badge_id") or registration_id,
                    "full_name": reg.get("full_name"),
                    "email": reg.get("email"),
                }
            # Meeting module domain alias: `visitor.*` mirrors the registration/attendee.
            if module == "meeting":
                ctx["visitor"] = dict(recipient_block)
        except Exception as exc:
            log.warning("build_context: registration fetch failed %s", exc)

    # Meeting + exhibitor (matching module only)
    if module == "meeting" and meeting_id:
        try:
            resp = await directus_get(
                f"/items/meetings/{meeting_id}"
                "?fields[]=id,scheduled_at,location,meeting_type,duration_minutes,event_id"
            )
            m = resp.get("data") or {}
            ctx["meeting"] = {
                "scheduled_at": _format_vn_datetime(m.get("scheduled_at")),
                "location": m.get("location"),
                "meeting_type": m.get("meeting_type"),
                "duration_minutes": m.get("duration_minutes"),
                "portal_url": f"https://portal.nexpo.vn/meetings?event={m.get('event_id')}",
            }
        except Exception as exc:
            log.warning("build_context: meeting fetch failed %s", exc)

    if module == "meeting" and exhibitor_id:
        try:
            resp = await directus_get(
                f"/items/exhibitors/{exhibitor_id}"
                "?fields[]=id,booth_code,translations.company_name,translations.languages_code"
            )
            ex = resp.get("data") or {}
            translations = ex.get("translations") or []
            en = next((t for t in translations if t.get("languages_code") == "en-US"), {})
            vi = next((t for t in translations if t.get("languages_code") == "vi-VN"), {})
            ctx["exhibitor"] = {
                "name": en.get("company_name") or vi.get("company_name"),
                "booth": ex.get("booth_code"),
                "booth_code": ex.get("booth_code"),  # alias — common typo / preferred wording
                "email": None,
            }
        except Exception as exc:
            log.warning("build_context: exhibitor fetch failed %s", exc)

    # Form answers (form module only)
    if module == "form" and form_submission_id:
        try:
            resp = await directus_get(
                f"/items/form_submissions/{form_submission_id}"
                "?fields[]=id,answers.field.id,answers.value"
            )
            sub = resp.get("data") or {}
            answers: dict[str, Any] = {}
            for ans in (sub.get("answers") or []):
                field = ans.get("field") or {}
                fid = field.get("id")
                if fid:
                    answers[str(fid)] = ans.get("value")
            ctx["form_answers"] = answers
        except Exception as exc:
            log.warning("build_context: form submission fetch failed %s", exc)

    return ctx


def get_allowed_keys(module: str, form_field_ids: Iterable[str] = ()) -> set[str]:
    """Return full set of allowed keys for a module, incl. dynamic form fields."""
    keys = set(ALLOWED_KEYS_BY_MODULE.get(module, set()))
    if module == "form":
        for fid in form_field_ids:
            keys.add(f"form.{fid}")
    return keys
