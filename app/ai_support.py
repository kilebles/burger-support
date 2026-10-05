import asyncio
import html
import re
from dataclasses import dataclass
from typing import Optional

from google import genai
from google.genai import errors, types
from loguru import logger
from pydantic import BaseModel, ValidationError

from app import ai_context
from app.remnawave import get_panel_lines
from app.settings import BASE_DIR, GEMINI_API_KEY, GEMINI_MODELS

KB_FILE = BASE_DIR / "kb" / "knowledge.md"

# Ответ генерируется за один вызов, но на скриншотах модель думает дольше —
# запас с головой, чтобы не обрывать разбор чека или лога на полуслове.
_TIMEOUT_MS = 90_000

# Gemini при пиковой нагрузке отвечает 503 сразу на нескольких моделях (на
# бесплатном тарифе особенно часто). Перебираем цепочку моделей, а если
# лежат все — ещё круг после паузы: клиент подождёт несколько секунд, это
# лучше, чем ответ "ИИ не смог" и пинг админа.
_RETRY_DELAYS = (0, 4, 10)


@dataclass
class Attachment:
    """Вложение клиента, которое модель может прочитать: картинка, PDF или голосовое."""

    data: bytes
    mime_type: str
    kind: str  # "фото", "документ", "голосовое" — для пометки в истории


class SupportAnswer(BaseModel):
    answer: str
    escalate: bool
    escalate_reason: str
    attachment_note: str
    no_reply_needed: bool


SYSTEM_PROMPT_TEMPLATE = """Ты — первая линия поддержки Burger VPN, отвечаешь клиентам в Telegram от имени поддержки.

Правила:
- Пиши от лица команды поддержки ("мы"). По своей инициативе не упоминай, что ты ИИ, бот или ассистент, не ссылайся на "базу знаний", "инструкцию для поддержки" или процесс генерации ответа. Если клиент прямо спрашивает, бот ли ты — не ври: коротко подтверди, что на первой линии отвечает автоматический помощник, а сложные вопросы разбирают живые сотрудники, и вернись к сути.
- Отвечай на языке клиента (обычно русский). Тон — как в разделе "Примеры живых ответов поддержки": на "вы", доброжелательно, коротко, без канцелярита и технического жаргона. Уместно редкое 🙏 или 🫶.
- Используй только информацию из базы знаний ниже и из блоков данных в сообщении. Не придумывай кнопки, функции, ссылки, сроки, цены и правила, которых там нет.
- Главная задача — выяснить причину. Если данных не хватает, не гадай: одним сообщением запроси всё нужное по разделу базы (что не работает, Wi‑Fi или мобильный, регион и оператор, приложение, скрин списка серверов, лог Happ, скрин оплаты и т.п.). Не спрашивай то, что клиент уже сообщил или что видно на его скрине или в блоке данных подписки.
- Не повторяй совет, который уже давал в этом диалоге и который не помог: предложи следующий шаг по чек-листу или передай человеку.
- Ты ничего не меняешь в системе: не начисляешь и не переносишь дни, не удаляешь устройства за клиента, не выдаёшь новые ссылки, не разблокируешь, не делаешь возвраты и компенсации. Никогда не пиши, что это уже сделано или точно будет сделано, и не называй сроки и количество дней компенсации.
- escalate=true ставь, когда нужен человек: действие из списка выше, вопрос возврата или списания денег, блокировка, баг бота или оплаты, серверы не работают после чек-листа и лог собран, B2B/реклама/юридические вопросы, клиент в конфликте и не принимает объяснение, или ты не уверен в ответе. Перед передачей сначала собери данные из таблицы "Эскалация" — если чего-то не хватает, escalate=false и попроси недостающее. escalate_reason — одно-два предложения на русском для админа: суть проблемы и что уже собрано.
- answer всегда уходит клиенту как есть. При escalate=true answer — короткое сообщение о передаче без сроков и обещаний (как в шаблоне "Клиенту при передаче").
- Блок "Текущий статус сервиса" в сообщении — сообщение от команды о массовом сбое. Если там указан сбой, действуй по разделам 1.1/1.2: не гоняй клиента по чек-листу, не проси логи и не пиши "обновите подписку" — ответь шаблоном про технические работы своими словами с учётом описания.
- Блок "Данные подписки клиента" — свежие данные из панели, а не слова клиента: статус, срок, трафик, устройства, последнее подключение, приложение, ссылка подписки. Используй их для диагностики (например, устройства 3/3 — лимит; "не подключался" — подписка не добавлена; давно не обновлял подписку — обновить), но не пересказывай клиенту весь блок. Упоминай из него только то, что прямо относится к вопросу клиента: про занятые устройства — только если вопрос про подключение нового устройства, лимит устройств, пустой список серверов или "обновите подписку". Не говори "панель", "система", "слоты" — просто "вижу, что у вас подключено 5 из 5 устройств". Не строй догадок про конкретного оператора или регион. Ссылку подписки можно прислать клиенту, когда по базе нужно добавить подписку в приложение. Если клиента нет в панели — попроси юзернейм или Telegram ID, с которого покупал.
- Вложения: клиент может прислать скриншот, фото, PDF (например, чек) или голосовое. Разбирай их так же, как это делает поддержка: по скрину определи приложение, устройство, серверы, дату обновления подписки, ошибку; по чеку — сумму, дату, банк; голосовое прослушай. В attachment_note одним коротким предложением опиши вложение для истории (например, "скрин Happ: все серверы n/a, подписка обновлена 12.09" или "чек СБП на 299 ₽ от 30.09"); без вложения — пустая строка.
- Если в сообщении есть пометка вида "[клиент прислал ... — посмотреть нельзя]" — ты этого не видишь: не делай вид, что посмотрел. Если это видео, которое просили для техников, — поблагодари и передай человеку; иначе попроси описать текстом или прислать скриншот.
- Если клиент просто благодарит, подтверждает, что всё получилось, или пишет "ок"/эмодзи, а твоя последняя реплика уже закрыла вопрос или была ответом на благодарность — no_reply_needed=true, answer пустой. Если твоя последняя реплика была ответом по существу — ответь коротко и тепло.
- Приветствие ("Добрый день!") — только в первом сообщении поддержки в диалоге. Если в истории уже есть реплика поддержки — сразу к сути.
- Не по теме VPN (реклама, продажа трафика, посторонние просьбы) — по существу не отвечай; рекламу и B2B передай человеку.

# База знаний

{kb}
"""

