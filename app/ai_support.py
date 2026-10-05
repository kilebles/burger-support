import asyncio
import base64
import html
import re
from dataclasses import dataclass
from typing import Optional

from anthropic import APIError, AsyncAnthropic
from loguru import logger
from pydantic import BaseModel, ValidationError

from app import ai_context
from app.remnawave import get_panel_lines
from app.settings import AI_MODEL, ANTHROPIC_API_KEY, BASE_DIR

KB_FILE = BASE_DIR / "kb" / "knowledge.md"

MAX_RETRIES = 3


@dataclass
class Attachment:
    """Вложение клиента, которое модель может прочитать: картинка или PDF."""

    data: bytes
    mime_type: str
    kind: str  # "фото" или "документ" — для пометки в истории


class SupportAnswer(BaseModel):
    answer: str
    escalate: bool
    escalate_reason: str
    attachment_note: str
    no_reply_needed: bool


# JSON-схема ответа для output_config.format — плоская, с additionalProperties=false,
# как требует structured outputs. Поля обязаны совпадать с SupportAnswer.
_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "escalate": {"type": "boolean"},
        "escalate_reason": {"type": "string"},
        "attachment_note": {"type": "string"},
        "no_reply_needed": {"type": "boolean"},
    },
    "required": ["answer", "escalate", "escalate_reason", "attachment_note", "no_reply_needed"],
    "additionalProperties": False,
}

SYSTEM_PROMPT_TEMPLATE = """Ты — первая линия поддержки Burger VPN, отвечаешь клиентам в Telegram от имени поддержки.

Правила:
- Пиши от лица команды поддержки ("мы"). По своей инициативе не упоминай, что ты ИИ, бот или ассистент, не ссылайся на "базу знаний", "инструкцию для поддержки" или процесс генерации ответа. Если клиент прямо спрашивает, бот ли ты — не ври: коротко подтверди, что на первой линии отвечает автоматический помощник, а сложные вопросы разбирают живые сотрудники, и вернись к сути.
- Отвечай на языке клиента (обычно русский). Тон — как в разделе "Примеры живых ответов поддержки": на "вы", доброжелательно, коротко, без канцелярита и технического жаргона. Уместно редкое 🙏 или 🫶.
- Используй только информацию из базы знаний ниже и из блоков данных в сообщении. Не придумывай кнопки, функции, ссылки, сроки, цены и правила, которых там нет.
- Главная задача — выяснить причину. Если данных не хватает, не гадай: запроси нужное по разделу базы (что не работает, Wi‑Fi или мобильный, регион и оператор, приложение, скрин списка серверов, лог Happ, скрин оплаты). Не спрашивай то, что клиент уже сообщил или что видно на его скрине или в блоке данных подписки.
- Когда причина ещё не ясна — задай уточняющий вопрос или дай один шаг и жди ответа. Не добавляй заранее план "а если не поможет, тогда сделайте...": один вопрос или один шаг — дальше по ответу клиента. Полный многошаговый список уместен только для готовой, точно применимой процедуры (например, как подключить новое устройство).
- Не повторяй совет, который уже давал в этом диалоге и который не помог: предложи следующий шаг по чек-листу или передай человеку.
- Ты ничего не меняешь в системе: не начисляешь и не переносишь дни, не удаляешь устройства за клиента, не выдаёшь новые ссылки, не разблокируешь, не делаешь возвраты и компенсации. Никогда не пиши, что это уже сделано или точно будет сделано, и не называй сроки и количество дней компенсации.
- escalate=true ставь, когда нужен человек: действие из списка выше, вопрос возврата или списания денег, блокировка, баг бота или оплаты, серверы не работают после чек-листа и лог собран, B2B/реклама/юридические вопросы, клиент в конфликте и не принимает объяснение, или ты не уверен в ответе. Перед передачей сначала собери данные из таблицы "Эскалация" — если чего-то не хватает, escalate=false и попроси недостающее. escalate_reason — одно предложение на русском для админа: суть проблемы и что уже собрано.
- answer всегда уходит клиенту как есть. При escalate=true answer — короткое сообщение о передаче без сроков и обещаний (как в шаблоне "Клиенту при передаче").
- Блок "Текущий статус сервиса" в сообщении — сообщение от команды о массовом сбое. Если там указан сбой, действуй по разделам 1.1/1.2: не гоняй клиента по чек-листу, не проси логи и не пиши "обновите подписку" — ответь шаблоном про технические работы своими словами с учётом описания.
- Блок "Данные подписки клиента" — свежие данные из панели, а не слова клиента: статус, срок, трафик, устройства, последнее подключение, последний сервер, приложение, ссылка подписки. Используй их для диагностики (статус "истекла" или "лимит трафика исчерпан" — вот и причина; "не подключался" — подписка не добавлена в приложение; давно не обновлял подписку — обновить), но не пересказывай клиенту весь блок и упоминай только то, что прямо относится к вопросу. Не говори "панель", "система", "слоты" — просто "вижу, что у вас...". Если клиента нет в панели — попроси юзернейм или Telegram ID, с которого покупал. Ссылку подписки можно прислать клиенту, когда по базе нужно добавить подписку в приложение. "Последняя нода" — внутреннее имя сервера: клиенту его не называй, говори о разделах из приложения ("Авто", страна, "Обход глушилок").
- Лимит устройств НЕ влияет на работу уже подключённых устройств. Он мешает только добавить или обновить подписку на новом устройстве: признаки — "превышен лимит устройств", пустой список серверов, только сервер "Поддержка", "обновите/оплатите подписку" при активных днях. Если серверы в приложении видны, но показывают n/a, не подключаются или не грузят сайты — это НЕ лимит устройств, не упоминай его, иди по разделам 1.3/1.4 и чек-листу. Занятые 5 из 5 устройств — нормальная ситуация, а не проблема.
- Вложения: клиент может прислать скриншот, фото или PDF (например, чек). Разбирай их так же, как это делает поддержка: по скрину определи приложение, устройство, видны ли серверы, их пинг, дату обновления подписки, ошибку; по чеку — сумму, дату, банк. В attachment_note одним коротким предложением опиши вложение для истории (например, "скрин Happ: серверы видны, все n/a, подписка обновлена 12.09" или "чек СБП на 299 ₽ от 30.09"); без вложения — пустая строка.
- Если в сообщении есть пометка вида "[клиент прислал ... — посмотреть нельзя]" — ты этого не видишь: не делай вид, что посмотрел. Если это голосовое — попроси коротко написать текстом. Если это видео, которое просили для техников, — поблагодари и передай человеку; иначе попроси описать текстом или прислать скриншот.
- Если клиент просто благодарит, подтверждает, что всё получилось, или пишет "ок"/эмодзи, а последняя реплика поддержки уже закрыла вопрос или была ответом на благодарность — no_reply_needed=true, answer пустой. Если последняя реплика поддержки была ответом по существу — ответь коротко и тепло.
- Приветствие ("Добрый день!") — только в первом сообщении поддержки в диалоге. Если в истории уже есть реплика поддержки — сразу к сути.
- Не по теме VPN (реклама, продажа трафика, посторонние просьбы) — по существу не отвечай; рекламу и B2B передай человеку.

===== БАЗА ЗНАНИЙ =====
{kb}
"""

