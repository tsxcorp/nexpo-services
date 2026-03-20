from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import qrcode
import io
import base64
import hashlib
from datetime import datetime
import os
from dotenv import load_dotenv
import httpx
import json
from typing import Optional, List

# Load environment variables
load_dotenv()

app = FastAPI(title="QR Code Generator API", version="1.0.0")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://app.nexpo.vn", "http://app.nexpo.vn", "https://admin.nexpo.vn", "http://localhost:3000", "http://localhost:3001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mailgun config
MAILGUN_API_KEY = os.getenv('MAILGUN_API_KEY', '')
MAILGUN_DOMAIN = os.getenv('MAILGUN_DOMAIN', '')
MAILGUN_API_URL = os.getenv('MAILGUN_API_URL', 'https://api.mailgun.net')

class QRCodeRequest(BaseModel):
    text: str

class QRCodeResponse(BaseModel):
    qr_code_base64: str
    file_name: str
    success: bool
    message: str

class EmailRequest(BaseModel):
    from_email: str
    to: str
    subject: str
    html: str
    content_qr: str

class EmailResponse(BaseModel):
    success: bool
    message: str
    message_id: str = None

@app.get("/")
async def root():
    return {"message": "QR Code Generator API is running!"}

@app.post("/gen-qr", response_model=QRCodeResponse)
async def generate_qr_code(request: QRCodeRequest):
    """
    Tạo QR code từ string và trả về base64
    """
    try:
        if not request.text.strip():
            raise HTTPException(status_code=400, detail="Text không được để trống")
        
        # Tạo QR code
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(request.text)
        qr.make(fit=True)
        
        # Tạo image
        img = qr.make_image(fill_color="black", back_color="white")
        
        # Chuyển đổi thành base64
        buffer = io.BytesIO()
        img.save(buffer, format='PNG')
        buffer.seek(0)
        img_base64 = base64.b64encode(buffer.getvalue()).decode()
        
        # Tạo tên file từ hash của nội dung và timestamp
        text_hash = hashlib.md5(request.text.encode()).hexdigest()[:8]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_name = f"qr_{text_hash}_{timestamp}.png"
        
        return QRCodeResponse(
            qr_code_base64=img_base64,
            file_name=file_name,
            success=True,
            message="QR code được tạo thành công"
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Lỗi khi tạo QR code: {str(e)}")

def generate_qr_code_bytes(content_qr: str) -> bytes:
    """
    Tạo QR code từ content_qr và trả về PNG bytes
    """
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(content_qr)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)
    return buffer.getvalue()

def inject_qr_extras(html: str, content_qr: str) -> str:
    """
    Inject UUID display text + Insight Hub button right after the QR img tag.
    If the extras are already injected (idempotent), skip.
    """
    import re
    insight_url = f"https://insights.nexpo.vn/{content_qr}"
    if insight_url in html:
        return html  # already injected

    extras = (
        # UUID display
        '<table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:12px;">'
        '<tr><td align="center">'
        '<p style="margin:0 0 4px;font-size:11px;font-weight:700;letter-spacing:1px;'
        'text-transform:uppercase;color:#64748B;font-family:\'Segoe UI\',Arial,sans-serif;">'
        'M&#227; &#273;&#259;ng k&#253; / Registration ID</p>'
        f'<p style="margin:0;font-size:13px;font-family:\'Courier New\',monospace;'
        f'color:#1E293B;background:#F1F5F9;padding:6px 14px;border-radius:6px;'
        f'letter-spacing:0.5px;display:inline-block;">{content_qr}</p>'
        '</td></tr>'
        # Insight Hub button
        '<tr><td align="center" style="padding-top:20px;">'
        f'<a href="{insight_url}" target="_blank" '
        'style="display:inline-block;background:linear-gradient(135deg,#1a1a2e 0%,#0f3460 100%);'
        'color:#FFFFFF;text-decoration:none;font-size:14px;font-weight:700;'
        'font-family:\'Segoe UI\',Arial,sans-serif;padding:12px 28px;border-radius:8px;'
        'letter-spacing:0.3px;">'
        'Access Insight Hub&nbsp;|&nbsp;Tr&#7909;y c&#7853;p C&#7893;ng th&#244;ng tin s&#7921; ki&#7879;n'
        '</a>'
        '</td></tr>'
        '</table>'
    )

    # Insert right after the QR img tag (find closing > of the img)
    qr_img_pattern = re.compile(
        r'(<img[^>]*src=["\']cid:qrcode\.png["\'][^>]*/?>)',
        re.IGNORECASE,
    )
    match = qr_img_pattern.search(html)
    if match:
        insert_pos = match.end()
        return html[:insert_pos] + extras + html[insert_pos:]

    # Fallback: insert before </body>
    if '</body>' in html:
        return html.replace('</body>', f'{extras}</body>', 1)
    return html + extras


