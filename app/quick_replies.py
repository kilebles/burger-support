"""Быстрые ответы админов из kb/quick-replies.md — без ИИ и без токенов.

В топике клиента: "/регион" — клиенту уходит готовый текст; "/q" — меню всех
быстрых ответов кнопками. Файл — таблицы "| /команда | текст |", звёздочка
после команды (пометка "проверить формулировку") игнорируется.
"""
import re

from loguru import logger

from app.settings import BASE_DIR

QUICK_REPLIES_FILE = BASE_DIR / "kb" / "quick-replies.md"

_ROW = re.compile(r"^\|\s*(/[^\s|*]+)\*?\s*\|\s*(.+?)\s*\|\s*$")


def _load() -> dict[str, str]:
    if not QUICK_REPLIES_FILE.exists():
        logger.warning(f"{QUICK_REPLIES_FILE} not found — quick replies disabled")
        return {}
    replies: dict[str, str] = {}
    for line in QUICK_REPLIES_FILE.read_text(encoding="utf-8").splitlines():
        match = _ROW.match(line)
        if match:
            replies[match.group(1).lower()] = match.group(2)
    logger.info(f"Loaded {len(replies)} quick replies")
    return replies


QUICK_REPLIES = _load()
COMMANDS = list(QUICK_REPLIES)


def find(text: str) -> str | None:
    """Текст быстрого ответа, если сообщение админа — ровно команда из файла."""
    command = text.strip().split()[0].split("@")[0].lower() if text.strip() else ""
    return QUICK_REPLIES.get(command)
