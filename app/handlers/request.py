import asyncio
from dataclasses import dataclass, field
from typing import List, Optional

from aiogram import Bot, F, Router
from aiogram.types import Message, User
from aiogram.utils.chat_action import ChatActionSender
from loguru import logger

from app import ai_context, ai_support
from app.ai_support import Attachment
from app.handlers.admin import notify_admin_escalation
from app.settings import SUPPORT_CHAT_ID
from app.topics import ensure_topic, send_to_topic


router = Router()

# Клиенты часто пишут вопрос несколькими сообщениями подряд ("Не работает" /
# "Ошибка" / "На айфоне" / скрин). Копим сообщения топика, пока не наступит
# короткая тишина, и отвечаем один раз на всё сразу — как прочитал бы пачку
# живой админ, прежде чем начать печатать ответ.
_DEBOUNCE_SECONDS = 6.0

# Что модель умеет прочитать сама. Документы-картинки (скрин, отправленный
# "файлом" без сжатия) и PDF-чеки читаем так же, как фото.
_IMAGE_MIME = {"image/jpeg", "image/png", "image/gif", "image/webp"}
_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
_MAX_ATTACHMENTS = 6

# Если ИИ передал вопрос человеку, но сам текст для клиента не сформировал
# (или упал), клиент всё равно не должен остаться в тишине на часы.
_HANDOFF_TEXT = "Спасибо, передал ваш вопрос коллегам — ответим здесь, как только разберёмся."


@dataclass
class _PendingBurst:
    bot: Bot
    user: User
    texts: list[str] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    task: Optional[asyncio.Task] = None


_pending: dict[int, _PendingBurst] = {}
_pending_locks: dict[int, asyncio.Lock] = {}


async def _send_to_client(bot: Bot, user_id: int, topic_id: int, text: str) -> bool:
    """Сообщение клиенту от лица поддержки: в топик (админ видит, что ушло) и
    копией клиенту, плюс запись в историю диалога."""
    try:
        sent = await bot.send_message(chat_id=SUPPORT_CHAT_ID, message_thread_id=topic_id, text=text, parse_mode=None)
        await bot.copy_message(chat_id=user_id, from_chat_id=SUPPORT_CHAT_ID, message_id=sent.message_id)
    except Exception:
        logger.exception(f"Failed to deliver support message to user_id={user_id}")
        return False
    await ai_context.append_turn(topic_id, "assistant", text)
    return True


async def _reply_with_ai(burst: _PendingBurst, topic_id: int, text: str) -> None:
    """Ответ ИИ клиенту и, если нужен человек, тег админа в топике."""
    result = await ai_support.get_ai_answer(topic_id, burst.user.id, text, burst.attachments)

    if result is None:
        if await ai_context.should_notify_escalation(topic_id):
            await notify_admin_escalation(burst.bot, topic_id, "ИИ не смог сформировать ответ, ответьте клиенту вручную.")
        return

    if result.no_reply_needed and not result.escalate:
        return

    answer = result.answer.strip() or (_HANDOFF_TEXT if result.escalate else "")
    if answer:
        await _send_to_client(burst.bot, burst.user.id, topic_id, answer)

    if result.escalate and await ai_context.should_notify_escalation(topic_id):
        await notify_admin_escalation(burst.bot, topic_id, result.escalate_reason or "ИИ передал вопрос человеку.")


async def _flush_burst(topic_id: int) -> None:
    """Ждёт окно тишины и только потом обрабатывает накопленную пачку целиком."""
    await asyncio.sleep(_DEBOUNCE_SECONDS)

    lock = _pending_locks.setdefault(topic_id, asyncio.Lock())
    async with lock:
        burst = _pending.pop(topic_id, None)
    if burst is None:
        return

    text = "\n".join(burst.texts)

    try:
        if await ai_context.get_outage():
            label = f"@{burst.user.username}" if burst.user.username else burst.user.full_name
            await ai_context.add_outage_contact(burst.user.id, label, topic_id)

        ai_active = await ai_context.is_ai_globally_enabled() and await ai_context.is_ai_enabled(topic_id)
        if not ai_active:
            # ИИ на паузе, но реплику клиента всё равно фиксируем — иначе после
            # возобновления у ИИ будет дыра в памяти о разговоре.
            kinds = ", ".join(sorted({a.kind for a in burst.attachments}))
            await ai_context.append_turn(topic_id, "user", f"{text}\n[{kinds}]".strip() if kinds else text)
            return

        async with ChatActionSender.typing(bot=burst.bot, chat_id=burst.user.id):
            await _reply_with_ai(burst, topic_id, text)
    except Exception:
        # Сообщение уже доставлено в топик — сбой ИИ-слоя не должен ничего ломать.
        logger.exception(f"AI layer failed for user_id={burst.user.id}, message stays admin-only")