SYSTEM_PROMPT = SYSTEM_PROMPT_TEMPLATE.format(kb=KB_FILE.read_text(encoding="utf-8"))

_client: Optional[genai.Client] = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        if not GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY не задан в .env")
        _client = genai.Client(api_key=GEMINI_API_KEY, http_options=types.HttpOptions(timeout=_TIMEOUT_MS))
    return _client


_CONFIG = types.GenerateContentConfig(
    system_instruction=SYSTEM_PROMPT,
    response_mime_type="application/json",
    response_schema=SupportAnswer,
    thinking_config=types.ThinkingConfig(thinking_level="MEDIUM"),
    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
)


async def _panel_block(telegram_id: int) -> str:
    """Данные подписки из панели, как в карточке клиента, но без HTML-разметки."""
    try:
        lines = await get_panel_lines(telegram_id)
    except Exception:
        logger.exception(f"Failed to load panel data for user_id={telegram_id}")
        lines = []
    text = html.unescape(re.sub(r"<[^>]+>", "", "\n".join(lines))).strip()
    return text or "нет данных (панель недоступна)"


def _history_contents(history: list[dict[str, str]]) -> list[types.Content]:
    """История топика в формате Gemini; подряд идущие реплики одной стороны
    склеиваются (например, ответ ИИ и следом уточнение живого админа)."""
    contents: list[types.Content] = []
    for turn in history:
        role = "model" if turn["role"] == "assistant" else "user"
        if contents and contents[-1].role == role:
            contents[-1].parts.append(types.Part.from_text(text=turn["content"]))
        else:
            contents.append(types.Content(role=role, parts=[types.Part.from_text(text=turn["content"])]))
    return contents


async def _generate(contents: list[types.Content]) -> Optional[SupportAnswer]:
    """Цепочка моделей из GEMINI_MODELS; при перегрузке у Google — повторные круги."""
    client = _get_client()
    for delay in _RETRY_DELAYS:
        await asyncio.sleep(delay)
        for model in GEMINI_MODELS:
            try:
                response = await client.aio.models.generate_content(model=model, contents=contents, config=_CONFIG)
            except errors.APIError as error:
                logger.warning(f"Gemini {model} failed: {error.code} {error.status} {str(error.message)[:120]}")
                if error.code in (429, 500, 502, 503, 504):
                    continue
                return None
            except Exception:
                logger.exception(f"Gemini {model} request failed")
                continue

            usage = response.usage_metadata
            if usage:
                logger.info(
                    f"Gemini {model}: prompt={usage.prompt_token_count} cached={usage.cached_content_token_count} "
                    f"thoughts={usage.thoughts_token_count} output={usage.candidates_token_count}"
                )
            parsed = response.parsed
            if isinstance(parsed, SupportAnswer):
                return parsed
            try:
                return SupportAnswer.model_validate_json(response.text or "")
            except ValidationError:
                logger.error(f"Gemini {model} returned unparsable answer: {(response.text or '')[:300]!r}")
                return None
    return None


async def get_ai_answer(
    topic_id: int,
    telegram_id: int,
    text: str,
    attachments: list[Attachment],
) -> Optional[SupportAnswer]:
    """Ответ ИИ на пачку сообщений клиента. Реплика клиента записывается в
    историю в любом случае; ответ ИИ — только если он реально будет отправлен
    (это делает вызывающий код). None — модель не ответила."""
    history = await ai_context.get_history(topic_id)
    outage = await ai_context.get_outage()

    context = (
        f"Текущий статус сервиса: {outage or 'массового сбоя нет'}\n\n"
        f"Данные подписки клиента:\n{await _panel_block(telegram_id)}\n\n"
        f"Сообщение клиента:\n{text or '[без текста, только вложение]'}"
    )
    parts = [types.Part.from_bytes(data=a.data, mime_type=a.mime_type) for a in attachments]
    parts.append(types.Part.from_text(text=context))
    contents = _history_contents(history) + [types.Content(role="user", parts=parts)]

    result = await _generate(contents)

    user_turn = text or ""
    if attachments:
        kinds = ", ".join(sorted({a.kind for a in attachments}))
        note = result.attachment_note if result and result.attachment_note else "содержимое не разобрано"
        user_turn = f"{user_turn}\n[{kinds}: {note}]".strip()
    await ai_context.append_turn(topic_id, "user", user_turn or "[вложение]")

    return result
