import asyncio
import io
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pypdf import PdfWriter

os.environ.setdefault("TOKEN", "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw")
os.environ.setdefault("SUPPORT_CHAT_ID", "-1001234567890")

from app import ai_context, ai_support  # noqa: E402
from app.handlers import request  # noqa: E402


async def test_burger_adaptive_debounce_and_photo_picker() -> None:
    bot = SimpleNamespace()
    user = SimpleNamespace(id=1, username="tester", full_name="Tester")

    opener_burst = request._PendingBurst(bot=bot, user=user, texts=["Добрый день!"])
    assert request._compute_debounce_seconds(opener_burst) == request._OPENER_DEBOUNCE_SECONDS

    substance_burst = request._PendingBurst(bot=bot, user=user, texts=["Добрый день, обходы n/a"])
    assert request._compute_debounce_seconds(substance_burst) == request._DEBOUNCE_SECONDS

    sizes = [
        SimpleNamespace(width=180, height=320),
        SimpleNamespace(width=591, height=1280),
        SimpleNamespace(width=1179, height=2556),
    ]
    assert request._pick_best_photo(sizes) is sizes[1]


async def test_burger_history_normalization_cache_and_pdf(monkeypatch: pytest.MonkeyPatch) -> None:
    history = [
        {"role": "user", "content": "Здравствуйте"},
        {"role": "user", "content": "Не грузит ютуб"},
        {"role": "assistant", "content": "Какое у вас приложение?"},
        {"role": "user", "content": "Happ"},
    ]
    monkeypatch.setattr(ai_context, "get_history", AsyncMock(return_value=history))
    monkeypatch.setattr(ai_context, "get_outage", AsyncMock(return_value=None))
    monkeypatch.setattr(ai_context, "get_bonuses", AsyncMock(return_value=[]))
    monkeypatch.setattr(ai_context, "append_turn", AsyncMock())
    monkeypatch.setattr(ai_support, "_panel_block", AsyncMock(return_value="Статус: активна"))

    captured: dict = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            stop_reason="tool_use",
            content=[
                SimpleNamespace(
                    type="tool_use",
                    id="tool_1",
                    name="submit_answer",
                    input={
                        "answer": "Нажмите кнопку обновления подписки в Happ.",
                        "escalate": False,
                        "escalate_reason": "",
                        "attachment_note": "скан PDF",
                        "no_reply_needed": False,
                        "video": "update_subscription",
                        "action": "none",
                        "action_days": 0,
                        "action_summary": "",
                    },
                )
            ],
            usage=None,
        )

    fake_client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(side_effect=fake_create)))
    monkeypatch.setattr(ai_support, "_client", fake_client)

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)

    res = await ai_support.get_ai_answer(
        topic_id=10,
        telegram_id=42,
        text="На айфоне",
        attachments=[ai_support.Attachment(data=buf.getvalue(), mime_type="application/pdf", kind="документ")],
    )
    assert res is not None
    assert res.video == "update_subscription"

    msgs = captured["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[0]["content"] == "Здравствуйте\n\nНе грузит ютуб"
    assert msgs[1]["content"][0]["cache_control"]["type"] == "ephemeral"
    assert "Happ\n\nНа айфоне" in msgs[2]["content"][-1]["text"]


async def test_burger_fast_paths_and_escalation_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ai_context,
        "get_history",
        AsyncMock(return_value=[{"role": "assistant", "content": "Спасибо, передал ваш вопрос коллегам."}]),
    )
    assert await request._is_redundant_ack(10, "Ок, жду 🙏") is True

    monkeypatch.setattr(
        ai_context,
        "get_history",
        AsyncMock(return_value=[{"role": "assistant", "content": "Попробуйте обновить подписку."}]),
    )
    assert await request._is_redundant_ack(10, "Спасибо, помогло!") is False

    # Проверка паузы ИИ после эскалации
    monkeypatch.setattr(
        ai_support,
        "get_ai_answer",
        AsyncMock(
            return_value=ai_support.SupportAnswer(
                answer="Передал коллегам на проверку.",
                escalate=True,
                escalate_reason="Клиент просит возврат.",
                attachment_note="",
                no_reply_needed=False,
            )
        ),
    )
    monkeypatch.setattr(ai_context, "should_notify_escalation", AsyncMock(return_value=True))
    pause_mock = AsyncMock()
    monkeypatch.setattr(ai_context, "pause_ai", pause_mock)
    monkeypatch.setattr(request, "notify_admin_escalation", AsyncMock())
    monkeypatch.setattr(request, "send_to_client", AsyncMock())

    burst = request._PendingBurst(
        bot=SimpleNamespace(),
        user=SimpleNamespace(id=55, username="u", full_name="User"),
        texts=["Хочу возврат"],
    )
    await request._reply_with_ai(burst, topic_id=77, text="Хочу возврат")
    pause_mock.assert_awaited_once_with(77, request._ESCALATION_PAUSE)


