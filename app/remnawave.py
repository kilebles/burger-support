import asyncio
import time
from html import escape
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from loguru import logger

from app.settings import REMNAWAVE_COOKIE, REMNAWAVE_TOKEN, REMNAWAVE_URL

_TIMEOUT = 6.0
_PANEL_CACHE_TTL = 15.0
_panel_cache: dict[int, tuple[float, list[str]]] = {}
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


def _format_dt(value: Optional[str], *, date_only: bool = False) -> Optional[str]:
    dt = _parse_dt(value)
    if not dt:
        return None
    msk = dt.astimezone(_MSK)
    return msk.strftime("%d.%m.%Y") if date_only else msk.strftime("%d.%m.%Y %H:%M")


def _extract_client_app(user_agent: Optional[str]) -> Optional[str]:
    """Короткое имя VPN-приложения из userAgent панели (например, 'Happ/6.3.0/ios/...' -> 'Happ 6.3.0')."""
    if not user_agent:
        return None
    raw = user_agent.strip()
    parts = [p.strip() for p in raw.split("/") if p.strip()]
    if not parts:
        return None
    app_name = parts[0]
    if len(parts) >= 2 and any(ch.isdigit() for ch in parts[1]) and len(parts[1]) <= 16:
        return f"{app_name} {parts[1]}"
    return app_name[:32]


def _format_hwid_device(device: dict[str, Any]) -> str:
    """Форматировать одну запись HWID-устройства из Remnawave для карточки и контекста ИИ."""
    head_parts = [device.get("platform"), device.get("deviceModel")]
    if device.get("osVersion"):
        head_parts.append(f"OS {device['osVersion']}")
    head = " · ".join(str(p) for p in head_parts if p) or str(device.get("hwid") or "неизвестно")

    meta: list[str] = []
    client_app = _extract_client_app(device.get("userAgent"))
    if client_app:
        meta.append(f"приложение: {client_app}")
    created = _format_dt(device.get("createdAt"), date_only=True)
    if created:
        meta.append(f"добавлено {created}")
    updated = _format_dt(device.get("updatedAt"))
    if updated:
        meta.append(f"активность {updated}")

    suffix = f" ({', '.join(meta)})" if meta else ""
    return f"  • {escape(head + suffix)}"


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
    lines.append(f"Устройства (HWID): {len(devices)}/{device_limit if device_limit else '∞'}")
    sorted_devices = sorted(
        devices,
        key=lambda d: (d.get("updatedAt") or d.get("createdAt") or ""),
        reverse=True,
    )
    for device in sorted_devices:
        lines.append(_format_hwid_device(device))

    if user.get("subscriptionUrl"):
        lines.append(f"Ссылка: <code>{user['subscriptionUrl']}</code>")
    return lines


async def get_panel_lines(telegram_id: int, *, force_refresh: bool = False) -> list[str]:
    """Строки карточки клиента с данными панели. Пусто, если панель недоступна."""
    if not REMNAWAVE_URL or not REMNAWAVE_TOKEN:
        return []
    now = time.monotonic()
    if not force_refresh:
        cached = _panel_cache.get(telegram_id)
        if cached is not None and (now - cached[0]) < _PANEL_CACHE_TTL:
            return list(cached[1])

    users = await _get_users(telegram_id)
    if not users:
        res = ["", "В панели не найден"]
        _panel_cache[telegram_id] = (now, res)
        return list(res)

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
    _panel_cache[telegram_id] = (time.monotonic(), lines)
    return list(lines)


# --- Действия по кнопкам админа в топике (запись в панель) ---


async def _post(path: str, payload: dict[str, Any]) -> bool:
    """POST к API панели. True — панель ответила 2xx."""
    if not REMNAWAVE_URL or not REMNAWAVE_TOKEN:
        return False
    headers = {"Authorization": f"Bearer {REMNAWAVE_TOKEN}"}
    if REMNAWAVE_COOKIE:
        headers["Cookie"] = REMNAWAVE_COOKIE
    try:
        async with httpx.AsyncClient(base_url=REMNAWAVE_URL.rstrip("/"), headers=headers, timeout=_TIMEOUT) as client:
            response = await client.post(path, json=payload)
    except httpx.HTTPError as error:
        logger.warning(f"Failed to reach Remnawave (POST {path}): {error}")
        return False
    if response.status_code >= 300:
        logger.warning(f"Remnawave POST {path} -> {response.status_code}: {response.text[:200]}")
        return False
    return True


async def get_personal_user(telegram_id: int) -> Optional[dict[str, Any]]:
    """Личная подписка клиента (username w_...), как её выбирает BurgerVPN;
    если личной нет — самая свежая по сроку."""
    users = await _get_users(telegram_id)
    if not users:
        return None
    personal = [u for u in users if (u.get("username") or "").startswith("w_")]
    pool = personal or users
    pool.sort(key=lambda u: u.get("expireAt") or "", reverse=True)
    return pool[0]


async def reset_devices(telegram_id: int) -> Optional[tuple[int, int]]:
    """Удалить все устройства личной подписки клиента (как "удалить устройство"
    в боте BurgerVPN, только все сразу). Возвращает (удалено, было) или None,
    если подписка не найдена. Других полей подписки не касается."""
    user = await get_personal_user(telegram_id)
    if user is None:
        return None
    user_id = user.get("id", user.get("uuid"))
    devices = await _get_devices(user_id)
    deleted = 0
    for device in devices:
        hwid = device.get("hwid")
        if hwid and await _post("/api/hwid/devices/delete", {"userId": user_id, "hwid": hwid}):
            deleted += 1
    _panel_cache.pop(telegram_id, None)
    logger.info(f"Reset devices for telegram_id={telegram_id}: {deleted}/{len(devices)}")
    return deleted, len(devices)
