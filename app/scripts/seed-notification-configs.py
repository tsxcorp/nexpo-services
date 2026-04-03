"""
Migration script: seed notification channel + trigger configs for existing tenants.

Usage:
    # Preview changes (no writes):
    python3 -m app.scripts.seed_notification_configs --dry-run

    # Apply changes:
    python3 -m app.scripts.seed_notification_configs

    # Also migrate meeting_email_templates → notification_templates:
    python3 -m app.scripts.seed_notification_configs --migrate-templates

Idempotent: skips tenants that already have configs.
"""

import asyncio
import argparse
import logging
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

DIRECTUS_URL = os.getenv("DIRECTUS_URL", "https://app.nexpo.vn")
DIRECTUS_ADMIN_TOKEN = os.getenv("DIRECTUS_ADMIN_TOKEN", "")
MAILGUN_API_KEY = os.getenv("MAILGUN_API_KEY", "")
MAILGUN_DOMAIN = os.getenv("MAILGUN_DOMAIN", "")

# Triggers that should have email channel by default.
# Mirrors DEFAULT_TRIGGER_CHANNELS in notification_config.py (email-only triggers).
DEFAULT_EMAIL_TRIGGERS: list[str] = [
    "registration.qr_email",
    "meeting.scheduled",
    "meeting.confirmed",
    "meeting.cancelled",
    "order.facility.created",
    "ticket.support.created",
    "candidate.interview_schedule",
    "match.status_changed",
    "form.submitted",
]
# lead.captured is excluded — in-app only (channels=[])

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Directus helpers (sync wrappers for script use) ───────────────────────────

def _headers() -> dict:
    return {
        "Authorization": f"Bearer {DIRECTUS_ADMIN_TOKEN}",
        "Content-Type": "application/json",
    }


async def _get(client: httpx.AsyncClient, path: str) -> dict:
    resp = await client.get(f"{DIRECTUS_URL}{path}", headers=_headers())
    resp.raise_for_status()
    return resp.json()


async def _post(client: httpx.AsyncClient, path: str, data: dict) -> dict:
    resp = await client.post(f"{DIRECTUS_URL}{path}", headers=_headers(), json=data)
    resp.raise_for_status()
    return resp.json()


# ── Step 1: Seed email channel configs ───────────────────────────────────────

async def seed_email_channel_configs(client: httpx.AsyncClient, dry_run: bool) -> int:
    """Create notification_channel_configs (email/mailgun) for tenants that don't have one.

    Returns count of configs created (or would-be created in dry-run).
    """
    log.info("=== Step 1: Seed email channel configs ===")

    if not MAILGUN_API_KEY or not MAILGUN_DOMAIN:
        log.warning("MAILGUN_API_KEY or MAILGUN_DOMAIN not set — skipping email channel seed")
        return 0

    # Fetch all active tenants
    resp = await _get(
        client,
        "/items/tenants"
        "?filter[status][_eq]=active"
        "&fields[]=id,name,settings"
        "&limit=-1",
    )
    tenants = (resp.get("data") or [])
    log.info("Found %d active tenants", len(tenants))

    created = 0

    for tenant in tenants:
        tenant_id = str(tenant["id"])
        tenant_name = tenant.get("name") or f"tenant-{tenant_id}"
        settings = tenant.get("settings") or {}

        # Check if email config already exists for this tenant (tenant-level, no event)
        check_resp = await _get(
            client,
            "/items/notification_channel_configs"
            f"?filter[tenant_id][_eq]={tenant_id}"
            "&filter[channel][_eq]=email"
            "&filter[event_id][_null]=true"
            "&fields[]=id&limit=1",
        )
        existing = check_resp.get("data") or []
        if existing:
            log.info("[SKIP] Tenant %s (%s) — email config already exists", tenant_id, tenant_name)
            continue

        payload = {
            "tenant_id": tenant_id,
            "channel": "email",
            "provider": "mailgun",
            "name": "Default Email (Mailgun)",
            "is_active": True,
            "credentials": {
                "api_key": MAILGUN_API_KEY,
                "domain": MAILGUN_DOMAIN,
            },
            "config": {
                "sender_name": settings.get("email_sender_name") or "Nexpo",
                "reply_to": settings.get("email_reply_to") or "",
            },
        }

        if dry_run:
            log.info("[DRY-RUN] Would create email channel config for tenant %s (%s)", tenant_id, tenant_name)
        else:
            await _post(client, "/items/notification_channel_configs", payload)
            log.info("[CREATED] Email channel config for tenant %s (%s)", tenant_id, tenant_name)

        created += 1

    log.info("Step 1 done — %d email configs %s", created, "would be created" if dry_run else "created")
    return created


# ── Step 2: Seed default trigger configs ─────────────────────────────────────

