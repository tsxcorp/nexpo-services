"""
Registration QR email handler — fetch registration, render template, send QR email, log activity.
"""
import re as _re
import json as _json
from html import escape as _esc
from datetime import datetime, timezone as _tz

from app.services.directus import directus_get, directus_post
from app.services.mailgun import send_mailgun
from app.services.qr_service import generate_qr_code_bytes, append_qr_cid_to_html, inject_qr_extras
from app.config import MAILGUN_API_KEY, MAILGUN_DOMAIN, MAILGUN_API_URL
from app.services.handlers.template_render import safe_substitute, build_context


def _format_field_value(value: str, field_type: str, option_map: dict) -> str:
    """Returns safe HTML snippet. Each text segment is HTML-escaped; multi-values joined with <br>."""
    if not value:
        return value
    if field_type == "date":
        m = _re.match(r"^(\d{4})-(\d{2})-(\d{2})", value)
        if m:
            return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
    if field_type in ("datetime", "timestamp"):
        try:
            d = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return d.strftime("%d/%m/%Y %H:%M")
        except Exception:
            pass
    if field_type in ("select", "radio") and option_map:
        return _esc(option_map.get(value, value))
    if field_type == "collection_picker":
        try:
            obj = _json.loads(value)
            snaps = obj.get("snapshots") or []
            labels = [s.get("label") for s in snaps if s.get("label")]
            if labels:
                return "<br>".join(_esc(str(x)) for x in labels)
            return "<br>".join(_esc(str(x)) for x in (obj.get("ids") or []))
        except Exception:
            return _esc(value)
    if field_type in ("multiselect", "checkbox", "dietary"):
        try:
            arr = _json.loads(value)
            if isinstance(arr, list):
                parts = [str(x) for x in arr if x]
                if option_map:
                    parts = [option_map.get(p, p) for p in parts]
                return "<br>".join(_esc(p) for p in parts)
        except Exception:
            pass
        parts = [v.strip() for v in value.split(",") if v.strip()]
        if option_map:
            parts = [option_map.get(p, p) for p in parts]
        return "<br>".join(_esc(p) for p in parts)
    return _esc(value).replace("\n", "<br>")


async def _log_reg_activity(
    registration_id: str, status: str, recipient: str,
    subject: str, triggered_by: str, error_message: str | None = None,
) -> None:
    """Log a registration email activity to Directus. Silent — never raises."""
    try:
        payload: dict = {
            "registration_id": registration_id, "channel": "email",
            "action": "qr_email", "status": status, "recipient": recipient,
            "subject": subject, "triggered_by": triggered_by,
            "date_created": datetime.now(_tz.utc).isoformat(),
        }
        if error_message:
            payload["error_message"] = error_message
        await directus_post("/items/registration_activities", payload)
    except Exception:
        pass


async def get_form_email_template_v2_or_legacy(
    event_id: str,
    form_id: str,
    trigger_key: str,
    language_code: str,
) -> dict:
    """Return V2 email_templates row if active, else signal legacy fallback.

    Uses language fallback chain: requested_lang → event.locale_override → vi → en.

    Return shape:
      {"source": "v2",     "template": {...email_templates row...}}
      {"source": "legacy", "template": None}   # caller uses forms.template_email
    """
    from app.services.handlers.notification_helpers import _try_lang_chain

    # Fetch event locale for fallback chain (best-effort)
    event_locale: str | None = None
    try:
        ev_resp = await directus_get(f"/items/events/{event_id}?fields[]=locale_override")
        event_locale = (ev_resp.get("data") or {}).get("locale_override")
    except Exception:
        pass

    async def _fetch_for_lang(lang: str) -> dict | None:
        try:
            resp = await directus_get(
                "/items/email_templates"
                f"?filter[event_id][_eq]={event_id}"
                f"&filter[form_id][_eq]={form_id}"
                "&filter[module][_eq]=form"
                f"&filter[trigger_key][_eq]={trigger_key}"
                f"&filter[language_code][_eq]={lang}"
                "&filter[is_active][_eq]=true"
                "&fields[]=id,subject,sender_name,html_compiled,mjml_source"
                "&sort[]=-date_updated&limit=1"
            )
            items = resp.get("data") or []
            return items[0] if items else None
        except Exception as exc:
            import logging
            logging.getLogger(__name__).info(
                "[registration_handler] v2 template lookup failed: %s", exc
            )
            return None

    template, _picked = await _try_lang_chain(_fetch_for_lang, language_code, event_locale)
    if template:
        return {"source": "v2", "template": template}
    return {"source": "legacy", "template": None}


