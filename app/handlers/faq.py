"""Частые вопросы для клиентов: готовые ответы кнопками, без ИИ и без токенов.

Только то, что подтверждено базой знаний и ботом BurgerVPN. Спорные правила
(лимит трафика, условия пробного периода, рефералка) сюда не попадают, пока
не зафиксированы, — на них отвечает ИИ по данным подписки или человек.
"""
from dataclasses import dataclass
from typing import Optional

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app import ai_context
from app.media import Video, send_video_to_user
from app.topics import get_topic_id

router = Router()

_FOOTER = "\n\nЕсли не помогло — просто напишите сюда, что не получилось, и пришлите скрин из приложения 🙏"


@dataclass(frozen=True)
class FaqItem:
    key: str
    title: str
    text: str
    video: Optional[Video] = None


FAQ = [
    FaqItem(
        "notworking",
        "🔌 Не работает VPN",
        "1. Обновите подписку кнопкой обновления в приложении — серверы часто меняются.\n"
        "2. Выберите другой сервер или «Авто».\n"
        "3. Если в Telegram включён прокси — выключите его: вместе с VPN он мешает подключению.\n"
        "4. Проверьте, работает ли сервер: подключитесь и откройте https://check-host.net/ — "
        "если там видны IP и страна сервера, значит VPN работает, а «n/a» — это просто сбой замера пинга.",
        Video.update_subscription,
    ),
    FaqItem(
        "mobile",
        "📶 Не работает на мобильном интернете",
        "Во время ограничений мобильного интернета (белые списки, глушилки) обычные иностранные серверы "
        "не работают — это норма. Выбирайте раздел «Обход глушилок» и попробуйте несколько серверов из него, "
        "в том числе внизу списка. Перед этим обновите подписку, лучше через Wi‑Fi.\n\n"
        "Проверка: без VPN откройте ya.ru, затем google.com. Не открывается даже Яндекс — нет интернета "
        "у оператора, VPN тут не поможет.",
    ),
    FaqItem(
        "update",
        "🔄 Как обновить подписку",
        "Нажмите кнопку обновления подписки в приложении — подтянутся актуальные серверы. "
        "При ограничениях мобильного интернета обновляйте через Wi‑Fi. Как это сделать — на видео.",
        Video.update_subscription,
    ),
    FaqItem(
        "devices",
        "📱 Лимит устройств / удалить устройство",
        "Устройство — это каждое приложение, куда добавлена подписка: телефон с двумя VPN-приложениями "
        "считается за два, роутер тоже занимает место. После переустановки приложения оно занимает новое место.\n\n"
        "Чтобы освободить место: в боте @VpnBurger_bot → «Управление подпиской» → удалите ненужные устройства, "
        "затем добавьте подписку на новом. Как это сделать — на видео.",
        Video.delete_device,
    ),
    FaqItem(
        "connect",
        "➕ Как подключить устройство",
        "Кнопка подключения в боте @VpnBurger_bot сама открывает приложение. Если не открылось — скопируйте "
        "ссылку подписки в боте и добавьте её в приложении через «+».\n\n"
        "• iPhone/iPad: Incy https://apps.apple.com/ru/app/incy/id6756943388 — инструкция "
        "https://telegra.ph/Kak-podklyuchit-BurgerVPN-na-IOS-08-04\n"
        "• Android: Happ или Incy\n"
        "• Windows/macOS: Happ https://www.happ.su/main/ru\n"
        "• Apple TV: https://telegra.ph/Kak-podklyuchit-BurgerVPN-na-Apple-TV-08-04\n\n"
        "Новое приложение занимает одно место в подписке.",
    ),
    FaqItem(
        "cancel",
        "🚫 Отменить автопродление",
        "В боте @VpnBurger_bot → «Управление подпиской» → «Отменить подписку». После этого списаний не будет. "
        "Также счёт можно отвязать в приложении банка. Как это сделать — на видео.",
        Video.cancel_subscription,
    ),
    FaqItem(
        "paid",
        "💳 Оплатил, а дней нет",
        "Сначала проверьте сообщение «Оплата прошла успешно» в боте — в нём есть кнопка подключения.\n\n"
        "Если дней всё равно нет — напишите сюда, на сколько устройств и дней брали подписку, и пришлите "
        "скрин оплаты или чек. Проверим и поправим.",
    ),
    FaqItem(
        "banks",
        "🏦 Не открываются банки и Госуслуги",
        "Некоторые банки и сайты не пускают с включённым VPN, даже через российский сервер. "
        "В Happ: «Настройки» → «Прокси для выбранных приложений» → добавьте банки и Госуслуги в исключения. "
        "Или просто выключайте VPN, когда заходите в них.",
    ),
    FaqItem(
        "story",
        "🎁 14 дней за сторис",
        "Акция для пользователей Telegram Premium, один раз на аккаунт:\n"
        "1. В боте @VpnBurger_bot → «Бонусы» → «14 дней за сторис» возьмите картинку и текст со ссылкой.\n"
        "2. Опубликуйте сторис для всех (не для «близких друзей») и не удаляйте её 24 часа.\n"
        "3. Пришлите сюда скрин опубликованной сторис — проверим и начислим дни.",
    ),
    FaqItem(
        "logs",
        "📄 Как прислать лог",
        "Лог помогает техникам понять, почему не работает сервер. В Happ:\n"
        "1. Подключитесь к серверу, который не работает, и попользуйтесь пару минут — "
        "не выключайте VPN перед снятием лога.\n"
        "2. «Настройки» → «Логи» (внизу списка).\n"
        "3. Нажмите на нужный лог → «⋮» или «Поделиться».\n"
        "4. Отправьте файл сюда. Если Telegram нет в списке — сохраните файл и прикрепите его в этот чат скрепкой.\n\n"
        "В других приложениях пришлите скрин или видео проблемы.",
    ),
]

_BY_KEY = {item.key: item for item in FAQ}


def faq_menu() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=item.title, callback_data=f"faq:{item.key}")] for item in FAQ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def faq_entry_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❓ Частые вопросы", callback_data="faq:menu")]])


_BACK = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="« Другие вопросы", callback_data="faq:menu")]])


@router.message(Command("faq"))
async def cmd_faq(message: Message):
    await message.answer("Выберите вопрос:", reply_markup=faq_menu())


@router.callback_query(F.data.startswith("faq:"))
async def on_faq(callback: CallbackQuery):
    key = callback.data.removeprefix("faq:")
    if key == "menu":
        await callback.message.answer("Выберите вопрос:", reply_markup=faq_menu())
        await callback.answer()
        return
    item = _BY_KEY.get(key)
    if item is None:
        await callback.answer()
        return
    await callback.answer()
    if item.video:
        await send_video_to_user(callback.bot, callback.from_user.id, item.video)
    await callback.message.answer(f"{item.title}\n\n{item.text}{_FOOTER}", reply_markup=_BACK, parse_mode=None)

    # Если у клиента уже есть заявка — пусть ИИ знает, что он уже это читал.
    topic_id = await get_topic_id(callback.from_user.id)
    if topic_id is not None:
        await ai_context.append_turn(topic_id, "user", f"[клиент открыл частый вопрос «{item.title}» и получил готовый ответ]")