async def seed_trigger_configs(client: httpx.AsyncClient, dry_run: bool) -> int:
    """Create notification_trigger_configs (email-only) for tenants missing them.

    Tenant-level only (event_id=null). Event-level overrides can be set via admin UI.
    Returns count of trigger configs created (or would-be created in dry-run).
    """
    log.info("=== Step 2: Seed default trigger configs ===")

    resp = await _get(
        client,
        "/items/tenants"
        "?filter[status][_eq]=active"
        "&fields[]=id,name"
        "&limit=-1",
    )
    tenants = resp.get("data") or []
    log.info("Found %d active tenants", len(tenants))

    total_created = 0

    for tenant in tenants:
        tenant_id = str(tenant["id"])
        tenant_name = tenant.get("name") or f"tenant-{tenant_id}"

        # Fetch existing tenant-level trigger configs in one call to avoid N+1
        existing_resp = await _get(
            client,
            "/items/notification_trigger_configs"
            f"?filter[tenant_id][_eq]={tenant_id}"
            "&filter[event_id][_null]=true"
            "&fields[]=trigger_type"
            "&limit=-1",
        )
        existing_triggers = {
            item["trigger_type"]
            for item in (existing_resp.get("data") or [])
            if item.get("trigger_type")
        }

        missing = [t for t in DEFAULT_EMAIL_TRIGGERS if t not in existing_triggers]
        if not missing:
            log.info("[SKIP] Tenant %s (%s) — all trigger configs already exist", tenant_id, tenant_name)
            continue

        for trigger_type in missing:
            payload = {
                "tenant_id": tenant_id,
                "trigger_type": trigger_type,
                "channels": ["email"],
                "is_active": True,
            }
            if dry_run:
                log.info(
                    "[DRY-RUN] Would create trigger config '%s' for tenant %s (%s)",
                    trigger_type, tenant_id, tenant_name,
                )
            else:
                await _post(client, "/items/notification_trigger_configs", payload)
                log.info(
                    "[CREATED] Trigger config '%s' for tenant %s (%s)",
                    trigger_type, tenant_id, tenant_name,
                )
            total_created += 1

    log.info(
        "Step 2 done — %d trigger configs %s",
        total_created,
        "would be created" if dry_run else "created",
    )
    return total_created


# ── Step 3 (optional): Migrate meeting_email_templates → notification_templates

async def migrate_meeting_templates(client: httpx.AsyncClient, dry_run: bool) -> int:
    """Convert legacy meeting_email_templates into notification_templates.

    This is optional — the legacy fallback in template_renderer.py already
    checks meeting_email_templates. Only run this to consolidate templates
    into the new collection.

    Returns count of templates migrated (or would-be migrated in dry-run).
    """
    log.info("=== Step 3 (optional): Migrate meeting_email_templates ===")

    resp = await _get(
        client,
        "/items/meeting_email_templates"
        "?fields[]=id,event_id,trigger_recipient,matching_type,subject,html_template,language,is_active"
        "&limit=-1",
    )
    templates = resp.get("data") or []
    log.info("Found %d meeting_email_templates to evaluate", len(templates))

    migrated = 0

    for tmpl in templates:
        event_id = tmpl.get("event_id")
        trigger_recipient = tmpl.get("trigger_recipient") or ""
        matching_type = tmpl.get("matching_type") or "talent_matching"
        html_template = tmpl.get("html_template")

        if not html_template:
            log.info("[SKIP] Template id=%s — no html_template content", tmpl["id"])
            continue

        # Build trigger_type name from trigger_recipient (e.g. "visitor_scheduled" → "meeting.scheduled")
        # Heuristic: use first word of trigger_recipient after stripping role prefix
        trigger_suffix = trigger_recipient.replace("visitor_", "").replace("exhibitor_", "")
        trigger_type = f"meeting.{trigger_suffix}" if trigger_suffix else "meeting.scheduled"

        # Check if already migrated (same event_id + trigger_type + channel=email)
        if event_id:
            check_resp = await _get(
                client,
                "/items/notification_templates"
                f"?filter[event_id][_eq]={event_id}"
                f"&filter[trigger_type][_eq]={trigger_type}"
                "&filter[channel][_eq]=email"
                "&fields[]=id&limit=1",
            )
            if check_resp.get("data"):
                log.info(
                    "[SKIP] Template id=%s — notification_template already exists for event %s / %s",
                    tmpl["id"], event_id, trigger_type,
                )
                continue

        payload = {
            "tenant_id": None,  # legacy templates have no tenant_id
            "event_id": event_id,
            "trigger_type": trigger_type,
            "channel": "email",
            "name": f"Meeting ({matching_type}) — {trigger_recipient}",
            "language": tmpl.get("language") or "vi",
            "subject": tmpl.get("subject"),
            "body_template": html_template,
            "is_active": tmpl.get("is_active", True),
        }

        if dry_run:
            log.info(
                "[DRY-RUN] Would migrate template id=%s → trigger_type=%s, event_id=%s",
                tmpl["id"], trigger_type, event_id,
            )
        else:
            await _post(client, "/items/notification_templates", payload)
            log.info(
                "[MIGRATED] Template id=%s → trigger_type=%s, event_id=%s",
                tmpl["id"], trigger_type, event_id,
            )
        migrated += 1

    log.info(
        "Step 3 done — %d meeting templates %s",
        migrated,
        "would be migrated" if dry_run else "migrated",
    )
    return migrated


# ── Main ──────────────────────────────────────────────────────────────────────

async def run(dry_run: bool = False, migrate_templates: bool = False) -> None:
    if not DIRECTUS_ADMIN_TOKEN:
        log.error("DIRECTUS_ADMIN_TOKEN is not set. Cannot proceed.")
        sys.exit(1)

    mode = "DRY-RUN (no changes will be written)" if dry_run else "LIVE (changes will be written)"
    log.info("Starting notification config seed — mode: %s", mode)

    async with httpx.AsyncClient(timeout=30) as client:
        channel_count = await seed_email_channel_configs(client, dry_run)
        trigger_count = await seed_trigger_configs(client, dry_run)

        template_count = 0
        if migrate_templates:
            template_count = await migrate_meeting_templates(client, dry_run)

    log.info(
        "Seed complete — channels: %d, triggers: %d, templates: %d",
        channel_count, trigger_count, template_count,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed notification channel + trigger configs for existing tenants."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without writing to Directus.",
    )
    parser.add_argument(
        "--migrate-templates",
        action="store_true",
        help="Also migrate meeting_email_templates → notification_templates (optional).",
    )
    args = parser.parse_args()

    asyncio.run(run(dry_run=args.dry_run, migrate_templates=args.migrate_templates))


if __name__ == "__main__":
    main()