def append_qr_cid_to_html(html: str) -> str:
    """
    Gắn thẻ img CID vào cuối HTML — QR sẽ được gửi kèm dưới dạng inline attachment.
    Nếu template đã có cid:qrcode.png ở đúng vị trí thì không append thêm.
    """
    import re
    # Strip any extra QR img tags — keep only the first one to avoid duplicates
    qr_pattern = re.compile(
        r'<(?:div[^>]*>\s*)?<img[^>]*src=["\']cid:qrcode\.png["\'][^>]*/?>(?:\s*</div>)?',
        re.IGNORECASE,
    )
    matches = qr_pattern.findall(html)
    if len(matches) > 1:
        # Remove all occurrences, then re-insert the first one before </body>
        html_stripped = qr_pattern.sub('', html)
        first_tag = matches[0]
        if '</body>' in html_stripped:
            return html_stripped.replace('</body>', f'{first_tag}</body>', 1)
        elif '</html>' in html_stripped:
            return html_stripped.replace('</html>', f'{first_tag}</html>', 1)
        return html_stripped + first_tag
    if 'cid:qrcode.png' in html:
        return html  # template already has exactly one QR at the right position
    qr_img_tag = (
        '<div style="text-align:center;margin:24px 0;">'
        '<img src="cid:qrcode.png" alt="QR Code" '
        'style="width:200px;height:200px;border:1px solid #ccc;border-radius:8px;" />'
        '</div>'
    )
    if '</body>' in html:
        return html.replace('</body>', f'{qr_img_tag}</body>', 1)
    elif '</html>' in html:
        return html.replace('</html>', f'{qr_img_tag}</html>', 1)
    return html + qr_img_tag

@app.post("/send-email-with-qr", response_model=EmailResponse)
async def send_email_with_qr(request: EmailRequest):
    """
    Nhận thông tin email, tạo QR code từ content_qr,
    gửi qua Mailgun với QR đính kèm inline (CID) — hoạt động trên Gmail
    """
    try:
        if not request.from_email.strip():
            raise HTTPException(status_code=400, detail="from_email không được để trống")
        if not request.to.strip():
            raise HTTPException(status_code=400, detail="to không được để trống")
        if not request.subject.strip():
            raise HTTPException(status_code=400, detail="subject không được để trống")
        if not request.content_qr.strip():
            raise HTTPException(status_code=400, detail="content_qr không được để trống")

        if not MAILGUN_API_KEY or not MAILGUN_DOMAIN:
            raise HTTPException(status_code=500, detail="Mailgun chưa được cấu hình")

        # Tạo QR PNG bytes
        qr_bytes = generate_qr_code_bytes(request.content_qr)

        # Gắn <img src="cid:qrcode.png"> vào HTML
        html_with_qr = append_qr_cid_to_html(request.html)

        # Inject UUID display + Insight Hub button right after QR img tag
        html_with_qr = inject_qr_extras(html_with_qr, request.content_qr)

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{MAILGUN_API_URL}/v3/{MAILGUN_DOMAIN}/messages",
                    auth=("api", MAILGUN_API_KEY),
                    data={
                        "from": request.from_email,
                        "to": request.to,
                        "subject": request.subject,
                        "html": html_with_qr,
                    },
                    files=[
                        ("inline", ("qrcode.png", qr_bytes, "image/png")),
                    ],
                )
                response.raise_for_status()
                result = response.json()

            return EmailResponse(
                success=True,
                message="Email đã được gửi thành công",
                message_id=result.get("id", ""),
            )

        except httpx.HTTPStatusError as e:
            raise HTTPException(
                status_code=500,
                detail=f"Lỗi khi gửi email qua Mailgun: {e.response.status_code} - {e.response.text[:200]}"
            )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Lỗi khi xử lý: {str(e)}")

