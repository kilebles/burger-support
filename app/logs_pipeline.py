"""Логи клиентских приложений (Happ, Incy, v2RayTun): распознать, прочитать
для ИИ и переслать в отдельный топик "Логи", чтобы техники видели их в одном месте.

Раньше ИИ не читал .log/.txt вовсе и просил лог по 5-6 раз; из 9 запросов
пригодными оказывались около двух (клиенты останавливали VPN перед снятием
лога, присылали скрин списка файлов вместо файла). Теперь бот сам видит
содержимое и сразу говорит, что не так с логом.
"""
import re
from html import escape
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, User
from loguru import logger

from app import ai_context
from app.settings import SUPPORT_CHAT_ID

LOGS_TOPIC_NAME = "📄 Логи"
_LOGS_TOPIC_KEY = "logs_topic_id"

_LOG_EXTENSIONS = (".log", ".txt")
_MAX_LOG_BYTES = 2 * 1024 * 1024

# ИИ получает не весь лог (это тысячи токенов на каждый файл), а выжимку,
# собранную кодом: строки с ошибками/отказами/остановкой туннеля плюс хвост.
# Полный файл видят техники в топике "Логи".
_MAX_DIGEST_CHARS = 2_500
_TAIL_LINES = 15
_IMPORTANT = re.compile(
    r"error|fail|refused|timeout|timed out|reset|denied|rejected|unreachable|no route|"
    r"tls|handshake|stopped|userinitiated|warning|panic|eof|i/o",
    re.IGNORECASE,
)

# Строки, по которым текстовое сообщение клиента опознаётся как вставленный лог.
_LOG_MARKERS = re.compile(
    r"\[(?:Info|Warning|Error|Debug)\]|Xray \d|XrayCoreStart|Tunnel (?:started|stopped)|"
    r"\b(?:core|app|proxy|transport/internet)\b:|\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}",
)


def is_log_document(message: Message) -> bool:
    doc = message.document
    if doc is None or (doc.file_size or 0) > _MAX_LOG_BYTES:
        return False
    name = (doc.file_name or "").lower()
    return name.endswith(_LOG_EXTENSIONS) or (doc.mime_type or "").startswith("text/")


def looks_like_log_text(text: str) -> bool:
    return len(text) >= 150 and len(_LOG_MARKERS.findall(text)) >= 2


async def read_log_document(message: Message) -> Optional[str]:
    """Полный текст лога из файла, или None, если скачать не удалось."""
    try:
        raw = (await message.bot.download(message.document)).read()
    except Exception:
        logger.exception(f"Failed to download log from user_id={message.from_user.id}")
        return None
    return raw.decode("utf-8", errors="replace")


def digest(content: str) -> str:
    """Выжимка лога для ИИ: значимые строки (повторы схлопнуты) + хвост, в пределах лимита."""
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    important: list[str] = []
    seen: set[str] = set()
    for line in lines:
        # Ключ без времени: одинаковые ошибки с разными таймстемпами — один раз.
        key = re.sub(r"[\d:./\[\]\- ]{8,}", "", line)[:120]
        if _IMPORTANT.search(line) and key not in seen:
            seen.add(key)
            important.append(line[:300])
    tail = [line[:300] for line in lines[-_TAIL_LINES:]]
    text = (
        f"всего строк: {len(lines)}\n"
        "значимые строки:\n" + ("\n".join(important[-40:]) or "нет") +
        "\nпоследние строки:\n" + "\n".join(tail)
    )
    return text[-_MAX_DIGEST_CHARS:]


def log_block(name: str, content: str) -> str:
    """Как лог попадает в запрос к ИИ — только выжимка."""
    return f"[выжимка лога клиента «{name}»]\n{digest(content)}\n[конец выжимки]"


def topic_link(topic_id: int) -> str:
    internal_id = str(SUPPORT_CHAT_ID).removeprefix("-100")
    return f"https://t.me/c/{internal_id}/{topic_id}"


async def _ensure_logs_topic(bot: Bot) -> int:
    stored = await ai_context.get_setting(_LOGS_TOPIC_KEY)
    if stored:
        return int(stored)
    topic = await bot.create_forum_topic(chat_id=SUPPORT_CHAT_ID, name=LOGS_TOPIC_NAME)
    await ai_context.set_setting(_LOGS_TOPIC_KEY, str(topic.message_thread_id))
    logger.info(f"Created logs topic {topic.message_thread_id}")
    return topic.message_thread_id


async def forward_to_logs_topic(bot: Bot, messages: list[Message], user: User, client_topic_id: int) -> None:
    """Шапка с клиентом и ссылкой на его топик + сам лог (файл или текст)."""
    who = f"@{escape(user.username)}" if user.username else escape(user.full_name)
    header = f"📄 Лог от {who} (<code>{user.id}</code>) · <a href=\"{topic_link(client_topic_id)}\">топик клиента</a>"

    for attempt in range(2):
        logs_topic = await _ensure_logs_topic(bot)
        try:
            await bot.send_message(chat_id=SUPPORT_CHAT_ID, message_thread_id=logs_topic, text=header)
            for message in messages:
                await message.copy_to(chat_id=SUPPORT_CHAT_ID, message_thread_id=logs_topic)
            return
        except TelegramBadRequest as error:
            if "thread not found" not in str(error).lower() or attempt:
                raise
            logger.warning("Logs topic is gone, recreating")
            await ai_context.set_setting(_LOGS_TOPIC_KEY, None)
