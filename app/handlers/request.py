import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
import re
from typing import Callable, List, Optional

from aiogram import Bot, F, Router
from aiogram.types import Message, User
from aiogram.utils.chat_action import ChatActionSender
from loguru import logger

from app import ai_context, ai_support, logs_pipeline
from app.admin_actions import post_action_card
from app.ai_support import Attachment
from app.delivery import send_to_client, send_video_to_client
from app.handlers.admin import notify_admin_escalation
from app.media import Video
from app.settings import SUPPORT_CHAT_ID
from app.topics import ensure_topic, send_to_topic


router = Router()

# Клиенты часто пишут вопрос несколькими сообщениями подряд ("Здравствуйте" /
# пауза 8–14 сек / "Не работает" / "На айфоне" / скрин). Базовое окно тишины —
# 10 сек, а если пришло только приветствие без сути — ждём до 16 сек.
_DEBOUNCE_SECONDS = 10.0
_OPENER_DEBOUNCE_SECONDS = 16.0
_PHOTO_MAX_SIDE = 1280

# Пауза ИИ в топике после эскалации на сотрудника, чтобы сообщения вдогонку
# ("Ау?", "Ну что там?", доп. скрины) не сжигали токены до ответа живого админа.
_ESCALATION_PAUSE = timedelta(hours=3)

# Что модель умеет прочитать сама. Документы-картинки (скрин, отправленный
# "файлом" без сжатия) и PDF-чеки читаем так же, как фото.
_IMAGE_MIME = {"image/jpeg", "image/png", "image/gif", "image/webp"}
_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
_MAX_ATTACHMENTS = 6

# Если ИИ передал вопрос человеку, но сам текст для клиента не сформировал
# (или упал), клиент всё равно не должен остаться в тишине на часы.
_HANDOFF_TEXT = "Спасибо, передал ваш вопрос коллегам — ответим здесь, как только разберёмся."
_VOICE_ONLY_TEXT = (
    "К сожалению, голосовые и видеосообщения здесь не прослушиваются — "
    "напишите, пожалуйста, ваш вопрос текстом или пришлите скриншот 🙏"
)

_OPENER_ONLY_RE = re.compile(
    r"^\s*(?:здравствуйте|привет(?:ствую)?|добр(?:ый|ого|ое)\s+(?:день|вечер|утр[оа]|времени\s+суток)|"
    r"подскажите(?:\s*,?\s*пожалуйста)?|помогите(?:\s*,?\s*пожалуйста)?|алло|ау+|тут\s+есть\s+кто)"
    r"[\s,!?.🙏👋🫶]*$",
    re.IGNORECASE,
)

_PURE_ACK_RE = re.compile(
    r"^\s*(?:спасибо(?:\s+большое|\s+огромное|\s+вам)?|спс|благодарю|"
    r"ок(?:ей)?|хорошо|ладно|понял[аи]?|принял[аи]?|ясно|ага|угу|жду(?:\s+ответа)?|"
    r"[👍👌🙏❤️🫶🤝✅🔥😊☺️]+)"
    r"(?:[\s,!?.👍👌🙏❤️🫶🤝✅🔥😊☺️]+"
    r"(?:спасибо(?:\s+большое|\s+огромное|\s+вам)?|спс|благодарю|"
    r"ок(?:ей)?|хорошо|ладно|понял[аи]?|принял[аи]?|ясно|жду(?:\s+ответа)?|"
    r"[👍👌🙏❤️🫶🤝✅🔥😊☺️]+))*\s*[.!?,🙏👍🫶❤️😊☺️]*$",
    re.IGNORECASE,
)

_CLOSING_OR_HANDOFF_RE = re.compile(
    r"(?:пожалуйста[,!\s]|рад[ыа]?\s+(?:был[аи]?\s+)?помочь|рад[ыа]?,\s+что|"
    r"обращайтесь|хорошего\s+(?:вам\s+)?дня|приятного\s+пользования|"
    r"передал[аи]?\s+(?:ваш\s+)?вопрос|передал[аи]?\s+коллегам|передал[аи]?\s+на\s+проверку|"
    r"получили\s+чек|скоро\s+подключится|ответим\s+(?:вам\s+)?здесь|вопрос\s+у\s+сотрудников|"
    r"не\s+прослушиваются)",
    re.IGNORECASE,
)


