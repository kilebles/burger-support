import asyncio
from html import escape
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from loguru import logger

from app.settings import REMNAWAVE_COOKIE, REMNAWAVE_TOKEN, REMNAWAVE_URL

_TIMEOUT = 6.0
# Москва без перехода на летнее время — фиксированного смещения достаточно,
# а в python:3.12-slim нет системной tzdata для zoneinfo.
_MSK = timezone(timedelta(hours=3))

_STATUS_NAMES = {
    "ACTIVE": "✅ активна",
    "DISABLED": "⛔️ отключена",
    "LIMITED": "⚠️ лимит трафика исчерпан",
    "EXPIRED": "⌛️ истекла",
}


async def _get(path: str, **params) -> Optional[dict[str, Any]]:
    """GET к API панели. None — интеграция выключена, сеть/HTTP-сбой или 404."""
    if not REMNAWAVE_URL or not REMNAWAVE_TOKEN:
        return None
    headers = {"Authorization": f"Bearer {REMNAWAVE_TOKEN}"}
    if REMNAWAVE_COOKIE:
        headers["Cookie"] = REMNAWAVE_COOKIE
    try:
        async with httpx.AsyncClient(base_url=REMNAWAVE_URL.rstrip("/"), headers=headers, timeout=_TIMEOUT) as client:
            response = await client.get(path, params=params or None)
    except httpx.HTTPError as error:
        logger.warning(f"Failed to reach Remnawave (GET {path}): {error}")
        return None
    if response.status_code != 200:
        if response.status_code != 404:
            logger.warning(f"Remnawave GET {path} -> {response.status_code}: {response.text[:200]}")
        return None
    return response.json()


async def _get_users(telegram_id: int) -> list[dict[str, Any]]:
    """Все подписки панели с этим telegramId: личная (w_...) и клановые (c<N>_...)."""
    result = await _get("/api/users/stream", telegramId=str(telegram_id), size=100)
    raw = (result or {}).get("response")
    if isinstance(raw, dict):
        return list(raw.get("users") or [])
    if isinstance(raw, list):
        return raw
    return []


async def _get_devices(user_id: Any) -> list[dict[str, Any]]:
    result = await _get(f"/api/hwid/devices/{user_id}")
    response = (result or {}).get("response")
    return list(response.get("devices") or []) if isinstance(response, dict) else []


async def _get_last_sub_request(user_id: Any) -> Optional[dict[str, Any]]:
    """Последний запрос подписки клиентским приложением: время и user-agent."""
    result = await _get(f"/api/users/{user_id}/subscription-request-history")
    response = (result or {}).get("response")
    records = list(response.get("records") or []) if isinstance(response, dict) else []
    return max(records, key=lambda r: r.get("requestAt") or "", default=None)


async def _get_node(node_uuid: Optional[str]) -> Optional[dict[str, Any]]:
    if not node_uuid:
        return None
    result = await _get(f"/api/nodes/{node_uuid}")
    response = (result or {}).get("response")
    return response if isinstance(response, dict) else None


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _format_dt(value: Optional[str]) -> Optional[str]:
    dt = _parse_dt(value)
    return dt.astimezone(_MSK).strftime("%d.%m.%Y %H:%M") if dt else None


def _format_gb(value: Any) -> str:
    return f"{int(value or 0) / 1024 ** 3:.1f}"


def _days_left(expire_at: datetime) -> str:
    delta = expire_at - datetime.now(timezone.utc)
    if delta.total_seconds() <= 0:
        return "истекла"
    days = delta.days
    return f"осталось {days} дн." if days else "меньше суток"


def _subscription_lines(
    user: dict[str, Any],
    devices: list[dict[str, Any]],
    last_request: Optional[dict[str, Any]],
    node: Optional[dict[str, Any]],
) -> list[str]:
    status = user.get("status") or ""
    lines = [
        f"Подписка: <code>{escape(user.get('username') or '—')}</code>",
        f"Статус: {_STATUS_NAMES.get(status, status or '—')}",
    ]

    expire_at = _parse_dt(user.get("expireAt"))
    if expire_at:
        lines.append(f"Действует до: {_format_dt(user['expireAt'])} МСК ({_days_left(expire_at)})")

    traffic = user.get("userTraffic") or {}
    used = traffic.get("usedTrafficBytes", user.get("usedTrafficBytes"))
    limit = user.get("trafficLimitBytes")
    limit_text = f"{_format_gb(limit)} ГБ" if limit else "∞"
    lines.append(f"Трафик: {_format_gb(used)} ГБ из {limit_text}")
    if traffic.get("lifetimeUsedTrafficBytes"):
        lines.append(f"Трафик за всё время: {_format_gb(traffic['lifetimeUsedTrafficBytes'])} ГБ")

    online_at = _format_dt(traffic.get("onlineAt") or user.get("onlineAt"))
    lines.append(f"Последний онлайн: {online_at or 'не подключался'}")
    first_connected = _format_dt(traffic.get("firstConnectedAt"))
    if first_connected:
        lines.append(f"Первое подключение: {first_connected}")

    if node:
        node_line = f"Последняя нода: {escape(node.get('name') or '—')}"
        if node.get("countryCode"):
            node_line += f" ({escape(node['countryCode'])})"
        if node.get("isDisabled"):
            node_line += " ⛔️ отключена"
        elif node.get("isConnected") is False:
            node_line += " ⚠️ недоступна"
        lines.append(node_line)

    if last_request:
        agent = last_request.get("userAgent")
        lines.append(
            f"Обновлял подписку: {_format_dt(last_request.get('requestAt')) or '—'}"
            + (f" ({escape(agent)})" if agent else "")
        )

    device_limit = user.get("hwidDeviceLimit")
    lines.append(f"Устройства: {len(devices)}/{device_limit if device_limit else '∞'}")
    for device in devices:
        parts = [device.get("platform"), device.get("deviceModel"), device.get("osVersion")]
        lines.append("  • " + escape(" · ".join(p for p in parts if p) or device.get("hwid") or "неизвестно"))

    if user.get("subscriptionUrl"):
        lines.append(f"Ссылка: <code>{user['subscriptionUrl']}</code>")
    return lines


async def get_panel_lines(telegram_id: int) -> list[str]:
    """Строки карточки клиента с данными панели. Пусто, если панель недоступна."""
    if not REMNAWAVE_URL or not REMNAWAVE_TOKEN:
        return []
    users = await _get_users(telegram_id)
    if not users:
        return ["", "В панели не найден"]

    # Личная подписка первой, дальше клановые — свежие по сроку выше.
    users.sort(key=lambda u: u.get("expireAt") or "", reverse=True)
    users.sort(key=lambda u: not (u.get("username") or "").startswith("w_"))

    lines: list[str] = []
    for user in users:
        user_id = user.get("id", user.get("uuid"))
        devices, last_request, node = await asyncio.gather(
            _get_devices(user_id),
            _get_last_sub_request(user_id),
            _get_node((user.get("userTraffic") or {}).get("lastConnectedNodeUuid")),
        )
        lines.append("")
        lines.extend(_subscription_lines(user, devices, last_request, node))
    return lines
