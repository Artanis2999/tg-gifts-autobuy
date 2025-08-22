import asyncio
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder
from zoneinfo import ZoneInfo

from settings import settings
import db
from payments import router as payments_router
import autobuy

import json
from html import escape
from aiogram.types import BufferedInputFile


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("giftbot")

TZ = ZoneInfo(settings.TIMEZONE)

bot = Bot(
    token=settings.BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

dp = Dispatcher()
dp.include_router(payments_router)

# Глобальный watcher
_watcher_stop = asyncio.Event()
_watcher_task: asyncio.Task | None = None

def _is_admin(user_id: int) -> bool:
    return int(user_id) == int(settings.ADMIN_ID)

# ---------- Команды ----------
@dp.message(F.text == "/start")
async def cmd_start(m: types.Message):
    await db.ensure_user(m.from_user.id, m.from_user.username)
    kb = InlineKeyboardBuilder()
    kb.button(text="Пополнить ⭐ 2000", callback_data="buy:2000")
    kb.button(text="Баланс", callback_data="balance")
    kb.adjust(2)
    text = (
    "Привет! Это авто-даритель подарков.\n\n"
    "Команды:\n"
    "/buy [сумма] — пополнить баланс звёздами\n"
    "/balance — показать баланс\n"
    "/autobuy_on — включить автоскуп\n"
    "/autobuy_off — выключить автоскуп\n\n"
    "Правила автоскупа:\n"
    "/rules — показать текущие правила\n"
    "/rules_price &lt;min&gt; &lt;max&gt; — задать ценовой диапазон в ⭐\n"
    "/limited_on — покупать только лимитные с остатком\n"
    "/limited_off — разрешить и обычные (не рекомендовано)\n\n"
    "Скорость (только для админа):\n"
    "/speed_fast [сек] — турбо-режим (по умолчанию 180 сек)\n"
    "/speed_base [сек] — базовый интервал (по умолчанию 10 сек)\n"
    "/speed_status — текущие интервалы"
    )
    await m.answer(text, reply_markup=kb.as_markup())

@dp.callback_query(F.data == "balance")
async def cb_balance(c: types.CallbackQuery):
    bal = await db.get_balance(c.from_user.id)
    await c.message.answer(f"Твой баланс: {bal} ⭐")
    await c.answer()

@dp.message(F.text.startswith("/balance"))
async def cmd_balance(m: types.Message):
    await db.ensure_user(m.from_user.id, m.from_user.username)
    bal = await db.get_balance(m.from_user.id)
    await m.answer(f"Твой баланс: {bal} ⭐")

@dp.message(F.text == "/autobuy_on")
async def cmd_ab_on(m: types.Message):
    await db.set_autobuy(m.from_user.id, True)
    await m.answer("Автоскуп включён. Дарим новые лимитные подарки по твоим правилам (если хватает ⭐).")

@dp.message(F.text == "/autobuy_off")
async def cmd_ab_off(m: types.Message):
    await db.set_autobuy(m.from_user.id, False)
    await m.answer("Автоскуп выключен.")

# ---------- Правила ----------
@dp.message(F.text == "/rules")
async def cmd_rules_show(m: types.Message):
    await db.ensure_user(m.from_user.id, m.from_user.username)
    r = await db.get_rules(m.from_user.id)
    only_limited = "Да (только лимитные)" if r["only_limited"] else "Нет (разрешены обычные)"
    await m.answer(
        f"Текущие правила:\n"
        f"• Только лимитные: <b>{only_limited}</b>\n"
        f"• Цена: <b>{r['min_price']} — {r['max_price']}</b> ⭐"
    )

@dp.message(F.text.startswith("/rules_price"))
async def cmd_rules_price(m: types.Message):
    parts = m.text.strip().split()
    if len(parts) < 3:
        return await m.answer("Использование: /rules_price <min> <max> (в ⭐)")
    try:
        pmin = int(parts[1])
        pmax = int(parts[2])
    except ValueError:
        return await m.answer("Мин/макс — целые числа в ⭐. Пример: /rules_price 100 5000")
    await db.set_price_range(m.from_user.id, pmin, pmax)
    r = await db.get_rules(m.from_user.id)
    await m.answer(f"OK. Цена ограничена: <b>{r['min_price']} — {r['max_price']}</b> ⭐")

@dp.message(F.text == "/debug_gifts")
async def cmd_debug_gifts(m: types.Message):
    if int(m.from_user.id) != int(settings.ADMIN_ID):
        return await m.answer("Эта команда доступна только админу.")
    data = await autobuy.fetch_available_gifts_raw()
    js = json.dumps(data, ensure_ascii=False, indent=2)
    # если влезает в сообщение — показываем в <pre>, иначе шлем файлом
    if len(js) < 3800:
        await m.answer(f"<pre>{escape(js)}</pre>")
    else:
        buf = BufferedInputFile(js.encode("utf-8"), filename="getAvailableGifts.json")
        await m.answer_document(buf, caption="Raw getAvailableGifts")


@dp.message(F.text == "/limited_on")
async def cmd_limited_on(m: types.Message):
    await db.set_only_limited(m.from_user.id, True)
    await m.answer("Включено правило: покупать только лимитные подарки с остатком.")

# @dp.message(F.text == "/limited_off")
# async def cmd_limited_off(m: types.Message):
#     await db.set_only_limited(m.from_user.id, False)
#     await m.answer("Выключено правило «только лимитные». Теперь разрешены и обычные (не рекомендовано).")

# ----- Управление скоростью (только админ) -----
@dp.message(F.text.startswith("/speed_fast"))
async def cmd_speed_fast(m: types.Message):
    if not _is_admin(m.from_user.id):
        return await m.answer("Эта команда доступна только админу.")
    parts = m.text.split()
    seconds = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 180
    autobuy.enable_turbo(seconds)
    await m.answer(f"🚀 Турбо включён на {seconds} сек. Текущий интервал: {autobuy.current_poll_interval():.2f} сек")

@dp.message(F.text.startswith("/speed_base"))
async def cmd_speed_base(m: types.Message):
    if not _is_admin(m.from_user.id):
        return await m.answer("Эта команда доступна только админу.")
    parts = m.text.split()
    try:
        seconds = float(parts[1]) if len(parts) > 1 else 10.0
    except ValueError:
        seconds = 10.0
    autobuy.set_base_interval(seconds)
    await m.answer(f"Базовый интервал опроса установлен: {seconds:.2f} сек")

@dp.message(F.text == "/speed_status")
async def cmd_speed_status(m: types.Message):
    await m.answer(
        f"Текущий интервал: {autobuy.current_poll_interval():.2f} сек\n"
        f"База: {autobuy.POLL_BASE_INTERVAL:.2f} сек\n"
        f"Турбо осталось: {autobuy.turbo_remaining()} сек"
    )


# ---------- Watcher lifecycle ----------
async def start_watcher():
    global _watcher_task
    _watcher_stop.clear()
    _watcher_task = asyncio.create_task(autobuy.watcher_loop(bot, _watcher_stop))

async def stop_watcher():
    _watcher_stop.set()
    if _watcher_task:
        try:
            await _watcher_task
        except Exception:
            pass

async def on_startup():
    await db.init_db(settings.DATABASE_URL)
    await autobuy.init_http()          # единая HTTP-сессия
    await start_watcher()
    try:
        await bot.send_message(settings.LOG_CHAT_ID, f"🚀 Бот запущен. TZ={settings.TIMEZONE}")
    except Exception:
        pass

async def on_shutdown():
    await stop_watcher()
    await autobuy.close_http()         # закрываем HTTP-сессию

async def main():
    await on_startup()
    try:
        await dp.start_polling(bot)
    finally:
        await on_shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
