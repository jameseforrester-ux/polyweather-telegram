"""Entrypoint."""
from __future__ import annotations
import os
import logging

from dotenv import load_dotenv
from telegram.ext import Application

load_dotenv()  # picks up .env at repo root

from .db import init_schema
from .ingest.climatology import load_airports
from . import bot as bot_module
from . import scheduler as sched

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
)
log = logging.getLogger("polywx")


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN missing in .env")
    if not os.getenv("TELEGRAM_OWNER_CHAT_ID"):
        log.warning("TELEGRAM_OWNER_CHAT_ID is not set — bot will refuse all messages.")

    init_schema()
    load_airports()

    app = Application.builder().token(token).build()
    bot_module.register(app)

    jq = app.job_queue
    metar_s = int(os.getenv("METAR_POLL_SECONDS", "600"))
    poly_s  = int(os.getenv("POLYMARKET_POLL_SECONDS", "300"))
    hrrr_s  = int(os.getenv("HRRR_POLL_SECONDS", "3600"))
    recomp_s = int(os.getenv("RECOMPUTE_SECONDS", "600"))

    # Stagger first-runs so we don't slam everything at once
    jq.run_repeating(sched.job_polymarket, interval=poly_s, first=5)
    jq.run_repeating(sched.job_metar, interval=metar_s, first=20)
    jq.run_repeating(sched.job_hrrr, interval=hrrr_s, first=60)
    jq.run_repeating(sched.job_alerts, interval=recomp_s, first=120)

    log.info("Polling Telegram...")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
