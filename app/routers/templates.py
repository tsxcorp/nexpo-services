from fastapi import APIRouter, HTTPException
import httpx
from app.models.schemas import GenerateEmailTemplateRequest, GenerateEmailTemplateResponse, EmailStyleConfig
from app.config import OPENROUTER_API_KEY

router = APIRouter()


@router.post("/generate-email-template", response_model=GenerateEmailTemplateResponse)
async def generate_email_template(request: GenerateEmailTemplateRequest):
    """Generate a styled HTML email template using AI based on form fields and event context."""
    if not OPENROUTER_API_KEY:
        raise HTTPException(status_code=503, detail="OpenRouter API key not configured")

    # ── Brand settings ────────────────────────────────────────────────────────
    style = request.email_style or EmailStyleConfig()
    primary = style.primary_color or "#E94560"
    h_start = style.header_color_start or "#1a1a2e"
    h_end   = style.header_color_end   or "#0f3460"
    logo_html = (
        f'<img src="{style.logo_url}" alt="Event Logo" style="max-height:48px;max-width:180px;display:block;" />'
        if style.logo_url else
        'Nexpo Platform'
    )
    event_label_badge = (style.event_label or request.event_name.upper())[:30]
    custom_footer = style.footer_text or "This is an automated notification from Nexpo Platform."
    section_header_bg = h_end

    # ── Meeting email — completely different prompt ────────────────────────────
    if not request.is_registration:
        trigger = request.form_purpose or "meeting_notification"

        TRIGGER_INFO = {
            "scheduled_exhibitor": {
                "subtitle": "New Meeting Request / Yêu Cầu Gặp Mặt Mới",
                "status_bg": "#EFF6FF", "status_border": "#3B82F6", "status_color": "#1D4ED8",
                "status_text": "📅 New meeting request received / Bạn có một yêu cầu gặp mặt mới",
                "recipient": "Exhibitor",
                "greeting_hint": "Greet the exhibitor company. Tell them a visitor has requested a meeting.",
                "closing_hint": "Ask the exhibitor to log in to the portal to confirm or decline the meeting request.",
                "cta": True,
                "section_title": "THÔNG TIN CUỘC HỌP / MEETING REQUEST DETAILS",
            },
            "confirmed_visitor": {
                "subtitle": "Meeting Confirmed / Cuộc Họp Đã Được Xác Nhận",
                "status_bg": "#F0FDF4", "status_border": "#22C55E", "status_color": "#166534",
                "status_text": "✅ Cuộc họp đã được xác nhận / Your meeting has been confirmed",
                "recipient": "Visitor",
                "greeting_hint": "Greet the visitor warmly by name. Tell them their meeting has been confirmed by the company.",
                "closing_hint": "Wish them good luck and remind them to arrive on time at the event.",
                "cta": False,
                "section_title": "THÔNG TIN CUỘC HỌP / MEETING DETAILS",
            },
            "cancelled_visitor": {
                "subtitle": "Meeting Cancelled / Cuộc Họp Đã Bị Hủy",
                "status_bg": "#FFF7ED", "status_border": "#F97316", "status_color": "#9A3412",
                "status_text": "❌ Cuộc họp đã bị hủy / Your meeting has been cancelled",
                "recipient": "Visitor",
                "greeting_hint": "Greet the visitor. Express regret that the meeting was cancelled.",
                "closing_hint": "Apologize for the inconvenience and encourage them to explore other opportunities at the event.",
                "cta": False,
                "section_title": "THÔNG TIN CUỘC HỌP / CANCELLED MEETING DETAILS",
            },
            "cancelled_exhibitor": {
                "subtitle": "Meeting Cancelled / Cuộc Họp Đã Bị Hủy",
                "status_bg": "#FFF7ED", "status_border": "#F97316", "status_color": "#9A3412",
                "status_text": "❌ Cuộc họp đã bị hủy / A meeting has been cancelled",
                "recipient": "Exhibitor",
                "greeting_hint": "Greet the exhibitor. Inform them that the meeting has been cancelled.",
                "closing_hint": "Keep it professional and informative. No action required.",
                "cta": False,
                "section_title": "THÔNG TIN CUỘC HỌP / CANCELLED MEETING DETAILS",
            },
        }
        info = TRIGGER_INFO.get(trigger) or TRIGGER_INFO["confirmed_visitor"]

        lang_instruction = {
            "vi": "Write ALL text in Vietnamese only.",
            "en": "Write ALL text in English only.",
            "bilingual": "Write ALL text bilingually: Vietnamese / English. Both languages side-by-side, e.g. 'Xin chào / Dear,'",
        }.get(request.language, "bilingual")

        tone_instruction = {
            "professional": "Use a professional, corporate tone.",
            "friendly": "Use a warm, friendly tone.",
            "formal": "Use a formal, official tone.",
        }.get(request.tone, "professional")

        cta_section = (
            f"""
8. CTA BUTTON (between closing text and footer)
   - Center-aligned button row
   - Button text: "Xem chi tiết / View Details"
   - Button href: {{{{portal_url}}}}
   - Style: display:inline-block; padding:12px 32px; background:{primary}; color:#fff; border-radius:8px; font-size:15px; font-weight:600; text-decoration:none;
"""
            if info["cta"] else ""
        )

        prompt = f"""You are a world-class HTML email designer. Create a stunning, professional HTML email template for a {info['subtitle']} email. Recipient: {info['recipient']}.

EVENT: {request.event_name}
LANGUAGE: {lang_instruction}
TONE: {tone_instruction}

AVAILABLE TEMPLATE VARIABLES (use these exactly, with double curly braces):
  - {{{{visitor_name}}}} — full name of the visitor/candidate
  - {{{{company_name}}}} — company/exhibitor name
  - {{{{job_title}}}} — the job position being discussed
  - {{{{scheduled_at}}}} — date and time of the meeting
  - {{{{location}}}} — location or room at the venue
  - {{{{portal_url}}}} — link to the exhibitor portal
  - {{{{event_name}}}} — name of the event

═══════════════════════════════════════════════
DESIGN SYSTEM — follow EXACTLY:
═══════════════════════════════════════════════

COLOR PALETTE:
  - Page background: #F0F4F8
  - Card background: #FFFFFF
  - Header gradient: linear-gradient(135deg, {h_start} 0%, {h_end} 100%)
  - Accent / primary: {primary}
  - Status badge bg: {info['status_bg']}, border: {info['status_border']}, text: {info['status_color']}
  - Section header bar: {section_header_bg} (dark)
  - Detail row odd bg: #F8FAFC
  - Detail row even bg: #FFFFFF
  - Footer bg: #1E293B; footer text: #94A3B8
  - Divider: #E2E8F0

TYPOGRAPHY:
  - Font: 'Segoe UI', Arial, sans-serif
  - Body: 15px, line-height: 1.6, color: #334155
  - Section headings: 11px, font-weight:700, UPPERCASE, letter-spacing:1.5px, color:#FFFFFF
  - Detail labels: 13px, font-weight:600, UPPERCASE, color:#64748B
  - Detail values: 15px, font-weight:500, color:#1E293B

LOGO / BRANDING: Use this exactly in the header:
  {logo_html}
  Event badge text: "{event_label_badge}"

═══════════════════════════════════════════════
EMAIL STRUCTURE — build in this exact order:
═══════════════════════════════════════════════

⚠️ SINGLE-COLUMN layout throughout. Every <tr> inside the card has exactly ONE <td>.
   The ONLY 2-column rows are inside the Meeting Details section (label 40% / value 60%).

1. OUTER WRAPPER
   - <table width="100%" style="background:#F0F4F8;padding:40px 16px;"> one <tr><td align="center">

2. CARD CONTAINER
   - <table width="100%" style="max-width:600px;background:#FFFFFF;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.08);"> — ALL sections are single-column rows

3. HEADER
   - Gradient background: {h_start} → {h_end}
   - Padding: 40px 32px 32px
   - Top: event label badge — pill, border:1.5px solid {primary}, color:{primary}, 11px, letter-spacing:2px, UPPERCASE
   - Event name: 28px, font-weight:800, white
   - Subtitle: "{info['subtitle']}" in #94A3B8, 15px
   - Decorative bar: 3px, gradient {primary} → {h_end}

4. STATUS BADGE
   - Strip: background:{info['status_bg']}, border-left:4px solid {info['status_border']}
   - Padding: 14px 24px, font-size:14px, color:{info['status_color']}, font-weight:600
   - Text: "{info['status_text']}"

5. GREETING
   - Padding: 28px 32px 0
   - {info['greeting_hint']}
   - Use {{{{visitor_name}}}} for visitor's name in greeting.

6. MEETING DETAILS SECTION
   - Section header: background:{section_header_bg}, padding:10px 24px
     Heading: "{info['section_title']}" — 11px, white, uppercase, letter-spacing:1.5px
   - TWO-COLUMN rows (label 40% / value 60%) for each detail:
     ⚠️ Include ALL of these rows:
     a. VISITOR / ỨNG VIÊN → {{{{visitor_name}}}} — with italic sub-text of {{{{job_title}}}} below
     b. COMPANY / CÔNG TY → {{{{company_name}}}}
     c. POSITION / VỊ TRÍ → {{{{job_title}}}}
     d. TIME / THỜI GIAN → {{{{scheduled_at}}}}
     e. LOCATION / ĐỊA ĐIỂM → {{{{location}}}}
   - Alternating row colors: #F8FAFC / #FFFFFF
   - Each row: thin bottom border 1px solid #E2E8F0 (skip last)
   - Label cells: class="detail-label" | Value cells: class="detail-value"
   - Gradient bar at bottom (3px, same as header)

7. CLOSING MESSAGE
   - Padding: 24px 32px
   - {info['closing_hint']}
{cta_section}
9. FOOTER
   - Background: #1E293B, padding: 28px 32px
   - Event name in white, 14px, font-weight:600
   - Copyright in #94A3B8, 13px
   - Divider: 1px solid #334155, margin: 12px 0
   - "{custom_footer}" in #64748B, 12px, italic

═══════════════════════════════════════════════
DARK MODE — REQUIRED:
═══════════════════════════════════════════════
Add <style> block in <head>:
  :root {{ color-scheme: light only; }}
  @media (prefers-color-scheme: dark) {{
    body, table, td, th, p, span, a, div {{ background-color: inherit !important; color: inherit !important; }}
    .email-body {{ background-color: #F0F4F8 !important; }}
    .card {{ background-color: #FFFFFF !important; }}
    .header {{ background-color: {h_start} !important; }}
    .detail-label {{ color: #64748B !important; }}
    .detail-value {{ color: #1E293B !important; }}
    .footer-section {{ background-color: #1E293B !important; }}
    .footer-text {{ color: #94A3B8 !important; }}
  }}
Also add <meta name="color-scheme" content="light only"> in <head>.
Add helper classes: email-body, card, header, detail-label, detail-value, footer-section, footer-text.
Keep ALL inline styles — classes are additions only.

═══════════════════════════════════════════════
STRICT RULES:
═══════════════════════════════════════════════
- Return ONLY raw HTML. No markdown fences, no explanation.
- ALL CSS inline + the dark mode <style> block.
- Use table/tr/td for all layout — no div-based layout.
- Every variable must appear exactly as written with double-curly braces: {{{{visitor_name}}}}, {{{{company_name}}}}, etc.
- Specify explicit color and background-color on EVERY td, p, span, a.
- Do NOT include a QR code section — this is a meeting notification, not a registration.
- Do NOT include any form registration details.
{f'''
⭐ ADDITIONAL USER REQUIREMENTS (apply these on top of the above):
{request.custom_instructions}
''' if request.custom_instructions else ''}
Generate the complete HTML email now:"""

    # ── Registration email (existing prompt) ──────────────────────────────────
    else:
        form_context = "registration confirmation" if request.is_registration else (request.form_purpose or "meeting notification")
        form_context_title = form_context.title()

        field_list = "\n".join(
            f'  - Variable: {{{{{f.id}}}}} | Label: "{f.label}" | Type: {f.type}'
            for f in request.fields
        ) or "  (none — use generic placeholder content)"

        lang_instruction = {
            "vi": "Write ALL text (headings, labels, body, footer) in Vietnamese only.",
            "en": "Write ALL text (headings, labels, body, footer) in English only.",
            "bilingual": (
                "Write ALL text bilingually. Format: Vietnamese / English (separated by ' / ').\n"
                "  - Section headings: e.g. 'CHI TIẾT ĐĂNG KÝ / REGISTRATION DETAILS'\n"
                "  - Row labels: EVERY label cell MUST have both languages, e.g. 'Họ và Tên / Full Name'\n"
                "  - Greeting: e.g. 'Xin chào / Dear,'\n"
                "  - Body text: each sentence bilingual\n"
                "  - Footer: bilingual\n"
                "  ⚠️ CRITICAL: Do NOT skip bilingual labels for ANY row. Every single label cell must contain both Vietnamese and English."
            ),
        }.get(request.language, "bilingual")

        tone_instruction = {
            "professional": "Use a professional, corporate tone.",
            "friendly": "Use a warm, friendly and welcoming tone.",
            "formal": "Use a formal, official tone.",
        }.get(request.tone, "professional")

        name_field_hint = ""
        for f in request.fields:
            if any(kw in f.label.lower() for kw in ["name", "họ tên", "tên", "full name", "họ và tên"]):
                name_field_hint = f"Use {{{{{f.id}}}}} as the recipient's name in the greeting."
                break

        prompt = f"""You are a world-class HTML email designer. Create a stunning, polished HTML email template for a {form_context} email. Think of award-winning transactional emails from top tech companies.

EVENT: {request.event_name}
LANGUAGE: {lang_instruction}
TONE: {tone_instruction}
{name_field_hint}

FORM FIELDS (insert these variables exactly as shown):
{field_list}

═══════════════════════════════════════════════
DESIGN SYSTEM — follow EXACTLY (brand colors already set for this event):
═══════════════════════════════════════════════

COLOR PALETTE:
  - Page background: #F0F4F8
  - Card background: #FFFFFF
  - Header gradient: linear-gradient(135deg, {h_start} 0%, {h_end} 100%)
  - Accent / primary: {primary}  (use for badges, highlights, confirmation badge border, CTA button)
  - Section header bar: {section_header_bg} (dark)
  - Detail row odd bg: #F8FAFC
  - Detail row even bg: #FFFFFF
  - Detail label text: #64748B (slate-500)
  - Detail value text: #1E293B (slate-900)
  - Footer bg: #1E293B
  - Footer text: #94A3B8
  - Divider color: #E2E8F0

TYPOGRAPHY:
  - Font stack: 'Segoe UI', Arial, sans-serif
  - Body font-size: 15px, line-height: 1.6, color: #334155
  - Section headings: 11px, font-weight: 700, letter-spacing: 1.5px, UPPERCASE, color: #FFFFFF
  - Detail labels: 13px, font-weight: 600, color: #64748B, UPPERCASE, letter-spacing: 0.5px
  - Detail values: 15px, font-weight: 500, color: #1E293B

SPACING: Use padding: 20px 24px for content areas. Row padding: 12px 16px. Section gap: margin-bottom: 16px.

LOGO / BRANDING in header: Use exactly this for the logo area at top of header:
  {logo_html}
  Event badge label text: "{event_label_badge}"

═══════════════════════════════════════════════
STRUCTURE — build in this exact order:
═══════════════════════════════════════════════

⚠️ CRITICAL LAYOUT RULE:
The card is a SINGLE-COLUMN layout. Every <tr> inside the card has exactly ONE <td> that spans the full width.
The ONLY place with 2 columns is inside the Registration Details rows (label 40% / value 60%).
Do NOT split the outer card, header, greeting, section headers, QR, or footer into multiple columns.

1. OUTER WRAPPER
   - <table width="100%" style="background:#F0F4F8;padding:40px 16px;">
   - One <tr><td align="center"> — single cell, full width

2. CARD CONTAINER
   - <table width="100%" style="max-width:600px;background:#FFFFFF;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.08);">
   - Every section is a <tr> with a SINGLE <td width="100%"> — full width, no side-by-side columns

3. HEADER SECTION (inside card)
   - Background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%)
   - Padding: 40px 32px 32px
   - Top: small event label badge — pill shape, border: 1.5px solid #E94560, color: #E94560, font-size: 11px, letter-spacing: 2px, padding: 4px 14px, border-radius: 20px, UPPERCASE
   - Main: event name in large bold white text (28px, font-weight: 800, margin: 16px 0 8px)
   - Sub: "{form_context_title}" subtitle in #94A3B8, 15px
   - Bottom decorative bar: 3px high table row, background: linear-gradient(90deg, #E94560, #0f3460)

4. CONFIRMATION BADGE (between header and greeting)
   - Light green confirmation strip: background: #F0FDF4, border-left: 4px solid #22C55E
   - Padding: 14px 24px, font-size: 14px, color: #166534
   - Text: checkmark ✓ + "Đăng ký thành công! (Registration Confirmed!)" or similar

5. GREETING SECTION
   - Padding: 28px 32px 0
   - Warm greeting line using the name variable if available, else generic greeting
   - 1-2 sentences confirming receipt

6. REGISTRATION DETAILS SECTION
   - Section header row: background: #0f3460, padding: 10px 24px
     Text: bilingual heading e.g. "CHI TIẾT ĐĂNG KÝ / REGISTRATION DETAILS" in white, 11px, uppercase, letter-spacing: 1.5px
   - For EACH field in the FORM FIELDS list above: alternating row background (#F8FAFC / #FFFFFF)
     - TWO-COLUMN table row: left cell (40% width) = label, right cell (60%) = variable value
     - Left cell: the field's Label text from the FORM FIELDS list, UPPERCASE, 12px, color #64748B, font-weight: 600, padding: 12px 16px
       ⚠️ If bilingual: show BOTH languages in label cell, e.g. "HỌ VÀ TÊN / FULL NAME"
       ⚠️ MUST include a row for EVERY field listed in FORM FIELDS — do not skip any field
     - Right cell: use the exact variable syntax from FORM FIELDS above — keep double-curly format e.g. {{{{company_name}}}}, {{{{visitor_name}}}}. Font: 15px, color #1E293B, font-weight: 500, padding: 12px 16px
     - Thin bottom border: 1px solid #E2E8F0 (skip on last row)
   - Bottom of section: 4px gradient bar (same as header decorative bar)

7. QR CODE SECTION — MUST come IMMEDIATELY after the Registration Details section, before closing message
   ⚠️ Place this section RIGHT HERE in the flow, not at the end of the email.
   - Table row containing a td with: background: #F8FAFC, border: 1px solid #E2E8F0, border-radius: 12px, margin: 24px 32px, padding: 24px, text-align: center
   - Section label: "MÃ QR CỦA BẠN (YOUR QR CODE)" — 11px, font-weight: 700, letter-spacing: 1.5px, uppercase, color: #0f3460
   - Instruction text: small grey text (13px, color #64748B) about showing QR at event entrance
   - QR image: use actual `<img src="cid:qrcode.png" alt="QR Code" style="width:200px;height:200px;display:block;margin:0 auto;border-radius:8px;border:1px solid #E2E8F0;" />` — do NOT use a div placeholder, use this exact img tag so the QR renders at this position

8. CLOSING MESSAGE — comes AFTER QR section
   - Padding: 24px 32px
   - 1-2 sentences of closing remarks, looking forward to seeing them at the event

9. FOOTER — the very last section
   - Background: #1E293B, padding: 28px 32px
   - Top: event name in white, 14px, font-weight: 600
   - Middle: copyright line in #94A3B8, 13px
   - Divider: 1px solid #334155, margin: 12px 0
   - Bottom: "{custom_footer}" in #64748B, 12px, font-style: italic

═══════════════════════════════════════════════
DARK MODE SUPPORT — REQUIRED:
═══════════════════════════════════════════════
Many email clients (Apple Mail, iOS Mail, Outlook 2019+) apply dark mode and invert or replace colors, making text unreadable. You MUST include dark mode protection.

Add a <style> block inside <head> with these exact rules:
  <style>
    /* Force light mode on supported clients */
    :root {{ color-scheme: light only; }}
    /* Prevent iOS Mail dark mode inversion */
    @media (prefers-color-scheme: dark) {{
      body, table, td, th, p, span, a, div {{
        background-color: inherit !important;
        color: inherit !important;
      }}
      /* Re-enforce all critical colors explicitly */
      .email-body {{ background-color: #F0F4F8 !important; }}
      .card {{ background-color: #FFFFFF !important; }}
      .header {{ background-color: #1a1a2e !important; }}
      .detail-label {{ color: #64748B !important; }}
      .detail-value {{ color: #1E293B !important; }}
      .footer-section {{ background-color: #1E293B !important; }}
      .footer-text {{ color: #94A3B8 !important; }}
    }}
  </style>

Also add <meta name="color-scheme" content="light only"> in <head>.

Add these helper classes (alongside inline styles — classes are for dark mode override only):
  - class="email-body" on the outermost <table>
  - class="card" on the card container <table>
  - class="header" on the header <td>
  - class="detail-label" on every label <td> in the registration rows
  - class="detail-value" on every value <td> in the registration rows
  - class="footer-section" on the footer <td>
  - class="footer-text" on footer text elements

⚠️ IMPORTANT: Keep ALL existing inline styles (style="...") — classes are ADDITIONS, not replacements. Both inline styles AND classes must be present together.

═══════════════════════════════════════════════
STRICT RULES:
═══════════════════════════════════════════════
- Return ONLY the raw HTML. No markdown fences, no explanation, no comments outside HTML.
- ALL CSS must be inline (style="...") PLUS the dark mode <style> block described above.
- Use table/tr/td for ALL layout — no div-based layout (email client compatibility).
- Every ${{uuid}} variable must appear exactly as written — never substitute with label text.
- Use {{event_name}} (curly braces, NO dollar sign) only if referencing the event name dynamically outside the header.
- border-radius on tables: use on the outer wrapper td, not on the table element itself for Outlook compat.
- For gradient backgrounds on table cells, use: background: #1a1a2e; (solid fallback first, then background-image: linear-gradient(...))
- Always specify explicit color and background-color on EVERY td, p, span, a element — never rely on inherited/default colors.

Generate the complete HTML email now:"""

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "openai/gpt-4o",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.4,
                    "max_tokens": 6000,
                },
            )
            resp.raise_for_status()
            result = resp.json()
            html = result["choices"][0]["message"]["content"].strip()

            if html.startswith("```"):
                lines = html.split("\n")
                html = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

            return GenerateEmailTemplateResponse(html=html, success=True)

    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"OpenRouter error: {e.response.text[:200]}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Generation failed: {str(e)}")