@dataclass
class _PendingBurst:
    bot: Bot
    user: User
    texts: list[str] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    # Выжимки логов: только в живой запрос к ИИ, не в историю.
    log_digests: list[str] = field(default_factory=list)
    voice_only_count: int = 0
    task: Optional[asyncio.Task] = None


@dataclass
class _InflightRun:
    burst: _PendingBurst
    task: asyncio.Task
    committed: bool = False

    def mark_committed(self) -> None:
        self.committed = True


@dataclass
class _Read:
    text: str
    attachment: Optional[Attachment] = None
    log_digest: str = ""
    is_voice_only: bool = False


_pending: dict[int, _PendingBurst] = {}
_inflight: dict[int, _InflightRun] = {}
_pending_locks: dict[int, asyncio.Lock] = {}
_exec_locks: dict[int, asyncio.Lock] = {}


def _compute_debounce_seconds(burst: _PendingBurst) -> float:
    """Если клиент написал только приветствие без сути и вложений — ждём дольше,
    чтобы он успел дописать сам вопрос в ту же пачку."""
    if not burst.attachments and not burst.log_digests and burst.voice_only_count == 0:
        joined = " ".join(burst.texts).strip()
        if joined and len(joined) <= 48 and _OPENER_ONLY_RE.match(joined):
            return _OPENER_DEBOUNCE_SECONDS
    return _DEBOUNCE_SECONDS


def _pick_best_photo(photos: list) -> object:
    """Выбрать размер фото с длинной стороной ~800..1280px (достаточно для чтения
    мелкого текста на скриншотах и чеках, но в 2–3 раза экономнее по vision-токенам,
    чем 2560px оригинал). Для моков без width/height возвращает последний элемент."""
    candidates = [
        p for p in photos
        if getattr(p, "width", None) and getattr(p, "height", None)
        and max(p.width, p.height) <= _PHOTO_MAX_SIDE
    ]
    if candidates and max(candidates[-1].width, candidates[-1].height) >= 600:
        return candidates[-1]
    return photos[-1]


async def _is_redundant_ack(topic_id: int, text: str) -> bool:
    """Короткое «спасибо / ок / 👍» после уже закрывающей реплики или передачи
    сотруднику не требует вызова LLM — просто сохраняем реплику в историю."""
    stripped = text.strip()
    if not stripped or len(stripped) > 40 or not _PURE_ACK_RE.match(stripped):
        return False
    history = await ai_context.get_history(topic_id)
    if not history or history[-1]["role"] != "assistant":
        return False
    return bool(_CLOSING_OR_HANDOFF_RE.search(history[-1]["content"]))


async def _reply_with_ai(
    burst: _PendingBurst,
    topic_id: int,
    text: str,
    *,
    on_commit: Optional[Callable[[], None]] = None,
) -> None:
    """Ответ ИИ клиенту и, если нужен человек, тег админа в топике."""
    result = await ai_support.get_ai_answer(
        topic_id,
        burst.user.id,
        text,
        burst.attachments,
        "\n\n".join(burst.log_digests),
        on_commit=on_commit,
    )

    if result is None:
        if await ai_context.should_notify_escalation(topic_id):
            await notify_admin_escalation(burst.bot, topic_id, "ИИ не смог сформировать ответ, ответьте клиенту вручную.")
            await send_to_client(burst.bot, burst.user.id, topic_id, _HANDOFF_TEXT)
        await ai_context.pause_ai(topic_id, _ESCALATION_PAUSE)
        return

    if result.no_reply_needed and not result.escalate:
        return

    answer = result.answer.strip() or (_HANDOFF_TEXT if result.escalate else "")
    if answer:
        await send_to_client(burst.bot, burst.user.id, topic_id, answer)

    if result.video in Video.__members__:
        await send_video_to_client(burst.bot, burst.user.id, topic_id, Video(result.video))

    needs_human = False
    if result.action != "none":
        needs_human = True
        try:
            await post_action_card(
                burst.bot, topic_id, burst.user.id, result.action, result.action_days, result.action_summary
            )
        except Exception:
            logger.exception(f"Failed to post action card {result.action} to topic {topic_id}")
        # Карточка сама по себе требует решения человека — тегаем, как при эскалации.
        if not result.escalate and await ai_context.should_notify_escalation(topic_id):
            await notify_admin_escalation(burst.bot, topic_id, "ИИ предлагает действие — карточка выше.")

    if result.escalate:
        needs_human = True
        if await ai_context.should_notify_escalation(topic_id):
            await notify_admin_escalation(burst.bot, topic_id, result.escalate_reason or "ИИ передал вопрос человеку.")

    if needs_human:
        await ai_context.pause_ai(topic_id, _ESCALATION_PAUSE)


