#!/usr/bin/env python3
"""
Backfill notification_log for meetings from existing in-app notifications.

This script:
1. Fetches all notifications with entity_type='meeting'
2. Groups them by entity_id (meeting_id)
3. Builds notification_log entries
4. Patches each meeting with the backfilled log

Run once after adding the notification_log field to meetings collection.

Usage:
    python scripts/backfill_meeting_notification_log.py

Environment:
    DIRECTUS_URL - Directus API URL (default: https://app.nexpo.vn)
    DIRECTUS_ADMIN_TOKEN - Admin token for Directus
"""

import asyncio
import os
import httpx
from collections import defaultdict
from datetime import datetime

DIRECTUS_URL = os.getenv("DIRECTUS_URL", "https://app.nexpo.vn")
DIRECTUS_ADMIN_TOKEN = os.getenv("DIRECTUS_ADMIN_TOKEN")

if not DIRECTUS_ADMIN_TOKEN:
    raise RuntimeError("DIRECTUS_ADMIN_TOKEN environment variable is required")


async def directus_get(client: httpx.AsyncClient, path: str) -> dict:
    """GET from Directus."""
    resp = await client.get(
        f"{DIRECTUS_URL}{path}",
        headers={"Authorization": f"Bearer {DIRECTUS_ADMIN_TOKEN}"},
    )
    resp.raise_for_status()
    return resp.json()


async def directus_patch(client: httpx.AsyncClient, path: str, data: dict) -> dict:
    """PATCH to Directus."""
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


def map_notif_type_to_trigger(notif_type: str) -> str:
    """Map notification type to trigger name."""
    mapping = {
        "meeting_scheduled": "scheduled",
        "meeting_confirmed": "confirmed",
        "meeting_cancelled": "cancelled",
    }
    return mapping.get(notif_type, notif_type or "unknown")


def infer_recipient_type(notif: dict) -> str:
    """
    Infer recipient type from notification context.
    - If link contains '/portal' or no admin link -> exhibitor
    - If link contains '/events/' (admin) -> could be organizer
    - We also check if this is a self-notification pattern
    """
    link = notif.get("link") or ""
    notif_type = notif.get("type") or ""

    # Organizer notifications typically link to admin
    if "/events/" in link and "open=" in link:
        return "organizer"

    # Exhibitor notifications link to portal
    if "/meetings" in link or "portal" in link.lower():
        return "exhibitor"

    # Default based on notification type patterns
    if "organizer" in notif_type.lower():
        return "organizer"

    return "exhibitor"


async def fetch_all_meeting_notifications(client: httpx.AsyncClient) -> list[dict]:
    """Fetch all notifications with entity_type='meeting'."""
    all_notifs = []
    offset = 0
    limit = 100

    while True:
        resp = await directus_get(
            client,
            f"/items/notifications"
            f"?filter[entity_type][_eq]=meeting"
            f"&fields[]=id,user_id,type,entity_id,date_created,link"
            f"&sort=-date_created"
            f"&limit={limit}&offset={offset}"
        )
        items = resp.get("data", [])
        if not items:
            break
        all_notifs.extend(items)
        offset += limit
        print(f"  Fetched {len(all_notifs)} notifications...")

    return all_notifs


async def fetch_meeting_existing_log(client: httpx.AsyncClient, meeting_id: str) -> list:
    """Fetch existing notification_log for a meeting."""
    try:
        resp = await directus_get(
            client,
            f"/items/meetings/{meeting_id}?fields[]=notification_log"
        )
        log = (resp.get("data") or {}).get("notification_log")
        if isinstance(log, list):
            return log
    except Exception:
        pass
    return []


