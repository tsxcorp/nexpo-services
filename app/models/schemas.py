from pydantic import BaseModel, Field
from typing import Literal, Optional, List


# ── QR ────────────────────────────────────────────────────────────────────────

class QRCodeRequest(BaseModel):
    text: str

class QRCodeResponse(BaseModel):
    qr_code_base64: str
    file_name: str
    success: bool
    message: str


# ── Email ─────────────────────────────────────────────────────────────────────

class EmailRequest(BaseModel):
    from_email: str
    to: str
    subject: str
    html: str
    content_qr: str
    registration_id: Optional[str] = None  # If provided, logs activity to registration_activities
    triggered_by: Optional[str] = None     # Who triggered this email (e.g. "self_registration", "admin")
    link_type: Optional[str] = "registration"  # "registration" | "ticket" — controls Insight Hub URL

class EmailResponse(BaseModel):
    success: bool
    message: str
    message_id: Optional[str] = None

class BulkEmailRecipient(BaseModel):
    email: str
    content_qr: str
    full_name: Optional[str] = None

class BulkEmailRequest(BaseModel):
    from_email: Optional[str] = None
    sender_name: Optional[str] = "Nexpo"
    subject: str
    html: str
    recipients: List[BulkEmailRecipient]

class BulkEmailResponse(BaseModel):
    sent: int
    failed: int
    errors: List[str]

class PlainEmailRequest(BaseModel):
    to: str
    subject: str
    html: str
    from_email: Optional[str] = None
    sender_name: Optional[str] = "Nexpo"


# ── Unified Notify ────────────────────────────────────────────────────────────

class NotifyRequest(BaseModel):
    type: str        # e.g. "meeting.scheduled", "order.facility.created"
    context: dict    # type-specific fields — see routers/notify.py for per-type docs


class BulkNotifyRequest(BaseModel):
    type: str              # e.g. "registration.qr_email", "meeting.confirmed"
    ids: List[str]         # list of IDs to process (registration_id, meeting_id, etc.)
    context: dict = {}     # optional extra context (event_id, trigger, etc.)

class BulkNotifyItemResult(BaseModel):
    id: str
    status: str            # "ok" | "error"
    email: Optional[str] = None
    error: Optional[str] = None

class BulkNotifyResponse(BaseModel):
    results: List[BulkNotifyItemResult]
    sent: int
    failed: int


# ── Meeting Notifications (legacy) ────────────────────────────────────────────

class MeetingNotificationRequest(BaseModel):
    meeting_id: str
    trigger: str   # "scheduled" | "confirmed" | "cancelled"
    event_name: Optional[str] = None


# ── AI Matching ───────────────────────────────────────────────────────────────

class MatchRunRequest(BaseModel):
    event_id: int
    job_requirement_id: Optional[str] = None
    exhibitor_id: Optional[str] = None
    score_threshold: float = 0.5
    max_candidates_per_job: int = 40
    keyword_threshold: float = 0.15
    rescore_pending: bool = True
    ai_model: str = "openai/gpt-4o-mini"

class MatchSuggestion(BaseModel):
    job_requirement_id: str
    registration_id: str
    exhibitor_id: str
    score: float
    matched_criteria: dict
    ai_reasoning: str

class MatchRunResponse(BaseModel):
    success: bool
    message: str
    suggestions_created: int
    suggestions: List[MatchSuggestion] = []


# ── AI Business Matching ─────────────────────────────────────────────────────

class BusinessMatchRunRequest(BaseModel):
    event_id: int
    business_requirement_id: Optional[str] = None
    exhibitor_id: Optional[str] = None
    score_threshold: float = 0.5
    max_candidates_per_requirement: int = 40
    keyword_threshold: float = 0.15
    rescore_pending: bool = True
    ai_model: Literal["openai/gpt-4o-mini", "openai/gpt-4o"] = "openai/gpt-4o-mini"

class BusinessMatchSuggestionOut(BaseModel):
    business_requirement_id: str
    registration_id: str
    exhibitor_id: str
    score: float
    matched_criteria: dict
    ai_reasoning: str

class BusinessMatchRunResponse(BaseModel):
    success: bool
    message: str
    suggestions_created: int
    suggestions: List[BusinessMatchSuggestionOut] = []


# ── AI Profile-based Business Matching ───────────────────────────────────────

class ProfileMatchRunRequest(BaseModel):
    event_id: int
    exhibitor_id: Optional[str] = None
    score_threshold: float = 0.5
    max_candidates_per_exhibitor: int = 40
    keyword_threshold: float = 0.0
    rescore_pending: bool = True
    ai_model: Literal["openai/gpt-4o-mini", "openai/gpt-4o"] = "openai/gpt-4o-mini"

class ProfileMatchSuggestionOut(BaseModel):
    exhibitor_id: str
    registration_id: str
    score: float
    matched_criteria: dict
    ai_reasoning: str

class ProfileMatchRunResponse(BaseModel):
    success: bool
    message: str
    suggestions_created: int
    suggestions: List[ProfileMatchSuggestionOut] = []


# ── Email Template Generation ─────────────────────────────────────────────────

class EmailTemplateField(BaseModel):
    id: str
    label: str
    type: str

class EmailStyleConfig(BaseModel):
    """Event-level brand settings for consistent email styling."""
    primary_color: Optional[str] = "#E94560"        # accent / CTA color
    header_color_start: Optional[str] = "#1a1a2e"   # header gradient start
    header_color_end: Optional[str] = "#0f3460"     # header gradient end
    logo_url: Optional[str] = None                  # event logo URL
    event_label: Optional[str] = None               # label badge text (e.g. "NEXPO 2025")
    footer_text: Optional[str] = None               # custom footer message

class GenerateEmailTemplateRequest(BaseModel):
    event_name: str
    form_purpose: Optional[str] = "registration"
    is_registration: bool = True
    language: str = "bilingual"
    tone: str = "professional"
    fields: List[EmailTemplateField] = []
    email_style: Optional[EmailStyleConfig] = None
    custom_instructions: Optional[str] = None  # Extra user-defined instructions for AI generation

class GenerateEmailTemplateResponse(BaseModel):
    html: str
    success: bool


# ── Floor Plan AI Detection ──────────────────────────────────────────────────

class ZoneBounds(BaseModel):
    x_pct: float  # 0-100, left edge percentage
    y_pct: float  # 0-100, top edge percentage
    w_pct: float  # 0-100, width percentage
    h_pct: float  # 0-100, height percentage

class DetectedZone(BaseModel):
    name: str
    type: str  # exhibition_hall, lobby, parking, entrance, stage, etc.
    bounds: ZoneBounds
    confidence: float  # 0-1

class ScaleHint(BaseModel):
    estimated_total_area_sqm: Optional[float] = None

class DetectZonesRequest(BaseModel):
    image_url: Optional[str] = None
    image_base64: Optional[str] = None

class DetectZonesResponse(BaseModel):
    zones: List[DetectedZone]
    scale_hint: Optional[ScaleHint] = None


# ── Password Reset (custom flow via Mailgun) ────────────────────────────────

class PasswordResetRequest(BaseModel):
    email: str
    app: Literal["admin", "portal", "console"] = "admin"

class PasswordResetResponse(BaseModel):
    success: bool
    message: str = ""

class ValidateResetTokenRequest(BaseModel):
    token: str

class ValidateResetTokenResponse(BaseModel):
    valid: bool
    email: Optional[str] = None
    error: Optional[str] = None

class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(..., min_length=8)

class ResetPasswordResponse(BaseModel):
    success: bool
    error: Optional[str] = None
