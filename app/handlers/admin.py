from datetime import timedelta
from html import escape
from typing import List, Optional

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from loguru import logger

from app import ai_context, quick_replies
from app.admin_actions import card_keyboard
from app.delivery import send_to_client
from app.settings import ADMIN_TAGS, SUPPORT_CHAT_ID
from app.topics import build_user_card, forget_topic, get_user_id


router = Router()
router.message.filter(F.chat.id == int(SUPPORT_CHAT_ID))
router.callback_query.filter(F.message.chat.id == int(SUPPORT_CHAT_ID))

# Сколько ИИ молчит в топике после сообщения живого админа: в реальных
# диалогах ИИ перебивал людей и противоречил им ("удаление устройств не
# поможет" сразу после совета админа удалить устройства). /start вернёт раньше.
_HUMAN_TAKEOVER = timedelta(hours=6)


async def notify_admin_escalation(bot: Bot, topic_id: int, reason: str) -> None:
    """Тег админа в топике, когда ИИ передаёт вопрос человеку. Сообщение
    отправляет сам бот, поэтому клиенту оно никогда не пересылается —
    forward_admin_message реагирует только на сообщения живых админов."""
    await bot.send_message(
        chat_id=SUPPORT_CHAT_ID,
        message_thread_id=topic_id,
        text=f"🙋 {escape(ADMIN_TAGS)} нужна помощь\n{escape(reason)}",
    )


async def _topic_of_request(message: Message) -> Optional[int]:
    """topic_id заявки, в которой отправлена команда, или None (с ответом админу)."""
    if not message.is_topic_message or message.message_thread_id is None:
        await message.reply("Команду нужно отправить внутри топика заявки.")
        return None
    if await get_user_id(message.message_thread_id) is None:
        await message.reply("Этот топик не привязан к заявке.")
        return None
    return message.message_thread_id


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
    await ai_context.forget(message.message_thread_id)
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
        reply_markup=card_keyboard(),
    )


@router.message(Command("stop"))
async def handle_stop_ai(message: Message):
    """Pause AI auto-answering in this topic until /start is sent here again."""
    topic_id = await _topic_of_request(message)
    if topic_id is None:
        return
    await ai_context.set_ai_enabled(topic_id, False)
    await message.reply("ИИ остановлен в этом топике. Включить: /start")


@router.message(Command("start"))
async def handle_start_ai(message: Message):
    """Resume AI auto-answering in this topic after /stop."""
    topic_id = await _topic_of_request(message)
    if topic_id is None:
        return
    await ai_context.set_ai_enabled(topic_id, True)
    await message.reply("ИИ снова работает в этом топике.")


@router.message(Command("pauseai"))
async def handle_pause_ai_global(message: Message):
    """Pause AI auto-answering everywhere at once. Independent of per-topic /stop."""
    await ai_context.set_ai_globally_enabled(False)
    await message.reply("ИИ остановлен во всех чатах. Включить: /resumeai")


@router.message(Command("resumeai"))
async def handle_resume_ai_global(message: Message):
    """Resume AI everywhere. Topics individually paused with /stop stay paused."""
    await ai_context.set_ai_globally_enabled(True)
    await message.reply("ИИ работает во всех чатах, кроме отдельно остановленных через /stop.")


@router.message(Command("outage"))
async def handle_outage(message: Message, command: CommandObject):
    """Массовый сбой: `/outage <что случилось>` — ИИ отвечает шаблоном про
    технические работы и не гоняет людей по чек-листу; `/outage off` — сбой
    закончился, бот выдаёт список писавших для компенсации; без аргументов — статус."""
    arg = (command.args or "").strip()

    if not arg:
        outage = await ai_context.get_outage()
        await message.reply(
            f"Сейчас сбой: {escape(outage)}\nЗакончился — /outage off" if outage
            else "Сбоя нет. Объявить: /outage &lt;что случилось&gt;, например «/outage не работают обходы на мобильном»"
        )
        return

    if arg.lower() == "off":
        await ai_context.set_outage(None)
        contacts = await ai_context.pop_outage_contacts()
        lines = [f"• {escape(label)} — <code>{user_id}</code>" for user_id, label, _ in contacts]
        summary = "\n".join(lines) if lines else "никто не писал"
        await message.reply(f"Сбой снят. Писали во время сбоя (им — компенсация):\n{summary}")
        return

    await ai_context.set_outage(arg)
    await message.reply(
        f"Сбой объявлен: {escape(arg)}\nИИ отвечает шаблоном про технические работы и ведёт список писавших. Снять: /outage off"
    )


def _quick_replies_menu() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=command, callback_data=f"qr:{index}")
        for index, command in enumerate(quick_replies.COMMANDS)
    ]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    rows.append([InlineKeyboardButton(text="✖️ Закрыть", callback_data="qr:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("q"))
async def handle_quick_menu(message: Message):
    """Меню быстрых ответов кнопками: нажатие отправляет текст клиенту этого топика."""
    if await _topic_of_request(message) is None:
        return
    if not quick_replies.COMMANDS:
        await message.reply("Быстрые ответы не загружены (нет kb/quick-replies.md).")
        return
    await message.reply("Быстрые ответы — нажмите, чтобы отправить клиенту:", reply_markup=_quick_replies_menu())


@router.callback_query(F.data.startswith("qr:"))
async def on_quick_reply(callback: CallbackQuery):
    choice = callback.data.removeprefix("qr:")
    if choice == "close":
        await callback.message.delete()
        await callback.answer()
        return
    topic_id = callback.message.message_thread_id
    user_id = await get_user_id(topic_id) if topic_id else None
    index = int(choice)
    if user_id is None or index >= len(quick_replies.COMMANDS):
        await callback.answer("Не удалось определить клиента.", show_alert=True)
        return
    text = quick_replies.QUICK_REPLIES[quick_replies.COMMANDS[index]]
    await callback.message.delete()
    if await send_to_client(callback.bot, user_id, topic_id, text, staff=True):
        await ai_context.pause_ai(topic_id, _HUMAN_TAKEOVER)
        await callback.answer("Отправлено")
    else:
        await callback.answer("Не удалось доставить клиенту", show_alert=True)


@router.message(F.is_topic_message, F.text.startswith("/"))
async def handle_slash_in_topic(message: Message):
    """Быстрый ответ по команде ("/регион"). Неизвестные команды клиенту не
    пересылаются — раньше опечатка в команде уходила клиенту как текст."""
    user_id = await get_user_id(message.message_thread_id)
    if user_id is None:
        return
    text = quick_replies.find(message.text)
    if text is None:
        await message.reply("Нет такой команды. Быстрые ответы: /q")
        return
    if await send_to_client(message.bot, user_id, message.message_thread_id, text, staff=True):
        await ai_context.pause_ai(message.message_thread_id, _HUMAN_TAKEOVER)
        try:
            await message.delete()  # в топике остаётся только сам текст, ушедший клиенту
        except Exception:
            pass
    else:
        await message.reply("Не удалось доставить сообщение клиенту.")


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
        return

    await ai_context.append_turn(message.message_thread_id, "assistant", f"[сотрудник] {message.caption or '[медиа]'}")
    await ai_context.pause_ai(message.message_thread_id, _HUMAN_TAKEOVER)


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
        return

    await ai_context.append_turn(
        message.message_thread_id, "assistant", f"[сотрудник] {message.text or message.caption or '[медиа]'}"
    )
    await ai_context.pause_ai(message.message_thread_id, _HUMAN_TAKEOVER)
