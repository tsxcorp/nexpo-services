# nexpo-services — Python Backend Services

## 🎯 Project Identity
- **Role**: Backend microservice
- **Domain**: `services.nexpo.vn`
- **Users**: Internal — called by nexpo-admin, nexpo-portal
- **Purpose**: QR code generation, email delivery (Mailgun), AI job matching (OpenRouter), email template AI generation

## 🛠️ Tech Stack
- Python 3.x + FastAPI
- httpx (async HTTP client)
- qrcode (QR generation)
- Mailgun API (email delivery)
- OpenRouter API → GPT-4o-mini (AI matching + template generation)
- Directus REST API (data source)

## 📡 API Endpoints
```
GET  /                          — Health check
POST /gen-qr                    — Generate QR code (returns base64 PNG)
POST /send-email-with-qr        — Send email with embedded QR via Mailgun
                                  Params: from_email, to, subject, html, content_qr
                                  + link_type: "registration"|"ticket" (default: "registration")
POST /send-email                — Send plain email without QR (coming — ticketing)
POST /match/run                 — Run AI job matching for an event
POST /generate-email-template   — AI-generated HTML email template
```

## 🔐 Environment Variables
```env
MAILGUN_API_KEY=...
MAILGUN_DOMAIN=...
MAILGUN_API_URL=https://api.mailgun.net
DIRECTUS_URL=https://app.nexpo.vn
DIRECTUS_ADMIN_TOKEN=...        # Admin token for Directus read/write
OPENROUTER_API_KEY=...          # For AI features
```

## ⚠️ CORS Configuration (MUST include ALL these origins)
```python
allow_origins=[
    "https://app.nexpo.vn",
    "https://admin.nexpo.vn",
    "https://portal.nexpo.vn",      # ← add nexpo-portal
    "https://insights.nexpo.vn",    # ← add nexpo-insight
    "http://localhost:3000",
    "http://localhost:3001",
    "http://localhost:3002",        # ← insight dev port
    "http://localhost:3003",        # ← portal dev port
]
```

## 📝 Code Conventions
- **Structure**: Refactor `main.py` → separate router modules:
  ```
  app/
  ├── main.py           — FastAPI app init + CORS
  ├── routers/
  │   ├── qr.py         — QR endpoints
  │   ├── email.py      — Email endpoints
  │   ├── matching.py   — AI matching
  │   └── templates.py  — Email template generation
  ├── services/
  │   ├── directus.py   — Directus API client
  │   ├── mailgun.py    — Mailgun client
  │   └── openrouter.py — AI client
  └── models/           — Pydantic models
  ```
- **Error handling**: Return structured errors with `detail` field; log internally
- **Async**: All endpoints must be `async def` — use `httpx.AsyncClient`
- **Timeouts**: Always set `timeout=30` on external HTTP calls
- **Type hints**: All functions must have full type annotations

## 🤖 AI Matching Rules
- Score threshold: `0.2` (only save suggestions above this)
- Fallback: keyword-based scoring when OpenRouter unavailable
- Tier 1: registration form answers
- Tier 2: candidate profile form answers (higher priority)
- Upsert pattern: check if suggestion exists before inserting

## 📧 Email Rules
- Always embed QR as CID inline attachment (not base64 in HTML)
- Inject UUID display + Insight Hub button after QR image
- Support bilingual templates (vi/en)

## 🎟️ Ticketing System (Coming)

> Full schema & plan: `nexpo-platform/.claude/ticketing-schema.md`

### ⚠️ Sửa `POST /send-email-with-qr` — link_type param

Hàm `inject_qr_extras()` hiện hardcode link `https://insights.nexpo.vn/{content_qr}`.
Với ticketed event, link phải là `https://insights.nexpo.vn/ticket/{ticket_code}`.

```python
class EmailRequest(BaseModel):
    from_email: str
    to: str
    subject: str
    html: str
    content_qr: str
    link_type: str = "registration"  # "registration" | "ticket"  ← THÊM MỚI

# Trong inject_qr_extras():
if link_type == "ticket":
    insight_url = f"https://insights.nexpo.vn/ticket/{content_qr}"
else:
    insight_url = f"https://insights.nexpo.vn/{content_qr}"
```

### Thêm APScheduler — expire pending orders

```
requirements.txt: thêm apscheduler==3.10.4
```

```python
# FastAPI lifespan pattern (KHÔNG dùng @app.on_event deprecated)
from contextlib import asynccontextmanager
from apscheduler.schedulers.asyncio import AsyncIOScheduler

scheduler = AsyncIOScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.add_job(expire_pending_orders, 'interval', minutes=5, id='expire_orders')
    scheduler.start()
    yield
    scheduler.shutdown()

app = FastAPI(lifespan=lifespan)

async def expire_pending_orders():
    # readItems ticket_orders WHERE status=pending AND expires_at < now()
    # updateItems: status = expired
    # rollback quantity_sold cho mỗi order_item
```

### Endpoint mới cần thêm — `POST /send-email`

Email không có QR (dùng cho: payment failed, order expired, claim link summary):

```python
class PlainEmailRequest(BaseModel):
    from_email: str
    to: str
    subject: str
    html: str

@app.post("/send-email")
async def send_plain_email(request: PlainEmailRequest):
    # Gửi qua Mailgun, không inject QR
    # Tương tự send-email-with-qr nhưng bỏ bước QR generation + inject
```

### Email trigger map

| Email | Gọi từ | Endpoint |
|---|---|---|
| Order confirmed (có QR) | nexpo-public | `/send-email-with-qr` với `link_type: "ticket"` |
| Claim link summary | nexpo-public | `/send-email` (no QR) |
| Claim completed | nexpo-public | `/send-email-with-qr` với `link_type: "registration"` |
| Payment failed | nexpo-public | `/send-email` (no QR) |
| Order expired | nexpo-services APScheduler | Mailgun trực tiếp trong Python |

### Email template mới — ticket confirmation

Gọi `/generate-email-template` với `form_purpose = "ticket_confirmation"`.
Email cần chứa: QR code + danh sách N claim links (per_ticket mode) hoặc N QR codes (none mode).

---

## 🚫 Do NOT
- Do NOT store secrets in code — use environment variables
- Do NOT use sync HTTP calls (`requests`) — always use `httpx.AsyncClient`
- Do NOT return raw Directus errors to clients — sanitize error messages
- Do NOT add new features to the monolithic `main.py` — create router modules
- Do NOT hardcode `insights.nexpo.vn/{id}` — luôn check `link_type` param

## 🔗 Related Projects
- `nexpo-admin`: Triggers matching via `/match/run`, uses email template generation
- `nexpo-public`: **Ticket checkout** — gọi `/send-email-with-qr` sau khi order paid (truyền `link_type: "ticket"`)
- `nexpo-portal`: May call `/gen-qr` for QR display
- **Directus**: `https://app.nexpo.vn` — data source for matching
- **Mailgun**: Email delivery provider
