from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from scheduler.jobs import (job_daily_report, job_monthly_report,
    job_reminder_1, job_reminder_2, job_late_alert,
    job_auto_late_charge, job_auto_cleanup)
from services.database import get_all_hotels
import logging

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")


async def start_scheduler():
    # Cleanup every 30 min — always
    scheduler.add_job(job_auto_cleanup, CronTrigger(minute="*/30", timezone="Asia/Kolkata"),
                      id="auto_cleanup", replace_existing=True)

    hotels = await get_all_hotels()
    for h in hotels:
        if not h.get("is_active"): continue
        hid = h["id"]

        scheduler.add_job(job_daily_report,
            CronTrigger(hour=h.get("sched_daily_report_hour",7), minute=0, timezone="Asia/Kolkata"),
            id=f"daily_{hid}", replace_existing=True)

        scheduler.add_job(job_monthly_report,
            CronTrigger(day=1, hour=h.get("sched_monthly_report_hour",9), minute=0, timezone="Asia/Kolkata"),
            id=f"monthly_{hid}", replace_existing=True)

        scheduler.add_job(job_reminder_1,
            CronTrigger(hour=h.get("sched_reminder1_hour",21), minute=0, timezone="Asia/Kolkata"),
            id=f"rem1_{hid}", replace_existing=True)

        scheduler.add_job(job_reminder_2,
            CronTrigger(hour=h.get("sched_reminder2_hour",10),
                        minute=h.get("sched_reminder2_min",30), timezone="Asia/Kolkata"),
            id=f"rem2_{hid}", replace_existing=True)

        scheduler.add_job(job_late_alert,
            CronTrigger(hour=h.get("sched_late_alert_hour",11),
                        minute=h.get("sched_late_alert_min",30), timezone="Asia/Kolkata"),
            id=f"late_{hid}", replace_existing=True)

        scheduler.add_job(job_auto_late_charge,
            CronTrigger(hour=h.get("sched_auto_charge_hour",12),
                        minute=h.get("sched_auto_charge_min",0), timezone="Asia/Kolkata"),
            id=f"charge_{hid}", replace_existing=True)

        logger.info(f"Scheduled jobs for: {h['hotel_name']}")

    scheduler.start()
    logger.info(f"Scheduler started — {len(scheduler.get_jobs())} jobs")


def stop_scheduler():
    if scheduler.running: scheduler.shutdown()
