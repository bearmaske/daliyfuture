import logging
import urllib.request
import urllib.parse
from config import config

# Setup logging
logger = logging.getLogger("dabao")
logger.setLevel(logging.INFO)

# Console handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(
    logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
)
logger.addHandler(console_handler)

# File handler
file_handler = logging.FileHandler(config.LOG_FILE)
file_handler.setFormatter(
    logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
)
logger.addHandler(file_handler)


def notify(title: str, message: str):
    """Send notification via all channels. Failures are logged but don't propagate."""
    logger.info(f"{title} | {message}")
    _send_telegram(title, message)
    _send_bark(title, message)


def _send_telegram(title: str, message: str):
    if not config.TELEGRAM_ENABLED or not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return
    try:
        from telegram import Bot
        import asyncio

        async def _send():
            bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
            await bot.send_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                text=f"*{title}*\n{message}",
                parse_mode="Markdown",
            )

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_send())
        except RuntimeError:
            asyncio.run(_send())
    except Exception as e:
        logger.warning(f"Telegram notification failed: {e}")


def _send_bark(title: str, message: str):
    if not config.BARK_ENABLED or not config.BARK_URLS:
        return
    import json as _json
    for bark_url in config.BARK_URLS:
        try:
            payload = _json.dumps({"title": title, "body": message}).encode("utf-8")
            req = urllib.request.Request(
                bark_url,
                data=payload,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                pass
        except Exception as e:
            logger.warning(f"Bark notification failed ({bark_url}): {e}")