# ─── Job Matching Engine ──────────────────────────────────────────────────────

DIRECTUS_URL = os.getenv("DIRECTUS_URL", "https://app.nexpo.vn")
DIRECTUS_ADMIN_TOKEN = os.getenv("DIRECTUS_ADMIN_TOKEN", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")


class MatchRunRequest(BaseModel):
    event_id: int
    job_requirement_id: Optional[str] = None  # None = match all jobs in event


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


async def directus_get(path: str) -> dict:
    """Fetch from Directus using admin token."""
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


async def score_match_with_gemini(job: dict, visitor_profile: dict) -> dict:
    """Use OpenRouter to score how well a visitor matches a job requirement."""
    if not OPENROUTER_API_KEY:
        return _simple_score_match(job, visitor_profile)

    prompt = f"""You are a hiring assistant. Score how well this job seeker matches the job requirement.

JOB REQUIREMENT:
- Title: {job.get('job_title', 'N/A')}
- Description: {job.get('description', 'N/A')}
- Requirements: {job.get('requirements', 'N/A')}
- Skills needed: {json.dumps(job.get('skills', []))}
- Experience level: {job.get('experience_level', 'N/A')}
- Employment type: {job.get('employment_type', 'N/A')}

JOB SEEKER PROFILE:
{json.dumps(visitor_profile, ensure_ascii=False, indent=2)}

Respond ONLY with valid JSON in this exact format:
{{
  "score": <float 0.0-1.0>,
  "matched_criteria": {{
    "skills_match": <float 0.0-1.0>,
    "experience_match": <float 0.0-1.0>,
    "role_match": <float 0.0-1.0>
  }},
  "reasoning": "<1-2 sentence explanation>"
}}"""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "openai/gpt-4o-mini",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1,
                    "max_tokens": 512,
                },
            )
            resp.raise_for_status()
            result = resp.json()
            text = result["choices"][0]["message"]["content"]
            text = text.strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            parsed = json.loads(text.strip())
            return {
                "score": float(parsed.get("score", 0.5)),
                "matched_criteria": parsed.get("matched_criteria", {}),
                "ai_reasoning": parsed.get("reasoning", ""),
            }
    except Exception as e:
        return _simple_score_match(job, visitor_profile)


def _simple_score_match(job: dict, visitor_profile: dict) -> dict:
    """Fallback: keyword-based matching when Gemini unavailable."""
    job_text = " ".join([
        str(job.get("job_title", "")),
        str(job.get("description", "")),
        str(job.get("requirements", "")),
        " ".join(job.get("skills", []) or []),
    ]).lower()

    profile_text = json.dumps(visitor_profile, ensure_ascii=False).lower()

    # Count keyword overlaps
    job_words = set(job_text.split())
    profile_words = set(profile_text.split())
    # Remove very common words
    stopwords = {"the", "a", "an", "and", "or", "for", "to", "of", "in", "is", "are", "với", "và", "của"}
    job_words -= stopwords
    profile_words -= stopwords

    if not job_words:
        score = 0.5
    else:
        overlap = len(job_words & profile_words)
        score = min(overlap / max(len(job_words), 1) * 2, 1.0)

    return {
        "score": round(score, 2),
        "matched_criteria": {"keyword_overlap": round(score, 2)},
        "ai_reasoning": f"Keyword-based score: {round(score * 100)}% overlap (Gemini API key not configured)",
    }


