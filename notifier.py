import logging
import logging.handlers
import os
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from config import config

TZ_CN = timezone(timedelta(hours=8))

# Setup logging
logger = logging.getLogger("dabao")
logger.setLevel(logging.INFO)

_LOG_FORMAT = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

# Console handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(_LOG_FORMAT)
logger.addHandler(console_handler)

# Daily rotating file handler → logs/dabao_2026-04-08.log
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)


class DailyFileHandler(logging.Handler):
    """Creates a new log file per day (UTC+8), e.g. logs/dabao_2026-04-08.log."""

    def __init__(self, log_dir: str):
        super().__init__()
        self.log_dir = log_dir
        self._current_date = None
        self._handler = None

    def _get_handler(self):
        today = datetime.now(TZ_CN).strftime("%Y-%m-%d")
        if today != self._current_date:
            if self._handler:
                self._handler.close()
            self._current_date = today
            path = os.path.join(self.log_dir, f"dabao_{today}.log")
            self._handler = logging.FileHandler(path, encoding="utf-8")
            self._handler.setFormatter(_LOG_FORMAT)
        return self._handler

    def emit(self, record):
        self._get_handler().emit(record)

    def close(self):
        if self._handler:
            self._handler.close()
        super().close()


file_handler = DailyFileHandler(LOG_DIR)
logger.addHandler(file_handler)


def _mode_prefix() -> str:
    return "[实盘]" if config.is_live else "[模拟]"


def notify(title: str, message: str):
    """Send notification via mode-specific channels.
    Live: Bark only. Paper: PushDeer + Telegram."""
    prefixed_title = f"{_mode_prefix()} {title}"
    logger.info(f"{prefixed_title} | {message}")
    if config.is_live:
        _send_bark(prefixed_title, message)
    else:
        _send_pushdeer(prefixed_title, message)
        _send_telegram(prefixed_title, message)


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


def _send_pushdeer(title: str, message: str):
    if not config.PUSHDEER_ENABLED or not config.PUSHDEER_KEYS:
        return
    for pushkey in config.PUSHDEER_KEYS:
        try:
            params = urllib.parse.urlencode({
                "pushkey": pushkey,
                "text": title,
                "desp": message,
                "type": "text",
            }).encode("utf-8")
            req = urllib.request.Request(
                "https://api2.pushdeer.com/message/push",
                data=params,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                pass
        except Exception as e:
            logger.warning(f"PushDeer notification failed: {e}")


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
