"""
Directus API client helpers.
All functions use DIRECTUS_ADMIN_TOKEN for privileged access.
"""
import httpx
from app.config import DIRECTUS_URL, DIRECTUS_ADMIN_TOKEN


# ── Generic HTTP helpers ──────────────────────────────────────────────────────

async def directus_get(path: str) -> dict:
    """GET from Directus using admin token."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{DIRECTUS_URL}{path}",
            headers={"Authorization": f"Bearer {DIRECTUS_ADMIN_TOKEN}"},
        )
        resp.raise_for_status()
        return resp.json()


async def directus_post(path: str, data: dict) -> dict:
    """POST to Directus using admin token."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{DIRECTUS_URL}{path}",
            headers={
                "Authorization": f"Bearer {DIRECTUS_ADMIN_TOKEN}",
                "Content-Type": "application/json",
            },
            json=data,
        )
        resp.raise_for_status()
        return resp.json()


async def directus_patch(path: str, data: dict) -> dict:
    """PATCH to Directus using admin token."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.patch(
            f"{DIRECTUS_URL}{path}",
            headers={
                "Authorization": f"Bearer {DIRECTUS_ADMIN_TOKEN}",
                "Content-Type": "application/json",
            },
            json=data,
        )
        resp.raise_for_status()
        return resp.json()


# ── Notifications ─────────────────────────────────────────────────────────────

async def create_notification(
    user_id: str,
    title: str,
    body: str = None,
    link: str = None,
    notif_type: str = None,
    entity_type: str = None,
    entity_id: str = None,
) -> None:
    """Create an in-app notification record in Directus. Silent — never raises."""
    try:
        payload = {"user_id": user_id, "title": title}
        if body:
            payload["body"] = body
        if link:
            payload["link"] = link
        if notif_type:
            payload["type"] = notif_type
        if entity_type:
            payload["entity_type"] = entity_type
        if entity_id:
            payload["entity_id"] = entity_id
        await directus_post("/items/notifications", payload)
    except Exception:
        pass  # never crash the caller over a notification


# ── Email / contact resolvers ─────────────────────────────────────────────────

async def resolve_visitor_email(registration_id: str) -> tuple[str, str]:
    """Returns (email, full_name). Checks form answers first, falls back to registrations.email."""
    try:
        reg_resp = await directus_get(
            f"/items/registrations/{registration_id}"
            "?fields[]=id,full_name,email"
        )
        reg = reg_resp.get("data", {})
        fallback_email = reg.get("email", "")
        full_name = reg.get("full_name", "")

        subs_resp = await directus_get(
            f"/items/form_submissions"
            f"?filter[registration_id][_eq]={registration_id}"
            "&fields[]=answers.value,answers.field.is_email_contact"
            "&limit=10"
        )
        for sub in subs_resp.get("data", []):
            for ans in sub.get("answers", []):
                field = ans.get("field") or {}
                if field.get("is_email_contact") and ans.get("value", "").strip():
                    return ans["value"].strip(), full_name
        return fallback_email, full_name
    except Exception:
        return "", ""


async def resolve_exhibitor_email(exhibitor_id: str, event_id: str) -> tuple[str, str]:
    """Returns (email, company_name). Uses exhibitor_events.representative_email first,
    falls back to exhibitors table directly if no exhibitor_events record found."""
    try:
        ee_resp = await directus_get(
            f"/items/exhibitor_events"
            f"?filter[exhibitor_id][_eq]={exhibitor_id}"
            f"&filter[event_id][_eq]={event_id}"
            "&fields[]=representative_email,nameboard,exhibitor_id.representative_email,"
            "exhibitor_id.user_id.email,exhibitor_id.translations.company_name,"
            "exhibitor_id.translations.languages_code"
            "&limit=1"
        )
        items = ee_resp.get("data", [])

        if items:
            ee = items[0]
            ex = ee.get("exhibitor_id") or {}
            email = (
                ee.get("representative_email")
                or ex.get("representative_email")
                or (ex.get("user_id") or {}).get("email")
                or ""
            )
            translations = ex.get("translations") or []
            t = next((t for t in translations if t.get("languages_code") == "vi-VN"), None) or (translations[0] if translations else {})
            company_name = t.get("company_name") or ee.get("nameboard") or ""
            return email, company_name

        # ── Fallback: query exhibitors table directly ──────────────────────────
        # This handles exhibitors that exist but have no exhibitor_events record yet
        ex_resp = await directus_get(
            f"/items/exhibitors/{exhibitor_id}"
            "?fields[]=representative_email,user_id.email,"
            "translations.company_name,translations.languages_code"
        )
        ex = ex_resp.get("data") or {}
        email = (
            ex.get("representative_email")
            or (ex.get("user_id") or {}).get("email")
            or ""
        )
        translations = ex.get("translations") or []
        t = next((t for t in translations if t.get("languages_code") == "vi-VN"), None) or (translations[0] if translations else {})
        company_name = t.get("company_name") or ""
        return email, company_name

    except Exception:
        return "", ""