async def handle_group_registration_qr(
    lead_registration_id: str,
    triggered_by: str = "admin",
) -> dict:
    """
    Send a group-confirmation email to the lead registrant.

    Dispatches V2 template when an active email_templates row exists for
    trigger_key='form_confirm_group', otherwise falls back to the legacy
    forms.template_email_group HTML.

    Group context injected into the template:
      {{group.member_names}}  — comma-joined full_names of all members
      {{group.member_count}}  — total number of group members

    Returns: { "email": str, "status": "sent" | "failed", "error"?: str }
    """
    try:
        # 1. Fetch lead registration (must have group_id + is_lead)
        lead_resp = await directus_get(
            f"/items/registrations/{lead_registration_id}"
            f"?fields[]=id,event_id,submissions,group_id,full_name,email"
        )
        lead = lead_resp.get("data") or {}
        if not lead:
            return {"email": "", "status": "failed", "error": f"Registration {lead_registration_id} not found"}

        event_id = str(lead.get("event_id") or "")
        group_id = lead.get("group_id")
        sub_id = lead.get("submissions")
        if isinstance(sub_id, dict):
            sub_id = sub_id.get("id", "")

        if not group_id:
            # Not a group registration — delegate to individual handler
            return await handle_registration_qr(lead_registration_id, triggered_by)

        # 2. Fetch all registrations in this group (for member list)
        members_resp = await directus_get(
            f"/items/registrations"
            f"?filter[group_id][_eq]={group_id}"
            f"&fields[]=id,full_name&limit=-1"
        )
        members = members_resp.get("data") or []
        member_names = [m.get("full_name") or "" for m in members if m.get("full_name")]
        member_count = len(members)

        # 3. Resolve form config (same as individual flow)
        form_resp = await directus_get(
            f"/items/forms?filter[event_id][_eq]={event_id}"
            f"&filter[is_registration][_eq]=true&filter[status][_eq]=published"
            f"&fields[]=id,template_email_group,email_subject,email_sender_name&limit=1"
        )
        forms = form_resp.get("data") or []
        if not forms:
            return {"email": "", "status": "failed", "error": "No published registration form found"}
        form = forms[0]
        form_id = form.get("id")

        # Determine send language
        send_lang = "vi"
        try:
            ev_lang_resp = await directus_get(f"/items/events/{event_id}?fields[]=locale_override,name")
            ev_data = ev_lang_resp.get("data") or {}
            locale = ev_data.get("locale_override") or "vi"
            send_lang = locale if locale in ("vi", "en") else "vi"
            event_name = ev_data.get("name") or ""
        except Exception:
            event_name = ""

        email_subject: str = form.get("email_subject") or "Group Registration Confirmation"
        sender_name: str = form.get("email_sender_name") or "Nexpo"
        email_subject = email_subject.replace("{event_name}", event_name)

        # 4. V2-or-legacy template resolution (trigger: form_confirm_group)
        html_template: str = ""
        use_v2 = False

        v2_result = await get_form_email_template_v2_or_legacy(
            event_id=event_id,
            form_id=form_id,
            trigger_key="form_confirm_group",
            language_code=send_lang,
        )
        if v2_result["source"] == "v2":
            v2_row = v2_result["template"]
            html_template = v2_row.get("html_compiled") or v2_row.get("mjml_source") or ""
            if v2_row.get("subject"):
                email_subject = v2_row["subject"]
            if v2_row.get("sender_name"):
                sender_name = v2_row["sender_name"]
            use_v2 = bool(html_template)

        # Legacy fallback: forms.template_email_group
        if not use_v2:
            html_template = form.get("template_email_group") or ""

        if not html_template:
            return {
                "email": "", "status": "failed",
                "error": "No group email template configured (V2 form_confirm_group or legacy template_email_group)",
            }

        # 5. Resolve lead email (from registration.email or email form field)
        recipient_email = lead.get("email") or ""
        if not recipient_email and sub_id:
            # Attempt to read email from form answers
            form_fields_resp = await directus_get(
                f"/items/form_fields?filter[form_id][_eq]={form_id}"
                f"&filter[is_email_contact][_eq]=true&fields[]=id&limit=1"
            )
            email_field_id = ((form_fields_resp.get("data") or [{}])[0] or {}).get("id")
            if email_field_id:
                ans_resp = await directus_get(
                    f"/items/form_answers?filter[submission][_eq]={sub_id}"
                    f"&filter[field][_eq]={email_field_id}&fields[]=value&limit=1"
                )
                recipient_email = ((ans_resp.get("data") or [{}])[0] or {}).get("value") or ""

        if not recipient_email:
            await _log_reg_activity(
                lead_registration_id, "failed", "", email_subject, triggered_by, "No email address found"
            )
            return {"email": "", "status": "failed", "error": "No email address found for group lead"}

        # 6. Build context + render
        html = html_template
        if use_v2:
            ctx = await build_context(
                "form",
                event_id=event_id,
                registration_id=lead_registration_id,
                form_submission_id=str(sub_id) if sub_id else None,
            )
            ctx["registration_id"] = lead_registration_id
            ctx["qr_code"] = "cid:qrcode.png"
            ctx["insight_hub_url"] = f"https://insight.nexpo.vn/{lead_registration_id}"
            # Inject group context — {{group.member_names}} and {{group.member_count}}
            ctx["group"] = {
                "member_names": ", ".join(member_names),
                "member_count": str(member_count),
            }
            html = safe_substitute(html, ctx, "form")
        else:
            # Legacy: simple string replace for member_names if organizer uses it
            html = html_template.replace("{{group.member_names}}", ", ".join(member_names))
            html = html.replace("{{group.member_count}}", str(member_count))

        # 7. Send via Mailgun with inline QR
        from_email = f"{sender_name} <no-reply@m.nexpo.vn>"
        qr_bytes = generate_qr_code_bytes(lead_registration_id)
        html_with_qr = append_qr_cid_to_html(html)
        html_with_qr = inject_qr_extras(html_with_qr, lead_registration_id)

        import httpx
        email_sent = False
        error_msg: str | None = None
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                mg_resp = await client.post(
                    f"{MAILGUN_API_URL}/v3/{MAILGUN_DOMAIN}/messages",
                    auth=("api", MAILGUN_API_KEY),
                    data={"from": from_email, "to": recipient_email, "subject": email_subject, "html": html_with_qr},
                    files=[("inline", ("qrcode.png", qr_bytes, "image/png"))],
                )
                if mg_resp.is_success:
                    email_sent = True
                else:
                    error_msg = f"Mailgun {mg_resp.status_code}: {mg_resp.text[:200]}"
        except Exception as e:
            error_msg = str(e)[:200]

        status = "success" if email_sent else "failed"
        await _log_reg_activity(lead_registration_id, status, recipient_email, email_subject, triggered_by, error_msg)

        return {
            "email": recipient_email,
            "status": "sent" if email_sent else "failed",
            **({"error": error_msg} if error_msg else {}),
        }

    except Exception as e:
        try:
            await _log_reg_activity(lead_registration_id, "failed", "", "", triggered_by, str(e)[:200])
        except Exception:
            pass
        return {"email": "", "status": "failed", "error": str(e)[:200]}