async def _flush_burst(topic_id: int, delay: float = _DEBOUNCE_SECONDS) -> None:
    """Ждёт окно тишины и только потом обрабатывает накопленную пачку целиком."""
    await asyncio.sleep(delay)

    exec_lock = _exec_locks.setdefault(topic_id, asyncio.Lock())
    lock = _pending_locks.setdefault(topic_id, asyncio.Lock())
    current_task = asyncio.current_task()

    async with exec_lock:
        async with lock:
            burst = _pending.get(topic_id)
            if burst is None or burst.task is not current_task:
                return
            _pending.pop(topic_id, None)
            assert current_task is not None
            run = _InflightRun(burst=burst, task=current_task)
            _inflight[topic_id] = run

        text = "\n".join(burst.texts)

        try:
            if await ai_context.get_outage():
                label = f"@{burst.user.username}" if burst.user.username else burst.user.full_name
                await ai_context.add_outage_contact(burst.user.id, label, topic_id)

            ai_active = await ai_context.is_ai_globally_enabled() and await ai_context.is_ai_enabled(topic_id)
            if not ai_active:
                # ИИ на паузе, но реплику клиента всё равно фиксируем — иначе после
                # возобновления у ИИ будет дыра в памяти о разговоре.
                run.mark_committed()
                kinds = ", ".join(sorted({a.kind for a in burst.attachments}))
                await ai_context.append_turn(topic_id, "user", f"{text}\n[{kinds}]".strip() if kinds else text)
                return

            # Fast-path 1: голосовое/кружочек без текста и вложений — вежливый ответ без траты токенов.
            if (
                burst.voice_only_count > 0
                and not burst.attachments
                and not burst.log_digests
                and len(burst.texts) == burst.voice_only_count
            ):
                run.mark_committed()
                await ai_context.append_turn(topic_id, "user", text)
                await send_to_client(burst.bot, burst.user.id, topic_id, _VOICE_ONLY_TEXT)
                return

            # Fast-path 2: чистое «спасибо / ок / 👍» после закрывающей реплики или эскалации.
            if not burst.attachments and not burst.log_digests and await _is_redundant_ack(topic_id, text):
                run.mark_committed()
                await ai_context.append_turn(topic_id, "user", text)
                return

            async with ChatActionSender.typing(bot=burst.bot, chat_id=burst.user.id):
                await _reply_with_ai(burst, topic_id, text, on_commit=run.mark_committed)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Сообщение уже доставлено в топик — сбой ИИ-слоя не должен ничего ломать.
            logger.exception(f"AI layer failed for user_id={burst.user.id}, message stays admin-only")
        finally:
            async with lock:
                if _inflight.get(topic_id) is run:
                    _inflight.pop(topic_id, None)


