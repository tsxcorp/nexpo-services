"""
nexpo-services — FastAPI application entry point.

All endpoint logic lives in app/routers/. Shared helpers in app/services/.
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.services.scheduler import scheduler, send_meeting_reminders, expire_pending_orders, send_trial_reminders, expire_form_drafts
from app.routers import qr, email, matching, meeting_notifs, notify, templates, pdf_export, floor_plan
from app.routers import subscriptions, webhooks_polar, webhooks_payos_subscription, invoices, coupons
from app.services.dunning_service import process_dunning


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
    scheduler.add_job(
        process_dunning,
        'cron',
        hour=1,  # 8:00 AM VN time (UTC+7)
        minute=0,
        id='subscription_dunning',
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        send_trial_reminders,
        'cron',
        hour=1,  # 8:00 AM VN time (UTC+7)
        minute=30,
        id='trial_reminders',
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        expire_form_drafts,
        'interval',
        hours=1,
        id='expire_form_drafts',
        replace_existing=True,
        misfire_grace_time=300,
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
        "https://namkhoi.nexpo.vn"
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
app.include_router(subscriptions.router)
app.include_router(webhooks_polar.router)
app.include_router(webhooks_payos_subscription.router)
app.include_router(invoices.router)
app.include_router(coupons.router)
app.include_router(floor_plan.router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