async def handle_registration_qr(registration_id: str, triggered_by: str = "admin") -> dict:
    """
    Fetch registration → resolve email + render HTML template → send QR email → log activity.
    Returns: { "email": str, "status": "sent" | "failed", "error"?: str }
    """
    try:
        # 1. Get registration + submission id
        reg_resp = await directus_get(
            f"/items/registrations/{registration_id}?fields[]=id,event_id,submissions"
        )
        reg = reg_resp.get("data") or {}
        if not reg:
            return {"email": "", "status": "failed", "error": f"Registration {registration_id} not found"}

        event_id = str(reg.get("event_id") or "")
        sub_id = reg.get("submissions")
        if isinstance(sub_id, dict):
            sub_id = sub_id.get("id", "")

        # 2. Get form config — try unified email_templates first, fallback to legacy forms fields
        form_resp = await directus_get(
            f"/items/forms?filter[event_id][_eq]={event_id}"
            f"&filter[is_registration][_eq]=true&filter[status][_eq]=published"
            f"&fields[]=id,template_email,email_subject,email_sender_name&limit=1"
        )
        forms = form_resp.get("data") or []
        if not forms:
            return {"email": "", "status": "failed", "error": "No published registration form found"}
        form = forms[0]
        form_id = form.get("id")

        # Determine language from submission (best effort: check event locale, default 'vi')
        send_lang = "vi"
        try:
            ev_lang_resp = await directus_get(f"/items/events/{event_id}?fields[]=locale_override")
            locale = (ev_lang_resp.get("data") or {}).get("locale_override") or "vi"
            send_lang = locale if locale in ("vi", "en") else "vi"
        except Exception:
            pass

        # V2-or-legacy template resolution (trigger: form_confirm)
        html_template: str = ""
        email_subject: str = form.get("email_subject") or "Registration Confirmation"
        sender_name: str = form.get("email_sender_name") or "Nexpo"
        use_v2 = False

        v2_result = await get_form_email_template_v2_or_legacy(
            event_id=event_id,
            form_id=form_id,
            trigger_key="form_confirm",
            language_code=send_lang,
        )
        if v2_result["source"] == "v2":
            v2_row = v2_result["template"]
            html_template = v2_row.get("html_compiled") or v2_row.get("mjml_source") or ""
            if v2_row.get("subject"):
                email_subject = v2_row["subject"]
            if v2_row.get("sender_name"):
                sender_name = v2_row["sender_name"]
            use_v2 = bool(html_template)

        # Legacy fallback when no active V2 row exists
        if not use_v2:
            html_template = form.get("template_email") or ""
            # Also apply safe_substitute on legacy templates so organizers can use
            # {{event.name}}, {{recipient.full_name}}, etc. alongside legacy ${uuid} syntax.
            # This is resolved later in step 6 after answer_by_field is built.

        # 3. Get event name for subject substitution
        try:
            ev_resp = await directus_get(f"/items/events/{event_id}?fields[]=name")
            event_name = (ev_resp.get("data") or {}).get("name") or ""
            email_subject = email_subject.replace("{event_name}", event_name)
        except Exception:
            event_name = ""

        from_email = f"{sender_name} <no-reply@m.nexpo.vn>"

        # 4. Get form fields (for type + option maps)
        fields_resp = await directus_get(
            f"/items/form_fields?filter[form_id][_eq]={form_id}"
            f"&fields[]=id,name,type,is_email_contact,translations.languages_code,translations.options"
            f"&limit=-1"
        )
        form_fields = fields_resp.get("data") or []
        email_field_id: str | None = None
        field_type_map: dict[str, str] = {}
        field_option_map: dict[str, dict] = {}

        for f in form_fields:
            fid = f.get("id", "")
            ftype = f.get("type", "")
            field_type_map[fid] = ftype
            if f.get("is_email_contact"):
                email_field_id = fid
            if ftype in ("select", "multiselect", "radio", "checkbox"):
                translations = f.get("translations") or []
                preferred = (
                    next((t for t in translations if t.get("languages_code") == "vi-VN"), None)
                    or next((t for t in translations if t.get("languages_code") == "en-US"), None)
                    or (translations[0] if translations else {})
                )
                opts = (preferred or {}).get("options") or []
                field_option_map[fid] = {o["value"]: o["label"] for o in opts if "value" in o and "label" in o}

        if not email_field_id:
            return {"email": "", "status": "failed", "error": "No email contact field (is_email_contact=true) on form"}

        # 5. Get form answers
        recipient_email = ""
        answers: list[dict] = []
        if sub_id:
            ans_resp = await directus_get(
                f"/items/form_answers?filter[submission][_eq]={sub_id}&fields[]=field,value&limit=-1"
            )
            answers = ans_resp.get("data") or []
            for ans in answers:
                if ans.get("field") == email_field_id and ans.get("value", "").strip():
                    recipient_email = ans["value"].strip()
                    break

        # Fallback: registration.email
        if not recipient_email:
            reg_email_resp = await directus_get(f"/items/registrations/{registration_id}?fields[]=email")
            recipient_email = (reg_email_resp.get("data") or {}).get("email") or ""

        if not recipient_email:
            await _log_reg_activity(registration_id, "failed", "", email_subject, triggered_by, "No email address found")
            return {"email": "", "status": "failed", "error": "No email address found"}

        # 6. Render HTML template — V2 uses safe_substitute; legacy uses ${uuid} substitution
        html = html_template
        answer_by_field: dict[str, str] = {}
        for ans in answers:
            fid = ans.get("field", "")
            val = _format_field_value(ans.get("value") or "", field_type_map.get(fid, ""), field_option_map.get(fid, {}))
            answer_by_field[fid] = val

        if use_v2:
            # Build context and use whitelist-enforced safe_substitute
            ctx = await build_context(
                "form",
                event_id=event_id,
                registration_id=registration_id,
                form_submission_id=str(sub_id) if sub_id else None,
            )
            # Also inject registration_id, qr_code, insight_hub_url as top-level vars
            ctx["registration_id"] = registration_id
            ctx["qr_code"] = "cid:qrcode.png"
            ctx["insight_hub_url"] = f"https://insight.nexpo.vn/{registration_id}"
            # Merge form answers for {{form.<uuid>}} resolution
            if "form_answers" not in ctx:
                ctx["form_answers"] = answer_by_field
            html = safe_substitute(html, ctx, "form")
        else:
            # Legacy path — dual-pass substitution so organizers can use BOTH syntaxes:
            #   Pass 1: {{scope.field}} V2 tokens via safe_substitute (event.name, recipient.*)
            #   Pass 2: legacy ${uuid} field refs (backward compat)
            # Build context for V2 token pass
            legacy_ctx = await build_context(
                "form",
                event_id=event_id,
                registration_id=registration_id,
                form_submission_id=str(sub_id) if sub_id else None,
            )
            # Top-level form-specific vars
            legacy_ctx["registration_id"] = registration_id
            legacy_ctx["qr_code"] = "cid:qrcode.png"
            legacy_ctx["insight_hub_url"] = f"https://insight.nexpo.vn/{registration_id}"
            # NOTE: Do NOT populate form_answers here — answer_by_field values are already
            # HTML-escaped (from _format_field_value). Passing them to safe_substitute would
            # double-escape. Legacy ${uuid} pass2 below handles form field substitution directly.
            # Pass 1: resolve V2 {{event.*}}, {{recipient.*}}, {{registration_id}}, etc.
            html = safe_substitute(html, legacy_ctx, "form")

            # Pass 2: legacy ${uuid} field substitution
            for fid, val in answer_by_field.items():
                html = html.replace(f"${{{fid}}}", val)

            # Replace <span data-field-id="UUID">...</span> with formatted answer
            _SPAN_RE = _re.compile(
                r'<span\b[^>]*?data-field-id=["\']([0-9a-f\-]{36})["\'][^>]*>.*?</span>',
                _re.DOTALL | _re.IGNORECASE,
            )
            html = _SPAN_RE.sub(lambda m: answer_by_field.get(m.group(1), ""), html)
            # Clear remaining unfilled ${uuid} placeholders
            html = _re.sub(r"\$\{[0-9a-f\-]{36}\}", "", html)

        # 7. Send via Mailgun with inline QR
        qr_bytes = generate_qr_code_bytes(registration_id)
        html_with_qr = append_qr_cid_to_html(html)
        html_with_qr = inject_qr_extras(html_with_qr, registration_id)

        import httpx
        email_sent = False
        error_msg: str | None = None
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                mg_resp = await client.post(
                    f"{MAILGUN_API_URL}/v3/{MAILGUN_DOMAIN}/messages",
                    auth=("api", MAILGUN_API_KEY),
                    data={"from": from_email, "to": recipient_email, "subject": email_subject, "html": html_with_qr},
                    files=[("inline", ("qrcode.png", qr_bytes, "image/png"))],
                )
                if mg_resp.is_success:
                    email_sent = True
                else:
                    error_msg = f"Mailgun {mg_resp.status_code}: {mg_resp.text[:200]}"
        except Exception as e:
            error_msg = str(e)[:200]

        # 8. Log activity
        status = "success" if email_sent else "failed"
        await _log_reg_activity(registration_id, status, recipient_email, email_subject, triggered_by, error_msg)

        return {
            "email": recipient_email,
            "status": "sent" if email_sent else "failed",
            **({"error": error_msg} if error_msg else {}),
        }

    except Exception as e:
        try:
            await _log_reg_activity(registration_id, "failed", "", "", triggered_by, str(e)[:200])
        except Exception:
            pass
        return {"email": "", "status": "failed", "error": str(e)[:200]}