SYSTEM_PROMPT = SYSTEM_PROMPT_TEMPLATE.format(kb=KB_FILE.read_text(encoding="utf-8"))

_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

_TRAILING_JSON_DEBRIS = re.compile(r"""[\s"'{}\[\],]+$""")

# Подтверждённый в support-x сбой: в answer попадает обрывок мета-рассуждения
# на английском ("wait fix format issue - need to produce proper JSON"). Ответ
# клиенту всегда по-русски и по существу — такие слова почти гарантированно утечка.
_LEAKED_META_RE = re.compile(r"\bjson\b|format issue|need to produce", re.IGNORECASE)

# Заявления о выполненном действии. У ассистента нет ни одного действия в
# системе, так что "начислили/перенёс/удалил/вернули" в ответе — всегда ложь
# клиенту: промпт-запрет это не удерживает полностью, поэтому проверка кодом.
_ACTION_CLAIM_RE = re.compile(
    # "удалили"/"вернули" сюда не входят: ими же описывают действия клиента
    # ("если вы удалили устройства") и починку серверов ("обходы вернули").
    r"\b(начислил[аи]?|добавил[аи]? вам|перенесл[аи]|разблокировал[аи]?|компенсировал[аи]?)\b",
    re.IGNORECASE,
)

_FALSE_CLAIM_CORRECTION = (
    "[СИСТЕМНАЯ ПРОВЕРКА] В твоём ответе сказано, что действие уже выполнено, но у тебя нет "
    "доступа к таким действиям — на самом деле ничего не произошло. Переформулируй ответ без "
    "утверждения о выполненном действии; если действие нужно — передай человеку (escalate=true)."
)


def _strip_trailing_json_debris(text: str) -> str:
    """Изредка модель дописывает после текста обрывок JSON-синтаксиса — убираем хвост."""
    return _TRAILING_JSON_DEBRIS.sub("", text)


def _first_sentence(text: str) -> str:
    """escalate_reason иногда получает мусорный хвост сразу после первого
    предложения. Точки внутри дат и сумм ("30.09", "169.00") предложение не
    заканчивают — терминатор только перед пробелом или концом строки."""
    match = re.search(r"[.!?](?=\s|$)", text)
    return text[: match.end()] if match else text


async def _panel_block(telegram_id: int) -> str:
    """Данные подписки из панели, как в карточке клиента, но без HTML-разметки."""
    try:
        lines = await get_panel_lines(telegram_id)
    except Exception:
        logger.exception(f"Failed to load panel data for user_id={telegram_id}")
        lines = []
    text = html.unescape(re.sub(r"<[^>]+>", "", "\n".join(lines))).strip()
    return text or "нет данных (панель недоступна)"


