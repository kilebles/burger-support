from typing import List, Optional

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import Message
from loguru import logger

from app.settings import SUPPORT_CHAT_ID
from app.topics import build_user_card, forget_topic, get_user_id


router = Router()
router.message.filter(F.chat.id == int(SUPPORT_CHAT_ID))


@router.message(Command("delete"))
async def handle_delete(message: Message):
    """Delete the request topic this command was sent in and notify the user."""
    if not message.is_topic_message or message.message_thread_id is None:
        await message.reply("Команду нужно отправить внутри топика заявки.")
        return

    user_id = await get_user_id(message.message_thread_id)
    if user_id is None:
        await message.reply("Этот топик не привязан к заявке.")
        return

    await message.bot.delete_forum_topic(
        chat_id=SUPPORT_CHAT_ID,
        message_thread_id=message.message_thread_id
    )
    await forget_topic(user_id)
    logger.info(f"Topic {message.message_thread_id} for user_id={user_id} deleted via /delete")

    try:
        await message.bot.send_message(
            chat_id=user_id,
            text="Ваша заявка была закрыта."
        )
    except TelegramForbiddenError:
        logger.warning(f"Could not notify user_id={user_id} about closed request: bot is blocked")


@router.message(Command("s"))
async def handle_refresh_card(message: Message):
    """Re-post the client's info card with fresh data into this topic. Never reaches the client."""
    if not message.is_topic_message or message.message_thread_id is None:
        await message.reply("Команду нужно отправить внутри топика заявки.")
        return

    user_id = await get_user_id(message.message_thread_id)
    if user_id is None:
        await message.reply("Этот топик не привязан к заявке.")
        return

    try:
        chat = await message.bot.get_chat(user_id)
    except TelegramBadRequest:
        await message.reply("Не удалось получить данные пользователя.")
        return

    await message.bot.send_message(
        chat_id=SUPPORT_CHAT_ID,
        message_thread_id=message.message_thread_id,
        text=await build_user_card(user_id, chat.full_name, chat.username),
    )


async def _notify_delivery_failure(message: Message, user_id: int, error: Exception) -> None:
    if isinstance(error, TelegramForbiddenError):
        logger.warning(f"Could not deliver reply to user_id={user_id}: bot is blocked")
        await message.reply("Не удалось доставить сообщение — пользователь заблокировал бота.")
    else:
        logger.exception(f"Failed to deliver reply to user_id={user_id}")
        await message.reply("Не удалось доставить сообщение пользователю, попробуйте ещё раз.")


@router.message(F.is_topic_message, F.media_group_id)
async def forward_admin_album(message: Message, album: Optional[List[Message]] = None):
    """Forward an admin's media album from a topic to the user."""
    if not album:
        album = [message]

    user_id = await get_user_id(message.message_thread_id)
    if user_id is None:
        return

    try:
        await message.bot.copy_messages(
            chat_id=user_id,
            from_chat_id=SUPPORT_CHAT_ID,
            message_ids=[msg.message_id for msg in album],
        )
    except Exception as error:
        await _notify_delivery_failure(message, user_id, error)


@router.message(F.is_topic_message, F.forum_topic_created.is_(None))
async def forward_admin_message(message: Message):
    """Forward an admin's message from a topic to the user (skips the auto "topic created" service message)."""
    user_id = await get_user_id(message.message_thread_id)
    if user_id is None:
        return

    try:
        await message.copy_to(chat_id=user_id)
    except Exception as error:
        await _notify_delivery_failure(message, user_id, error)
