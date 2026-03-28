"""
Bot entry point.

Start with:
    python -m notify_bot.run_bot

Or via Docker CMD (already configured in Dockerfile):
    CMD ["python", "-m", "notify_bot.run_bot"]
"""

from __future__ import annotations

import atexit
import logging
import os
import pathlib
import urllib.request

import telegram.error
from telegram import BotCommand
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from notify_bot import config, db
from notify_bot.handlers.admin import (
    approval_callback,
    approve_cmd,
    deny_cmd,
    myip_cmd,
    pending_cmd,
    users_cmd,
)
from notify_bot.handlers.common import help_command, request_access, start
from notify_bot.handlers.enroll import build_enroll_handler, unenroll_command
from notify_bot.handlers.eur import eur_command
from notify_bot.handlers.obligations import (
    clamp_command,
    driver_command,
    fines_command,
    gtp_command,
    mtpl_command,
    plate_command,
    sticker_command,
    vehicle_command,
    vignette_command,
)
from notify_bot.scheduler.jobs import daily_obligations_report

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=os.environ.get("LOGLEVEL", "INFO").upper(),
)
logger = logging.getLogger(__name__)


def _register_atexit_logout(token: str) -> None:
    """Register a best-effort synchronous logout on process exit.

    Calling getUpdates with timeout=0 releases our long-poll slot so the
    next instance that starts does not immediately get a 409 Conflict.
    """

    def _logout() -> None:
        try:
            url = f"https://api.telegram.org/bot{token}/getUpdates?timeout=0&limit=1"
            urllib.request.urlopen(url, timeout=5)  # noqa: S310
        except Exception:
            pass

    atexit.register(_logout)


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler. Stops the bot on Conflict (duplicate instance)."""
    err = context.error
    if isinstance(err, telegram.error.Conflict):
        logger.critical(
            "Conflict: another bot instance is running. Stopping this instance."
        )
        # Schedule a graceful stop so the event loop can wind down cleanly.
        context.application.stop_running()
        return
    logger.warning("Unhandled update error: %s", err, exc_info=err)


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

    # Steal the polling slot from any competing instance that is still running.
    # getUpdates with timeout=0 causes Telegram to cancel the other instance's
    # active long-poll and immediately return to us with a 200, so WE win the slot.
    try:
        await application.bot.get_updates(timeout=0)
        logger.info("Polling slot acquired (any previous instance evicted)")
    except Exception as exc:
        logger.warning("Could not pre-steal polling slot: %s", exc)

    # Register the Telegram command menu
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
            BotCommand("gtp", "Check technical inspection validity"),
            BotCommand("mtpl", "Check civil liability (MTPL) insurance"),
            BotCommand("fines", "Check traffic fines (KAT)"),
            BotCommand("vehicle", "Show vehicle registration data"),
            BotCommand("myip", "Show bot's public IP (admin only)"),
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
    application.add_handler(CommandHandler("myip", myip_cmd))

    # ── Feature commands ──────────────────────────────────────────────────────
    application.add_handler(CommandHandler("change", eur_command))
    application.add_handler(CommandHandler("unenroll", unenroll_command))
    application.add_handler(CommandHandler("driver", driver_command))
    application.add_handler(CommandHandler("plate", plate_command))
    application.add_handler(CommandHandler("vignette", vignette_command))
    application.add_handler(CommandHandler("sticker", sticker_command))
    application.add_handler(CommandHandler("clamp", clamp_command))
    application.add_handler(CommandHandler("gtp", gtp_command))
    application.add_handler(CommandHandler("mtpl", mtpl_command))
    application.add_handler(CommandHandler("fines", fines_command))
    application.add_handler(CommandHandler("vehicle", vehicle_command))

    # ── Inline button callbacks ───────────────────────────────────────────────
    # Pattern must be registered before a generic catch-all if one were added
    application.add_handler(CallbackQueryHandler(approval_callback, pattern=r"^(approve|deny):"))

    # ── Global error handler ──────────────────────────────────────────────────
    application.add_error_handler(_error_handler)

    # Register atexit logout so our polling slot is released on exit,
    # meaning the next instance won't get a 409 Conflict on startup.
    _register_atexit_logout(config.TOKEN)

    logger.info(
        "Bot starting — admin_id=%s, db=%s, report_time=%s UTC",
        config.ADMIN_TELEGRAM_ID,
        config.DATABASE_PATH,
        config.DAILY_REPORT_TIME,
    )
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    run_bot()
