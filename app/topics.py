import asyncio
import json
from typing import Awaitable, Callable, Optional

import aiofiles
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import User
from loguru import logger

from app.remnawave import get_panel_lines
from app.settings import DATA_DIR, SUPPORT_CHAT_ID

TOPICS_FILE = DATA_DIR / "topics.json"

_cache: Optional[dict[int, int]] = None  # user_id -> message_thread_id
_file_lock = asyncio.Lock()
_topic_locks: dict[int, asyncio.Lock] = {}


async def _load() -> dict[int, int]:
    global _cache
    if _cache is None:
        if TOPICS_FILE.exists():
            async with aiofiles.open(TOPICS_FILE, "r", encoding="utf-8") as f:
                raw = await f.read()
            _cache = {int(k): v for k, v in (json.loads(raw) if raw else {}).items()}
        else:
            _cache = {}
    return _cache


async def _save() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(TOPICS_FILE, "w", encoding="utf-8") as f:
        await f.write(json.dumps(_cache, ensure_ascii=False, indent=2))


async def get_user_id(topic_id: int) -> Optional[int]:
    """Look up which user a support-group topic belongs to."""
    cache = await _load()
    for user_id, tid in cache.items():
        if tid == topic_id:
            return user_id
    return None


async def forget_topic(user_id: int) -> None:
    """Drop a user's topic mapping, e.g. after the topic was deleted."""
    async with _file_lock:
        cache = await _load()
        if cache.pop(user_id, None) is not None:
            await _save()


async def build_user_card(user_id: int, full_name: str, username: str | None) -> str:
    """Info card text for a user: Telegram identity + subscription info from the Remnawave panel."""
    username_line = f"@{username}" if username else "не указан"
    card_lines = [
        f"👤 {full_name}",
        f"Username: {username_line}",
        f"ID: <code>{user_id}</code>",
        *await get_panel_lines(user_id),
    ]
    return "\n".join(card_lines)


async def ensure_topic(bot: Bot, user: User) -> int:
    """Get the user's support topic, creating one in the support chat on first contact."""
    lock = _topic_locks.setdefault(user.id, asyncio.Lock())
    async with lock:
        cache = await _load()
        topic_id = cache.get(user.id)
        if topic_id is not None:
            return topic_id

        topic_name = f"@{user.username}" if user.username else user.full_name
        topic = await bot.create_forum_topic(chat_id=SUPPORT_CHAT_ID, name=topic_name)
        logger.info(f"Created topic {topic.message_thread_id} for user_id={user.id} ({topic_name})")

        text = await build_user_card(user.id, user.full_name, user.username)
        await bot.send_message(
            chat_id=SUPPORT_CHAT_ID,
            message_thread_id=topic.message_thread_id,
            text=text,
        )

        async with _file_lock:
            cache[user.id] = topic.message_thread_id
            await _save()

        return topic.message_thread_id


async def send_to_topic(
    bot: Bot,
    user: User,
    sender: Callable[[int], Awaitable[None]],
) -> None:
    """Ensure the user's topic exists and run `sender(topic_id)`, recreating the topic once if it was deleted out-of-band."""
    topic_id = await ensure_topic(bot, user)
    try:
        await sender(topic_id)
    except TelegramBadRequest as error:
        if "thread not found" not in str(error).lower():
            logger.error(f"Failed to deliver message to topic {topic_id} for user_id={user.id}: {error}")
            raise
        logger.warning(f"Topic {topic_id} for user_id={user.id} is gone, recreating")
        await forget_topic(user.id)
        topic_id = await ensure_topic(bot, user)
        await sender(topic_id)
    else:
        logger.debug(f"Delivered message to topic {topic_id} for user_id={user.id}")
