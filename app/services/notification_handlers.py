"""
Backward-compatible barrel — re-exports all handlers from per-type modules.
Import from here or directly from app.services.handlers.<module>.

Modules:
  handlers/notification_helpers.py  — shared: log, template lookup, substitute
  handlers/meeting_handler.py       — handle_meeting (scheduled/confirmed/cancelled)
  handlers/registration_handler.py  — handle_registration_qr + _log_reg_activity
  handlers/event_handler.py         — handle_order_facility_created, handle_ticket_support_created, handle_lead_captured
  handlers/interview_handler.py     — handle_candidate_interview_schedule
"""

# Re-export all public handler functions for backward compatibility
# Importers like notify.py, meeting_notifs.py, registration_processor.py
# can keep their existing `from app.services.notification_handlers import ...`

from app.services.handlers.notification_helpers import (  # noqa: F401
    append_meeting_notification_log,
    get_meeting_template as _get_meeting_template,
    substitute as _substitute,
)

from app.services.handlers.meeting_handler import handle_meeting  # noqa: F401

from app.services.handlers.registration_handler import (  # noqa: F401
    handle_registration_qr,
    handle_group_registration_qr,
)

from app.services.handlers.event_handler import (  # noqa: F401
    handle_order_facility_created,
    handle_ticket_support_created,
    handle_lead_captured,
)

from app.services.handlers.interview_handler import (  # noqa: F401
    handle_candidate_interview_schedule,
)
