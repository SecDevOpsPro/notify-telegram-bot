"""
Bot entry point.

Start with:
    python -m notify_bot.run_bot

Or via Docker CMD (already configured in Dockerfile):
    CMD ["python", "-m", "notify_bot.run_bot"]
"""

from __future__ import annotations

import logging
import os
import pathlib

from telegram import BotCommand
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from notify_bot import config, db
from notify_bot.handlers.admin import (
    approval_callback,
    approve_cmd,
    deny_cmd,
    pending_cmd,
    users_cmd,
)
from notify_bot.handlers.common import help_command, request_access, start
from notify_bot.handlers.enroll import build_enroll_handler, unenroll_command
from notify_bot.handlers.eur import eur_command
from notify_bot.handlers.obligations import (
    clamp_command,
    driver_command,
    plate_command,
    sticker_command,
    vignette_command,
)
from notify_bot.scheduler.jobs import daily_obligations_report

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=os.environ.get("LOGLEVEL", "INFO").upper(),
)
logger = logging.getLogger(__name__)


async def _post_init(application: Application) -> None:
    """
    Called by PTB after the Application is built but before polling starts.
    Used to initialise the database and register the daily scheduled job.
    """
    # Ensure the data directory exists
    db_dir = pathlib.Path(config.DATABASE_PATH).parent
    db_dir.mkdir(parents=True, exist_ok=True)

    await db.init_db()
    logger.info("Database initialised at %s", config.DATABASE_PATH)

    # Register commands so the Telegram menu (/) shows them all
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Welcome message"),
            BotCommand("help", "Show all available commands"),
            BotCommand("request", "Ask the admin for access"),
            BotCommand("change", "EUR exchange rates (Cuba)"),
            BotCommand("enroll", "Save your personal data"),
            BotCommand("unenroll", "Delete your saved profile data"),
            BotCommand("driver", "Check driving licence obligations (MVR)"),
            BotCommand("plate", "Check vehicle obligations (MVR)"),
            BotCommand("vignette", "Check road e-vignette (bgtoll.bg)"),
            BotCommand("sticker", "Check Sofia parking sticker"),
            BotCommand("clamp", "Check wheel-clamp status"),
        ]
    )
    logger.info("Bot commands menu updated")

    job_queue = application.job_queue
    if job_queue:
        job_queue.run_daily(daily_obligations_report, time=config.DAILY_REPORT_TIME)
        logger.info("Daily report scheduled at %s UTC", config.DAILY_REPORT_TIME)
    else:
        logger.warning(
            "JobQueue not available — daily reports will not run. "
            "Install python-telegram-bot[job-queue] to enable."
        )


def run_bot() -> None:
    """Build and start the bot application."""
    if not config.TOKEN:
        raise RuntimeError(
            "TOKEN environment variable is not set. "
            "Create a bot via @BotFather and export its token."
        )

    application = (
        Application.builder()
        .token(config.TOKEN)
        .concurrent_updates(True)
        .post_init(_post_init)
        .build()
    )

    # ── ConversationHandlers must come first ──────────────────────────────────
    application.add_handler(build_enroll_handler())

    # ── Public commands ───────────────────────────────────────────────────────
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("request", request_access))

    # ── Admin commands ────────────────────────────────────────────────────────
    application.add_handler(CommandHandler("approve", approve_cmd))
    application.add_handler(CommandHandler("deny", deny_cmd))
    application.add_handler(CommandHandler("pending", pending_cmd))
    application.add_handler(CommandHandler("users", users_cmd))

    # ── Feature commands ──────────────────────────────────────────────────────
    application.add_handler(CommandHandler("change", eur_command))
    application.add_handler(CommandHandler("unenroll", unenroll_command))
    application.add_handler(CommandHandler("driver", driver_command))
    application.add_handler(CommandHandler("plate", plate_command))
    application.add_handler(CommandHandler("vignette", vignette_command))
    application.add_handler(CommandHandler("sticker", sticker_command))
    application.add_handler(CommandHandler("clamp", clamp_command))

    # ── Inline button callbacks ───────────────────────────────────────────────
    # Pattern must be registered before a generic catch-all if one were added
    application.add_handler(CallbackQueryHandler(approval_callback, pattern=r"^(approve|deny):"))

    logger.info(
        "Bot starting — admin_id=%s, db=%s, report_time=%s UTC",
        config.ADMIN_TELEGRAM_ID,
        config.DATABASE_PATH,
        config.DAILY_REPORT_TIME,
    )
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    run_bot()
