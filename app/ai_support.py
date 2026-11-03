import asyncio
import base64
import html
import io
import json
import re
from dataclasses import dataclass
from typing import Callable, Optional

from anthropic import APIError, AsyncAnthropic
from loguru import logger
from pydantic import BaseModel, ValidationError
from pypdf import PdfReader

from app import ai_context
from app.media import Video
from app.remnawave import get_panel_lines
from app.settings import (
    AI_FALLBACK_MODEL,
    AI_MODEL,
    ANTHROPIC_API_KEY,
    ANTHROPIC_FALLBACK_MODEL,
    BASE_DIR,
    GPUNIQ_API_KEY,
    GPUNIQ_BASE_URL,
)

KB_FILE = BASE_DIR / "kb" / "knowledge.md"

MAX_RETRIES = 3
MAX_OUTPUT_TOKENS = 1200
_MAX_PDF_TEXT_CHARS = 4000
_MAX_PDF_IMAGE_BYTES = 5 * 1024 * 1024


@dataclass
class Attachment:
    """Вложение клиента, которое модель может прочитать: картинка или PDF."""

    data: bytes
    mime_type: str
    kind: str  # "фото" или "документ" — для пометки в истории


ACTIONS = ("none", "story_bonus", "compensation", "transfer_days", "reset_devices")
VIDEOS = ("none",) + tuple(v.value for v in Video)


class SupportAnswer(BaseModel):
    answer: str
    escalate: bool
    escalate_reason: str
    attachment_note: str
    no_reply_needed: bool
    # Видеоинструкция, которую приложить к ответу (или "none").
    video: str = "none"
    # Предложение действия для админа: карточка с кнопками в топике.
    action: str = "none"
    action_days: int = 0
    action_summary: str = ""


# JSON-схема ответа — плоская, с additionalProperties=false.
# Передаётся через принудительный tool_choice (submit_answer), так как
# GPUniq /v1/messages поддерживает tools/tool_choice, но игнорирует output_config.
_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "escalate": {"type": "boolean"},
        "escalate_reason": {"type": "string"},
        "attachment_note": {"type": "string"},
        "no_reply_needed": {"type": "boolean"},
        "video": {"type": "string", "enum": list(VIDEOS)},
        "action": {"type": "string", "enum": list(ACTIONS)},
        "action_days": {"type": "integer"},
        "action_summary": {"type": "string"},
    },
    "required": [
        "answer", "escalate", "escalate_reason", "attachment_note", "no_reply_needed",
        "video", "action", "action_days", "action_summary",
    ],
    "additionalProperties": False,
}

_SUBMIT_ANSWER_TOOL = {
    "name": "submit_answer",
    "description": "Отправить структурированный ответ клиенту и/или передать вопрос сотруднику.",
    "input_schema": _ANSWER_SCHEMA,
}