def _attachment_block(attachment: Attachment) -> dict:
    data = base64.standard_b64encode(attachment.data).decode("ascii")
    if attachment.mime_type == "application/pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}}
    return {"type": "image", "source": {"type": "base64", "media_type": attachment.mime_type, "data": data}}


def _history_entry(text: str, attachments: list[Attachment], note: str) -> str:
    """Что пишем в историю за клиента: сырые вложения туда не попадают никогда,
    только то, что модель в них разглядела."""
    if not attachments:
        return text
    kinds = ", ".join(sorted({a.kind for a in attachments}))
    return f"{text}\n[{kinds}: {note or 'содержимое не разобрано'}]".strip()


async def get_ai_answer(
    topic_id: int,
    telegram_id: int,
    text: str,
    attachments: list[Attachment],
) -> Optional[SupportAnswer]:
    """Ответ ИИ на пачку сообщений клиента. Реплика клиента записывается в
    историю в любом случае; ответ ИИ — вызывающим кодом, только если он реально
    ушёл клиенту. None — модель так и не дала пригодный ответ."""
    if _client is None:
        raise RuntimeError("ANTHROPIC_API_KEY не задан в .env")

    history = await ai_context.get_history(topic_id)
    outage = await ai_context.get_outage()

    context = (
        f"Текущий статус сервиса: {outage or 'массового сбоя нет'}\n\n"
        f"Данные подписки клиента:\n{await _panel_block(telegram_id)}\n\n"
        f"Сообщение клиента:\n{text or '[без текста, только вложение]'}"
    )
    # Вложения перед текстом — так рекомендует Anthropic. cache_control на
    # последнем блоке: следующий ход читает этот, включая картинки, из кэша.
    content: list[dict] = [_attachment_block(a) for a in attachments]
    content.append({"type": "text", "text": context, "cache_control": {"type": "ephemeral", "ttl": "1h"}})
    messages: list[dict] = [{"role": t["role"], "content": t["content"]} for t in history]
    messages.append({"role": "user", "content": content})

    result: Optional[SupportAnswer] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = await _client.messages.create(
                model=AI_MODEL,
                max_tokens=16000,
                # Как в support-x: thinking выключен, формат гарантирует json_schema,
                # effort=medium — на high модель уходила в долгие размышления.
                thinking={"type": "disabled"},
                system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
                messages=messages,
                output_config={"effort": "medium", "format": {"type": "json_schema", "schema": _ANSWER_SCHEMA}},
            )
        except APIError as exc:
            logger.warning(f"Anthropic API error (attempt {attempt}/{MAX_RETRIES}): {exc}")
            await asyncio.sleep(attempt * 2)
            continue

        usage = response.usage
        logger.info(
            f"Claude {AI_MODEL}: input={usage.input_tokens} cache_read={usage.cache_read_input_tokens} "
            f"cache_write={usage.cache_creation_input_tokens} output={usage.output_tokens} stop={response.stop_reason}"
        )
        raw = "".join(b.text for b in response.content if b.type == "text").strip()
        if not raw:
            # Пустой ответ — почти всегда обрыв по max_tokens; повтор даст то же самое.
            logger.error(f"Claude returned no text (stop_reason={response.stop_reason})")
            break
        try:
            parsed = SupportAnswer.model_validate_json(raw)
        except ValidationError as exc:
            logger.warning(f"Claude returned invalid JSON (attempt {attempt}/{MAX_RETRIES}): {exc}")
            continue

        if not parsed.no_reply_needed and not parsed.answer and not parsed.escalate_reason:
            logger.warning(f"Claude returned an empty answer (attempt {attempt}/{MAX_RETRIES})")
            continue
        if _LEAKED_META_RE.search(parsed.answer) or _LEAKED_META_RE.search(parsed.escalate_reason):
            logger.warning(f"Claude leaked service text (attempt {attempt}/{MAX_RETRIES}): {parsed.answer!r}")
            continue
        if _ACTION_CLAIM_RE.search(parsed.answer):
            # Поправка только в локальном запросе — в историю топика не пишется.
            logger.warning(f"Claude claimed an action it can't perform (attempt {attempt}/{MAX_RETRIES}): {parsed.answer!r}")
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": _FALSE_CLAIM_CORRECTION})
            continue

        parsed.answer = _strip_trailing_json_debris(parsed.answer)
        parsed.escalate_reason = _strip_trailing_json_debris(_first_sentence(parsed.escalate_reason))
        parsed.attachment_note = _strip_trailing_json_debris(parsed.attachment_note)
        result = parsed
        break

    if result is None:
        logger.error(f"Claude gave no usable answer for topic {topic_id}")
    note = result.attachment_note if result else ""
    await ai_context.append_turn(topic_id, "user", _history_entry(text, attachments, note) or "[вложение]")
    return result