async def extract_visitor_profile(submission: dict, matching_fields: List[dict]) -> dict:
    """Extract visitor profile from form submission answers."""
    profile = {}
    answers = submission.get("answers", []) or []
    for answer in answers:
        field_id = None
        if isinstance(answer.get("field"), dict):
            field_id = answer["field"].get("id")
        elif isinstance(answer.get("field"), str):
            field_id = answer["field"]

        # Find if this field is a matching field
        matching_field = next((f for f in matching_fields if str(f.get("id")) == str(field_id)), None)
        if matching_field and matching_field.get("use_for_matching"):
            attr = matching_field.get("matching_attribute", "other")
            label = None
            for t in (matching_field.get("translations") or []):
                if t.get("languages_code") in ("en-US", "vi-VN"):
                    label = t.get("label")
                    break
            key = attr if attr else (label or field_id or "field")
            profile[key] = answer.get("value")
    return profile


@app.post("/match/run", response_model=MatchRunResponse)
async def run_job_matching(request: MatchRunRequest):
    """
    Run AI job matching for an event.
    Fetches job requirements + visitor submissions, scores with Gemini,
    and creates job_match_suggestions in Directus.
    """
    if not DIRECTUS_ADMIN_TOKEN:
        raise HTTPException(status_code=500, detail="DIRECTUS_ADMIN_TOKEN not configured")

    event_id = request.event_id

    try:
        # 1. Fetch job requirements for this event
        job_filter = f"filter[event_id][_eq]={event_id}&filter[status][_eq]=published"
        if request.job_requirement_id:
            job_filter = f"filter[id][_eq]={request.job_requirement_id}"

        jobs_resp = await directus_get(
            f"/items/job_requirements?{job_filter}"
            "&fields[]=id,job_title,description,requirements,skills,experience_level,employment_type,exhibitor_id"
            "&limit=100"
        )
        jobs = jobs_resp.get("data", [])

        if not jobs:
            return MatchRunResponse(success=True, message="No published job requirements found", suggestions_created=0)

        # 2. Fetch form fields marked use_for_matching for this event
        fields_resp = await directus_get(
            f"/items/form_fields?filter[event_id][_eq]={event_id}&filter[use_for_matching][_eq]=true"
            "&fields[]=id,name,use_for_matching,matching_attribute,translations.languages_code,translations.label"
            "&limit=200"
        )
        matching_fields = fields_resp.get("data", [])

        # 3. Fetch registrations with their submissions/answers for this event (tier 1)
        regs_resp = await directus_get(
            f"/items/registrations?filter[event_id][_eq]={event_id}"
            "&filter[submissions][_nnull]=true"
            "&fields[]=id,submissions.id,submissions.form,submissions.answers.value,submissions.answers.field.id"
            "&limit=500"
        )
        registrations = regs_resp.get("data", [])

        # Get form IDs that have matching fields
        form_ids_resp = await directus_get(
            f"/items/form_fields?filter[event_id][_eq]={event_id}&filter[use_for_matching][_eq]=true"
            "&fields[]=form_id&limit=50"
        )
        form_ids = {item.get("form_id") for item in form_ids_resp.get("data", []) if item.get("form_id")}

        # Build tier 1 profiles from registration submissions
        tier1_by_registration: dict = {}
        for reg in registrations:
            sub = reg.get("submissions")
            if not sub or not isinstance(sub, dict):
                continue
            sub_form = sub.get("form")
            if form_ids and sub_form not in form_ids:
                continue
            profile = await extract_visitor_profile(
                {"answers": sub.get("answers") or []}, matching_fields
            )
            if profile:
                tier1_by_registration[reg["id"]] = profile

        # 4. Fetch tier 2: matching form submissions linked via registration_id
        # Find the candidate profiles form for this event
        candidate_form_resp = await directus_get(
            f"/items/forms?filter[event_id][_eq]={event_id}"
            "&filter[linked_module][_eq]=candidate_profiles"
            "&fields[]=id&limit=1"
        )
        candidate_forms = candidate_form_resp.get("data", [])

        tier2_by_registration: dict = {}
        tier2_matching_fields: list = []

        if candidate_forms:
            candidate_form_id = candidate_forms[0]["id"]

            # Fetch form fields for the candidate form that are tagged use_for_matching
            t2_fields_resp = await directus_get(
                f"/items/form_fields?filter[form_id][_eq]={candidate_form_id}&filter[use_for_matching][_eq]=true"
                "&fields[]=id,name,use_for_matching,matching_attribute,translations.languages_code,translations.label"
                "&limit=200"
            )
            tier2_matching_fields = t2_fields_resp.get("data", [])

            # Fetch all submissions for this form that have a registration_id
            t2_subs_resp = await directus_get(
                f"/items/form_submissions?filter[form][_eq]={candidate_form_id}"
                "&filter[registration_id][_nnull]=true"
                "&fields[]=id,registration_id,answers.value,answers.field.id"
                "&limit=1000"
            )
            for sub in t2_subs_resp.get("data", []):
                reg_id = sub.get("registration_id")
                if not reg_id:
                    continue
                reg_id = reg_id if isinstance(reg_id, str) else str(reg_id)
                profile = await extract_visitor_profile(sub, tier2_matching_fields)
                if profile:
                    tier2_by_registration[reg_id] = profile

        # Merge tier 1 + tier 2: tier 2 takes priority per attribute
        all_registration_ids = set(tier1_by_registration.keys()) | set(tier2_by_registration.keys())
        submissions = []
        for reg_id in all_registration_ids:
            merged = {**(tier1_by_registration.get(reg_id) or {}), **(tier2_by_registration.get(reg_id) or {})}
            if merged:
                submissions.append({"registration_id": reg_id, "answers": [], "_merged_profile": merged})

        if not submissions:
            return MatchRunResponse(success=True, message="No visitor profiles found for matching", suggestions_created=0)

        # 4. Score each job × visitor pair and create suggestions
        suggestions_created = 0
        all_suggestions: List[MatchSuggestion] = []
        SCORE_THRESHOLD = 0.2  # Only save suggestions above this score

        for job in jobs:
            exhibitor_id = job.get("exhibitor_id")
            for submission in submissions:
                registration_id = submission.get("registration_id")
                if not registration_id:
                    continue

                # Use pre-merged profile if available, otherwise extract from answers
                visitor_profile = submission.get("_merged_profile") or await extract_visitor_profile(submission, matching_fields)
                if not visitor_profile:
                    continue

                # Score with Gemini
                score_result = await score_match_with_gemini(job, visitor_profile)
                score = score_result["score"]

                if score < SCORE_THRESHOLD:
                    continue

                suggestion = MatchSuggestion(
                    job_requirement_id=str(job["id"]),
                    registration_id=str(registration_id) if isinstance(registration_id, (str, int)) else str(registration_id.get("id", "")),
                    exhibitor_id=str(exhibitor_id) if exhibitor_id else "",
                    score=score,
                    matched_criteria=score_result["matched_criteria"],
                    ai_reasoning=score_result["ai_reasoning"],
                )
                all_suggestions.append(suggestion)

                # Check if suggestion already exists
                existing_resp = await directus_get(
                    f"/items/job_match_suggestions"
                    f"?filter[event_id][_eq]={event_id}"
                    f"&filter[job_requirement_id][_eq]={job['id']}"
                    f"&filter[registration_id][_eq]={suggestion.registration_id}"
                    "&fields[]=id&limit=1"
                )
                existing = existing_resp.get("data", [])

                suggestion_data = {
                    "event_id": event_id,
                    "job_requirement_id": suggestion.job_requirement_id,
                    "registration_id": suggestion.registration_id,
                    "exhibitor_id": suggestion.exhibitor_id if suggestion.exhibitor_id else None,
                    "score": round(score, 4),
                    "matched_criteria": suggestion.matched_criteria,
                    "ai_reasoning": suggestion.ai_reasoning,
                    "status": "pending",
                }

                if existing:
                    # Update existing suggestion score
                    await directus_patch(
                        f"/items/job_match_suggestions/{existing[0]['id']}",
                        suggestion_data,
                    )
                else:
                    await directus_post("/items/job_match_suggestions", suggestion_data)
                    suggestions_created += 1

        return MatchRunResponse(
            success=True,
            message=f"Matching complete. {suggestions_created} new suggestions created.",
            suggestions_created=suggestions_created,
            suggestions=all_suggestions,
        )

    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"Directus error: {e.response.status_code} {e.response.text[:200]}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Matching error: {str(e)}")


