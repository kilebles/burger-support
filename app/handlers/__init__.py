from aiogram import F, Router
from aiogram.enums import ChatType

from app.handlers.start import router as start_router, set_commands
from app.handlers.request import router as request_router
from app.handlers.admin import router as admin_router
from app.handlers.faq import router as faq_router
from app.admin_actions import router as admin_actions_router


router = Router()

# User-facing router (only private chats)
user_router = Router()
user_router.message.filter(F.chat.type == ChatType.PRIVATE)

user_router.include_router(start_router)
user_router.include_router(faq_router)
user_router.include_router(request_router)  # Must be last - catches all other messages

# Include routers in order
router.include_router(admin_actions_router)  # Inline admin buttons in topics
router.include_router(admin_router)  # Admin works in the support group
router.include_router(user_router)   # Users only in private

__all__ = ["router", "set_commands"]