async def _read_message(message: Message) -> _Read:
    """Текст сообщения для ИИ и вложение, которое модель может прочитать.
    Логи (файлом или вставленным текстом) превращаются в выжимку; нечитаемое
    (видео, архивы, слишком большие файлы) — в пометку."""
    text = message.text or message.caption or ""

    if logs_pipeline.is_log_document(message):
        name = message.document.file_name or "лог"
        content = await logs_pipeline.read_log_document(message)
        if content is None:
            return _Read(f"{text}\n[клиент прислал лог «{name}» — не удалось загрузить]".strip())
        return _Read(f"{text}\n[клиент прислал лог «{name}», переслан техникам]".strip(), log_digest=logs_pipeline.log_block(name, content))
    if message.text and logs_pipeline.looks_like_log_text(message.text):
        return _Read("[клиент вставил лог текстом, переслан техникам]", log_digest=logs_pipeline.log_block("текст", message.text))

    source = None
    mime = ""
    kind = ""
    if message.photo:
        source, mime, kind = _pick_best_photo(message.photo), "image/jpeg", "фото"
    elif message.voice or message.video_note:
        label = "голосовое" if message.voice else "видеосообщение"
        return _Read(
            f"{text}\n[клиент прислал {label} — прослушать нельзя]".strip(),
            is_voice_only=not bool(text.strip()),
        )
    elif message.document and not message.animation:
        doc_mime = message.document.mime_type or ""
        if doc_mime in _IMAGE_MIME:
            source, mime, kind = message.document, doc_mime, "фото"
        elif doc_mime == "application/pdf":
            source, mime, kind = message.document, doc_mime, "документ"
        else:
            name = message.document.file_name or "без имени"
            return _Read(f"{text}\n[клиент прислал файл «{name}» — посмотреть нельзя]".strip())
    elif message.video:
        return _Read(f"{text}\n[клиент прислал видео — посмотреть нельзя]".strip())
    elif message.sticker:
        return _Read(f"{text}\n[стикер {message.sticker.emoji or ''}]".strip())

    if source is None:
        return _Read(text)
    if (getattr(source, "file_size", 0) or 0) > _MAX_ATTACHMENT_BYTES:
        return _Read(f"{text}\n[клиент прислал слишком большой файл ({kind}) — посмотреть нельзя]".strip())
    try:
        data = (await message.bot.download(source)).read()
    except Exception:
        logger.exception(f"Failed to download {kind} from user_id={message.from_user.id}")
        return _Read(f"{text}\n[клиент прислал {kind} — не удалось загрузить]".strip())
    return _Read(text, Attachment(data=data, mime_type=mime, kind=kind))


def _merge_burst_prefix(target: _PendingBurst, prefix: _PendingBurst) -> None:
    """Вернуть прерванную на лету пачку в начало новой пачки, чтобы ИИ ответил
    один раз на все сообщения клиента сразу."""
    target.texts = prefix.texts + target.texts
    combined_attachments = prefix.attachments + target.attachments
    target.attachments = combined_attachments[:_MAX_ATTACHMENTS]
    target.log_digests = prefix.log_digests + target.log_digests
    target.voice_only_count += prefix.voice_only_count


async def _queue_for_ai(messages: list[Message]) -> None:
    """Добавить сообщения клиента в пачку топика и перезапустить таймер тишины.
    Если предыдущий запрос к LLM ещё летит и не зафиксирован — отменяем его и
    склеиваем обе пачки в один общий запрос."""
    first = messages[0]
    topic_id = await ensure_topic(first.bot, first.from_user)
    read = [await _read_message(m) for m in messages]

    log_messages = [m for m, r in zip(messages, read) if r.log_digest]
    if log_messages:
        try:
            await logs_pipeline.forward_to_logs_topic(first.bot, log_messages, first.from_user, topic_id)
        except Exception:
            logger.exception(f"Failed to forward log of user_id={first.from_user.id} to logs topic")

    lock = _pending_locks.setdefault(topic_id, asyncio.Lock())
    async with lock:
        burst = _pending.get(topic_id)
        if burst is None:
            burst = _PendingBurst(bot=first.bot, user=first.from_user)
            _pending[topic_id] = burst

        inflight = _inflight.get(topic_id)
        if inflight is not None and not inflight.committed and not inflight.task.done():
            _inflight.pop(topic_id, None)
            inflight.task.cancel()
            _merge_burst_prefix(burst, inflight.burst)
            logger.info(f"Coalesced in-flight AI request for topic {topic_id} with newly arrived message")

        for item in read:
            if item.text:
                burst.texts.append(item.text)
            if item.is_voice_only:
                burst.voice_only_count += 1
            if item.attachment is not None and len(burst.attachments) < _MAX_ATTACHMENTS:
                burst.attachments.append(item.attachment)
            if item.log_digest:
                burst.log_digests.append(item.log_digest)

        if burst.task is not None:
            burst.task.cancel()
        delay = _compute_debounce_seconds(burst)
        burst.task = asyncio.create_task(_flush_burst(topic_id, delay))


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