SYSTEM_PROMPT_TEMPLATE = """Ты — первая линия поддержки Burger VPN, отвечаешь клиентам в Telegram от имени поддержки.

Правила:
- Пиши от лица команды поддержки ("мы"). По своей инициативе не упоминай, что ты ИИ, бот или ассистент, не ссылайся на "базу знаний", "инструкцию для поддержки" или процесс генерации ответа. Если клиент прямо спрашивает, бот ли ты — не ври: коротко подтверди, что на первой линии отвечает автоматический помощник, а сложные вопросы разбирают живые сотрудники, и вернись к сути.
- Отвечай на языке клиента (обычно русский). Тон — как в разделе "Примеры живых ответов поддержки": на "вы", доброжелательно, коротко, без канцелярита и технического жаргона. Уместно редкое 🙏 или 🫶.
- Используй только информацию из базы знаний ниже и из блоков данных в сообщении. Не придумывай кнопки, функции, ссылки, сроки, цены и правила, которых там нет.
- Главная задача — выяснить причину. Если данных не хватает, не гадай: запроси нужное по разделу базы (что не работает, Wi‑Fi или мобильный, регион и оператор, приложение, скрин списка серверов, скрин оплаты). Никогда не проси клиента присылать логи из приложения. Не спрашивай то, что клиент уже сообщил или что видно на его скрине или в блоке данных подписки.
- Когда причина ещё не ясна — задай уточняющий вопрос или дай один шаг и жди ответа. Не добавляй заранее план "а если не поможет, тогда сделайте...": один вопрос или один шаг — дальше по ответу клиента. Полный многошаговый список уместен только для готовой, точно применимой процедуры (например, как подключить новое устройство).
- Не повторяй совет, который уже давал в этом диалоге и который не помог: предложи следующий шаг по чек-листу. Когда обычные шаги (обновление подписки, смена сервера/обхода, проверка без VPN) не помогают — предложи перезагрузить устройство (телефон или компьютер) и попробовать подключиться снова. Если вообще тупик и не пингуются/не работают ни «Обход глушилок», ни обычные серверы — попроси проверить подключение с другой сети (с Wi‑Fi на мобильный или наоборот) или на другом устройстве в той же сети: если с другой сети на том же устройстве всё работает — дело в операторе/провайдере; если на другом устройстве в той же сети всё работает — проблема локально на стороне устройства клиента (конфликт профилей VPN/DNS, сетевые настройки). Передавай человеку только если это не помогло.
- Ты ничего не меняешь в системе: не начисляешь и не переносишь дни, не удаляешь устройства за клиента, не выдаёшь новые ссылки, не разблокируешь, не делаешь возвраты и компенсации. Никогда не пиши, что это уже сделано или точно будет сделано, и не называй сроки и количество дней компенсации.
- escalate=true ставь, когда нужен человек: действие из списка выше, вопрос возврата или списания денег, блокировка, баг бота или оплаты, серверы не работают после чек-листа и перезагрузки устройства, B2B/реклама/юридические вопросы, клиент в конфликте и не принимает объяснение, или ты не уверен в ответе. Перед передачей сначала собери данные из таблицы "Эскалация" — если чего-то не хватает, escalate=false и попроси недостающее. escalate_reason — одно предложение на русском для админа: суть проблемы и что уже собрано.
- answer всегда уходит клиенту как есть. При escalate=true answer — короткое сообщение о передаче без сроков и обещаний (как в шаблоне "Клиенту при передаче").
- Блок "Текущий статус сервиса" в сообщении — сообщение от команды о массовом сбое. Если там указан сбой, действуй по разделам 1.1/1.2: не гоняй клиента по чек-листу и не пиши "обновите подписку" — ответь шаблоном про технические работы своими словами с учётом описания.
- Блок "Данные подписки клиента" — свежие данные из панели Remnawave, а не слова клиента: статус, срок, трафик, привязанные устройства (`Устройства (HWID): X/Y` + список конкретных моделей, платформ, приложений и дат активности), последнее подключение, последний сервер, ссылка подписки. Используй их для диагностики (статус "истекла" или "лимит трафика исчерпан" — вот и причина; "не подключался" — подписка не добавлена в приложение; давно не обновлял подписку — обновить), но не пересказывай клиенту весь блок и упоминай только то, что прямо относится к вопросу. Не говори "панель", "система", "слоты", "HWID" — просто "вижу, что у вас...". Если клиента нет в панели — попроси юзернейм или Telegram ID, с которого покупал. Ссылку подписки можно прислать клиенту, когда по базе нужно добавить подписку в приложение. "Последняя нода" — внутреннее имя сервера: клиенту его не называй, говори о разделах из приложения ("Авто", страна, "Обход глушилок").
- Устройства и лимит устройств (ВАЖНО):
  1) Лимит устройств НЕ влияет на работу уже подключённых устройств. Он мешает только добавить или обновить подписку на новом устройстве: признаки — "превышен лимит устройств", пустой список серверов, только сервер "Поддержка", "обновите/оплатите подписку" при активных днях. Если серверы в приложении видны, но показывают n/a, не подключаются или не грузят сайты — это НЕ лимит устройств, не упоминай его, иди по разделам 1.3/1.4 и чек-листу. Занятые 5 из 5 устройств сами по себе — нормальная ситуация, а не поломка.
  2) Когда вопрос ИМЕННО про лимит устройств, подключение ещё одного устройства (телефон, ПК, ТВ), переустановку приложения или отвязку устройств — **НЕ навязывай с порога докупку устройств!** Сначала назови клиенту простыми словами данные из `Устройства (HWID)` в блоке подписки: сколько мест занято из лимита и какие именно устройства/приложения сейчас привязаны (если видно два одинаковых устройства после переустановки приложения или два разных клиента вроде Happ и Incy на одном телефоне — прямо подскажи это). В первую очередь предложи **бесплатно отвязать (удалить) старое или ненужное устройство** в основном боте `@VpnBurger_bot` → **«Управление подпиской»** → **«Мои устройства»** → выбрать устройство → **«Удалить устройство»**, и добавить на его место другое (приложи `video="delete_device"`, если ещё не отправлял его в этом диалоге). И спокойно добавь без навязывания оба варианта: он может **удалить ненужное устройство в «Мои устройства» и добавить вместо него другое**, либо — **если действительно нужно пользоваться бóльшим числом устройств одновременно — докупить дополнительные места** через `@VpnBurger_bot` → **«Управление подпиской»** → **«Расширить подписку»**.
- Вложения: клиент может прислать скриншот, фото или PDF (например, чек). Разбирай их так же, как это делает поддержка: по скрину определи приложение, устройство, видны ли серверы, их пинг, дату обновления подписки, ошибку; по чеку — сумму, дату, банк. В attachment_note одним коротким предложением опиши вложение для истории (например, "скрин Happ: серверы видны, все n/a, подписка обновлена 12.09" или "чек СБП на 299 ₽ от 30.09"); без вложения — пустая строка.
- Если в сообщении есть пометка вида "[клиент прислал ... — посмотреть нельзя]" — ты этого не видишь: не делай вид, что посмотрел. Если это голосовое — попроси коротко написать текстом. Если это видео, которое просили для техников, — поблагодари и передай человеку; иначе попроси описать текстом или прислать скриншот.
- Если клиент просто благодарит, подтверждает, что всё получилось, или пишет "ок"/эмодзи, а последняя реплика поддержки уже закрыла вопрос или была ответом на благодарность — no_reply_needed=true, answer пустой. Если последняя реплика поддержки была ответом по существу — ответь коротко и тепло.
- Приветствие ("Добрый день!") — только в первом сообщении поддержки в диалоге. Если в истории уже есть реплика поддержки — сразу к сути.
- Не по теме VPN (реклама, продажа трафика, вакансии, посторонние просьбы) — по существу не отвечай: коротко "Передал коллегам" и escalate=true.
- Этот чат — поддержка. Подписка, оплата, устройства, бонусы и ссылка подписки — в основном боте @VpnBurger_bot: когда отправляешь клиента что-то сделать, называй его (например, "в боте @VpnBurger_bot → «Управление подпиской»"). Не описывай интерфейс подробнее, чем написано в базе знаний.
- Никогда не говори клиенту, что какое-то правило "не зафиксировано" или "не описано" — это внутреннее. Если не знаешь ответа, скажи, что уточнишь у коллег, и передай человеку.
- Пиши о себе в мужском роде ("передал", "проверил", "рад"), всегда одинаково.
- Реплики в истории с пометкой "[сотрудник]" написал живой сотрудник поддержки. Никогда не противоречь ему и не отменяй его советы ("удаление устройств не поможет" после совета сотрудника удалить устройства — недопустимо). Продолжай в его русле; если не согласен с фактами — escalate=true и напиши об этом в escalate_reason, клиенту — нейтрально.
- Не повторяй одну и ту же просьбу (регион, скрин) больше двух раз за диалог: если клиент не может или не хочет это прислать — работай с тем, что есть, или передай человеку. Не повторяй "передал коллегам", если уже говорил это в последних репликах и ничего нового не произошло — на "ну что там?" ответь, что вопрос у сотрудников, без повторной передачи (escalate=false).
- Если клиент просит вернуть деньги — сначала ответь именно про возврат (раздел 2.5): спроси причину, тариф и дату оплаты, передай человеку. Не отвечай на запрос возврата чек-листом диагностики.
- Лимит трафика: действует только на серверы "Обход глушилок", обычные иностранные серверы безлимитны (см. раздел 5.1). Точный расход и лимит конкретного клиента бери из блока "Данные подписки клиента". Докупить дополнительные гигабайты клиент может самостоятельно в боте @VpnBurger_bot → «Управление подпиской» (после докупки нужно обновить подписку в приложении). Эскалируй только если кнопка докупки пропала или после оплаты ГБ не начислились. Сервер "Поддержка" в списке — это признак незагруженной подписки, а не отдельный сервер.
- Пробный период и автопродление: не говори, продлевается ли пробный период автоматически и сколько он стоит. Как отключить автопродление — раздел 2.4.
- Не пиши, что "напомнил админу", "проверил в системе" и т.п. — у тебя нет таких действий.

Видео (поле video): приложи видеоинструкцию, если клиенту нужно именно это действие — update_subscription (обновить подписку в приложении), cancel_subscription (отменить автопродление/подписку в боте), delete_device (удалить устройство в боте). Не прикладывай одно и то же видео повторно, если в истории уже есть "[видеоинструкция: ...]" с ним. В остальных случаях — "none". В тексте можно сказать "отправляю видео, как это сделать".

Действия для сотрудника (поле action) — ты их не выполняешь, а предлагаешь: в топике появится карточка с кнопками, решение примет сотрудник. Клиенту не обещай результат, скажи, что передал на проверку.
- story_bonus — клиент прислал скрин опубликованной сторис по акции "14 дней за сторис" (картинка Burger VPN и ссылка на бота). action_days=14. Проверь по скрину, что сторис видна всем: зелёный значок/надпись "близкие друзья" — не подходит, попроси опубликовать для всех. Premium и 24 часа по скрину не проверить — укажи это в action_summary. Если в блоке "Выданные бонусы" уже есть story_bonus — не предлагай повторно, объясни, что акция разовая.
- compensation — сбой был на нашей стороне и клиент несколько дней не мог пользоваться VPN; action_days — твоя оценка дней простоя по истории (не называй их клиенту).
- transfer_days — перенос дней из другого VPN (раздел 5.3, нужны скрины оплаты и остатка дней) или со старого аккаунта Telegram; action_days — сколько дней по скринам.
- reset_devices — клиенту нужно удалить все устройства (лимит, чужой подключился), а сам в боте он не справляется. action_days=0.
- Во всех остальных случаях — "none", action_days=0. action_summary — одно-два предложения для сотрудника: что прислал клиент, что проверено, что смущает.

Логи: сам логи никогда не запрашивай. Если клиент по своей инициативе прислал лог и в сообщении есть "[выжимка лога клиента ...]" — учти её при ответе и при наличии ошибок подключения передай техникам (escalate=true).
ФОРМАТ ВЫВОДА (КРИТИЧНО): для отправки любого ответа ОБЯЗАТЕЛЬНО вызывай инструмент submit_answer со всеми обязательными полями схемы. Никогда не отвечай обычным текстом и не пиши никакой сопроводительный текст вне аргументов инструмента submit_answer.

===== БАЗА ЗНАНИЙ =====
{kb}

ВАЖНО: в истории диалога прошлые ответы сохранены просто текстом для компактности, но твой новый ответ ВСЕГДА должен отправляться только через вызов инструмента submit_answer (не обычным текстом).
"""

