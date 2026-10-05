from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import BotCommand, BotCommandScopeChat, Message

from app.settings import SUPPORT_CHAT_ID


router = Router()


@router.message(CommandStart())
async def send_welcome(message: Message):
    """Handle /start command - show welcome message."""
    welcome_text = (
        "Это бот поддержки Burger VPN\n\n"
        "Мы работаем 24/7, опишите вашу проблему – мы ее решим"
    )
    await message.answer(welcome_text)


async def set_commands(bot):
    """Set bot commands: /start for users; topic and AI controls for admins in the support chat."""
    await bot.set_my_commands([
        BotCommand(command="start", description="Обратиться в поддержку")
    ])
    await bot.set_my_commands(
        [
            BotCommand(command="delete", description="Удалить топик заявки"),
            BotCommand(command="s", description="Обновить карточку клиента"),
            BotCommand(command="stop", description="Остановить ИИ в этом топике"),
            BotCommand(command="start", description="Включить ИИ в этом топике"),
            BotCommand(command="pauseai", description="Остановить ИИ во всех чатах"),
            BotCommand(command="resumeai", description="Включить ИИ во всех чатах"),
            BotCommand(command="outage", description="Массовый сбой: /outage текст | off"),
        ],
        scope=BotCommandScopeChat(chat_id=int(SUPPORT_CHAT_ID))
    )
