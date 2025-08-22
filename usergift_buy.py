import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional
from dotenv import load_dotenv
load_dotenv()

import os
import aiohttp
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError

SESSION = "user_session"  # сессия именно аккаунта, не бота

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

GIFTS_ENDPOINT = "https://<your-monitor-source>/getAvailableGifts"  # куда сейчас дергаешь
MONITOR_INTERVAL_OK = (0.5, 1.2)  # сек
MONITOR_INTERVAL_IDLE = (2.0, 3.0)  # сек, когда всё пусто долгое время
TARGET_CHANNEL = "september1_gift"  # без t.me/

DESIRED_GIFTS = {
    # приоритетные «лимитки» (если знаешь id/slug)
    # "rose_gold_tiger": {"max_price": 9999},
}

logger = logging.getLogger("autobuy")
logging.basicConfig(level=logging.INFO)


def now_ms() -> int:
    return int(time.time() * 1000)


@asynccontextmanager
async def aiohttp_session():
    async with aiohttp.ClientSession() as s:
        yield s


class GiftMonitor:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.etag = None

    async def fetch(self) -> Optional[Dict[str, Any]]:
        headers = {}
        if self.etag:
            headers["If-None-Match"] = self.etag
        try:
            async with self.session.get(GIFTS_ENDPOINT, headers=headers, timeout=5) as r:
                if r.status == 304:
                    return None
                if et := r.headers.get("ETag"):
                    self.etag = et
                if r.status == 401:
                    # Не фатально для мониторинга
                    data = await r.text()
                    logger.debug("Unauthorized on gifts endpoint: %s", data)
                    return {}
                r.raise_for_status()
                return await r.json()
        except Exception as e:
            logger.warning("fetch gifts error: %s", e)
            return {}

    @staticmethod
    def parse_limited(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for it in items:
            # Безопасный парсинг
            gift_id = it.get("id") or it.get("gift_id") or it.get("slug")
            total = it.get("total_count")
            remain = it.get("remaining_count")
            is_limited = it.get("is_limited")
            price = it.get("price_stars") or it.get("price")  # в звёздах

            limited_flag = False
            if is_limited is True:
                limited_flag = True
            elif total is not None:
                limited_flag = True
            elif remain is not None:
                limited_flag = True

            out.append({
                "gift_id": gift_id,
                "price": price,
                "total_count": total,
                "remaining_count": remain,
                "is_limited": limited_flag
            })
        return out


class Buyer:
    def __init__(self, client: TelegramClient):
        self.client = client
        self.last_buys: Dict[str, int] = {}  # gift_id -> ts

    async def ensure_stars_balance(self, need: int) -> bool:
        # TODO: подставь свою проверку баланса Stars из твоего payments.py
        # return await get_stars_balance(self.client) >= need
        return True

    async def already_bought_recently(self, gift_id: str, cooldown_sec=60) -> bool:
        ts = self.last_buys.get(gift_id)
        return bool(ts and (time.time() - ts < cooldown_sec))

    async def buy_gift(self, gift_id: str, price: int) -> bool:
        """
        Пытается купить подарок gift_id со стороны ЮЗЕР-АККА.
        Верни True при успехе.
        """
        try:
            ok = await self.ensure_stars_balance(price or 0)
            if not ok:
                logger.warning("Not enough stars for %s", gift_id)
                return False

            # === ВАРИАНТ А: прямые вызовы (заглушки) ===
            # tx = await start_gift_purchase(self.client, gift_id)
            # await confirm_stars_transaction(self.client, tx.id)

            # === ВАРИАНТ B: через дееплинк бота подарков (заглушка) ===
            # link = f"https://t.me/gifts?start=gift_{gift_id}"
            # await open_deeplink_and_confirm(self.client, link)

            # Пометь как купленный
            self.last_buys[gift_id] = time.time()
            return True

        except FloodWaitError as e:
            logger.error("FLOOD_WAIT %s", e)
            await asyncio.sleep(e.seconds + 1)
            return False
        except Exception as e:
            logger.exception("Unexpected buy error: %s", e)
            return False


async def notify_channel(client: TelegramClient, text: str, extra_json: Optional[dict] = None):
    msg = text
    if extra_json:
        msg += "\n\n<code>" + json.dumps(extra_json, ensure_ascii=False) + "</code>"
    await client.send_message(TARGET_CHANNEL, msg, parse_mode="html")


async def main():
    async with aiohttp_session() as http:
        monitor = GiftMonitor(http)

        client = TelegramClient(SESSION, API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            print("Нужна авторизация: отправь код/пароль в консоль при первом запуске.")
            await client.send_code_request("+10000000000")  # <-- поставь свой номер или авторизуйся заранее
            return

        buyer = Buyer(client)

        idle_hits = 0
        while True:
            data = await monitor.fetch()
            if data is None:
                # 304 – ничего не изменилось
                await asyncio.sleep(0.5)
                continue

            items = data.get("gifts") or data.get("items") or []
            limited = monitor.parse_limited(items)

            # Фильтрация «интересных»
            candidates = []
            for g in limited:
                if not g["is_limited"]:
                    continue
                rid = g["remaining_count"]
                if rid is None or rid > 0:
                    candidates.append(g)

            if candidates:
                idle_hits = 0
                # Сортируем по приоритету (твои желаемые сверху), затем по цене
                def prio(g):
                    return (0 if (g["gift_id"] in DESIRED_GIFTS) else 1, g["price"] or 10**9)
                candidates.sort(key=prio)

                # Берем топ‑1 и делаем 2–3 параллельные попытки покупки
                target = candidates[0]
                gift_id = str(target["gift_id"])
                price = int(target["price"] or 0)

                if not await buyer.already_bought_recently(gift_id):
                    await notify_channel(client, f"Пробуем купить: <b>{gift_id}</b>", target)
                    attempts = [
                        buyer.buy_gift(gift_id, price),
                        buyer.buy_gift(gift_id, price),
                        buyer.buy_gift(gift_id, price),
                    ]
                    done, pending = await asyncio.wait(attempts, return_when=asyncio.FIRST_COMPLETED)
                    success = any(t.result() for t in done if not t.cancelled())
                    # Отменим оставшиеся
                    for p in pending:
                        p.cancel()

                    if success:
                        await notify_channel(client, f"✅ Куплено: <b>{gift_id}</b>", target)
                    else:
                        await notify_channel(client, f"❌ Не удалось: <b>{gift_id}</b>", target)
                else:
                    logger.info("Skip %s: already attempted very recently", gift_id)

                await asyncio.sleep(0.5)
            else:
                idle_hits += 1
                # реже опрашиваем если долго пусто
                delay = MONITOR_INTERVAL_IDLE[1] if idle_hits > 30 else MONITOR_INTERVAL_IDLE[0]
                await asyncio.sleep(delay)


if __name__ == "__main__":
    asyncio.run(main())
