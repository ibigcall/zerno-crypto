"""Конфигурация из окружения (.env рядом с корнем проекта)."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _int(name, default):
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _bool(name, default=False):
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# FreeCryptoAPI
FCA_API_KEY = (os.getenv("FCA_API_KEY") or "").strip()
FCA_BASE_URL = (os.getenv("FCA_BASE_URL") or "https://api.freecryptoapi.com/v1").rstrip("/")
FCA_DEMO = _bool("FCA_DEMO") or not FCA_API_KEY

CACHE_TTL_QUOTES = _int("CACHE_TTL_QUOTES", 300)
CACHE_TTL_OHLC = _int("CACHE_TTL_OHLC", 3600)
CACHE_TTL_FG = _int("CACHE_TTL_FG", 1800)
CACHE_TTL_LIST = _int("CACHE_TTL_LIST", 86400)
CACHE_TTL_ANALYSIS = _int("CACHE_TTL_ANALYSIS", 3600)
SPARK_DAYS = max(5, _int("SPARK_DAYS", 14))
ENABLE_TECHNICAL = _bool("ENABLE_TECHNICAL")
ENABLE_NEWS = _bool("ENABLE_NEWS")

# Ollama
OLLAMA_URL = (os.getenv("OLLAMA_URL") or "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL") or "deepseek-r1:latest"
OLLAMA_TIMEOUT = _int("OLLAMA_TIMEOUT", 300)
OLLAMA_NUM_CTX = _int("OLLAMA_NUM_CTX", 8192)

# Backend
HOST = os.getenv("HOST") or "127.0.0.1"
PORT = _int("PORT", 5310)
_db = os.getenv("DB_PATH") or "data/zerno.sqlite3"
DB_PATH = str(ROOT / _db) if not os.path.isabs(_db) else _db
DEFAULT_WATCHLIST = [
    s.strip().upper()
    for s in (os.getenv("DEFAULT_WATCHLIST") or "BTC,ETH,SOL,TON,ARB,LINK").split(",")
    if s.strip()
]
BOT_API_TOKEN = (os.getenv("BOT_API_TOKEN") or "").strip()

# Telegram
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
API_BASE = (os.getenv("API_BASE") or f"http://{HOST}:{PORT}").rstrip("/")
TELEGRAM_ALLOWED_CHATS = [
    s.strip() for s in (os.getenv("TELEGRAM_ALLOWED_CHATS") or "").split(",") if s.strip()
]

FRONTEND_DIR = ROOT / "frontend"
