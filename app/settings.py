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

    class Config:
        env_file = BASE_DIR / ".env"


config = Settings()

TOKEN = config.token
SUPPORT_CHAT_ID = config.support_chat_id
REMNAWAVE_URL = config.remnawave_url
REMNAWAVE_TOKEN = config.remnawave_token
REMNAWAVE_COOKIE = config.remnawave_cookie
