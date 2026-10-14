"""Отправка клиенту от лица поддержки: всё уходит сначала в топик (админы видят,
что получил клиент), затем копией клиенту, и записывается в историю для ИИ."""
from typing import Optional

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup
from loguru import logger

from app import ai_context
from app.media import Video, send_video_to_topic
from app.settings import SUPPORT_CHAT_ID


async def send_to_client(
    bot: Bot,
    user_id: int,
    topic_id: int,
    text: str,
    *,
    staff: bool = False,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> bool:
    """staff=True — текст от живого сотрудника (быстрый ответ, кнопка), в
    истории помечается, чтобы ИИ не противоречил человеку."""
    try:
        sent = await bot.send_message(chat_id=SUPPORT_CHAT_ID, message_thread_id=topic_id, text=text, parse_mode=None)
        await bot.copy_message(
            chat_id=user_id, from_chat_id=SUPPORT_CHAT_ID, message_id=sent.message_id, reply_markup=reply_markup
        )
    except Exception:
        logger.exception(f"Failed to deliver support message to user_id={user_id}")
        return False
    await ai_context.append_turn(topic_id, "assistant", f"[сотрудник] {text}" if staff else text)
    return True


async def send_video_to_client(bot: Bot, user_id: int, topic_id: int, video: Video) -> bool:
    try:
        message_id = await send_video_to_topic(bot, topic_id, video)
        if message_id is None:
            return False
        await bot.copy_message(chat_id=user_id, from_chat_id=SUPPORT_CHAT_ID, message_id=message_id)
    except Exception:
        logger.exception(f"Failed to deliver video {video.value} to user_id={user_id}")
        return False
    await ai_context.append_turn(topic_id, "assistant", f"[видеоинструкция: {video.value}]")
    return True