SYSTEM_PROMPT = SYSTEM_PROMPT_TEMPLATE.format(kb=KB_FILE.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class _ProviderRoute:
    client: AsyncAnthropic
    model: str
    billing_group: str


def _build_clients() -> tuple[Optional[AsyncAnthropic], Optional[AsyncAnthropic]]:
    primary: Optional[AsyncAnthropic] = None
    fallback: Optional[AsyncAnthropic] = None
    if GPUNIQ_API_KEY:
        primary = AsyncAnthropic(api_key=GPUNIQ_API_KEY, base_url=GPUNIQ_BASE_URL)
    if ANTHROPIC_API_KEY:
        client = (
            AsyncAnthropic(api_key=ANTHROPIC_API_KEY, base_url=GPUNIQ_BASE_URL)
            if ANTHROPIC_API_KEY.startswith("gpuniq_")
            else AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        )
        if primary is None:
            primary = client
        elif not ANTHROPIC_API_KEY.startswith("gpuniq_"):
            fallback = client
    return primary, fallback


_client, _fallback_client = _build_clients()
_primary_is_gpuniq = bool(GPUNIQ_API_KEY or ANTHROPIC_API_KEY.startswith("gpuniq_"))
_initial_client = _client


def _get_routes() -> list[_ProviderRoute]:
    """Цепочка маршрутов: основная модель GPUniq -> резервная модель GPUniq -> прямой Anthropic."""
    if _client is None:
        return []
    # Если _client подменён в тестах — вызываем только его.
    if _client is not _initial_client:
        return [_ProviderRoute(client=_client, model=AI_MODEL, billing_group="mock")]

    routes: list[_ProviderRoute] = []
    if _primary_is_gpuniq:
        routes.append(_ProviderRoute(client=_client, model=AI_MODEL, billing_group="gpuniq"))
        if AI_FALLBACK_MODEL and AI_FALLBACK_MODEL != AI_MODEL:
            routes.append(_ProviderRoute(client=_client, model=AI_FALLBACK_MODEL, billing_group="gpuniq"))
    else:
        direct_model = AI_MODEL if AI_MODEL.startswith("claude-") else ANTHROPIC_FALLBACK_MODEL
        routes.append(_ProviderRoute(client=_client, model=direct_model, billing_group="anthropic"))

    if _fallback_client is not None:
        routes.append(
            _ProviderRoute(client=_fallback_client, model=ANTHROPIC_FALLBACK_MODEL, billing_group="anthropic")
        )
    return routes

_BILLING_ERROR_MARKERS = (
    "credit balance is too low",
    "insufficient balance",
    "insufficient_balance",
    "insufficient_quota",
    "payment required",
    "invalid_api_key",
    "api_key_revoked",
)


def _is_billing_or_auth_error(exc: APIError) -> bool:
    if getattr(exc, "status_code", None) in (401, 402):
        return True
    err_lower = str(exc).lower()
    return any(m in err_lower for m in _BILLING_ERROR_MARKERS)

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


def _detect_image_mime(data: bytes) -> Optional[str]:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def extract_pdf_payload(data: bytes) -> tuple[str, Optional[tuple[bytes, str]]]:
    """Извлечь текст из цифрового PDF-чека (Сбер, Т-Банк, Альфа, СБП и др.),
    а для сканов без текстового слоя — попробовать достать встроенное изображение."""
    try:
        reader = PdfReader(io.BytesIO(data))
        pages_text = [(page.extract_text() or "").strip() for page in reader.pages[:5]]
        extracted = "\n".join(p for p in pages_text if p).strip()
        if extracted:
            return extracted[:_MAX_PDF_TEXT_CHARS], None

        for page in reader.pages[:2]:
            for img in getattr(page, "images", ()):
                img_bytes = getattr(img, "data", b"")
                mime = _detect_image_mime(img_bytes) if img_bytes else None
                if mime and len(img_bytes) <= _MAX_PDF_IMAGE_BYTES:
                    return "", (img_bytes, mime)
    except Exception as exc:
        logger.warning(f"Failed to parse PDF attachment: {exc}")
    return "", None


def _attachment_blocks(attachment: Attachment) -> list[dict]:
    """Представление вложения для LLM. GPUniq /v1/messages игнорирует блоки
    type='document' (PDF), поэтому PDF-чеки передаём извлечённым текстом (~80 токенов
    вместо тысяч и 100% совместимость со всеми моделями) или встроенной картинкой."""
    if attachment.mime_type == "application/pdf":
        pdf_text, embedded_image = extract_pdf_payload(attachment.data)
        if pdf_text:
            return [{"type": "text", "text": f"[Содержимое прикреплённого PDF-документа]:\n{pdf_text}"}]
        if embedded_image is not None:
            img_bytes, img_mime = embedded_image
            b64 = base64.standard_b64encode(img_bytes).decode("ascii")
            return [{"type": "image", "source": {"type": "base64", "media_type": img_mime, "data": b64}}]
        return [
            {
                "type": "text",
                "text": (
                    "[Клиент прислал сканированный PDF-документ без текстового слоя — "
                    "вежливо попроси прислать обычный скриншот этого документа/чека]"
                ),
            }
        ]
    data = base64.standard_b64encode(attachment.data).decode("ascii")
    return [{"type": "image", "source": {"type": "base64", "media_type": attachment.mime_type, "data": data}}]


def _normalize_history(history: list[dict[str, str]]) -> tuple[list[dict[str, str]], str]:
    """Склеить подряд идущие реплики одной роли (возникают при паузах ИИ или
    no_reply_needed), чтобы история строго чередовала user/assistant.
    Если история заканчивается репликой user без ответа — возвращаем её отдельно
    для объединения с текущим сообщением клиента."""
    merged: list[dict[str, str]] = []
    for turn in history:
        content = (turn.get("content") or "").strip()
        if not content:
            continue
        role = turn["role"]
        if merged and merged[-1]["role"] == role:
            merged[-1]["content"] = f"{merged[-1]['content']}\n\n{content}"
        else:
            merged.append({"role": role, "content": content})

    trailing_user = ""
    if merged and merged[-1]["role"] == "user":
        trailing_user = merged.pop()["content"]
    return merged, trailing_user


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
    extra_context: str = "",
    *,
    on_commit: Optional[Callable[[], None]] = None,
) -> Optional[SupportAnswer]:
    """Ответ ИИ на пачку сообщений клиента. Реплика клиента записывается в
    историю в любом случае; ответ ИИ — вызывающим кодом, только если он реально
    ушёл клиенту. extra_context (выжимки логов) уходит только в этот запрос и
    в историю не пишется — иначе каждый следующий ход тянул бы его заново.
    None — модель так и не дала пригодный ответ."""
    routes = _get_routes()
    if not routes:
        raise RuntimeError("GPUNIQ_API_KEY / ANTHROPIC_API_KEY не задан в .env")

    raw_history = await ai_context.get_history(topic_id)
    history, trailing_user = _normalize_history(raw_history)
    outage = await ai_context.get_outage()

    bonuses = await ai_context.get_bonuses(telegram_id)
    bonuses_text = ", ".join(f"{kind} +{days} дн. ({at[:10]})" for kind, days, at in bonuses) or "нет"
    combined_client_text = (
        f"{trailing_user}\n\n{text}".strip() if trailing_user else text
    ) or "[без текста, только вложение]"
    context = (
        f"Текущий статус сервиса: {outage or 'массового сбоя нет'}\n\n"
        f"Данные подписки клиента:\n{await _panel_block(telegram_id)}\n\n"
        f"Выданные бонусы: {bonuses_text}\n\n"
        f"Сообщение клиента:\n{combined_client_text}"
        + (f"\n\n{extra_context}" if extra_context else "")
    )
    # Инкрементальный breakpoint на последней реплике истории: следующий ход
    # в том же топике читает весь предыдущий диалог из кэша.
    messages: list[dict] = []
    for i, turn in enumerate(history):
        if i == len(history) - 1:
            messages.append(
                {
                    "role": turn["role"],
                    "content": [
                        {
                            "type": "text",
                            "text": turn["content"],
                            "cache_control": {"type": "ephemeral", "ttl": "1h"},
                        }
                    ],
                }
            )
        else:
            messages.append({"role": turn["role"], "content": turn["content"]})

    # Вложения перед текстом — так рекомендует Anthropic. cache_control на
    # последнем блоке кэширует текущий ход при локальных ретраях.
    content: list[dict] = []
    for attachment in attachments:
        content.extend(_attachment_blocks(attachment))
    content.append({"type": "text", "text": context, "cache_control": {"type": "ephemeral", "ttl": "1h"}})
    messages.append({"role": "user", "content": content})

    dead_billing_groups: set[str] = set()
    route_idx = 0

    result: Optional[SupportAnswer] = None
    attempt = 1
    while attempt <= MAX_RETRIES:
        active_route = routes[route_idx]
        try:
            response = await active_route.client.messages.create(
                model=active_route.model,
                max_tokens=MAX_OUTPUT_TOKENS,
                thinking={"type": "disabled"},
                system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
                messages=messages,
                tools=[_SUBMIT_ANSWER_TOOL],
                tool_choice={"type": "tool", "name": "submit_answer"},
            )
        except APIError as exc:
            if _is_billing_or_auth_error(exc):
                dead_billing_groups.add(active_route.billing_group)
                live = [i for i, r in enumerate(routes) if r.billing_group not in dead_billing_groups]
                if not live:
                    logger.critical(f"AI billing/auth error on all providers: {exc}")
                    break
                logger.warning(
                    f"AI provider {active_route.billing_group}/{active_route.model} billing/auth error ({exc}), "
                    f"switching to fallback {routes[live[0]].billing_group}/{routes[live[0]].model}"
                )
                route_idx = live[0]
                continue

            logger.warning(
                f"AI API error on {active_route.billing_group}/{active_route.model} "
                f"(attempt {attempt}/{MAX_RETRIES}): {exc}"
            )
            next_idx = next(
                (
                    idx
                    for offset in range(1, len(routes))
                    if routes[(idx := (route_idx + offset) % len(routes))].billing_group not in dead_billing_groups
                ),
                None,
            )
            if next_idx is not None:
                route_idx = next_idx
                logger.info(f"Switching to fallback AI route {routes[route_idx].billing_group}/{routes[route_idx].model}")
            else:
                await asyncio.sleep(attempt * 2)
            attempt += 1
            continue

        usage = response.usage
        if usage is not None:
            logger.info(
                f"AI {active_route.model}: input={usage.input_tokens} cache_read={usage.cache_read_input_tokens} "
                f"cache_write={usage.cache_creation_input_tokens} output={usage.output_tokens} stop={response.stop_reason}"
            )
        submit_block = next(
            (b for b in response.content if b.type == "tool_use" and b.name == "submit_answer"),
            None,
        )
        if submit_block is not None:
            raw = json.dumps(submit_block.input, ensure_ascii=False)
        else:
            raw = "".join(b.text for b in response.content if b.type == "text").strip()
        if not raw:
            # Пустой ответ — почти всегда обрыв по max_tokens; повтор даст то же самое.
            logger.error(f"AI {active_route.model} returned no text (stop_reason={response.stop_reason})")
            break
        try:
            parsed = SupportAnswer.model_validate_json(raw)
        except ValidationError as exc:
            logger.warning(f"AI {active_route.model} returned invalid JSON (attempt {attempt}/{MAX_RETRIES}): {exc}")
            if submit_block is None:
                messages.append({"role": "assistant", "content": raw})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Ошибка формата: ты ответил обычным текстом вместо вызова инструмента submit_answer. "
                            "ОБЯЗАТЕЛЬНО вызови инструмент submit_answer со всеми обязательными полями схемы."
                        ),
                    }
                )
            attempt += 1
            continue

        if not parsed.no_reply_needed and not parsed.answer and not parsed.escalate_reason:
            logger.warning(f"AI {active_route.model} returned an empty answer (attempt {attempt}/{MAX_RETRIES})")
            attempt += 1
            continue
        if _LEAKED_META_RE.search(parsed.answer) or _LEAKED_META_RE.search(parsed.escalate_reason):
            logger.warning(f"AI {active_route.model} leaked service text (attempt {attempt}/{MAX_RETRIES}): {parsed.answer!r}")
            attempt += 1
            continue
        if _ACTION_CLAIM_RE.search(parsed.answer):
            # Поправка только в локальном запросе — в историю топика не пишется.
            logger.warning(f"AI {active_route.model} claimed an action it can't perform (attempt {attempt}/{MAX_RETRIES}): {parsed.answer!r}")
            if submit_block is not None:
                messages.append({"role": "assistant", "content": response.content})
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": submit_block.id,
                                "content": _FALSE_CLAIM_CORRECTION,
                                "is_error": True,
                            }
                        ],
                    }
                )
            else:
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content": _FALSE_CLAIM_CORRECTION})
            attempt += 1
            continue

        parsed.answer = _strip_trailing_json_debris(parsed.answer)
        parsed.escalate_reason = _strip_trailing_json_debris(_first_sentence(parsed.escalate_reason))
        parsed.attachment_note = _strip_trailing_json_debris(parsed.attachment_note)
        parsed.action_summary = _strip_trailing_json_debris(parsed.action_summary)
        result = parsed
        break

    if on_commit is not None:
        on_commit()
    if result is None:
        logger.error(f"AI gave no usable answer for topic {topic_id}")
    note = result.attachment_note if result else ""
    await ai_context.append_turn(topic_id, "user", _history_entry(text, attachments, note) or "[вложение]")
    return result