async def _read_message(message: Message) -> tuple[str, Optional[Attachment]]:
    """Текст сообщения для ИИ и вложение, которое модель может прочитать.
    Нечитаемое (видео, архивы, слишком большие файлы) превращается в пометку."""
    text = message.text or message.caption or ""

    source = None
    mime = ""
    kind = ""
    if message.photo:
        source, mime, kind = message.photo[-1], "image/jpeg", "фото"  # -1 = наибольшее разрешение
    elif message.voice:
        return f"{text}\n[клиент прислал голосовое — прослушать нельзя]".strip(), None
    elif message.document and not message.animation:
        doc_mime = message.document.mime_type or ""
        if doc_mime in _IMAGE_MIME:
            source, mime, kind = message.document, doc_mime, "фото"
        elif doc_mime == "application/pdf":
            source, mime, kind = message.document, doc_mime, "документ"
        else:
            name = message.document.file_name or "без имени"
            return f"{text}\n[клиент прислал файл «{name}» — посмотреть нельзя]".strip(), None
    elif message.video or message.video_note:
        return f"{text}\n[клиент прислал видео — посмотреть нельзя]".strip(), None
    elif message.sticker:
        return f"{text}\n[стикер {message.sticker.emoji or ''}]".strip(), None

    if source is None:
        return text, None
    if (getattr(source, "file_size", 0) or 0) > _MAX_ATTACHMENT_BYTES:
        return f"{text}\n[клиент прислал слишком большой файл ({kind}) — посмотреть нельзя]".strip(), None
    try:
        data = (await message.bot.download(source)).read()
    except Exception:
        logger.exception(f"Failed to download {kind} from user_id={message.from_user.id}")
        return f"{text}\n[клиент прислал {kind} — не удалось загрузить]".strip(), None
    return text, Attachment(data=data, mime_type=mime, kind=kind)


async def _queue_for_ai(messages: list[Message]) -> None:
    """Добавить сообщения клиента в пачку топика и перезапустить таймер тишины."""
    first = messages[0]
    topic_id = await ensure_topic(first.bot, first.from_user)
    read = [await _read_message(m) for m in messages]

    lock = _pending_locks.setdefault(topic_id, asyncio.Lock())
    async with lock:
        burst = _pending.get(topic_id)
        if burst is None:
            burst = _PendingBurst(bot=first.bot, user=first.from_user)
            _pending[topic_id] = burst

        for text, attachment in read:
            if text:
                burst.texts.append(text)
            if attachment is not None and len(burst.attachments) < _MAX_ATTACHMENTS:
                burst.attachments.append(attachment)

        if burst.task is not None:
            burst.task.cancel()
        burst.task = asyncio.create_task(_flush_burst(topic_id))


@router.message(F.media_group_id)
async def forward_user_album(message: Message, album: Optional[List[Message]] = None):
    """Forward a user's media album into their support topic, then queue it for the AI."""
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
        return

    await _queue_for_ai(album)


@router.message()
async def forward_user_message(message: Message):
    """Forward any user message into their support topic, creating it on first contact,
    then queue it for a debounced AI reply covering the whole burst of messages."""

    async def sender(topic_id: int) -> None:
        await message.copy_to(chat_id=SUPPORT_CHAT_ID, message_thread_id=topic_id)

    try:
        await send_to_topic(message.bot, message.from_user, sender)
    except Exception:
        logger.exception(f"Failed to forward message from user_id={message.from_user.id} to support")
        await message.answer("Не удалось отправить сообщение в поддержку, попробуйте ещё раз позже.")
        return

    await _queue_for_ai([message])
