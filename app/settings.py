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
    gpuniq_api_key: str = ""
    gpuniq_base_url: str = "https://api.gpuniq.com"
    anthropic_api_key: str = ""
    ai_model: str = "gpt-6-luna"
    ai_fallback_model: str = "gemini-3-flash"
    anthropic_fallback_model: str = "claude-haiku-4-5-20251001"
    admin_tags: str = "@asradyuk"

    class Config:
        env_file = BASE_DIR / ".env"


config = Settings()

TOKEN = config.token
SUPPORT_CHAT_ID = config.support_chat_id
REMNAWAVE_URL = config.remnawave_url
REMNAWAVE_TOKEN = config.remnawave_token
REMNAWAVE_COOKIE = config.remnawave_cookie
GPUNIQ_API_KEY = config.gpuniq_api_key
GPUNIQ_BASE_URL = config.gpuniq_base_url
ANTHROPIC_API_KEY = config.anthropic_api_key
AI_MODEL = config.ai_model
AI_FALLBACK_MODEL = config.ai_fallback_model
ANTHROPIC_FALLBACK_MODEL = config.anthropic_fallback_model
ADMIN_TAGS = config.admin_tags
