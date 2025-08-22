import os
from dataclasses import dataclass
from dotenv import load_dotenv
load_dotenv()


def _getenv(name: str, default: str | None = None) -> str:
    val = os.getenv(name, default)
    if val is None or val == "":
        raise RuntimeError(f"Missing required env var: {name}")
    return val

@dataclass(frozen=True)
class Settings:
    BOT_TOKEN: str = _getenv("BOT_TOKEN")
    ADMIN_ID: int = int(_getenv("ADMIN_ID", "0"))
    LOG_CHAT_ID: int = int(os.getenv("LOG_CHAT_ID", os.getenv("ADMIN_ID", "0")))
    TIMEZONE: str = os.getenv("TIMEZONE", "UTC")
    STARS_CURRENCY: str = os.getenv("STARS_CURRENCY", "XTR")
    # БД: если не задано — локальный SQLite-файл
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///giftbot.db")

settings = Settings()
