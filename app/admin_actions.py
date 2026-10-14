"""Действия админа кнопками в топике клиента.

ИИ сам распознаёт повод (сторис, перенос дней, компенсация, сброс устройств)
и кладёт в топик карточку с собранными данными; решение и действие — за
человеком по кнопке. Кнопки обрабатываются только в группе поддержки, то есть
нажать их могут только её участники.

Начисление дней пока выполняется вручную в BurgerVPN ("Выдать дни"): там срок
подписки хранится и в панели, и в своей БД, а запись только в панель даёт
ложные уведомления об окончании и досрочное автосписание. Кнопка "Начислил"
фиксирует решение, учитывает бонус (сторис — один раз) и сообщает клиенту.
Сброс устройств выполняется прямо из бота через панель.
"""
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from loguru import logger

from app import ai_context, remnawave
from app.delivery import send_to_client
from app.settings import SUPPORT_CHAT_ID
from app.topics import build_user_card, get_user_id

router = Router()
router.callback_query.filter(F.message.chat.id == int(SUPPORT_CHAT_ID))

CREDIT_KINDS = {
    "story_bonus": "14 дней за сторис",
    "compensation": "компенсация",
    "transfer_days": "перенос дней",
}

_CLIENT_CREDITED = "Начислили вам {days} дн. 🫶 Обновите подписку в приложении, чтобы изменения подтянулись."
_CLIENT_DEVICES_RESET = (
    "Удалили все устройства из вашей подписки. Добавьте подписку в приложение заново — "
    "место для устройства освободилось 🙏"
)


class Act(CallbackData, prefix="act"):
    op: str  # credit | deny | devask | devyes | devno | card
    kind: str = ""
    days: int = 0


def credit_keyboard(kind: str, days: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"✅ Начислил {days} дн.", callback_data=Act(op="credit", kind=kind, days=days).pack()),
        InlineKeyboardButton(text="❌ Отказать", callback_data=Act(op="deny", kind=kind).pack()),
    ]])


def card_keyboard() -> InlineKeyboardMarkup:
    """Под карточкой клиента: обновить данные и сброс устройств (с подтверждением)."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Обновить", callback_data=Act(op="card").pack()),
        InlineKeyboardButton(text="🗑 Сбросить устройства", callback_data=Act(op="devask").pack()),
    ]])


def _devices_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Да, удалить все устройства", callback_data=Act(op="devyes").pack()),
        InlineKeyboardButton(text="Отмена", callback_data=Act(op="devno").pack()),
    ]])


async def post_action_card(bot: Bot, topic_id: int, user_id: int, kind: str, days: int, summary: str) -> None:
    """Карточка предложения ИИ в топике клиента."""
    if kind == "reset_devices":
        text = f"🧾 <b>ИИ предлагает сбросить устройства</b>\n{escape(summary)}"
        markup = _devices_confirm_keyboard()
    else:
        given = [k for k, _, _ in await ai_context.get_bonuses(user_id)]
        warning = "\n⚠️ Бонус за сторис этот клиент уже получал." if kind == "story_bonus" and "story_bonus" in given else ""
        text = (
            f"🧾 <b>{escape(CREDIT_KINDS.get(kind, kind)).capitalize()}: {days} дн.</b>\n{escape(summary)}{warning}\n\n"
            f"Начислите в BurgerVPN: Админка → Выдать дни → ID <code>{user_id}</code>, {days} дн. "
            f"Затем нажмите «Начислил» — клиенту уйдёт уведомление."
        )
        markup = credit_keyboard(kind, days)
    await bot.send_message(chat_id=SUPPORT_CHAT_ID, message_thread_id=topic_id, text=text, reply_markup=markup)


def _admin_name(callback: CallbackQuery) -> str:
    user = callback.from_user
    return f"@{user.username}" if user.username else user.full_name


async def _resolve(callback: CallbackQuery) -> tuple[int, int] | None:
    topic_id = callback.message.message_thread_id
    user_id = await get_user_id(topic_id) if topic_id else None
    if user_id is None:
        await callback.answer("Этот топик не привязан к клиенту.", show_alert=True)
        return None
    return topic_id, user_id


@router.callback_query(Act.filter(F.op == "credit"))
async def on_credit(callback: CallbackQuery, callback_data: Act):
    resolved = await _resolve(callback)
    if resolved is None:
        return
    topic_id, user_id = resolved
    await ai_context.add_bonus(user_id, callback_data.kind, callback_data.days)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(f"✅ Начислено {callback_data.days} дн. — {escape(_admin_name(callback))}")
    await send_to_client(callback.bot, user_id, topic_id, _CLIENT_CREDITED.format(days=callback_data.days), staff=True)
    await callback.answer("Клиенту отправлено уведомление")
    logger.info(f"{_admin_name(callback)} confirmed {callback_data.kind} +{callback_data.days}d for user_id={user_id}")


@router.callback_query(Act.filter(F.op == "deny"))
async def on_deny(callback: CallbackQuery, callback_data: Act):
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply(
        f"❌ Отказано — {escape(_admin_name(callback))}. Напишите клиенту причину в этом топике."
    )
    await callback.answer()


@router.callback_query(Act.filter(F.op == "devask"))
async def on_devices_ask(callback: CallbackQuery):
    await callback.message.reply("Удалить все устройства из подписки клиента?", reply_markup=_devices_confirm_keyboard())
    await callback.answer()


@router.callback_query(Act.filter(F.op == "devno"))
async def on_devices_cancel(callback: CallbackQuery):
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Отменено")


@router.callback_query(Act.filter(F.op == "devyes"))
async def on_devices_reset(callback: CallbackQuery):
    resolved = await _resolve(callback)
    if resolved is None:
        return
    topic_id, user_id = resolved
    await callback.message.edit_reply_markup(reply_markup=None)
    result = await remnawave.reset_devices(user_id)
    if result is None:
        await callback.message.reply("Подписка клиента в панели не найдена — сбросить нечего.")
        await callback.answer()
        return
    deleted, total = result
    await callback.message.reply(f"🗑 Удалено устройств: {deleted} из {total} — {escape(_admin_name(callback))}")
    if deleted:
        await send_to_client(callback.bot, user_id, topic_id, _CLIENT_DEVICES_RESET, staff=True)
    await callback.answer()


@router.callback_query(Act.filter(F.op == "card"))
async def on_card_refresh(callback: CallbackQuery):
    resolved = await _resolve(callback)
    if resolved is None:
        return
    topic_id, user_id = resolved
    chat = await callback.bot.get_chat(user_id)
    await callback.bot.send_message(
        chat_id=SUPPORT_CHAT_ID,
        message_thread_id=topic_id,
        text=await build_user_card(user_id, chat.full_name, chat.username),
        reply_markup=card_keyboard(),
    )
    await callback.answer()
