import asyncio
import aiohttp
import random
import time
from typing import List, Dict, Optional

from settings import settings
import db

API_BASE = f"https://api.telegram.org/bot{settings.BOT_TOKEN}"

# ========= ЕДИНАЯ HTTP-СЕССИЯ =========
_session: aiohttp.ClientSession | None = None

async def init_http():
    global _session
    if _session is None:
        _session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=100, enable_cleanup_closed=True)
        )

async def close_http():
    global _session
    if _session:
        await _session.close()
        _session = None

async def _api_post(method: str, data: Dict) -> Dict:
    if _session is None:
        await init_http()
    async with _session.post(f"{API_BASE}/{method}", json=data, timeout=20) as r:
        try:
            resp = await r.json()
        except Exception:
            resp = {"ok": False, "error_code": r.status, "description": "non-JSON"}

        # Flood control (429)
        status_429 = (r.status == 429) or (resp.get("error_code") == 429)
        if status_429:
            retry = 1
            params = resp.get("parameters") or {}
            if "retry_after" in params:
                try:
                    retry = int(params["retry_after"])
                except Exception:
                    pass
            await db.log("WARN", f"Flood wait {retry}s on {method}")
            await asyncio.sleep(retry + 0.05)
        return resp

# ========= РЕЙТ-КОНТРОЛЬ =========
GLOBAL_RPS = 25
_PER_CHAT_LAST: dict[int, float] = {}
_GLOBAL_LAST = 0.0

async def _rate_limit(chat_id: int | None = None):
    global _GLOBAL_LAST
    now = time.monotonic()
    wait = max(0.0, _GLOBAL_LAST + 1.0 / GLOBAL_RPS - now)
    if chat_id is not None:
        last = _PER_CHAT_LAST.get(chat_id, 0.0)
        wait = max(wait, last + 1.0 - now)  # 1 msg/sec в чат
        _PER_CHAT_LAST[chat_id] = now + wait
    if wait > 0:
        await asyncio.sleep(wait)
    _GLOBAL_LAST = now + wait

# ========= ИНТЕРВАЛЫ ОПРОСА (ТУРБО) =========
POLL_BASE_INTERVAL = 10.0
POLL_TURBO_INTERVAL = 0.5
_TURBO_UNTIL = 0.0

def set_base_interval(seconds: float) -> None:
    global POLL_BASE_INTERVAL
    POLL_BASE_INTERVAL = max(0.5, float(seconds))

def enable_turbo(seconds: int = 180) -> None:
    global _TURBO_UNTIL
    _TURBO_UNTIL = time.monotonic() + max(1, int(seconds))

def turbo_remaining() -> int:
    rem = int(_TURBO_UNTIL - time.monotonic())
    return rem if rem > 0 else 0

def current_poll_interval() -> float:
    return POLL_TURBO_INTERVAL if turbo_remaining() > 0 else POLL_BASE_INTERVAL

# ========= УТИЛИТЫ ПАРСИНГА КАТАЛОГА =========
def _to_int_or_none(v) -> Optional[int]:
    try:
        return int(v)
    except Exception:
        return None

def _extract_supply(item: dict) -> Optional[int]:
    # пытаемся найти поле «остаток/лимит» среди типичных ключей
    for key in ("supply", "remaining", "remaining_count", "left", "stock_left", "available", "available_count"):
        if key in item and item[key] is not None:
            val = _to_int_or_none(item[key])
            if val is not None:
                return val
    return None

def _is_limited(item: dict, supply: Optional[int]) -> bool:
    # явные флаги + наличие числового supply
    flags = (
        bool(item.get("limited")),
        bool(item.get("is_limited")),
        bool(item.get("limited_supply")),
        bool(item.get("has_supply")),
    )
    return any(flags) or (supply is not None)

async def fetch_available_gifts() -> List[Dict]:
    try:
        await _rate_limit()
        resp = await _api_post("getAvailableGifts", {})
        if not resp.get("ok"):
            await db.log("WARN", f"getAvailableGifts not ok: {resp}")
            return []
        res = resp.get("result") or {}
        items = res.get("gifts") if isinstance(res, dict) else (res or [])
        normalized = []
        for it in items:
            normalized.append({
                "id": it.get("id"),
                # используем эмодзи как короткий "титул" (в ответе нет названия)
                "title": (it.get("sticker", {}) or {}).get("emoji", "") or "Gift",
                "price": int(it.get("star_count", 0)),
                # этих полей нет в API — оставляем служебно пустыми
                "limited": False,
                "supply": None,
            })
        return normalized
    except Exception as e:
        await db.log("WARN", f"getAvailableGifts failed: {e}")
        return []


async def send_gift(to_user_id: int, gift_id: str, text: str = "") -> bool:
    try:
        await _rate_limit(to_user_id)
        payload = {"user_id": to_user_id, "gift_id": str(gift_id)}
        if text:
            payload["text"] = text
        resp = await _api_post("sendGift", payload)
        ok = bool(resp.get("ok"))
        if not ok:
            await db.log("WARN", f"sendGift failed: {resp}")
        return ok
    except Exception as e:
        await db.log("WARN", f"sendGift error: {e}")
        return False

# внизу рядом с fetch_available_gifts()
async def fetch_available_gifts_raw() -> dict:
    try:
        await _rate_limit()
        return await _api_post("getAvailableGifts", {})
    except Exception as e:
        await db.log("WARN", f"getAvailableGifts(raw) failed: {e}")
        return {"ok": False, "error": str(e)}


# ========= ОСНОВНАЯ ЛОГИКА (с правилами) =========
async def check_new_gifts_and_autobuy(bot) -> None:
    gifts = await fetch_available_gifts()
    if not gifts:
        return

    known_ids = await db.known_gift_ids()
    await db.upsert_gifts_cache(gifts)

    # "редкие" в текущем API трактуем как "новые" — их и так выбираем диффом
    new_gifts = [g for g in gifts if str(g["id"]) not in known_ids]
    if not new_gifts:
        return

    await db.log("INFO", f"New gifts: {', '.join(str(g['id']) for g in new_gifts)}")

    users = await db.autobuy_users_with_rules()
    if not users:
        return

    for g in new_gifts:
        price = int(g["price"])
        for row in users:
            uid = int(row["user_id"])
            bal = int(row["balance"])
            min_price = int(row["min_price"])
            max_price = int(row["max_price"])

            if price < min_price or price > max_price:
                continue
            if bal < price:
                continue

            ok = await send_gift(uid, str(g["id"]), text="🎁 Новый подарок!")
            if ok:
                await db.add_balance(uid, -price)
                try:
                    await bot.send_message(uid, f"🎁 Отправлен подарок: {g['title']} (−{price} ⭐)")
                except Exception:
                    pass

            await asyncio.sleep(0)  # yield


# ========= WATCHER =========
async def watcher_loop(bot, stop_event: asyncio.Event) -> None:
    await db.log("INFO", "Watcher started")
    while not stop_event.is_set():
        try:
            await check_new_gifts_and_autobuy(bot)
        except Exception as e:
            await db.log("WARN", f"watcher iteration error: {e}")
        delay = current_poll_interval() + random.uniform(0, 0.2)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
    await db.log("INFO", "Watcher stopped")