# ─── Email Template AI Generation ────────────────────────────────────────────

class EmailTemplateField(BaseModel):
    id: str
    label: str
    type: str

class GenerateEmailTemplateRequest(BaseModel):
    event_name: str
    form_purpose: Optional[str] = "registration"
    is_registration: bool = True
    language: str = "bilingual"   # "vi" | "en" | "bilingual"
    tone: str = "professional"    # "professional" | "friendly" | "formal"
    fields: List[EmailTemplateField] = []

class GenerateEmailTemplateResponse(BaseModel):
    html: str
    success: bool

@app.post("/generate-email-template", response_model=GenerateEmailTemplateResponse)
async def generate_email_template(request: GenerateEmailTemplateRequest):
    """Generate a styled HTML email template using AI based on form fields and event context."""
    if not OPENROUTER_API_KEY:
        raise HTTPException(status_code=503, detail="OpenRouter API key not configured")

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

    # Build field list with explicit bilingual label instruction
    field_list = "\n".join(
        f'  - Variable: ${{{f.id}}} | Label: "{f.label}" | Type: {f.type}'
        for f in request.fields
    )

    form_context = "registration confirmation" if request.is_registration else (request.form_purpose or "form submission confirmation")

    # Find name-like fields for greeting
    name_field_hint = ""
    for f in request.fields:
        if any(kw in f.label.lower() for kw in ["name", "họ tên", "tên", "full name", "họ và tên"]):
            name_field_hint = f"Use ${{{f.id}}} as the registrant's name in the greeting."
            break

    prompt = f"""You are a world-class HTML email designer. Create a stunning, polished HTML email template for a {form_context} email. Think of award-winning transactional emails from top tech companies.

EVENT: {request.event_name}
LANGUAGE: {lang_instruction}
TONE: {tone_instruction}
{name_field_hint}

FORM FIELDS (insert these variables exactly as shown):
{field_list}

═══════════════════════════════════════════════
DESIGN SYSTEM — follow EXACTLY:
═══════════════════════════════════════════════

COLOR PALETTE:
  - Page background: #F0F4F8
  - Card background: #FFFFFF
  - Header gradient: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%)
  - Accent / primary: #E94560  (use for badges, highlights, confirmation badge border)
  - Section header bar: #0f3460 (dark navy)
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
   - Sub: "{form_context.title()}" subtitle in #94A3B8, 15px
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
     - Right cell: use the exact variable syntax ${{field_id}} from the FORM FIELDS list, 15px, color #1E293B, font-weight: 500, padding: 12px 16px
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
   - Bottom: "Đây là email tự động, vui lòng không trả lời. / This is an automated email, please do not reply." in #64748B, 12px, font-style: italic

═══════════════════════════════════════════════
STRICT RULES:
═══════════════════════════════════════════════
- Return ONLY the raw HTML. No markdown fences, no explanation, no comments outside HTML.
- ALL CSS must be inline (style="..."). Zero <style> tags, zero classes.
- Use table/tr/td for ALL layout — no div-based layout (email client compatibility).
- Every ${{uuid}} variable must appear exactly as written — never substitute with label text.
- Use {{event_name}} (curly braces, NO dollar sign) only if referencing the event name dynamically outside the header.
- border-radius on tables: use on the outer wrapper td, not on the table element itself for Outlook compat.
- For gradient backgrounds on table cells, use: background: #1a1a2e; (solid fallback first, then background-image: linear-gradient(...))

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

            # Strip markdown code fences if model wrapped the output
            if html.startswith("```"):
                lines = html.split("\n")
                html = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

            return GenerateEmailTemplateResponse(html=html, success=True)

    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"OpenRouter error: {e.response.text[:200]}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Generation failed: {str(e)}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