async def backfill():
    """Main backfill logic."""
    print("=" * 60)
    print("Backfill Meeting Notification Log")
    print("=" * 60)
    print(f"Directus URL: {DIRECTUS_URL}")
    print()

    async with httpx.AsyncClient(timeout=60) as client:
        # Step 1: Fetch all meeting notifications
        print("[1/4] Fetching all meeting notifications...")
        notifications = await fetch_all_meeting_notifications(client)
        print(f"  Found {len(notifications)} notifications")

        if not notifications:
            print("  No notifications to process. Done!")
            return

        # Step 2: Group by meeting_id
        print("\n[2/4] Grouping by meeting_id...")
        by_meeting: dict[str, list[dict]] = defaultdict(list)
        for notif in notifications:
            meeting_id = notif.get("entity_id")
            if meeting_id:
                by_meeting[meeting_id].append(notif)
        print(f"  Found {len(by_meeting)} unique meetings with notifications")

        # Step 3: Build log entries for each meeting
        print("\n[3/4] Building notification_log entries...")
        meeting_logs: dict[str, list[dict]] = {}

        for meeting_id, notifs in by_meeting.items():
            entries = []
            for notif in notifs:
                # Parse date_created
                date_created = notif.get("date_created") or ""
                try:
                    # Directus returns ISO format
                    if date_created:
                        dt = datetime.fromisoformat(date_created.replace("Z", "+00:00"))
                        timestamp = dt.isoformat()
                    else:
                        timestamp = None
                except Exception:
                    timestamp = date_created

                entry = {
                    "timestamp": timestamp,
                    "trigger": map_notif_type_to_trigger(notif.get("type")),
                    "channel": "in_app",
                    "recipient_type": infer_recipient_type(notif),
                    "recipient": notif.get("user_id") or "",
                    "status": "sent",
                    "backfilled": True,  # Mark as backfilled
                }
                entries.append(entry)

            # Sort by timestamp (oldest first)
            entries.sort(key=lambda e: e.get("timestamp") or "")
            meeting_logs[meeting_id] = entries

        # Step 4: Patch each meeting
        print("\n[4/4] Patching meetings with notification_log...")
        success_count = 0
        skip_count = 0
        error_count = 0

        for i, (meeting_id, new_entries) in enumerate(meeting_logs.items(), 1):
            try:
                # Fetch existing log
                existing_log = await fetch_meeting_existing_log(client, meeting_id)

                # Check if already has non-backfilled entries (real logs)
                has_real_logs = any(
                    not entry.get("backfilled")
                    for entry in existing_log
                    if isinstance(entry, dict)
                )

                if has_real_logs:
                    # Don't overwrite real logs, just append backfilled ones that aren't duplicates
                    existing_timestamps = {
                        e.get("timestamp") for e in existing_log if isinstance(e, dict)
                    }
                    new_unique = [
                        e for e in new_entries
                        if e.get("timestamp") not in existing_timestamps
                    ]
                    if new_unique:
                        merged_log = existing_log + new_unique
                    else:
                        skip_count += 1
                        continue
                else:
                    # No real logs, safe to set backfilled entries
                    # But preserve any existing backfilled entries
                    existing_timestamps = {
                        e.get("timestamp") for e in existing_log if isinstance(e, dict)
                    }
                    new_unique = [
                        e for e in new_entries
                        if e.get("timestamp") not in existing_timestamps
                    ]
                    merged_log = existing_log + new_unique

                if not merged_log or merged_log == existing_log:
                    skip_count += 1
                    continue

                # Patch
                await directus_patch(
                    client,
                    f"/items/meetings/{meeting_id}",
                    {"notification_log": merged_log}
                )
                success_count += 1

                if i % 10 == 0:
                    print(f"  Processed {i}/{len(meeting_logs)} meetings...")

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    # Meeting doesn't exist anymore
                    skip_count += 1
                else:
                    error_count += 1
                    print(f"  Error patching meeting {meeting_id}: {e}")
            except Exception as e:
                error_count += 1
                print(f"  Error patching meeting {meeting_id}: {e}")

        print()
        print("=" * 60)
        print("Backfill Complete!")
        print("=" * 60)
        print(f"  Meetings updated: {success_count}")
        print(f"  Meetings skipped: {skip_count}")
        print(f"  Errors: {error_count}")


if __name__ == "__main__":
    asyncio.run(backfill())