async def test_burger_inflight_coalescing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(request, "_pending", {})
    monkeypatch.setattr(request, "_inflight", {})
    monkeypatch.setattr(request, "_pending_locks", {})
    monkeypatch.setattr(request, "_exec_locks", {})
    monkeypatch.setattr(request, "_DEBOUNCE_SECONDS", 0.02)
    monkeypatch.setattr(request, "_OPENER_DEBOUNCE_SECONDS", 0.02)
    monkeypatch.setattr(request, "ensure_topic", AsyncMock(return_value=303))
    monkeypatch.setattr(ai_context, "get_outage", AsyncMock(return_value=None))
    monkeypatch.setattr(ai_context, "is_ai_globally_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(ai_context, "is_ai_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(ai_context, "get_history", AsyncMock(return_value=[]))

    class DummyTyping:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(request.ChatActionSender, "typing", lambda **kw: DummyTyping())

    calls: list[str] = []
    first_started = asyncio.Event()

    async def slow_then_fast(burst, topic_id, text, *, on_commit=None):
        calls.append(text)
        if len(calls) == 1:
            first_started.set()
            await asyncio.sleep(5.0)
        if on_commit:
            on_commit()

    monkeypatch.setattr(request, "_reply_with_ai", slow_then_fast)

    def make_msg(txt: str):
        return SimpleNamespace(
            text=txt,
            caption=None,
            photo=None,
            voice=None,
            video_note=None,
            document=None,
            animation=None,
            video=None,
            sticker=None,
            from_user=SimpleNamespace(id=101, username="client", full_name="Client"),
            bot=SimpleNamespace(),
        )

    await request._queue_for_ai([make_msg("Привет")])
    await asyncio.wait_for(first_started.wait(), timeout=1.0)
    await request._queue_for_ai([make_msg("Все серверы n/a в Happ")])
    await asyncio.sleep(0.08)

    assert calls == ["Привет", "Привет\nВсе серверы n/a в Happ"]


async def test_burger_remnawave_hwid_formatting_and_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import remnawave
    from app.handlers import faq

    monkeypatch.setattr(remnawave, "REMNAWAVE_URL", "https://panel.example.com")
    monkeypatch.setattr(remnawave, "REMNAWAVE_TOKEN", "test-token")
    monkeypatch.setattr(remnawave, "_panel_cache", {})

    calls: list[str] = []

    async def fake_get(path: str, **params):
        calls.append(path)
        if path == "/api/users/stream":
            return {
                "response": {
                    "users": [
                        {
                            "id": 16269,
                            "username": "w_555",
                            "status": "ACTIVE",
                            "expireAt": "2026-12-15T10:00:00.000Z",
                            "trafficLimitBytes": 26843545600,
                            "hwidDeviceLimit": 2,
                            "subscriptionUrl": "https://sub.vpnburger.ru/abc",
                            "userTraffic": {"usedTrafficBytes": 1073741824},
                        }
                    ]
                }
            }
        if path == "/api/hwid/devices/16269":
            return {
                "response": {
                    "total": 2,
                    "devices": [
                        {
                            "hwid": "hw_old",
                            "platform": "macOS",
                            "osVersion": "27.0",
                            "deviceModel": "Mac",
                            "userAgent": "Happ/6.3.0/macos catalyst/261028",
                            "createdAt": "2026-11-01T12:00:00.000Z",
                            "updatedAt": "2026-11-02T09:00:00.000Z",
                        },
                        {
                            "hwid": "hw_new",
                            "platform": "iOS",
                            "osVersion": "27.0",
                            "deviceModel": "iPhone 17",
                            "userAgent": "Incy/7.1.0/ios",
                            "createdAt": "2026-10-31T10:31:30.000Z",
                            "updatedAt": "2026-11-03T18:40:00.000Z",
                        },
                    ],
                }
            }
        if path == "/api/users/16269/subscription-request-history":
            return {"response": {"records": []}}
        return None

    monkeypatch.setattr(remnawave, "_get", fake_get)

    lines = await remnawave.get_panel_lines(555)
    joined = "\n".join(lines)
    assert "Устройства (HWID): 2/2" in joined
    assert "iOS · iPhone 17 · OS 27.0 (приложение: Incy 7.1.0, добавлено 31.10.2026, активность 03.11.2026 21:40)" in joined
    assert "macOS · Mac · OS 27.0 (приложение: Happ 6.3.0, добавлено 01.11.2026, активность 02.11.2026 12:00)" in joined
    # Сортировка по свежей активности: iPhone 17 выше Mac
    assert joined.index("iPhone 17") < joined.index("macOS · Mac")

    first_call_count = len(calls)
    await remnawave.get_panel_lines(555)
    assert len(calls) == first_call_count  # взято из TTL-кэша

    await remnawave.get_panel_lines(555, force_refresh=True)
    assert len(calls) > first_call_count  # принудительное обновление обошло кэш

    assert "Мои устройства" in ai_support.SYSTEM_PROMPT
    assert "Расширить подписку" in ai_support.SYSTEM_PROMPT
    assert "Сменить тариф" not in ai_support.SYSTEM_PROMPT
    assert "Мои устройства" in faq._BY_KEY["devices"].text


async def test_send_to_client_disables_link_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import delivery

    sent_calls: list[dict] = []

    async def fake_send_message(**kwargs):
        sent_calls.append(kwargs)
        return SimpleNamespace(message_id=len(sent_calls))

    monkeypatch.setattr(ai_context, "append_turn", AsyncMock())
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=fake_send_message))

    ok = await delivery.send_to_client(bot, user_id=777, topic_id=88, text="Ссылка: https://example.com")
    assert ok is True
    assert len(sent_calls) == 2
    assert all(call["link_preview_options"].is_disabled is True for call in sent_calls)
