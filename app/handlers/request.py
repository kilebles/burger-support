from typing import List, Optional

from aiogram import F, Router
from aiogram.types import Message
from loguru import logger

from app.settings import SUPPORT_CHAT_ID
from app.topics import send_to_topic


router = Router()


@router.message(F.media_group_id)
async def forward_user_album(message: Message, album: Optional[List[Message]] = None):
    """Forward a user's media album into their support topic."""
    if not album:
        album = [message]

    async def sender(topic_id: int) -> None:
        await message.bot.copy_messages(
            chat_id=SUPPORT_CHAT_ID,
            from_chat_id=message.from_user.id,
            message_ids=[msg.message_id for msg in album],
            message_thread_id=topic_id,
        )

    try:
        await send_to_topic(message.bot, message.from_user, sender)
    except Exception:
        logger.exception(f"Failed to forward album from user_id={message.from_user.id} to support")
        await message.answer("Не удалось отправить сообщение в поддержку, попробуйте ещё раз позже.")


@router.message()
async def forward_user_message(message: Message):
    """Forward any user message into their support topic, creating it on first contact."""

    async def sender(topic_id: int) -> None:
        await message.copy_to(chat_id=SUPPORT_CHAT_ID, message_thread_id=topic_id)

    try:
        await send_to_topic(message.bot, message.from_user, sender)
    except Exception:
        logger.exception(f"Failed to forward message from user_id={message.from_user.id} to support")
        await message.answer("Не удалось отправить сообщение в поддержку, попробуйте ещё раз позже.")
