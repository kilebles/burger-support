from pathlib import Path

from pydantic_settings import BaseSettings

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"


class Settings(BaseSettings):
    token: str
    support_chat_id: str
    remnawave_url: str = ""
    remnawave_token: str = ""
    remnawave_cookie: str = ""
    gemini_api_key: str = ""
    # Цепочка моделей через запятую: первая основная, остальные — на случай перегрузки.
    gemini_models: str = "gemini-3.8-flash,gemini-3.7-flash,gemini-3.5-flash"
    admin_tags: str = ""

    class Config:
        env_file = BASE_DIR / ".env"


config = Settings()

TOKEN = config.token
SUPPORT_CHAT_ID = config.support_chat_id
REMNAWAVE_URL = config.remnawave_url
REMNAWAVE_TOKEN = config.remnawave_token
REMNAWAVE_COOKIE = config.remnawave_cookie
GEMINI_API_KEY = config.gemini_api_key
GEMINI_MODELS = [m.strip() for m in config.gemini_models.split(",") if m.strip()]
ADMIN_TAGS = config.admin_tags
