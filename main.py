"""
nexpo-services — FastAPI application entry point.

All endpoint logic lives in app/routers/. Shared helpers in app/services/.
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.services.scheduler import scheduler, send_meeting_reminders, expire_pending_orders
from app.routers import qr, email, matching, meeting_notifs, notify, templates, pdf_export


@asynccontextmanager
async def lifespan(app: FastAPI):
    async def _run_reminders():
        await send_meeting_reminders()

    async def _expire_orders():
        await expire_pending_orders()

    scheduler.add_job(
        _run_reminders,
        'interval',
        hours=1,
        id='meeting_reminders',
        replace_existing=True,
        misfire_grace_time=300,
    )
    scheduler.add_job(
        _expire_orders,
        'interval',
        minutes=5,
        id='expire_pending_orders',
        replace_existing=True,
        misfire_grace_time=60,
    )
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="Nexpo Services API", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://app.nexpo.vn",
        "http://app.nexpo.vn",
        "https://admin.nexpo.vn",
        "https://portal.nexpo.vn",
        "https://insights.nexpo.vn",
        "http://localhost:3000",
        "http://localhost:3001",
        "http://localhost:3002",
        "http://localhost:3003",
        "https://cms.nexpo.vn",
        "http://namkhoi.nexpo.vn"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(notify.router)
app.include_router(qr.router)
app.include_router(email.router)
app.include_router(matching.router)
app.include_router(meeting_notifs.router)
app.include_router(templates.router)
app.include_router(pdf_export.router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
